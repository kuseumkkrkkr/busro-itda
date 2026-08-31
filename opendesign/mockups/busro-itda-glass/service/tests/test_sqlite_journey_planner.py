from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from journey_planner import JourneyPlanner, PlannerLimitError  # noqa: E402
from network_catalog import NetworkCatalog  # noqa: E402
from sqlite_journey_planner import (  # noqa: E402
    _SearchContext,
    SQLiteJourneyPlanner,
    PlannerBusyError,
    required_index_ddl,
)


FIXED_NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


class SQLiteJourneyPlannerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "catalog.sqlite3"
        self.catalog = NetworkCatalog(self.path, clock=lambda: FIXED_NOW)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_default_search_wall_covers_nationwide_multi_transfer_queries(self) -> None:
        planner = SQLiteJourneyPlanner(self.path)
        self.assertEqual(planner.search_wall_seconds, 15.0)
        self.assertEqual(planner.short_result_ttl_seconds, 300.0)

    @staticmethod
    def stop(
        node_id: str,
        order: int,
        latitude: float,
        longitude: float,
        *,
        node_name: str | None = None,
        direction: str = "",
        can_board: bool = True,
        can_alight: bool = True,
    ) -> dict[str, object]:
        return {
            "node_id": node_id,
            "node_name": node_name or node_id,
            "node_order": order,
            "latitude": latitude,
            "longitude": longitude,
            "direction": direction,
            "can_board": can_board,
            "can_alight": can_alight,
        }

    def hydrate(
        self,
        route_id: str,
        stops: list[dict[str, object]],
        *,
        city_code: str = "25",
    ) -> None:
        self.catalog.hydrate_route_sequence(
            city_code=city_code,
            route_id=route_id,
            ordered_stops=stops,
            source=f"TEST:{route_id}",
            captured_at="2026-08-31T00:00:00Z",
        )

    def hydrate_two_route_fixture(self) -> None:
        self.hydrate(
            "A_OUTBOUND",
            [
                self.stop("ORIGIN", 1, 36.0000, 127.0000),
                self.stop("TRANSFER", 2, 36.0100, 127.0100),
            ],
        )
        self.hydrate(
            "B_OUTBOUND",
            [
                self.stop("TRANSFER", 10, 36.0100, 127.0100),
                self.stop("MID", 11, 36.0150, 127.0150),
                self.stop("DESTINATION", 12, 36.0200, 127.0200),
            ],
        )

    def test_matches_existing_planner_route_transfers_and_direction(self) -> None:
        self.hydrate_two_route_fixture()
        existing = JourneyPlanner().plan(
            self.catalog.snapshot(),
            origin_node_id="ORIGIN",
            destination_node_id="DESTINATION",
            origin_city_code="25",
            destination_city_code="25",
            transfer_radius_m=300,
            alternatives=1,
        )["alternatives"][0]
        sqlite_result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="ORIGIN",
            destination_node_id="DESTINATION",
            origin_city_code="25",
            destination_city_code="25",
        )
        sqlite_candidate = sqlite_result["alternatives"][0]

        self.assertEqual(sqlite_candidate["route_ids"], existing["route_ids"])
        self.assertEqual(sqlite_candidate["transfers"], existing["transfers"])
        ride_steps = [
            step for step in sqlite_candidate["steps"] if step["kind"] == "ride"
        ]
        self.assertTrue(ride_steps)
        self.assertTrue(
            all(step["from"]["node_order"] < step["to"]["node_order"] for step in ride_steps)
        )
        self.assertEqual(
            sqlite_result["graph"]["topology_materialization"],
            "on_demand_indexed_sqlite",
        )

        low_transfer = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="ORIGIN",
            destination_node_id="DESTINATION",
            origin_city_code="25",
            destination_city_code="25",
            alternatives=1,
            preference="low_transfer",
        )
        self.assertEqual(
            low_transfer["graph"]["alternative_algorithm"],
            "transfer_layer_primary",
        )
        self.assertEqual(low_transfer["graph"]["transfer_layer_sizes"], [1, 1])
        self.assertEqual(
            low_transfer["alternatives"][0]["route_ids"],
            ["A_OUTBOUND", "B_OUTBOUND"],
        )

    def test_ride_step_contains_all_ten_stops_for_nine_order_delta(self) -> None:
        self.hydrate(
            "TEN_STOPS",
            [
                self.stop(
                    f"STOP_{order}",
                    order,
                    35.0 + (order - 20) * 0.01,
                    127.0,
                )
                for order in range(20, 30)
            ],
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="STOP_20",
            destination_node_id="STOP_29",
            preference="low_transfer",
        )
        ride = next(
            step
            for step in result["alternatives"][0]["steps"]
            if step["kind"] == "ride"
        )

        self.assertEqual(ride["stop_order_delta"], 9)
        self.assertEqual(ride["stop_count"], 10)
        self.assertEqual(len(ride["segment_stops"]), 10)
        self.assertEqual(
            [stop["node_order"] for stop in ride["segment_stops"]],
            list(range(20, 30)),
        )
        self.assertFalse(ride["segment_stops_truncated"])
        # Catalog identity + two endpoint lookups + one route load. Candidate
        # enrichment reuses that route load rather than issuing another query.
        self.assertEqual(result["graph"]["queries"], 4)

    def test_sparse_node_orders_use_sequence_rows_for_cost_and_stop_count(self) -> None:
        self.hydrate(
            "SPARSE_FIRST",
            [
                self.stop("O", 10, 36.000, 127.000),
                self.stop("X", 1_000, 36.010, 127.010),
            ],
        )
        self.hydrate(
            "SPARSE_SECOND",
            [
                self.stop("X", 50, 36.010, 127.010),
                self.stop("D", 900, 36.040, 127.040),
            ],
        )
        self.hydrate(
            "DENSE_FIRST",
            [
                self.stop("O", 1, 36.000, 127.000),
                self.stop("M", 2, 36.020, 127.000),
                self.stop("Y", 3, 36.030, 127.000),
            ],
        )
        self.hydrate(
            "DENSE_SECOND",
            [
                self.stop("Y", 1, 36.030, 127.000),
                self.stop("D", 2, 36.040, 127.040),
            ],
        )

        planner = SQLiteJourneyPlanner(self.path)
        for preference in ("low_transfer", "challenge"):
            with self.subTest(preference=preference):
                result = planner.plan(
                    origin_node_id="O",
                    destination_node_id="D",
                    alternatives=1,
                    preference=preference,
                )
                candidate = result["alternatives"][0]
                rides = [
                    step
                    for step in candidate["steps"]
                    if step["kind"] == "ride"
                ]

                self.assertEqual(
                    candidate["route_ids"],
                    ["SPARSE_FIRST", "SPARSE_SECOND"],
                )
                self.assertEqual(candidate["ride_order_delta"], 2)
                self.assertEqual(
                    [ride["stop_order_delta"] for ride in rides],
                    [1, 1],
                )
                self.assertEqual(
                    [ride["stop_count"] for ride in rides],
                    [2, 2],
                )

    def test_ride_segment_uniformly_samples_more_than_160_stops(self) -> None:
        stop_total = 201
        self.hydrate(
            "LONG_SEGMENT",
            [
                self.stop(
                    f"LONG_{order}",
                    order,
                    35.0 + (order - 1) * 0.01,
                    127.0,
                )
                for order in range(1, stop_total + 1)
            ],
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="LONG_1",
            destination_node_id=f"LONG_{stop_total}",
            preference="low_transfer",
        )
        ride = next(
            step
            for step in result["alternatives"][0]["steps"]
            if step["kind"] == "ride"
        )
        sampled_orders = [
            stop["node_order"] for stop in ride["segment_stops"]
        ]
        expected_orders = [
            1 + (sample_index * (stop_total - 1)) // 159
            for sample_index in range(160)
        ]

        self.assertEqual(ride["stop_order_delta"], 200)
        self.assertEqual(ride["stop_count"], stop_total)
        self.assertEqual(len(ride["segment_stops"]), 160)
        self.assertEqual(sampled_orders, expected_orders)
        self.assertEqual(sampled_orders[0], 1)
        self.assertEqual(sampled_orders[-1], stop_total)
        self.assertTrue(ride["segment_stops_truncated"])
        self.assertEqual(result["graph"]["queries"], 4)

    def test_compressed_real_991_b1_607_route_uses_both_transfer_types(self) -> None:
        # Current TAGO route IDs/orders and transfer coordinates, reduced to
        # the six states needed to retain direction and interchange identity.
        self.hydrate(
            "SJB293000331",
            [
                self.stop("SJB293001072", 20, 36.599743, 127.295111, direction="0"),
                self.stop("SJB293062013", 34, 36.505150, 127.261382, direction="0"),
            ],
            city_code="12",
        )
        self.hydrate(
            "DJB30300128",
            [
                self.stop("DJB8007080", 35, 36.505080, 127.261510, direction="1"),
                self.stop("DJB8001420", 54, 36.333435, 127.431404, direction="1"),
            ],
            city_code="25",
        )
        self.hydrate(
            "DJB30300074",
            [
                self.stop("DJB8001420", 21, 36.333435, 127.431404, direction="1"),
                self.stop(
                    "DJB8005033",
                    53,
                    36.299640,
                    127.566340,
                    node_name="옥천버스앞",
                    direction="1",
                ),
            ],
            city_code="25",
        )
        self.hydrate(
            "DJB_OPPOSITE_SIDE",
            [
                self.stop(
                    "DJB8005032",
                    1,
                    36.299573,
                    127.566392,
                    node_name="옥천버스앞",
                    direction="0",
                ),
                self.stop(
                    "DJB_AWAY_FROM_OCKCHEON",
                    2,
                    36.310000,
                    127.580000,
                    direction="0",
                ),
            ],
            city_code="25",
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="SJB293001072",
            destination_node_id="DJB8005033",
            origin_city_code="12",
            destination_city_code="25",
        )
        candidate = result["alternatives"][0]
        self.assertEqual(
            candidate["route_ids"],
            ["SJB293000331", "DJB30300128", "DJB30300074"],
        )
        self.assertEqual(candidate["transfers"], 2)
        transfers = [step for step in candidate["steps"] if step["kind"] == "transfer"]
        self.assertEqual(
            [step["evidence"]["type"] for step in transfers],
            ["geodesic_proximity", "shared_node_id"],
        )
        self.assertLess(transfers[0]["distance_m"], 15)

        opposite_side_selection = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="SJB293001072",
            destination_node_id="DJB8005032",
            origin_city_code="12",
            destination_city_code="25",
            destination_access={
                "city_code": "25",
                "node_id": "DJB8005032",
                "node_name": "옥천버스앞",
                "latitude": 36.299573,
                "longitude": 127.566392,
            },
            preference="low_transfer",
        )
        opposite_candidate = opposite_side_selection["alternatives"][0]
        self.assertEqual(
            opposite_candidate["route_ids"],
            ["SJB293000331", "DJB30300128", "DJB30300074"],
        )
        egress = [
            step
            for step in opposite_candidate["steps"]
            if step["kind"] == "walk"
        ]
        self.assertEqual(len(egress), 1)
        self.assertEqual(egress[0]["from"]["node_id"], "DJB8005033")
        self.assertEqual(egress[0]["to"]["node_id"], "DJB8005032")
        self.assertLess(egress[0]["distance_m"], 10)

        reverse = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="DJB8005033",
            destination_node_id="SJB293001072",
            origin_city_code="25",
            destination_city_code="12",
        )
        self.assertEqual(reverse["alternatives"], [])
        self.assertEqual(reverse["reason"], "NO_DIRECTED_PATH_IN_SQLITE_GRAPH")

    def test_static_endpoint_coordinates_snap_without_fabricating_a_stop(self) -> None:
        self.hydrate(
            "ONE_DIRECTION",
            [
                self.stop("ORIGIN", 1, 36.290000, 127.550000),
                self.stop("GRAPH_DEST", 2, 36.299640, 127.566340),
            ],
        )
        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="ORIGIN",
            destination_node_id="STATIC_DEST",
            origin_city_code="25",
            destination_city_code="33330",
            destination_access={
                "city_code": "33330",
                "node_id": "STATIC_DEST",
                "node_name": "옥천버스앞",
                "latitude": 36.299573,
                "longitude": 127.566392,
            },
        )
        candidate = result["alternatives"][0]
        self.assertEqual(candidate["route_ids"], ["ONE_DIRECTION"])
        walk = [step for step in candidate["steps"] if step["kind"] == "walk"]
        self.assertEqual(len(walk), 1)
        self.assertEqual(walk[0]["access_kind"], "egress")
        self.assertEqual(walk[0]["to"]["node_id"], "STATIC_DEST")
        self.assertLess(walk[0]["distance_m"], 10)

    def test_exact_endpoints_route_without_coordinates_and_do_not_trigger_nearby_fallback(self) -> None:
        self.hydrate(
            "EXACT-NO-COORDINATES",
            [
                {
                    "node_id": "EXACT-A",
                    "node_name": "EXACT-A",
                    "node_order": 1,
                },
                {
                    "node_id": "EXACT-B",
                    "node_name": "EXACT-B",
                    "node_order": 2,
                },
            ],
        )
        origin_access = self.catalog.planning_stop_reference(
            node_id="EXACT-A", city_code="25"
        )
        destination_access = self.catalog.planning_stop_reference(
            node_id="EXACT-B", city_code="25"
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="EXACT-A",
            destination_node_id="EXACT-B",
            origin_city_code="25",
            destination_city_code="25",
            origin_access=origin_access,
            destination_access=destination_access,
        )

        self.assertEqual(
            result["alternatives"][0]["route_ids"],
            ["EXACT-NO-COORDINATES"],
        )
        self.assertEqual(result["alternatives"][0]["walking_m"], 0.0)

        missing = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="CATALOG-ONLY",
            destination_node_id="EXACT-B",
            origin_city_code="25",
            destination_city_code="25",
            origin_access={
                "city_code": "25",
                "node_id": "CATALOG-ONLY",
                "node_name": "좌표 없는 카탈로그 정류장",
                "latitude": None,
                "longitude": None,
            },
            destination_access=destination_access,
        )
        self.assertEqual(missing["alternatives"], [])
        self.assertEqual(missing["reason"], "STOP_NOT_IN_ACTIVE_SEQUENCE")

    def test_exact_endpoint_does_not_union_nearby_route_states(self) -> None:
        self.hydrate(
            "EXACT_ROUTE",
            [
                self.stop("ORIGIN", 1, 36.000000, 127.000000),
                self.stop("DESTINATION", 2, 36.010000, 127.010000),
            ],
        )
        self.hydrate(
            "NEARBY_ROUTE",
            [
                self.stop("NEAR_ORIGIN", 1, 36.000050, 127.000050),
                self.stop("OTHER_DESTINATION", 2, 36.020000, 127.020000),
            ],
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="ORIGIN",
            destination_node_id="DESTINATION",
            origin_city_code="25",
            destination_city_code="25",
            origin_access={
                "city_code": "25",
                "node_id": "ORIGIN",
                "node_name": "ORIGIN",
                "latitude": 36.000000,
                "longitude": 127.000000,
            },
            alternatives=1,
            preference="low_transfer",
        )

        self.assertEqual(result["alternatives"][0]["route_ids"], ["EXACT_ROUTE"])
        self.assertEqual(result["graph"]["transfer_layer_sizes"], [1])

    def test_exact_endpoint_wins_over_same_name_nearby_counterpart(self) -> None:
        self.hydrate(
            "EXACT_ROUTE",
            [
                self.stop("ORIGIN", 1, 36.0000, 127.0000),
                self.stop("EXACT_MID_1", 2, 36.0030, 127.0030),
                self.stop("EXACT_MID_2", 3, 36.0060, 127.0060),
                self.stop(
                    "SELECTED_DEST",
                    4,
                    36.0100,
                    127.0100,
                    node_name="중앙역",
                ),
            ],
        )
        self.hydrate(
            "SHORT_COUNTERPART_ROUTE",
            [
                self.stop("ORIGIN", 1, 36.0000, 127.0000),
                self.stop(
                    "DEST_COUNTERPART",
                    2,
                    36.01004,
                    127.01004,
                    node_name=" 중앙역 ",
                ),
            ],
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="ORIGIN",
            destination_node_id="SELECTED_DEST",
            origin_city_code="25",
            destination_city_code="25",
            destination_access={
                "city_code": "25",
                "node_id": "SELECTED_DEST",
                "node_name": "중앙역",
                "latitude": 36.0100,
                "longitude": 127.0100,
            },
            preference="low_transfer",
        )

        candidate = result["alternatives"][0]
        self.assertEqual(candidate["route_ids"], ["EXACT_ROUTE"])
        self.assertEqual(candidate["walking_m"], 0.0)
        self.assertEqual(candidate["endpoint_access"], [])

    def test_exact_endpoint_wins_even_when_counterpart_is_a_shallower_path(self) -> None:
        self.hydrate(
            "EXACT_FIRST",
            [
                self.stop("ORIGIN", 1, 36.0000, 127.0000),
                self.stop("TRANSFER", 2, 36.0050, 127.0050),
            ],
        )
        self.hydrate(
            "EXACT_SECOND",
            [
                self.stop("TRANSFER", 1, 36.0050, 127.0050),
                self.stop(
                    "SELECTED_DEST",
                    2,
                    36.0100,
                    127.0100,
                    node_name="중앙역",
                ),
            ],
        )
        self.hydrate(
            "COUNTERPART_DIRECT",
            [
                self.stop("ORIGIN", 1, 36.0000, 127.0000),
                self.stop(
                    "DEST_COUNTERPART",
                    2,
                    36.01004,
                    127.01004,
                    node_name="중앙역",
                ),
            ],
        )

        for preference in ("low_transfer", "diverse", "reliable", "challenge"):
            with self.subTest(preference=preference):
                result = SQLiteJourneyPlanner(self.path).plan(
                    origin_node_id="ORIGIN",
                    destination_node_id="SELECTED_DEST",
                    origin_city_code="25",
                    destination_city_code="25",
                    destination_access={
                        "city_code": "25",
                        "node_id": "SELECTED_DEST",
                        "node_name": "중앙역",
                        "latitude": 36.0100,
                        "longitude": 127.0100,
                    },
                    preference=preference,
                    alternatives=1,
                )

                candidate = result["alternatives"][0]
                self.assertEqual(
                    candidate["route_ids"],
                    ["EXACT_FIRST", "EXACT_SECOND"],
                )
                self.assertEqual(candidate["transfers"], 1)
                self.assertEqual(candidate["walking_m"], 0.0)
                self.assertEqual(candidate["endpoint_access"], [])
                self.assertTrue(
                    result["graph"]["endpoint_exact_search_complete"]
                )

    def test_same_name_nearby_destination_is_an_explicit_egress_fallback(self) -> None:
        self.hydrate(
            "GOOD_ROUTE",
            [
                self.stop("ORIGIN", 1, 36.0000, 127.0000),
                self.stop(
                    "DEST_COUNTERPART",
                    2,
                    36.01004,
                    127.01004,
                    node_name="중앙역",
                ),
            ],
        )
        self.hydrate(
            "WRONG_DIRECTION",
            [
                self.stop(
                    "SELECTED_DEST",
                    1,
                    36.0100,
                    127.0100,
                    node_name="중앙역",
                ),
                self.stop("AWAY", 2, 36.0200, 127.0200),
            ],
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="ORIGIN",
            destination_node_id="SELECTED_DEST",
            origin_city_code="25",
            destination_city_code="25",
            destination_access={
                "city_code": "25",
                "node_id": "SELECTED_DEST",
                "node_name": "중앙역",
                "latitude": 36.0100,
                "longitude": 127.0100,
            },
            preference="low_transfer",
        )

        candidate = result["alternatives"][0]
        self.assertEqual(candidate["route_ids"], ["GOOD_ROUTE"])
        walk = [step for step in candidate["steps"] if step["kind"] == "walk"]
        self.assertEqual(len(walk), 1)
        self.assertEqual(walk[0]["access_kind"], "egress")
        self.assertEqual(walk[0]["from"]["node_id"], "DEST_COUNTERPART")
        self.assertEqual(walk[0]["to"]["node_id"], "SELECTED_DEST")
        self.assertEqual(
            walk[0]["evidence"]["type"],
            "same_name_nearby_stop_access",
        )
        self.assertLess(walk[0]["distance_m"], 10)

    def test_same_name_nearby_origin_is_an_explicit_access_fallback(self) -> None:
        self.hydrate(
            "GOOD_ROUTE",
            [
                self.stop(
                    "ORIGIN_COUNTERPART",
                    1,
                    36.00004,
                    127.00004,
                    node_name="시청앞",
                ),
                self.stop("DESTINATION", 2, 36.0100, 127.0100),
            ],
        )
        self.hydrate(
            "WRONG_DIRECTION",
            [
                self.stop(
                    "SELECTED_ORIGIN",
                    1,
                    36.0000,
                    127.0000,
                    node_name="시청앞",
                ),
                self.stop("AWAY", 2, 35.9900, 126.9900),
            ],
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="SELECTED_ORIGIN",
            destination_node_id="DESTINATION",
            origin_city_code="25",
            destination_city_code="25",
            origin_access={
                "city_code": "25",
                "node_id": "SELECTED_ORIGIN",
                "node_name": "시청앞",
                "latitude": 36.0000,
                "longitude": 127.0000,
            },
            preference="low_transfer",
        )

        candidate = result["alternatives"][0]
        self.assertEqual(candidate["route_ids"], ["GOOD_ROUTE"])
        walk = [step for step in candidate["steps"] if step["kind"] == "walk"]
        self.assertEqual(len(walk), 1)
        self.assertEqual(walk[0]["access_kind"], "access")
        self.assertEqual(walk[0]["from"]["node_id"], "SELECTED_ORIGIN")
        self.assertEqual(walk[0]["to"]["node_id"], "ORIGIN_COUNTERPART")
        self.assertEqual(
            walk[0]["evidence"]["type"],
            "same_name_nearby_stop_access",
        )

    def test_endpoint_counterpart_rejects_far_and_cross_city_homonyms(self) -> None:
        self.hydrate(
            "ORIGIN_ROUTE",
            [
                self.stop("ORIGIN", 1, 36.0000, 127.0000),
                self.stop("LOCAL_END", 2, 36.0010, 127.0010),
            ],
        )
        self.hydrate(
            "SELECTED_ROUTE",
            [
                self.stop(
                    "SELECTED_DEST",
                    1,
                    36.0100,
                    127.0100,
                    node_name="터미널",
                ),
                self.stop("AWAY", 2, 36.0200, 127.0200),
            ],
        )
        self.hydrate(
            "FAR_HOMONYM",
            [
                self.stop("FAR_START", 1, 35.9000, 126.9000),
                self.stop(
                    "FAR_DEST",
                    2,
                    36.0200,
                    127.0100,
                    node_name="터미널",
                ),
            ],
        )
        self.hydrate(
            "CROSS_CITY_HOMONYM",
            [
                self.stop("CROSS_START", 1, 35.8000, 126.8000),
                self.stop(
                    "CROSS_DEST",
                    2,
                    36.01004,
                    127.01004,
                    node_name="터미널",
                ),
            ],
            city_code="26",
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="ORIGIN",
            destination_node_id="SELECTED_DEST",
            origin_city_code="25",
            destination_city_code="25",
            destination_access={
                "city_code": "25",
                "node_id": "SELECTED_DEST",
                "node_name": "터미널",
                "latitude": 36.0100,
                "longitude": 127.0100,
            },
            preference="low_transfer",
        )

        self.assertEqual(result["alternatives"], [])
        self.assertEqual(result["reason"], "NO_DIRECTED_PATH_IN_SQLITE_GRAPH")

    def test_query_and_expansion_limits_raise_instead_of_truncating(self) -> None:
        self.hydrate_two_route_fixture()
        with self.assertRaisesRegex(PlannerLimitError, "exceeds 1 queries"):
            SQLiteJourneyPlanner(self.path, max_queries=1).plan(
                origin_node_id="ORIGIN",
                destination_node_id="DESTINATION",
            )

    def test_returns_distinct_alternatives_and_preference_changes_ranking(self) -> None:
        self.hydrate(
            "DIRECT",
            [
                self.stop("O", 1, 36.000, 127.000),
                self.stop("D", 10, 36.040, 127.040),
            ],
        )
        self.hydrate(
            "A1",
            [
                self.stop("O", 1, 36.000, 127.000),
                self.stop("X", 2, 36.010, 127.010),
            ],
        )
        self.hydrate(
            "A2",
            [
                self.stop("X", 1, 36.010, 127.010),
                self.stop("D", 2, 36.040, 127.040),
            ],
        )
        self.hydrate(
            "B1",
            [
                self.stop("O", 1, 36.000, 127.000),
                self.stop("Y", 2, 36.025, 127.000),
            ],
        )
        self.hydrate(
            "B2",
            [
                self.stop("Y", 1, 36.025, 127.000),
                self.stop("D", 2, 36.040, 127.040),
            ],
        )
        planner = SQLiteJourneyPlanner(self.path)

        low_transfer = planner.plan(
            origin_node_id="O",
            destination_node_id="D",
            alternatives=3,
            preference="low_transfer",
        )
        diverse = planner.plan(
            origin_node_id="O",
            destination_node_id="D",
            alternatives=3,
            preference="diverse",
        )
        challenge = planner.plan(
            origin_node_id="O",
            destination_node_id="D",
            alternatives=3,
            preference="challenge",
        )

        low_paths = [tuple(item["route_ids"]) for item in low_transfer["alternatives"]]
        self.assertEqual(low_paths[0], ("DIRECT",))
        self.assertEqual(len(low_paths), 3)
        self.assertEqual(len(set(low_paths)), 3)
        self.assertEqual(len(diverse["alternatives"]), 3)
        self.assertEqual(len(challenge["alternatives"][0]["route_ids"]), 2)
        self.assertEqual(
            diverse["graph"]["alternative_algorithm"],
            "budgeted_route_signature_dijkstra",
        )
        self.assertEqual(diverse["graph"]["alternatives_requested"], 3)
        self.assertEqual(diverse["graph"]["alternatives_returned"], 3)
        self.assertFalse(diverse["graph"]["alternatives_truncated"])

    def test_result_cache_is_revision_scoped_bounded_and_mutation_safe(self) -> None:
        self.hydrate_two_route_fixture()
        planner = SQLiteJourneyPlanner(self.path, result_cache_entries=1)
        request = {
            "origin_node_id": "ORIGIN",
            "destination_node_id": "DESTINATION",
        }

        first = planner.plan(**request)
        second = planner.plan(**request)
        self.assertEqual(first["graph"]["result_cache"]["status"], "miss")
        self.assertEqual(second["graph"]["result_cache"]["status"], "hit")
        self.assertEqual(second["graph"]["result_cache"]["entries"], 1)

        second["alternatives"][0]["route_ids"].append("MUTATED_BY_CALLER")
        isolated = planner.plan(**request)
        self.assertNotIn(
            "MUTATED_BY_CALLER", isolated["alternatives"][0]["route_ids"]
        )

        self.hydrate(
            "UNRELATED_REVISION_BUMP",
            [
                self.stop("N1", 1, 35.0, 126.0),
                self.stop("N2", 2, 35.1, 126.1),
            ],
        )
        after_revision = planner.plan(**request)
        self.assertEqual(
            after_revision["graph"]["result_cache"]["status"], "miss"
        )

    def test_same_key_fifty_concurrent_misses_compute_once(self) -> None:
        self.hydrate_two_route_fixture()
        planner = SQLiteJourneyPlanner(self.path)
        original = planner._compute_cache_miss
        calls = 0
        calls_lock = threading.Lock()
        start = threading.Barrier(50)

        def counted(**kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return original(**kwargs)

        planner._compute_cache_miss = counted

        def request():
            start.wait(timeout=5)
            return planner.plan(
                origin_node_id="ORIGIN",
                destination_node_id="DESTINATION",
            )

        with ThreadPoolExecutor(max_workers=50) as executor:
            results = list(executor.map(lambda _: request(), range(50)))

        self.assertEqual(calls, 1)
        self.assertTrue(
            all(
                result["alternatives"][0]["route_ids"]
                == ["A_OUTBOUND", "B_OUTBOUND"]
                for result in results
            )
        )
        self.assertEqual(planner._result_flights, {})

    def test_search_limited_results_are_not_retained(self) -> None:
        self.hydrate_two_route_fixture()
        planner = SQLiteJourneyPlanner(self.path)
        calls = 0
        results = [
            {
                "status": "READY",
                "reason": None,
                "graph": {
                    "limit_reason": "WALL_CLOCK_BUDGET",
                    "alternative_search_complete": True,
                },
                "alternatives": [],
            },
            {
                "status": "READY",
                "reason": None,
                "graph": {
                    "limit_reason": None,
                    "alternative_search_complete": False,
                },
                "alternatives": [],
            },
            {
                "status": "DATA_GAP",
                "reason": "SEARCH_BUDGET_REACHED",
                "graph": {
                    "limit_reason": None,
                    "alternative_search_complete": True,
                },
                "alternatives": [],
            },
        ]

        def limited(context, **kwargs):
            nonlocal calls
            calls += 1
            return results[calls - 1]

        planner._search = limited
        request = {
            "origin_node_id": "ORIGIN",
            "destination_node_id": "DESTINATION",
        }
        observed = [planner.plan(**request) for _ in range(3)]

        self.assertEqual(calls, 3)
        self.assertEqual(planner._result_cache, {})
        self.assertEqual(
            [item["graph"]["result_cache"]["status"] for item in observed],
            ["not_stored_non_deterministic"] * 3,
        )

    def test_deterministic_negative_results_use_short_ttl_cache(self) -> None:
        self.hydrate(
            "ONE_WAY",
            [
                self.stop("START", 1, 36.0000, 127.0000),
                self.stop("END", 2, 36.0100, 127.0100),
            ],
        )
        planner = SQLiteJourneyPlanner(self.path)
        requests = (
            (
                {
                    "origin_node_id": "START",
                    "destination_node_id": "MISSING",
                    "origin_city_code": "25",
                    "destination_city_code": "25",
                },
                "STOP_NOT_IN_ACTIVE_SEQUENCE",
            ),
            (
                {
                    "origin_node_id": "START",
                    "destination_node_id": "REMOTE",
                    "origin_city_code": "25",
                    "destination_city_code": "25",
                    "destination_access": {
                        "city_code": "25",
                        "node_id": "REMOTE",
                        "node_name": "REMOTE",
                        "latitude": 37.0000,
                        "longitude": 128.0000,
                    },
                },
                "STOP_NOT_ROUTABLE_NEARBY",
            ),
            (
                {
                    "origin_node_id": "END",
                    "destination_node_id": "START",
                    "origin_city_code": "25",
                    "destination_city_code": "25",
                },
                "NO_DIRECTED_PATH_IN_SQLITE_GRAPH",
            ),
        )

        for request, reason in requests:
            with self.subTest(reason=reason):
                first = planner.plan(**request)
                second = planner.plan(**request)
                self.assertEqual(first["reason"], reason)
                self.assertEqual(
                    first["graph"]["result_cache"]["status"],
                    "miss_short_ttl",
                )
                self.assertEqual(second["reason"], reason)
                self.assertEqual(
                    second["graph"]["result_cache"]["status"],
                    "hit_short_ttl",
                )

    def test_limited_or_incomplete_negative_results_are_never_cached(self) -> None:
        limited_reasons = (
            "WALL_CLOCK_BUDGET",
            "MAX_TRANSFER_LAYERS",
            "STATE_LABEL_BUDGET",
            "ADDITIONAL_ALTERNATIVE_EXPANSION_BUDGET",
            "PRIMARY_ROUTE_FAST_PATH",
        )
        for limit_reason in limited_reasons:
            with self.subTest(limit_reason=limit_reason):
                self.assertEqual(
                    SQLiteJourneyPlanner._result_cache_policy(
                        {
                            "status": "DATA_GAP",
                            "reason": "NO_DIRECTED_PATH_IN_SQLITE_GRAPH",
                            "graph": {
                                "limit_reason": limit_reason,
                                "alternative_search_complete": True,
                            },
                            "alternatives": [],
                        }
                    ),
                    "none",
                )

        self.assertEqual(
            SQLiteJourneyPlanner._result_cache_policy(
                {
                    "status": "READY",
                    "reason": None,
                    "graph": {
                        "limit_reason": "WALL_CLOCK_BUDGET",
                        "alternative_search_complete": False,
                        "endpoint_exact_search_complete": False,
                    },
                    "alternatives": [{"route_ids": ["COUNTERPART_ONLY"]}],
                }
            ),
            "none",
        )

        self.assertEqual(
            SQLiteJourneyPlanner._result_cache_policy(
                {
                    "status": "READY",
                    "reason": None,
                    "graph": {
                        "limit_reason": "ENDPOINT_EXACT_GRACE_BUDGET",
                        "alternative_search_complete": False,
                        "endpoint_exact_search_complete": False,
                    },
                    "alternatives": [{"route_ids": ["COUNTERPART_ONLY"]}],
                }
            ),
            "short_ttl",
        )

        for graph in (
            {"limit_reason": None, "alternative_search_complete": False},
            {"limit_reason": None},
        ):
            with self.subTest(graph=graph):
                self.assertEqual(
                    SQLiteJourneyPlanner._result_cache_policy(
                        {
                            "status": "DATA_GAP",
                            "reason": "NO_DIRECTED_PATH_IN_SQLITE_GRAPH",
                            "graph": graph,
                            "alternatives": [],
                        }
                    ),
                    "none",
                )

    def test_positive_endpoint_grace_fallback_uses_short_ttl_cache(self) -> None:
        self.hydrate(
            "COUNTERPART_DIRECT",
            [
                self.stop("ORIGIN", 1, 36.0000, 127.0000),
                self.stop(
                    "DEST_COUNTERPART",
                    2,
                    36.01004,
                    127.01004,
                    node_name="중앙역",
                ),
            ],
        )
        self.hydrate(
            "EXACT_WRONG_DIRECTION",
            [
                self.stop(
                    "SELECTED_DEST",
                    1,
                    36.0100,
                    127.0100,
                    node_name="중앙역",
                ),
                self.stop("AWAY", 2, 36.0200, 127.0200),
            ],
        )
        planner = SQLiteJourneyPlanner(self.path)
        planner.endpoint_exact_grace_seconds = 0.0
        request = {
            "origin_node_id": "ORIGIN",
            "destination_node_id": "SELECTED_DEST",
            "origin_city_code": "25",
            "destination_city_code": "25",
            "destination_access": {
                "city_code": "25",
                "node_id": "SELECTED_DEST",
                "node_name": "중앙역",
                "latitude": 36.0100,
                "longitude": 127.0100,
            },
            "preference": "low_transfer",
        }

        first = planner.plan(**request)
        second = planner.plan(**request)

        self.assertEqual(
            first["alternatives"][0]["route_ids"],
            ["COUNTERPART_DIRECT"],
        )
        self.assertFalse(first["graph"]["endpoint_exact_search_complete"])
        self.assertEqual(
            first["graph"]["limit_reason"],
            "ENDPOINT_EXACT_GRACE_BUDGET",
        )
        self.assertEqual(
            first["graph"]["result_cache"]["status"],
            "miss_short_ttl",
        )
        self.assertEqual(
            second["graph"]["result_cache"]["status"],
            "hit_short_ttl",
        )

    def test_verified_primary_fast_path_uses_short_ttl_then_expires(self) -> None:
        self.hydrate_two_route_fixture()
        planner = SQLiteJourneyPlanner(self.path)
        now = [100.0]
        planner._monotonic_clock = lambda: now[0]
        calls = 0

        def primary_fast_path(context, **kwargs):
            nonlocal calls
            calls += 1
            return {
                "status": "READY",
                "reason": None,
                "graph": {
                    "limit_reason": "PRIMARY_ROUTE_FAST_PATH",
                    "alternative_search_complete": False,
                    "alternatives_requested": 5,
                    "alternatives_returned": 1,
                },
                "alternatives": [
                    {
                        "route_ids": ["A_OUTBOUND", "B_OUTBOUND"],
                        "steps": [],
                    }
                ],
            }

        planner._search = primary_fast_path
        request = {
            "origin_node_id": "ORIGIN",
            "destination_node_id": "DESTINATION",
            "alternatives": 5,
        }
        first = planner.plan(**request)
        second = planner.plan(**request)

        self.assertEqual(calls, 1)
        self.assertEqual(first["graph"]["result_cache"]["status"], "miss_short_ttl")
        self.assertEqual(second["graph"]["result_cache"]["status"], "hit_short_ttl")

        self.hydrate(
            "SHORT_TTL_REVISION_BUMP",
            [
                self.stop("RB1", 1, 35.0, 126.0),
                self.stop("RB2", 2, 35.1, 126.1),
            ],
        )
        after_revision = planner.plan(**request)
        self.assertEqual(calls, 2)
        self.assertEqual(
            after_revision["graph"]["result_cache"]["status"],
            "miss_short_ttl",
        )

        now[0] += planner.short_result_ttl_seconds + 0.001
        after_expiry = planner.plan(**request)
        self.assertEqual(calls, 3)
        self.assertEqual(
            after_expiry["graph"]["result_cache"]["status"],
            "miss_short_ttl",
        )
        self.assertEqual(len(planner._result_cache), 1)

    def test_singleflight_exception_reaches_all_waiters_and_cleans_up(self) -> None:
        self.hydrate_two_route_fixture()
        planner = SQLiteJourneyPlanner(self.path)
        calls = 0
        calls_lock = threading.Lock()
        start = threading.Barrier(8)

        def explode(**kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with planner._result_flights_lock:
                    waiters = sum(
                        flight.waiters
                        for flight in planner._result_flights.values()
                    )
                if waiters >= 7:
                    break
                time.sleep(0.005)
            raise PlannerLimitError("singleflight test failure")

        planner._compute_cache_miss = explode

        def request():
            start.wait(timeout=5)
            try:
                planner.plan(
                    origin_node_id="ORIGIN",
                    destination_node_id="DESTINATION",
                )
            except PlannerLimitError as exc:
                return str(exc)
            return "NO_ERROR"

        with ThreadPoolExecutor(max_workers=8) as executor:
            errors = list(executor.map(lambda _: request(), range(8)))

        self.assertEqual(calls, 1)
        self.assertEqual(errors, ["singleflight test failure"] * 8)
        self.assertEqual(planner._result_flights, {})

    def test_singleflight_wait_timeout_does_not_cancel_or_leak_leader(self) -> None:
        self.hydrate_two_route_fixture()
        planner = SQLiteJourneyPlanner(self.path)
        planner._singleflight_wait_seconds = 0.02
        original = planner._compute_cache_miss
        entered = threading.Event()
        release = threading.Event()

        def blocked(**kwargs):
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("test leader was not released")
            return original(**kwargs)

        planner._compute_cache_miss = blocked
        request = {
            "origin_node_id": "ORIGIN",
            "destination_node_id": "DESTINATION",
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            leader = executor.submit(planner.plan, **request)
            self.assertTrue(entered.wait(timeout=1))
            with self.assertRaises(PlannerBusyError):
                planner.plan(**request)
            release.set()
            self.assertEqual(leader.result(timeout=2)["status"], "READY")

        self.assertEqual(planner._result_flights, {})

    def test_singleflight_keeps_different_keys_parallel(self) -> None:
        self.hydrate_two_route_fixture()
        planner = SQLiteJourneyPlanner(self.path)
        original = planner._compute_cache_miss
        rendezvous = threading.Barrier(2)
        active = 0
        maximum_active = 0
        active_lock = threading.Lock()

        def concurrent(**kwargs):
            nonlocal active, maximum_active
            with active_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                rendezvous.wait(timeout=2)
                return original(**kwargs)
            finally:
                with active_lock:
                    active -= 1

        planner._compute_cache_miss = concurrent
        with ThreadPoolExecutor(max_workers=2) as executor:
            direct = executor.submit(
                planner.plan,
                origin_node_id="ORIGIN",
                destination_node_id="TRANSFER",
            )
            transfer = executor.submit(
                planner.plan,
                origin_node_id="ORIGIN",
                destination_node_id="DESTINATION",
            )
            self.assertEqual(direct.result(timeout=3)["status"], "READY")
            self.assertEqual(transfer.result(timeout=3)["status"], "READY")

        self.assertEqual(maximum_active, 2)
        self.assertEqual(planner._result_flights, {})

    def test_alternative_search_stops_at_explicit_post_primary_budget(self) -> None:
        self.hydrate(
            "DIRECT",
            [
                self.stop("O", 1, 36.000, 127.000),
                self.stop("D", 2, 36.010, 127.010),
            ],
        )
        self.hydrate(
            "DETOUR_1",
            [
                self.stop("O", 1, 36.000, 127.000),
                self.stop("X", 2, 36.005, 127.005),
            ],
        )
        self.hydrate(
            "DETOUR_2",
            [
                self.stop("X", 1, 36.005, 127.005),
                self.stop("D", 2, 36.010, 127.010),
            ],
        )
        result = SQLiteJourneyPlanner(
            self.path,
            additional_alternative_expansions=1,
        ).plan(
            origin_node_id="O",
            destination_node_id="D",
            alternatives=5,
            preference="diverse",
        )

        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["alternatives"][0]["route_ids"], ["DIRECT"])
        self.assertTrue(result["graph"]["alternatives_truncated"])
        self.assertEqual(
            result["graph"]["limit_reason"],
            "ADDITIONAL_ALTERNATIVE_EXPANSION_BUDGET",
        )

    def test_nonempty_direction_change_is_a_hard_ride_boundary(self) -> None:
        self.hydrate(
            "MIXED",
            [
                self.stop("A", 1, 36.00, 127.00, direction="0"),
                self.stop("B", 2, 36.01, 127.01, direction="0"),
                self.stop("C", 3, 36.02, 127.02, direction="1"),
                self.stop("D", 4, 36.03, 127.03, direction="1"),
            ],
        )
        planner = SQLiteJourneyPlanner(self.path)

        blocked = planner.plan(origin_node_id="A", destination_node_id="D")
        first_segment = planner.plan(origin_node_id="A", destination_node_id="B")
        second_segment = planner.plan(origin_node_id="C", destination_node_id="D")

        self.assertEqual(blocked["alternatives"], [])
        self.assertEqual(blocked["reason"], "NO_DIRECTED_PATH_IN_SQLITE_GRAPH")
        self.assertEqual(first_segment["status"], "READY")
        self.assertEqual(second_segment["status"], "READY")

    def test_blank_direction_and_long_same_direction_hop_remain_traversable(self) -> None:
        self.hydrate(
            "BLANK_BRIDGE",
            [
                self.stop("A", 1, 36.00, 127.00, direction="0"),
                self.stop("B", 2, 36.01, 127.01, direction=""),
                self.stop("C", 3, 36.02, 127.02, direction="1"),
            ],
        )
        self.hydrate(
            "LONG_SAME",
            [
                self.stop("L1", 1, 35.00, 127.00, direction="2"),
                self.stop("L2", 2, 35.23, 127.00, direction="2"),
            ],
        )
        planner = SQLiteJourneyPlanner(self.path)

        self.assertEqual(
            planner.plan(origin_node_id="A", destination_node_id="C")["status"],
            "READY",
        )
        self.assertEqual(
            planner.plan(origin_node_id="L1", destination_node_id="L2")["status"],
            "READY",
        )

    def test_unknown_nonempty_direction_change_also_splits(self) -> None:
        self.hydrate(
            "UNKNOWN_DIRECTION",
            [
                self.stop("U1", 1, 36.00, 127.00, direction="1"),
                self.stop("U2", 2, 36.01, 127.01, direction="2"),
            ],
        )
        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="U1", destination_node_id="U2"
        )
        self.assertEqual(result["alternatives"], [])

    def test_parallel_capacity_exhaustion_is_a_distinct_busy_error(self) -> None:
        self.hydrate_two_route_fixture()
        planner = SQLiteJourneyPlanner(
            self.path,
            max_parallel_searches=1,
            admission_timeout_seconds=0.01,
        )
        self.assertTrue(planner._search_slots.acquire(timeout=0.01))
        try:
            with self.assertRaises(PlannerBusyError):
                planner.plan(
                    origin_node_id="ORIGIN",
                    destination_node_id="DESTINATION",
                )
        finally:
            planner._search_slots.release()
        with self.assertRaisesRegex(PlannerLimitError, "exceeds 1 route-state expansions"):
            SQLiteJourneyPlanner(self.path, max_expansions=1).plan(
                origin_node_id="ORIGIN",
                destination_node_id="DESTINATION",
            )

    def test_hot_cache_hit_bypasses_saturated_search_capacity(self) -> None:
        self.hydrate_two_route_fixture()
        planner = SQLiteJourneyPlanner(
            self.path,
            max_parallel_searches=1,
            admission_timeout_seconds=0.01,
        )
        request = {
            "origin_node_id": "ORIGIN",
            "destination_node_id": "DESTINATION",
        }
        planner.plan(**request)
        self.assertTrue(planner._search_slots.acquire(timeout=0.01))
        try:
            cached = planner.plan(**request)
        finally:
            planner._search_slots.release()

        self.assertEqual(cached["graph"]["result_cache"]["status"], "hit")

    def test_primary_allows_same_route_after_direction_split(self) -> None:
        # A's first and second direction segments cannot be ridden through,
        # but leaving A and later boarding its second segment is a valid path.
        self.hydrate(
            "A",
            [
                self.stop("O", 1, 36.000, 127.000, direction="0"),
                self.stop("J", 2, 36.010, 127.010, direction="0"),
                self.stop("K", 3, 36.020, 127.020, direction="1"),
                self.stop("D", 4, 36.030, 127.030, direction="1"),
            ],
        )
        self.hydrate(
            "B",
            [
                self.stop("O", 1, 36.000, 127.000),
                self.stop("J", 2, 36.010, 127.010),
            ],
        )
        self.hydrate(
            "C",
            [
                self.stop("J", 1, 36.010, 127.010),
                self.stop("K", 2, 36.020, 127.020),
            ],
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="O",
            destination_node_id="D",
            preference="low_transfer",
        )

        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["alternatives"][0]["route_ids"], ["A", "C", "A"])

    def test_direction_split_chain_b_c_a_remains_routable(self) -> None:
        self.hydrate(
            "A",
            [
                self.stop("P", 1, 35.990, 126.990, direction="0"),
                self.stop("J", 2, 36.010, 127.010, direction="0"),
                self.stop("K", 3, 36.020, 127.020, direction="1"),
                self.stop("D", 4, 36.030, 127.030, direction="1"),
            ],
        )
        self.hydrate(
            "B",
            [
                self.stop("O", 1, 36.000, 127.000),
                self.stop("J", 2, 36.010, 127.010),
            ],
        )
        self.hydrate(
            "C",
            [
                self.stop("J", 1, 36.010, 127.010),
                self.stop("K", 2, 36.020, 127.020),
            ],
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="O",
            destination_node_id="D",
            preference="low_transfer",
        )

        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["alternatives"][0]["route_ids"], ["B", "C", "A"])

    def test_converging_route_histories_keep_distinct_alternatives(self) -> None:
        for route_id in ("R1", "R2"):
            self.hydrate(
                route_id,
                [
                    self.stop("O", 1, 36.000, 127.000),
                    self.stop("J", 2, 36.010, 127.010),
                ],
            )
        self.hydrate(
            "C",
            [
                self.stop("J", 1, 36.010, 127.010),
                self.stop("D", 2, 36.020, 127.020),
            ],
        )

        result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="O",
            destination_node_id="D",
            alternatives=2,
            preference="diverse",
        )
        signatures = {
            tuple(candidate["route_ids"])
            for candidate in result["alternatives"]
        }

        self.assertEqual(signatures, {("R1", "C"), ("R2", "C")})
        self.assertFalse(result["graph"]["alternatives_truncated"])

    def test_default_transfer_layer_bound_supports_fourteen_route_chain(self) -> None:
        route_ids = [f"CHAIN_{index:02d}" for index in range(14)]
        for index, route_id in enumerate(route_ids):
            self.hydrate(
                route_id,
                [
                    self.stop(
                        f"N{index}",
                        1,
                        36.000 + index * 0.010,
                        127.000,
                    ),
                    self.stop(
                        f"N{index + 1}",
                        2,
                        36.010 + index * 0.010,
                        127.000,
                    ),
                ],
            )

        bounded_out = SQLiteJourneyPlanner(
            self.path,
            max_transfer_layers=12,
        ).plan(
            origin_node_id="N0",
            destination_node_id="N14",
            preference="low_transfer",
        )
        default_result = SQLiteJourneyPlanner(self.path).plan(
            origin_node_id="N0",
            destination_node_id="N14",
            preference="low_transfer",
        )

        self.assertEqual(bounded_out["alternatives"], [])
        self.assertEqual(bounded_out["graph"]["limit_reason"], "MAX_TRANSFER_LAYERS")
        self.assertEqual(default_result["status"], "READY")
        self.assertEqual(default_result["alternatives"][0]["route_ids"], route_ids)
        self.assertEqual(default_result["graph"]["max_transfer_layers"], 32)

    def test_sql_progress_handler_interrupts_work_past_wall_deadline(self) -> None:
        planner = SQLiteJourneyPlanner(self.path)
        with closing(planner._connect()) as connection:
            context = _SearchContext(
                connection=connection,
                max_queries=planner.max_queries,
                max_expansions=planner.max_expansions,
                max_rows_per_lookup=planner.max_rows_per_lookup,
                max_stops_per_route=planner.max_stops_per_route,
                route_cache_entries=planner.route_cache_entries,
                transfer_cache_entries=planner.transfer_cache_entries,
                max_parallel_searches=planner.max_parallel_searches,
            )
            context.arm_sql_deadline(time.monotonic() - 1.0)
            started = time.monotonic()
            with self.assertRaisesRegex(PlannerLimitError, "wall-clock SQL deadline"):
                cursor = context.execute(
                    """
                    WITH RECURSIVE counter(value) AS (
                        VALUES(0)
                        UNION ALL
                        SELECT value + 1 FROM counter WHERE value < 100000000
                    )
                    SELECT value FROM counter
                    """
                )
                planner._bounded_rows(
                    context,
                    cursor,
                    limit=100000001,
                    label="deadline regression query",
                )
            self.assertLess(time.monotonic() - started, 1.0)

    def test_required_index_ddl_is_documentation_only(self) -> None:
        before = self._index_names()
        statements = required_index_ddl()
        after = self._index_names()

        self.assertEqual(before, after)
        self.assertEqual(len(statements), 3)
        self.assertTrue(all(statement.startswith("CREATE INDEX IF NOT EXISTS") for statement in statements))
        self.assertTrue(any("node_lookup" in statement for statement in statements))
        self.assertTrue(any("coordinate_lookup" in statement for statement in statements))

    def _index_names(self) -> set[str]:
        connection = sqlite3.connect(self.path)
        try:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
