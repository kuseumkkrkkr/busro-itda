from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from network_catalog import CatalogValidationError, NetworkCatalog  # noqa: E402
from tago import TagoError  # noqa: E402
from topology_ingest import IngestConfig, TopologyIngestor  # noqa: E402


FIXED_NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


def response(items, *, total=None, page=1, size=100):
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"},
            "body": {
                "items": {"item": items},
                "pageNo": page,
                "numOfRows": size,
                "totalCount": len(items) if total is None else total,
            },
        }
    }


class FakeTago:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, operation: str, parameters: dict[str, str]):
        self.calls.append((operation, dict(parameters)))
        if operation == "cities":
            return response([{"citycode": "25", "cityname": "대전광역시"}])
        if operation == "routes":
            return response(
                [{"citycode": "25", "routeid": "DJB_ROUTE_1", "routeno": "101"}],
                total=1,
                page=int(parameters["pageNo"]),
                size=int(parameters["numOfRows"]),
            )
        if operation == "route_stops":
            page = int(parameters["pageNo"])
            all_items = [
                {"citycode": "25", "routeid": "DJB_ROUTE_1", "nodeid": "DJB_A", "nodenm": "기점", "nodeord": 1, "gpslati": 36.30, "gpslong": 127.30},
                {"citycode": "25", "routeid": "DJB_ROUTE_1", "nodeid": "DJB_B", "nodenm": "중간", "nodeord": 2, "gpslati": 36.31, "gpslong": 127.31},
                {"citycode": "25", "routeid": "DJB_ROUTE_1", "nodeid": "DJB_C", "nodenm": "종점", "nodeord": 3, "gpslati": 36.32, "gpslong": 127.32},
            ]
            size = int(parameters["numOfRows"])
            start = (page - 1) * size
            return response(all_items[start : start + size], total=3, page=page, size=size)
        raise AssertionError(operation)


class TopologyIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = NetworkCatalog(
            Path(self.temp.name) / "catalog.sqlite3", clock=lambda: FIXED_NOW
        )

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **changes):
        values = {
            "request_budget": 20,
            "requests_per_second": 0,
            "page_size": 2,
            "max_route_pages": 3,
            "max_discovery_pages": 3,
        }
        values.update(changes)
        return IngestConfig(**values)

    def ingestor(self, fake, **changes):
        return TopologyIngestor(
            catalog=self.catalog,
            fetcher=fake,
            config=self.config(**changes),
            clock=lambda: FIXED_NOW,
            monotonic=lambda: 0.0,
            sleeper=lambda _: None,
        )

    def test_discovers_tago_native_ids_and_hydrates_all_pages(self):
        fake = FakeTago()
        result = self.ingestor(fake).run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["coverage"]["targets"], 1)
        self.assertEqual(result["coverage"]["complete"], 1)
        sequence = self.catalog.planning_snapshot().route_sequences[0]
        self.assertEqual(sequence.route_id, "DJB_ROUTE_1")
        self.assertEqual([stop.node_id for stop in sequence.stops], ["DJB_A", "DJB_B", "DJB_C"])
        self.assertEqual([call[0] for call in fake.calls], ["cities", "routes", "route_stops", "route_stops"])

    def test_budget_checkpoint_resumes_at_next_unfetched_page(self):
        first = FakeTago()
        first_result = self.ingestor(first, request_budget=3).run()
        self.assertEqual(first_result["run"]["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(first_result["coverage"]["statuses"], {"DEFERRED": 1})
        # Re-open the SQLite catalog to model a new CLI process after exit.
        self.catalog = NetworkCatalog(
            Path(self.temp.name) / "catalog.sqlite3", clock=lambda: FIXED_NOW
        )
        second = FakeTago()
        second_result = self.ingestor(second, request_budget=3).run()
        self.assertEqual(second_result["run"]["status"], "COMPLETE")
        route_pages = [params["pageNo"] for operation, params in second.calls if operation == "route_stops"]
        self.assertEqual(route_pages, ["2"])
        self.assertEqual(second_result["coverage"]["complete"], 1)

    def test_explicit_refresh_skips_unchanged_sequence_version(self):
        self.ingestor(FakeTago()).run()
        before = self.catalog.active_route_sequence_info(city_code="25", route_id="DJB_ROUTE_1")
        refreshed = self.ingestor(FakeTago(), refresh_complete=True).run()
        after = self.catalog.active_route_sequence_info(city_code="25", route_id="DJB_ROUTE_1")
        self.assertEqual(before["sequence_id"], after["sequence_id"])
        self.assertEqual(refreshed["run"]["unchanged"], 1)
        self.assertEqual(len(self.catalog.planning_snapshot().route_sequences), 1)

    def test_code_30_stops_discovery_as_truthful_data_gap(self):
        def denied(operation, parameters):
            raise TagoError("30", "SERVICE KEY IS NOT REGISTERED ERROR")

        result = self.ingestor(denied).run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["run"]["status"], "DATA_GAP")
        self.assertIn("authorization", result["notice"])
        self.assertEqual(result["coverage"]["targets"], 0)

    def test_one_city_service_error_is_recorded_and_other_cities_continue(self):
        def partial(operation, parameters):
            if operation == "cities":
                return response(
                    [
                        {"citycode": "25", "cityname": "오류 지역"},
                        {"citycode": "26", "cityname": "정상 지역"},
                    ]
                )
            if operation == "routes" and parameters["cityCode"] == "25":
                raise TagoError("99", "UPSTREAM CITY ERROR")
            if operation == "routes":
                return response(
                    [{"citycode": "26", "routeid": "OK_ROUTE", "routeno": "1"}],
                    total=1,
                    page=1,
                    size=2,
                )
            if operation == "route_stops":
                return response(
                    [
                        {"citycode": "26", "routeid": "OK_ROUTE", "nodeid": "A", "nodenm": "A", "nodeord": 1},
                        {"citycode": "26", "routeid": "OK_ROUTE", "nodeid": "B", "nodenm": "B", "nodeord": 2},
                    ],
                    total=2,
                    page=1,
                    size=2,
                )
            raise AssertionError(operation)

        result = self.ingestor(partial).run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["run"]["status"], "PARTIAL")
        self.assertEqual(result["discovery_failures"], 1)
        self.assertEqual(result["coverage"]["complete"], 1)
        self.assertEqual(
            self.catalog.planning_snapshot().route_sequences[0].route_id,
            "OK_ROUTE",
        )
        with self.catalog.connect() as connection:
            failed = connection.execute(
                "SELECT error_code FROM topology_discovery_progress WHERE provider='TAGO' AND scope_key='routes:25'"
            ).fetchone()
        self.assertEqual(failed["error_code"], "99")

    def test_hangul_route_ids_survive_and_bad_city_data_does_not_block_20_targets(self):
        route_ids = ["GMB수점10"] + [f"ROUTE_{index:02d}" for index in range(19)]

        def partial_discovery(operation, parameters):
            if operation == "cities":
                return response(
                    [
                        {"citycode": "37050", "cityname": "정상 지역"},
                        {"citycode": "38010", "cityname": "오류 지역"},
                    ]
                )
            if operation == "routes" and parameters["cityCode"] == "37050":
                return response(
                    [
                        {
                            "citycode": "37050",
                            "routeid": route_id,
                            "routeno": str(index + 1),
                        }
                        for index, route_id in enumerate(route_ids)
                    ],
                    total=20,
                    page=1,
                    size=100,
                )
            if operation == "routes":
                return response(
                    [
                        {
                            "citycode": "38010",
                            "routeid": "INVALID/ROUTE",
                            "routeno": "1",
                        }
                    ],
                    total=1,
                    page=1,
                    size=100,
                )
            if operation == "route_stops":
                route_id = parameters["routeId"]
                prefix = "HANG" if route_id == "GMB수점10" else route_id[-2:]
                return response(
                    [
                        {
                            "citycode": "37050",
                            "routeid": route_id,
                            "nodeid": f"{prefix}A",
                            "nodenm": "A",
                            "nodeord": 1,
                        },
                        {
                            "citycode": "37050",
                            "routeid": route_id,
                            "nodeid": f"{prefix}B",
                            "nodenm": "B",
                            "nodeord": 2,
                        },
                    ],
                    total=2,
                    page=1,
                    size=100,
                )
            raise AssertionError(operation)

        result = self.ingestor(
            partial_discovery,
            request_budget=50,
            page_size=100,
            target_limit=20,
        ).run()

        self.assertTrue(result["ok"])
        self.assertEqual(result["run"]["status"], "PARTIAL")
        self.assertEqual(result["run"]["targets_processed"], 20)
        self.assertEqual(result["run"]["succeeded"], 20)
        self.assertEqual(result["coverage"]["targets"], 20)
        self.assertEqual(result["coverage"]["complete"], 20)
        self.assertEqual(result["discovery_failures"], 1)
        self.assertIn(
            "GMB수점10",
            {
                sequence.route_id
                for sequence in self.catalog.planning_snapshot().route_sequences
            },
        )
        with self.catalog.connect() as connection:
            failed = connection.execute(
                "SELECT status,error_code FROM topology_discovery_progress "
                "WHERE provider='TAGO' AND scope_key='routes:38010'"
            ).fetchone()
        self.assertEqual(dict(failed), {
            "status": "FAILED",
            "error_code": "ROUTE_DISCOVERY_DATA_GAP",
        })

    def test_static_catalog_identifiers_require_explicit_provider_verification(self):
        with self.assertRaises(CatalogValidationError):
            self.catalog.seed_topology_targets_from_catalog(provider="TAGO")


if __name__ == "__main__":
    unittest.main()
