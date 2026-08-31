from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

from journey_planner import (  # noqa: E402
    JourneyPlanner,
    MAX_WALK_TARGET_STOPS,
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
        node_id, order, latitude, longitude, *, can_board=True, can_alight=True,
        direction="",
    ):
        return {
            "node_id": node_id,
            "node_name": node_id,
            "node_order": order,
            "latitude": latitude,
            "longitude": longitude,
            "direction": direction,
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

    def test_graph_coverage_uses_full_catalog_denominator(self):
        self.hydrate(
            "HYDRATED_ONLY",
            [
                self.stop("O", 1, 36.5000, 127.3000),
                self.stop("D", 2, 36.5100, 127.3100),
            ],
        )
        snapshot = replace(
            self.catalog.planning_snapshot(),
            catalog_route_count=3,
        )

        coverage = JourneyPlanner().plan(
            snapshot,
            origin_node_id="O",
            destination_node_id="D",
            alternatives=1,
        )["graph"]["coverage"]

        self.assertEqual(coverage["hydrated_routes"], 1)
        self.assertEqual(coverage["catalog_routes"], 3)
        self.assertEqual(coverage["missing_routes"], 2)
        self.assertEqual(coverage["status"], "PARTIAL")
        self.assertFalse(coverage["nationwide_topology_complete"])

    def test_graph_coverage_counts_only_hydrated_discovered_targets(self):
        self.hydrate(
            "LEGACY_ONLY",
            [
                self.stop("O", 1, 36.5000, 127.3000),
                self.stop("D", 2, 36.5100, 127.3100),
            ],
        )
        snapshot = replace(
            self.catalog.planning_snapshot(),
            topology_target_count=5,
            topology_complete_count=5,
            topology_discovery_complete=True,
            topology_hydrated_count=4,
        )

        coverage = JourneyPlanner().plan(
            snapshot,
            origin_node_id="O",
            destination_node_id="D",
            alternatives=1,
        )["graph"]["coverage"]

        self.assertEqual(coverage["hydrated_routes"], 1)
        self.assertEqual(coverage["hydrated_discovered_targets"], 4)
        self.assertEqual(coverage["missing_routes"], 1)
        self.assertFalse(coverage["nationwide_topology_complete"])

    def test_real_991_b1_607_direction_reaches_okcheon_with_two_transfers(self):
        # Current TAGO IDs/orders/coordinates, compressed to the six stops
        # needed to preserve the authoritative direction and transfer points.
        self.hydrate(
            "SJB293000331",
            [
                self.stop("SJB293001072", 20, 36.599743, 127.295111),
                self.stop("SJB293062013", 34, 36.505150, 127.261382),
            ],
            city_code="12",
        )
        self.hydrate(
            "DJB30300128",
            [
                self.stop(
                    "DJB8007080", 35, 36.505080, 127.261510,
                    direction="1",
                ),
                self.stop(
                    "DJB8001420", 54, 36.333435, 127.431404,
                    direction="1",
                ),
            ],
            city_code="25",
        )
        self.hydrate(
            "DJB30300074",
            [
                self.stop(
                    "DJB8001420", 21, 36.333435, 127.431404,
                    direction="0",
                ),
                self.stop(
                    "DJB8005033", 53, 36.299640, 127.566340,
                    direction="0",
                ),
            ],
            city_code="25",
        )
        snapshot = self.catalog.snapshot()
        planner = JourneyPlanner()

        forward = planner.plan(
            snapshot,
            origin_node_id="SJB293001072",
            destination_node_id="DJB8005033",
            origin_city_code="12",
            destination_city_code="25",
            transfer_radius_m=50,
            alternatives=1,
        )

        candidate = forward["alternatives"][0]
        self.assertEqual(
            candidate["route_ids"],
            ["SJB293000331", "DJB30300128", "DJB30300074"],
        )
        self.assertEqual(candidate["transfers"], 2)
        transfers = [step for step in candidate["steps"] if step["kind"] == "transfer"]
        self.assertEqual(len(transfers), 2)
        self.assertEqual(
            (transfers[0]["from"]["node_id"], transfers[0]["to"]["node_id"]),
            ("SJB293062013", "DJB8007080"),
        )
        self.assertEqual(transfers[0]["evidence"]["type"], "geodesic_proximity")
        self.assertLess(transfers[0]["distance_m"], 15)
        self.assertEqual(
            (transfers[1]["from"]["node_id"], transfers[1]["to"]["node_id"]),
            ("DJB8001420", "DJB8001420"),
        )
        self.assertEqual(transfers[1]["evidence"]["type"], "shared_node_id")

        reverse = planner.plan(
            snapshot,
            origin_node_id="DJB8005033",
            destination_node_id="SJB293001072",
            origin_city_code="25",
            destination_city_code="12",
            transfer_radius_m=50,
            alternatives=1,
        )
        self.assertEqual(reverse["alternatives"], [])
        self.assertEqual(reverse["reason"], "NO_DIRECTED_PATH_IN_HYDRATED_GRAPH")

    def test_explicit_direction_changes_split_rides_but_blank_and_distance_do_not(self):
        self.hydrate(
            "SPLIT",
            [
                self.stop("SO", 1, 36.00, 127.00, direction="0"),
                self.stop("SA", 2, 36.01, 127.00, direction="0"),
                self.stop("SB", 3, 36.10, 127.00, direction="1"),
                self.stop("SD", 4, 36.11, 127.00, direction="1"),
            ],
        )
        self.hydrate(
            "BLANK_EVIDENCE",
            [
                self.stop("BO", 1, 37.00, 128.00, direction="0"),
                self.stop("BX", 2, 37.01, 128.00, direction=""),
                self.stop("BD", 3, 37.02, 128.00, direction="1"),
            ],
        )
        self.hydrate(
            "LONG_SAME_DIRECTION",
            [
                self.stop("LO", 1, 34.70, 126.30, direction="2"),
                self.stop("LD", 2, 34.95, 126.30, direction="2"),
            ],
        )
        self.hydrate(
            "UNKNOWN_SPLIT",
            [
                self.stop("UO", 1, 35.50, 128.50, direction="1"),
                self.stop("UD", 2, 35.51, 128.50, direction="2"),
            ],
        )
        snapshot = self.catalog.snapshot()
        planner = JourneyPlanner()

        split = planner.plan(
            snapshot,
            origin_node_id="SO",
            destination_node_id="SD",
            transfer_radius_m=50,
            alternatives=1,
        )
        self.assertEqual(split["alternatives"], [])
        self.assertEqual(split["reason"], "NO_DIRECTED_PATH_IN_HYDRATED_GRAPH")
        self.assertEqual(
            planner.plan(
                snapshot,
                origin_node_id="SO",
                destination_node_id="SA",
                transfer_radius_m=50,
                alternatives=1,
            )["alternatives"][0]["route_ids"],
            ["SPLIT"],
        )
        self.assertEqual(
            planner.plan(
                snapshot,
                origin_node_id="SB",
                destination_node_id="SD",
                transfer_radius_m=50,
                alternatives=1,
            )["alternatives"][0]["route_ids"],
            ["SPLIT"],
        )

        blank = planner.plan(
            snapshot,
            origin_node_id="BO",
            destination_node_id="BD",
            transfer_radius_m=50,
            alternatives=1,
        )
        self.assertEqual(blank["alternatives"][0]["route_ids"], ["BLANK_EVIDENCE"])
        self.assertEqual(
            blank["graph"]["directionality"],
            "ascending_node_order_with_nonempty_direction_boundaries",
        )

        long_same_direction = planner.plan(
            snapshot,
            origin_node_id="LO",
            destination_node_id="LD",
            transfer_radius_m=50,
            alternatives=1,
        )
        self.assertEqual(
            long_same_direction["alternatives"][0]["route_ids"],
            ["LONG_SAME_DIRECTION"],
        )
        unknown_split = planner.plan(
            snapshot,
            origin_node_id="UO",
            destination_node_id="UD",
            transfer_radius_m=50,
            alternatives=1,
        )
        self.assertEqual(unknown_split["alternatives"], [])
        self.assertEqual(
            unknown_split["reason"], "NO_DIRECTED_PATH_IN_HYDRATED_GRAPH"
        )

    def test_static_okcheon_stop_snaps_to_routable_terminal_with_walk_step(self):
        self.hydrate(
            "DJB30300074",
            [
                self.stop("O", 1, 36.290000, 127.550000),
                self.stop("DJB8005033", 2, 36.299640, 127.566340),
            ],
            city_code="25",
        )

        result = JourneyPlanner().plan(
            self.catalog.snapshot(),
            origin_node_id="O",
            destination_node_id="OCB276000024",
            origin_city_code="25",
            destination_city_code="33330",
            transfer_radius_m=50,
            alternatives=1,
            destination_access={
                "city_code": "33330",
                "node_id": "OCB276000024",
                "node_name": "옥천버스앞",
                "latitude": 36.299573,
                "longitude": 127.566392,
            },
        )

        candidate = result["alternatives"][0]
        self.assertEqual(candidate["route_ids"], ["DJB30300074"])
        self.assertEqual(candidate["steps"][-1]["kind"], "walk")
        self.assertEqual(candidate["steps"][-1]["access_kind"], "egress")
        self.assertEqual(
            (
                candidate["steps"][-1]["from"]["node_id"],
                candidate["steps"][-1]["to"]["node_id"],
            ),
            ("DJB8005033", "OCB276000024"),
        )
        self.assertGreater(candidate["walking_m"], 0)
        self.assertLess(candidate["walking_m"], 50)

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

    def test_exhausted_criterion_does_not_hide_later_transfer_route(self):
        self.hydrate(
            "DIRECT",
            [self.stop("O", 1, 36.5000, 127.3000), self.stop("D", 2, 36.5200, 127.3200)],
        )
        self.hydrate(
            "LOCAL_A",
            [self.stop("O", 1, 36.5000, 127.3000), self.stop("X", 2, 36.5100, 127.3100)],
        )
        self.hydrate(
            "LOCAL_B",
            [self.stop("X", 1, 36.5100, 127.3100), self.stop("D", 2, 36.5200, 127.3200)],
        )
        planner = JourneyPlanner()
        snapshot = self.catalog.snapshot()
        graph = planner.build_graph(snapshot, transfer_radius_m=50)
        starts = tuple(graph.node_id_indexes["O"])
        goals = frozenset(graph.node_id_indexes["D"])
        direct = planner._shortest_path(
            graph, starts, goals, "minimum_transfers", Counter(), [planner.max_expansions], 0,
        )
        transfer = planner._shortest_path(
            graph,
            starts,
            goals,
            "explorer",
            Counter({edge.edge_id: 100 for edge in direct}),
            [planner.max_expansions],
            0,
        )
        self.assertEqual([edge.route_id for edge in direct if edge.kind == "ride"], ["DIRECT"])
        self.assertEqual(
            list(dict.fromkeys(edge.route_id for edge in transfer if edge.kind == "ride")),
            ["LOCAL_A", "LOCAL_B"],
        )

        def criterion_paths(_graph, _starts, _goals, criterion, _penalties, _budget, _attempt):
            return transfer if criterion == "explorer" else direct

        with patch.object(planner, "_shortest_path", side_effect=criterion_paths):
            result = planner.plan(
                snapshot,
                origin_node_id="O",
                destination_node_id="D",
                transfer_radius_m=50,
                alternatives=3,
                preference="diverse",
            )

        self.assertEqual(
            [candidate["route_ids"] for candidate in result["alternatives"]],
            [["DIRECT"], ["LOCAL_A", "LOCAL_B"]],
        )

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

    def test_proximity_transfer_searches_two_longitude_cells_at_korean_latitude(self):
        self.hydrate(
            "WEST",
            [
                self.stop("O", 1, 36.4900, 127.2900),
                self.stop("X", 2, 36.5000, 127.3000),
            ],
        )
        self.hydrate(
            "EAST",
            [
                self.stop("Y", 1, 36.5000, 127.3030),
                self.stop("D", 2, 36.5100, 127.3130),
            ],
        )
        planner = JourneyPlanner()
        graph = planner.build_graph(self.catalog.snapshot(), transfer_radius_m=300)
        x_group = graph.state_stop_groups[graph.node_id_indexes["X"][0]]
        y_group = graph.state_stop_groups[graph.node_id_indexes["Y"][0]]
        x_coordinate = graph.stop_group_coordinates[x_group]
        y_coordinate = graph.stop_group_coordinates[y_group]
        x_cell = (
            math.floor(x_coordinate[0] / graph.spatial_cell_degrees),
            math.floor(x_coordinate[1] / graph.spatial_cell_degrees),
        )
        y_cell = (
            math.floor(y_coordinate[0] / graph.spatial_cell_degrees),
            math.floor(y_coordinate[1] / graph.spatial_cell_degrees),
        )
        self.assertEqual(abs(x_cell[1] - y_cell[1]), 2)

        result = planner.plan(
            self.catalog.snapshot(),
            origin_node_id="O",
            destination_node_id="D",
            transfer_radius_m=300,
            alternatives=1,
        )

        transfer = next(
            step
            for step in result["alternatives"][0]["steps"]
            if step["kind"] == "transfer"
        )
        self.assertEqual((transfer["from"]["node_id"], transfer["to"]["node_id"]), ("X", "Y"))
        self.assertLess(transfer["distance_m"], 300)

    def test_walk_target_limit_fails_explicitly_instead_of_slicing_candidates(self):
        self.assertGreaterEqual(MAX_WALK_TARGET_STOPS, 128)
        self.hydrate(
            "SOURCE",
            [
                self.stop("O", 1, 36.4900, 127.2900),
                self.stop("X", 2, 36.5000, 127.3000),
            ],
        )
        for index in range(MAX_WALK_TARGET_STOPS + 1):
            self.hydrate(
                f"TARGET_{index:03d}",
                [
                    self.stop(f"Y_{index:03d}", 1, 36.5000, 127.3000),
                    self.stop(f"D_{index:03d}", 2, 36.5100, 127.3100),
                ],
            )
        planner = JourneyPlanner()
        graph = planner.build_graph(self.catalog.snapshot(), transfer_radius_m=300)
        source_index = graph.node_id_indexes["X"][0]

        with self.assertRaisesRegex(
            PlannerLimitError,
            rf"walk-transfer targets exceed the {MAX_WALK_TARGET_STOPS}-stop CPU bound",
        ):
            planner._transfer_edges(graph, source_index)

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
