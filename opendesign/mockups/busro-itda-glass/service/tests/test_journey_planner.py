from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

from journey_planner import (  # noqa: E402
    JourneyPlanner,
    PlannerLimitError,
    PlannerValidationError,
)
from network_catalog import NetworkCatalog  # noqa: E402


FIXED_NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


class JourneyPlannerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = NetworkCatalog(Path(self.temp.name) / "catalog.sqlite3", clock=lambda: FIXED_NOW)

    def tearDown(self):
        self.temp.cleanup()

    def hydrate(self, route_id, stops, city_code="25"):
        return self.catalog.hydrate_route_sequence(
            city_code=city_code,
            route_id=route_id,
            ordered_stops=stops,
            source=f"TEST:{route_id}",
            captured_at="2026-08-31T00:00:00Z",
        )

    @staticmethod
    def stop(
        node_id, order, latitude, longitude, *, can_board=True, can_alight=True
    ):
        return {
            "node_id": node_id,
            "node_name": node_id,
            "node_order": order,
            "latitude": latitude,
            "longitude": longitude,
            "can_board": can_board,
            "can_alight": can_alight,
        }

    def hydrate_three_paths(self):
        self.hydrate("R1", [self.stop("O", 1, 36.5000, 127.3000), self.stop("A", 2, 36.5060, 127.3060), self.stop("D", 3, 36.5200, 127.3200)])
        self.hydrate("R2", [self.stop("O", 1, 36.5000, 127.3000), self.stop("B", 2, 36.5100, 127.2950), self.stop("D", 3, 36.5200, 127.3200)])
        self.hydrate("R3", [self.stop("O", 1, 36.5000, 127.3000), self.stop("C", 2, 36.5150, 127.3100), self.stop("D", 3, 36.5200, 127.3200)])

    @staticmethod
    def verified_timetable():
        return {
            "verified": True,
            "basis": "verified_official_timetable",
            "source": "TEST:OFFICIAL_TIMETABLE",
            "captured_at": "2026-08-31T00:00:00Z",
        }

    def test_three_alternatives_are_distinct_deterministic_and_evidence_backed(self):
        self.hydrate_three_paths()
        snapshot = self.catalog.snapshot()
        first = JourneyPlanner().plan(snapshot, origin_node_id="O", destination_node_id="D", transfer_radius_m=50, alternatives=3)
        second = JourneyPlanner().plan(snapshot, origin_node_id="O", destination_node_id="D", transfer_radius_m=50, alternatives=3)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual([item["criterion"] for item in first["alternatives"]], ["minimum_transfers", "generalized_cost", "explorer"])
        signatures = {tuple(item["route_ids"]) for item in first["alternatives"]}
        self.assertEqual(signatures, {("R1",), ("R2",), ("R3",)})
        for candidate in first["alternatives"]:
            self.assertEqual(candidate["evidence"]["topology"], "all_active_hydrated_route_sequences")
            self.assertEqual(candidate["coverage"]["structural"], 1.0)
            self.assertEqual(candidate["estimated_minutes"], None)
            self.assertFalse(candidate["operating_assumption"])
        self.assertEqual(first["graph"]["algorithm"], "directed_dijkstra")
        self.assertEqual(first["graph"]["coverage"]["hydrated_routes"], 3)
        self.assertIsNone(first["graph"]["coverage"]["nationwide_topology_complete"])
        self.assertEqual(first["graph"]["transfer_edges"], "LAZY_STOP_INDEX")

    def test_exact_shared_stop_builds_a_real_multi_route_journey(self):
        self.hydrate(
            "WEST",
            [self.stop("O", 1, 36.5000, 127.3000), self.stop("X", 2, 36.5100, 127.3100)],
        )
        self.hydrate(
            "EAST",
            [self.stop("X", 1, 36.5100, 127.3100), self.stop("D", 2, 36.5200, 127.3200)],
        )
        result = JourneyPlanner().plan(
            self.catalog.snapshot(),
            origin_node_id="O",
            destination_node_id="D",
            transfer_radius_m=50,
            alternatives=1,
        )
        candidate = result["alternatives"][0]
        self.assertEqual(candidate["route_ids"], ["WEST", "EAST"])
        self.assertEqual(candidate["transfers"], 1)
        transfer = next(step for step in candidate["steps"] if step["kind"] == "transfer")
        self.assertEqual(transfer["from"]["node_id"], "X")
        self.assertEqual(transfer["to"]["node_id"], "X")
        self.assertEqual(transfer["evidence"]["type"], "shared_node_id")

    def test_access_rules_block_endpoints_and_transfers_but_allow_through_rides(self):
        self.hydrate(
            "THROUGH",
            [
                self.stop("O", 1, 36.50, 127.30),
                self.stop(
                    "X", 2, 36.51, 127.31,
                    can_board=False, can_alight=False,
                ),
                self.stop("D", 3, 36.52, 127.32),
            ],
        )
        through = JourneyPlanner().plan(
            self.catalog.snapshot(), origin_node_id="O", destination_node_id="D",
            alternatives=1,
        )
        self.assertEqual(through["alternatives"][0]["route_ids"], ["THROUGH"])

        blocked_start = JourneyPlanner().plan(
            self.catalog.snapshot(), origin_node_id="X", destination_node_id="D",
            alternatives=1,
        )
        blocked_end = JourneyPlanner().plan(
            self.catalog.snapshot(), origin_node_id="O", destination_node_id="X",
            alternatives=1,
        )
        self.assertEqual(blocked_start["reason"], "STOP_ACCESS_RESTRICTED")
        self.assertEqual(blocked_end["reason"], "STOP_ACCESS_RESTRICTED")

        self.hydrate(
            "NO_ALIGHT",
            [
                self.stop("O2", 1, 36.60, 127.40),
                self.stop("T2", 2, 36.61, 127.41, can_alight=False),
            ],
        )
        self.hydrate(
            "AFTER_ALIGHT",
            [
                self.stop("T2", 1, 36.61, 127.41),
                self.stop("D2", 2, 36.62, 127.42),
            ],
        )
        no_alight_transfer = JourneyPlanner().plan(
            self.catalog.snapshot(), origin_node_id="O2", destination_node_id="D2",
            alternatives=1,
        )
        self.assertEqual(
            no_alight_transfer["reason"], "NO_DIRECTED_PATH_IN_HYDRATED_GRAPH"
        )

        self.hydrate(
            "BEFORE_BOARD",
            [
                self.stop("O3", 1, 36.70, 127.50),
                self.stop("T3", 2, 36.71, 127.51),
            ],
        )
        self.hydrate(
            "NO_BOARD",
            [
                self.stop("T3", 1, 36.71, 127.51, can_board=False),
                self.stop("D3", 2, 36.72, 127.52),
            ],
        )
        no_board_transfer = JourneyPlanner().plan(
            self.catalog.snapshot(), origin_node_id="O3", destination_node_id="D3",
            alternatives=1,
        )
        self.assertEqual(
            no_board_transfer["reason"], "NO_DIRECTED_PATH_IN_HYDRATED_GRAPH"
        )

    def test_candidate_steps_expose_official_stop_coordinates_for_map(self):
        self.hydrate(
            "MAP",
            [
                self.stop("O", 1, 36.5001, 127.3001),
                self.stop("D", 2, 36.5202, 127.3202),
            ],
        )
        candidate = JourneyPlanner().plan(
            self.catalog.snapshot(),
            origin_node_id="O",
            destination_node_id="D",
            alternatives=1,
        )["alternatives"][0]

        step = candidate["steps"][0]
        self.assertEqual(
            (step["from"]["latitude"], step["from"]["longitude"]),
            (36.5001, 127.3001),
        )
        self.assertEqual(
            (step["to"]["latitude"], step["to"]["longitude"]),
            (36.5202, 127.3202),
        )

    def test_preference_changes_dijkstra_ranking_without_fake_reliability(self):
        direct = [self.stop("O", 1, 36.5000, 127.3000)]
        direct.extend(
            self.stop(f"L{index}", index + 1, 36.5000 + index * 0.0001, 127.3000)
            for index in range(1, 20)
        )
        direct.append(self.stop("D", 21, 36.5200, 127.3200))
        self.hydrate("DIRECT", direct)
        self.hydrate("SHORT_A", [self.stop("O", 1, 36.5000, 127.3000), self.stop("X", 2, 36.5100, 127.3100)])
        self.hydrate("SHORT_B", [self.stop("X", 1, 36.5100, 127.3100), self.stop("D", 2, 36.5200, 127.3200)])
        snapshot = self.catalog.snapshot()

        low_transfer = JourneyPlanner().plan(
            snapshot, origin_node_id="O", destination_node_id="D",
            transfer_radius_m=50, alternatives=1, preference="low_transfer",
        )["alternatives"][0]
        challenge = JourneyPlanner().plan(
            snapshot, origin_node_id="O", destination_node_id="D",
            transfer_radius_m=50, alternatives=1, preference="challenge",
        )["alternatives"][0]

        self.assertEqual(low_transfer["criterion"], "minimum_transfers")
        self.assertEqual(low_transfer["route_ids"], ["DIRECT"])
        self.assertEqual(challenge["criterion"], "explorer")
        self.assertEqual(challenge["route_ids"], ["SHORT_A", "SHORT_B"])
        self.assertIsNone(challenge["success_probability"])

    def test_disconnected_components_are_reported_as_data_gap(self):
        self.hydrate("LEFT", [self.stop("O", 1, 36.50, 127.30), self.stop("A", 2, 36.51, 127.31)])
        self.hydrate("RIGHT", [self.stop("B", 1, 37.50, 128.30), self.stop("D", 2, 37.51, 128.31)])
        result = JourneyPlanner().plan(
            self.catalog.snapshot(), origin_node_id="O", destination_node_id="D", alternatives=1,
        )
        self.assertEqual(result["status"], "DATA_GAP")
        self.assertEqual(result["reason"], "NO_DIRECTED_PATH_IN_HYDRATED_GRAPH")
        self.assertEqual(result["alternatives"], [])

    def test_transfer_edges_are_lazy_at_dense_shared_stops(self):
        for index in range(30):
            self.hydrate(
                f"R{index:02d}",
                [self.stop("S", 1, 36.5, 127.3), self.stop("T", 2, 36.51, 127.31)],
            )
        planner = JourneyPlanner()
        graph = planner.build_graph(self.catalog.snapshot())
        self.assertEqual(len(graph.nodes), 60)
        self.assertEqual(len(graph.edges), 30)
        self.assertEqual(planner.max_graph_nodes, 500_000)
        self.assertEqual(planner.max_parallel_searches, 8)

    def test_transfer_edge_ids_are_deterministic_unique_and_internal(self):
        for route_id in ("USER_ROUTE_A", "USER_ROUTE_B", "USER_ROUTE_C"):
            self.hydrate(
                route_id,
                [self.stop("USER_STOP", 1, 36.5, 127.3), self.stop(f"{route_id}_END", 2, 36.51, 127.31)],
            )
        snapshot = self.catalog.snapshot()
        first_graph = JourneyPlanner().build_graph(snapshot)
        second_graph = JourneyPlanner().build_graph(snapshot)

        def transfer_ids(planner, graph):
            return [
                edge.edge_id
                for node_index in range(len(graph.nodes))
                for edge in planner._transfer_edges(graph, node_index)
            ]

        first_ids = transfer_ids(JourneyPlanner(), first_graph)
        second_ids = transfer_ids(JourneyPlanner(), second_graph)
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(len(first_ids), 12)
        self.assertEqual(len(first_ids), len(set(first_ids)))
        for edge_id in first_ids:
            self.assertRegex(edge_id, r"^transfer:\d+:\d+:(shared_node_id|geodesic_proximity)$")
            self.assertNotIn("USER_ROUTE", edge_id)
            self.assertNotIn("USER_STOP", edge_id)

    def test_duplicate_alternative_is_searched_at_most_twice(self):
        self.hydrate(
            "ONLY",
            [self.stop("O", 1, 36.5, 127.3), self.stop("D", 2, 36.51, 127.31)],
        )
        planner = JourneyPlanner()
        with patch.object(planner, "_shortest_path", wraps=planner._shortest_path) as shortest_path:
            result = planner.plan(
                self.catalog.snapshot(),
                origin_node_id="O",
                destination_node_id="D",
                alternatives=2,
            )
        self.assertEqual(len(result["alternatives"]), 1)
        self.assertEqual(shortest_path.call_count, 3)

    def test_missing_schedule_or_passage_history_never_emits_probability(self):
        self.hydrate_three_paths()
        result = JourneyPlanner().plan(self.catalog.snapshot(), origin_node_id="O", destination_node_id="D", alternatives=1)
        candidate = result["alternatives"][0]
        self.assertEqual(result["status"], "DATA_GAP")
        self.assertEqual(candidate["status"], "DATA_GAP")
        self.assertIsNone(candidate["success_probability"])
        self.assertIn("VERIFIED_TIMETABLE_REQUIRED", candidate["reasons"])
        self.assertIn("PASSAGE_HISTORY_REQUIRED", candidate["reasons"])

        flag_only = JourneyPlanner().plan(
            self.catalog.snapshot(),
            origin_node_id="O",
            destination_node_id="D",
            alternatives=1,
            service_evidence={"R1": {"verified": True}, "R2": True, "R3": {"verified": True}},
        )
        self.assertIn("VERIFIED_TIMETABLE_REQUIRED", flag_only["alternatives"][0]["reasons"])

        schedule_only = JourneyPlanner().plan(
            self.catalog.snapshot(),
            origin_node_id="O",
            destination_node_id="D",
            alternatives=1,
            service_evidence={route: self.verified_timetable() for route in ("R1", "R2", "R3")},
        )
        self.assertIsNone(schedule_only["alternatives"][0]["success_probability"])
        self.assertIn("PASSAGE_HISTORY_REQUIRED", schedule_only["alternatives"][0]["reasons"])

    def test_reconstructed_passage_ratio_never_becomes_success_probability(self):
        self.hydrate_three_paths()
        service = {route: self.verified_timetable() for route in ("R1", "R2", "R3")}
        insufficient = {
            route: {"sample_count": 7, "observed_passage_ratio": 0.9}
            for route in service
        }
        gap = JourneyPlanner().plan(
            self.catalog.snapshot(), origin_node_id="O", destination_node_id="D", alternatives=1,
            service_evidence=service, passage_history=insufficient,
        )
        self.assertIsNone(gap["alternatives"][0]["success_probability"])
        sufficient = {
            route: {"sample_count": 8, "observed_passage_ratio": 0.9}
            for route in service
        }
        observed = JourneyPlanner().plan(
            self.catalog.snapshot(), origin_node_id="O", destination_node_id="D", alternatives=1,
            service_evidence=service, passage_history=sufficient,
        )
        candidate = observed["alternatives"][0]
        self.assertEqual(observed["status"], "DATA_GAP")
        self.assertIsNone(candidate["success_probability"])
        self.assertNotIn("PASSAGE_HISTORY_REQUIRED", candidate["reasons"])
        self.assertIn("VALIDATED_JOURNEY_SUCCESS_MODEL_REQUIRED", candidate["reasons"])
        self.assertEqual(
            candidate["evidence"]["passage_routes"]["R1"]["observed_passage_ratio"],
            0.9,
        )
        self.assertFalse(
            candidate["reliability"]["historical_gtfs_prior"]["projection_allowed"]
        )

    def test_route_edges_are_directional_and_never_inferred_in_reverse(self):
        self.hydrate("ONEWAY", [self.stop("O", 1, 36.5, 127.3), self.stop("M", 2, 36.51, 127.31), self.stop("D", 3, 36.52, 127.32)])
        planner = JourneyPlanner()
        forward = planner.plan(self.catalog.snapshot(), origin_node_id="O", destination_node_id="D", alternatives=1)
        reverse = planner.plan(self.catalog.snapshot(), origin_node_id="D", destination_node_id="O", alternatives=1)
        self.assertEqual(len(forward["alternatives"]), 1)
        self.assertEqual(reverse["status"], "DATA_GAP")
        self.assertEqual(reverse["reason"], "NO_DIRECTED_PATH_IN_HYDRATED_GRAPH")

    def test_proximity_transfer_respects_configured_50_to_800_meter_radius(self):
        self.hydrate("WEST", [self.stop("O", 1, 36.5000, 127.3000), self.stop("X", 2, 36.5100, 127.3100)])
        self.hydrate("EAST", [self.stop("Y", 1, 36.5106, 127.3100), self.stop("D", 2, 36.5200, 127.3200)])
        planner = JourneyPlanner()
        too_short = planner.plan(self.catalog.snapshot(), origin_node_id="O", destination_node_id="D", transfer_radius_m=50, alternatives=1)
        connected = planner.plan(self.catalog.snapshot(), origin_node_id="O", destination_node_id="D", transfer_radius_m=100, alternatives=1)
        self.assertEqual(too_short["alternatives"], [])
        self.assertEqual(connected["alternatives"][0]["transfers"], 1)
        transfer = [step for step in connected["alternatives"][0]["steps"] if step["kind"] == "transfer"][0]
        self.assertEqual(transfer["evidence"]["type"], "geodesic_proximity")
        self.assertGreater(transfer["distance_m"], 50)
        self.assertLess(transfer["distance_m"], 100)
        with self.assertRaises(PlannerValidationError):
            planner.build_graph(self.catalog.snapshot(), transfer_radius_m=49)
        with self.assertRaises(PlannerValidationError):
            planner.build_graph(self.catalog.snapshot(), transfer_radius_m=801)

    def test_graph_snapshot_and_cache_are_immutable(self):
        self.hydrate_three_paths()
        planner = JourneyPlanner()
        first = planner.build_graph(self.catalog.snapshot(), transfer_radius_m=300)
        second = planner.build_graph(self.catalog.snapshot(), transfer_radius_m=300)
        self.assertIs(first, second)
        self.assertIsInstance(first.nodes, tuple)
        self.assertIsInstance(first.edges, tuple)
        self.assertIsInstance(first.adjacency, tuple)
        with self.assertRaises(FrozenInstanceError):
            first.version = "mutated"
        with self.assertRaises(TypeError):
            first.node_id_indexes["O"] = ()

    def test_nodes_alternatives_and_cpu_limits_are_enforced(self):
        self.hydrate_three_paths()
        snapshot = self.catalog.snapshot()
        with self.assertRaises(PlannerLimitError):
            JourneyPlanner(max_graph_nodes=5).build_graph(snapshot)
        with self.assertRaises(PlannerLimitError):
            JourneyPlanner().plan(snapshot, origin_node_id="O", destination_node_id="D", alternatives=6)
        with self.assertRaises(PlannerLimitError):
            JourneyPlanner(max_expansions=1).plan(snapshot, origin_node_id="O", destination_node_id="D", alternatives=1)

    def test_unhydrated_catalog_returns_data_gap_instead_of_guessing(self):
        result = JourneyPlanner().plan(self.catalog.snapshot(), origin_node_id="O", destination_node_id="D", alternatives=3)
        self.assertEqual(result["status"], "DATA_GAP")
        self.assertEqual(result["reason"], "STOP_NOT_IN_HYDRATED_SEQUENCE")
        self.assertEqual(result["alternatives"], [])


if __name__ == "__main__":
    unittest.main()
