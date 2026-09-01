from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stderr
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from network_catalog import CatalogValidationError, NetworkCatalog  # noqa: E402
from tago import TagoError, normalize_catalog  # noqa: E402
from topology_ingest import (  # noqa: E402
    MAX_LOCAL_API_RESPONSE_BYTES,
    IngestConfig,
    LocalLiveApiFetcher,
    TopologyProcessLocked,
    TopologyIngestor,
    run_fill_loop,
    _catalog_process_lock,
    _local_live_api_origin,
    _only_route_target,
    _parser,
)


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


class FakeHttpResponse:
    def __init__(self, payload, *, status=200, content_length=True):
        self.status = status
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.headers = {}
        if content_length:
            self.headers["Content-Length"] = str(len(self.body))
        self.read_called = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit):
        self.read_called = True
        return self.body[:limit]


class FakeLocalApi:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def __call__(self, request, *, timeout):
        parsed = urlsplit(request.full_url)
        self.calls.append((request, timeout))
        return FakeHttpResponse(self.payloads[parsed.path])


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

    def seed_targets(self, count):
        self.catalog.upsert_topology_targets(
            provider="TAGO",
            routes=[
                {
                    "city_code": "25",
                    "route_id": f"ROUTE_{index:03d}",
                    "route_no": str(index),
                }
                for index in range(count)
            ],
            discovery_source="TEST_VERIFIED_TAGO_IDS",
        )

    @staticmethod
    def one_page(parameters):
        route_id = parameters["routeId"]
        suffix = route_id.rsplit("_", 1)[-1]
        return response(
            [
                {
                    "citycode": "25",
                    "routeid": route_id,
                    "nodeid": f"N{suffix}A",
                    "nodenm": "A",
                    "nodeord": 1,
                },
                {
                    "citycode": "25",
                    "routeid": route_id,
                    "nodeid": f"N{suffix}B",
                    "nodenm": "B",
                    "nodeord": 2,
                },
            ],
            total=2,
            page=1,
            size=2,
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

    def test_only_routes_discover_all_targets_but_hydrate_exact_selection(self):
        calls = []
        routes = [
            {"citycode": "25", "routeid": f"ROUTE_{index:03d}", "routeno": str(index)}
            for index in range(3)
        ]

        def fetch(operation, parameters):
            calls.append((operation, dict(parameters)))
            if operation == "cities":
                return response([{"citycode": "25", "cityname": "대전광역시"}])
            if operation == "routes":
                page = int(parameters["pageNo"])
                size = int(parameters["numOfRows"])
                start = (page - 1) * size
                return response(
                    routes[start : start + size], total=len(routes), page=page, size=size
                )
            if operation == "route_stops":
                return self.one_page(parameters)
            raise AssertionError(operation)

        result = self.ingestor(
            fetch,
            only_routes=(("25", "ROUTE_001"),),
        ).run()

        hydrated = [
            parameters["routeId"]
            for operation, parameters in calls
            if operation == "route_stops"
        ]
        self.assertEqual(hydrated, ["ROUTE_001"])
        self.assertEqual(result["run"]["status"], "COMPLETE")
        self.assertEqual(result["run"]["succeeded"], 1)
        self.assertEqual(result["coverage"]["targets"], 3)
        self.assertEqual(result["coverage"]["statuses"], {"COMPLETE": 1, "PENDING": 2})

        calls.clear()
        parallel = self.ingestor(
            fetch,
            only_routes=(("25", "ROUTE_000"), ("25", "ROUTE_002")),
            workers=2,
        ).run()
        parallel_routes = [
            parameters["routeId"]
            for operation, parameters in calls
            if operation == "route_stops"
        ]
        self.assertCountEqual(parallel_routes, ["ROUTE_000", "ROUTE_002"])
        self.assertEqual(parallel["run"]["status"], "COMPLETE")
        self.assertEqual(parallel["run"]["succeeded"], 2)
        self.assertEqual(parallel["coverage"]["statuses"], {"COMPLETE": 3})

        calls.clear()
        refreshed = self.ingestor(
            fetch,
            only_routes=(("25", "ROUTE_001"),),
            refresh_complete=True,
        ).run()
        refreshed_routes = [
            parameters["routeId"]
            for operation, parameters in calls
            if operation == "route_stops"
        ]
        self.assertEqual(refreshed_routes, ["ROUTE_001"])
        self.assertEqual(refreshed["run"]["status"], "COMPLETE")
        self.assertEqual(refreshed["run"]["unchanged"], 1)
        self.assertEqual(
            refreshed["coverage"]["statuses"], {"COMPLETE": 2, "UNCHANGED": 1}
        )

    def test_only_routes_missing_target_fails_explicitly_without_fallback_claim(self):
        fake = FakeTago()
        result = self.ingestor(
            fake,
            only_routes=(("25", "MISSING_ROUTE"),),
        ).run()

        self.assertFalse(result["ok"])
        self.assertEqual(result["run"]["status"], "FAILED")
        self.assertIn("requested topology target does not exist", result["error"])
        self.assertIn("25:MISSING_ROUTE", result["error"])
        self.assertNotIn("route_stops", [operation for operation, _ in fake.calls])
        self.assertEqual(result["coverage"]["statuses"], {"PENDING": 1})

    def test_specific_claim_validates_run_ownership_and_retry_limit(self):
        self.seed_targets(2)
        self.catalog.create_topology_run(
            run_id="run_owner",
            provider="TAGO",
            target_source="tago",
            request_budget=10,
            target_limit=None,
        )
        claimed = self.catalog.claim_specific_topology_target(
            provider="TAGO",
            run_id="run_owner",
            city_code="25",
            route_id="ROUTE_001",
        )
        self.assertEqual(claimed["route_id"], "ROUTE_001")
        with self.assertRaisesRegex(CatalogValidationError, "already owned"):
            self.catalog.claim_specific_topology_target(
                provider="TAGO",
                run_id="run_owner",
                city_code="25",
                route_id="ROUTE_001",
            )
        with self.assertRaisesRegex(CatalogValidationError, "does not exist"):
            self.catalog.claim_specific_topology_target(
                provider="TAGO",
                run_id="run_owner",
                city_code="25",
                route_id="MISSING_ROUTE",
            )
        self.catalog.stage_topology_page(
            provider="TAGO",
            city_code="25",
            route_id="ROUTE_001",
            page_no=1,
            items=[],
            total_count=0,
        )
        self.catalog.defer_or_fail_topology_target(
            provider="TAGO",
            city_code="25",
            route_id="ROUTE_001",
            deferred=False,
            error_code="TEST_FAILURE",
            error_message="test failure",
        )
        self.catalog.finish_topology_run("run_owner", "PARTIAL")

        for index in range(2):
            run_id = f"run_retry_{index}"
            self.catalog.create_topology_run(
                run_id=run_id,
                provider="TAGO",
                target_source="tago",
                request_budget=10,
                target_limit=None,
            )
            retried = self.catalog.claim_specific_topology_target(
                provider="TAGO",
                run_id=run_id,
                city_code="25",
                route_id="ROUTE_001",
            )
            if index == 0:
                self.assertEqual(retried["next_page"], 1)
                self.assertEqual(retried["pages_fetched"], 0)
                with self.catalog.connect() as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM topology_pages WHERE route_id='ROUTE_001'"
                        ).fetchone()[0],
                        0,
                    )
            self.catalog.defer_or_fail_topology_target(
                provider="TAGO",
                city_code="25",
                route_id="ROUTE_001",
                deferred=False,
                error_code="TEST_FAILURE",
                error_message="test failure",
            )
            self.catalog.finish_topology_run(run_id, "PARTIAL")

        self.catalog.create_topology_run(
            run_id="run_retry_exhausted",
            provider="TAGO",
            target_source="tago",
            request_budget=10,
            target_limit=None,
        )
        with self.assertRaisesRegex(CatalogValidationError, "exhausted retry attempts"):
            self.catalog.claim_specific_topology_target(
                provider="TAGO",
                run_id="run_retry_exhausted",
                city_code="25",
                route_id="ROUTE_001",
            )

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

    def test_fill_loop_drains_resumable_queue(self):
        result = run_fill_loop(
            catalog=self.catalog,
            fetcher=FakeTago(),
            config=self.config(),
            max_cycles=2,
            pause_seconds=0,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["cycles"], 1)
        self.assertEqual(result["coverage"]["complete"], 1)

    def test_exhausted_transient_failures_can_be_requeued_explicitly(self):
        self.seed_targets(1)
        with self.catalog.connect() as connection:
            connection.execute(
                "UPDATE topology_progress SET status='FAILED',attempts=3,error_code='TAGO_TIMEOUT',error_message='TAGO request timed out' "
                "WHERE provider='TAGO' AND city_code='25' AND route_id='ROUTE_000'"
            )
            connection.commit()
        requeued = self.catalog.requeue_topology_failures(
            provider="TAGO",
            error_codes={"TAGO_TIMEOUT"},
        )
        self.assertEqual(requeued, 1)
        with self.catalog.connect() as connection:
            row = connection.execute(
                "SELECT status,attempts,error_code FROM topology_progress WHERE provider='TAGO' AND city_code='25' AND route_id='ROUTE_000'"
            ).fetchone()
        self.assertEqual(dict(row), {"status": "PENDING", "attempts": 0, "error_code": None})

    def test_failed_validation_retry_restarts_from_page_one(self):
        self.seed_targets(1)
        first_calls = []

        def empty_first_page(operation, parameters):
            self.assertEqual(operation, "route_stops")
            first_calls.append(parameters["pageNo"])
            return response([], total=0, page=1, size=2)

        first = self.ingestor(
            empty_first_page,
            request_budget=2,
            target_limit=1,
            target_source="catalog",
            trust_catalog_identifiers=True,
        ).run()
        self.assertEqual(first["run"]["status"], "PARTIAL")
        self.assertEqual(first_calls, ["1"])

        second_calls = []

        def valid_retry(operation, parameters):
            self.assertEqual(operation, "route_stops")
            second_calls.append(parameters["pageNo"])
            return self.one_page(parameters)

        second = self.ingestor(
            valid_retry,
            request_budget=2,
            target_limit=1,
            target_source="catalog",
            trust_catalog_identifiers=True,
        ).run()
        self.assertEqual(second["run"]["status"], "COMPLETE")
        self.assertEqual(second_calls, ["1"])
        self.assertEqual(second["coverage"]["complete"], 1)
        with self.catalog.connect() as connection:
            pages = connection.execute(
                "SELECT COUNT(*) FROM topology_pages"
            ).fetchone()[0]
        self.assertEqual(pages, 0)

    def test_single_point_spike_is_quarantined_before_sequence_hash_or_activation(self):
        self.seed_targets(1)
        with self.catalog.connect() as connection:
            connection.execute(
                "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                (
                    "seq_existing_spike",
                    "25",
                    "ROUTE_000",
                    "ORIGINAL_SOURCE",
                    "2026-08-30T00:00:00Z",
                    "b" * 64,
                    3,
                    "2026-08-31T00:00:00Z",
                ),
            )
            connection.executemany(
                "INSERT INTO route_sequence_stops("
                "sequence_id,node_order,node_id,node_name,latitude,longitude,"
                "direction,can_board,can_alight) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "seq_existing_spike",
                        1,
                        "OLD_A",
                        "A",
                        36.000,
                        127.0,
                        "0",
                        1,
                        1,
                    ),
                    (
                        "seq_existing_spike",
                        2,
                        "OLD_B",
                        "B",
                        36.300,
                        127.0,
                        "0",
                        1,
                        1,
                    ),
                    (
                        "seq_existing_spike",
                        3,
                        "OLD_C",
                        "C",
                        36.001,
                        127.0,
                        "0",
                        1,
                        1,
                    ),
                ],
            )
            connection.execute(
                "INSERT INTO active_route_sequences VALUES(?,?,?)",
                ("25", "ROUTE_000", "seq_existing_spike"),
            )
            connection.commit()

        def spike_route(operation, parameters):
            self.assertEqual(operation, "route_stops")
            route_id = parameters["routeId"]
            page = int(parameters["pageNo"])
            size = int(parameters["numOfRows"])
            items = [
                {
                    "citycode": "25",
                    "routeid": route_id,
                    "nodeid": "NODE_A",
                    "nodenm": "A",
                    "nodeord": 1,
                    "gpslati": 36.000,
                    "gpslong": 127.0,
                    "updowncd": "0",
                },
                {
                    "citycode": "25",
                    "routeid": route_id,
                    "nodeid": "NODE_B",
                    "nodenm": "B",
                    "nodeord": 2,
                    "gpslati": 36.300,
                    "gpslong": 127.0,
                    "updowncd": "0",
                },
                {
                    "citycode": "25",
                    "routeid": route_id,
                    "nodeid": "NODE_C",
                    "nodenm": "C",
                    "nodeord": 3,
                    "gpslati": 36.001,
                    "gpslong": 127.0,
                    "updowncd": "0",
                },
            ]
            start = (page - 1) * size
            return response(
                items[start : start + size], total=3, page=page, size=size
            )

        with patch.object(
            self.catalog,
            "route_sequence_sha256",
            side_effect=AssertionError("sequence hash must not run"),
        ) as sequence_hash:
            result = self.ingestor(
                spike_route,
                request_budget=4,
                target_limit=1,
                target_source="catalog",
                trust_catalog_identifiers=True,
            ).run()

        sequence_hash.assert_not_called()
        self.assertEqual(result["run"]["status"], "PARTIAL")
        self.assertEqual(result["run"]["failed"], 1)
        with self.catalog.connect() as connection:
            versions = connection.execute(
                "SELECT COUNT(*) FROM route_sequence_versions"
            ).fetchone()[0]
            active = connection.execute(
                "SELECT COUNT(*) FROM active_route_sequences"
            ).fetchone()[0]
            progress = connection.execute(
                "SELECT status,error_code,error_message FROM topology_progress "
                "WHERE provider='TAGO' AND city_code='25' AND route_id='ROUTE_000'"
            ).fetchone()
        self.assertEqual(versions, 1)
        self.assertEqual(active, 0)
        self.assertEqual(progress["status"], "FAILED")
        self.assertEqual(progress["error_code"], "SINGLE_POINT_ROUTE_SPIKE")
        self.assertLessEqual(len(progress["error_message"]), 240)

    def test_local_http_429_defers_claimed_target_as_budget_exhausted(self):
        payloads = {
            "/api/cities": {
                "ok": True,
                "mode": "live",
                "cities": [{"city_code": "25", "city_name": "대전광역시"}],
                "upstream": {"total_count": 1, "page_no": 1, "num_rows": 1},
            },
            "/api/routes": {
                "ok": True,
                "mode": "live",
                "routes": [
                    {"city_code": "25", "route_id": "DJB_ROUTE_1", "route_no": "101"}
                ],
                "upstream": {"total_count": 1, "page_no": 1, "num_rows": 2},
            },
        }

        def throttled(request, *, timeout):
            parsed = urlsplit(request.full_url)
            if parsed.path == "/api/routes/stops":
                raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)
            return FakeHttpResponse(payloads[parsed.path])

        fetcher = LocalLiveApiFetcher(
            "http://127.0.0.1:8791", timeout_seconds=4.0, open_url=throttled
        )
        result = self.ingestor(fetcher).run()

        self.assertEqual(result["run"]["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(result["coverage"]["statuses"], {"DEFERRED": 1})
        self.assertEqual(result["run"]["failed"], 0)

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

    def test_workers_share_exact_request_budget_and_cleanup_claims(self):
        self.seed_targets(40)
        first_wave = threading.Barrier(8)
        lock = threading.Lock()
        calls = 0

        def fetch(operation, parameters):
            nonlocal calls
            self.assertEqual(operation, "route_stops")
            with lock:
                calls += 1
                ordinal = calls
            if ordinal <= 8:
                first_wave.wait(timeout=3)
            return self.one_page(parameters)

        result = self.ingestor(
            fetch,
            request_budget=11,
            target_source="catalog",
            trust_catalog_identifiers=True,
            workers=8,
        ).run()

        self.assertEqual(result["run"]["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(calls, 11)
        self.assertEqual(result["run"]["requests_used"], 11)
        self.assertEqual(result["run"]["succeeded"], 11)
        self.assertEqual(
            result["run"]["targets_processed"],
            result["run"]["succeeded"] + result["run"]["deferred"],
        )
        with self.catalog.connect() as connection:
            in_progress = connection.execute(
                "SELECT COUNT(*) FROM topology_progress WHERE status='IN_PROGRESS'"
            ).fetchone()[0]
        self.assertEqual(in_progress, 0)

    def test_exhausted_budget_never_waits_for_the_next_rate_slot(self):
        self.seed_targets(2)
        sleeps = []
        ingestor = TopologyIngestor(
            catalog=self.catalog,
            fetcher=lambda operation, parameters: self.one_page(parameters),
            config=self.config(
                request_budget=1,
                requests_per_second=0.1,
                target_source="catalog",
                trust_catalog_identifiers=True,
            ),
            clock=lambda: FIXED_NOW,
            monotonic=lambda: 0.0,
            sleeper=sleeps.append,
        )
        result = ingestor.run()
        self.assertEqual(result["run"]["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(sleeps, [])

    def test_workers_respect_global_target_limit_below_worker_count(self):
        self.seed_targets(40)
        first_wave = threading.Barrier(5)
        lock = threading.Lock()
        calls = []

        def fetch(operation, parameters):
            with lock:
                calls.append(parameters["routeId"])
            first_wave.wait(timeout=3)
            return self.one_page(parameters)

        result = self.ingestor(
            fetch,
            request_budget=40,
            target_limit=5,
            target_source="catalog",
            trust_catalog_identifiers=True,
            workers=8,
        ).run()

        self.assertEqual(len(calls), 5)
        self.assertEqual(len(set(calls)), 5)
        self.assertEqual(result["run"]["targets_processed"], 5)
        self.assertEqual(result["run"]["succeeded"], 5)
        with self.catalog.connect() as connection:
            statuses = dict(
                connection.execute(
                    "SELECT status,COUNT(*) FROM topology_progress GROUP BY status"
                ).fetchall()
            )
        self.assertEqual(statuses, {"COMPLETE": 5, "PENDING": 35})

    def test_workers_claim_each_target_once_and_keep_catalog_integrity(self):
        self.seed_targets(32)
        first_wave = threading.Barrier(8)
        lock = threading.Lock()
        calls = Counter()
        started = 0

        def fetch(operation, parameters):
            nonlocal started
            route_id = parameters["routeId"]
            with lock:
                calls[route_id] += 1
                started += 1
                ordinal = started
            if ordinal <= 8:
                first_wave.wait(timeout=3)
            return self.one_page(parameters)

        result = self.ingestor(
            fetch,
            request_budget=40,
            target_source="catalog",
            trust_catalog_identifiers=True,
            workers=8,
        ).run()

        self.assertEqual(result["run"]["succeeded"], 32)
        self.assertEqual(set(calls.values()), {1})
        with self.catalog.connect() as connection:
            attempts = connection.execute(
                "SELECT MIN(attempts),MAX(attempts),COUNT(*) FROM topology_progress"
            ).fetchone()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(tuple(attempts), (1, 1, 32))
        self.assertEqual(integrity, "ok")

    def test_workers_keep_fetches_parallel_but_serialize_all_catalog_calls(self):
        self.seed_targets(4)
        call_lock = threading.Lock()
        fetch_barrier = threading.Barrier(4)
        active_fetches = 0
        max_active_fetches = 0
        active_catalog_calls = 0
        max_active_catalog_calls = 0
        catalog_call_count = 0
        inner_catalog = self.catalog

        class TrackingCatalog:
            def __getattr__(tracking_self, name):
                nonlocal active_catalog_calls
                nonlocal max_active_catalog_calls
                nonlocal catalog_call_count
                attribute = getattr(inner_catalog, name)
                if not callable(attribute):
                    return attribute

                def tracked(*args, **kwargs):
                    nonlocal active_catalog_calls
                    nonlocal max_active_catalog_calls
                    nonlocal catalog_call_count
                    with call_lock:
                        active_catalog_calls += 1
                        catalog_call_count += 1
                        max_active_catalog_calls = max(
                            max_active_catalog_calls, active_catalog_calls
                        )
                    try:
                        # Hold the call open long enough that unlocked workers
                        # deterministically overlap before entering SQLite.
                        time.sleep(0.005)
                        return attribute(*args, **kwargs)
                    finally:
                        with call_lock:
                            active_catalog_calls -= 1

                return tracked

        def fetch(operation, parameters):
            nonlocal active_fetches, max_active_fetches
            with call_lock:
                active_fetches += 1
                max_active_fetches = max(max_active_fetches, active_fetches)
            try:
                fetch_barrier.wait(timeout=3)
                return self.one_page(parameters)
            finally:
                with call_lock:
                    active_fetches -= 1

        result = TopologyIngestor(
            catalog=TrackingCatalog(),
            fetcher=fetch,
            config=self.config(
                request_budget=8,
                target_source="catalog",
                trust_catalog_identifiers=True,
                workers=4,
            ),
            clock=lambda: FIXED_NOW,
            monotonic=lambda: 0.0,
            sleeper=lambda _: None,
        ).run()

        self.assertEqual(result["run"]["status"], "COMPLETE")
        self.assertEqual(result["run"]["succeeded"], 4)
        self.assertEqual(max_active_fetches, 4)
        self.assertGreater(catalog_call_count, 0)
        self.assertEqual(max_active_catalog_calls, 1)

    def test_unknown_worker_exception_remains_fatal_for_parallel_run(self):
        self.seed_targets(12)
        first_wave = threading.Barrier(4)
        call_lock = threading.Lock()
        calls = 0
        holder = {}

        def fetch(operation, parameters):
            nonlocal calls
            with call_lock:
                calls += 1
                ordinal = calls
            first_wave.wait(timeout=3)
            if ordinal == 1:
                raise RuntimeError("internal-secret-marker")
            self.assertTrue(holder["ingestor"]._stop_event.wait(timeout=3))
            return self.one_page(parameters)

        ingestor = self.ingestor(
            fetch,
            request_budget=20,
            target_source="catalog",
            trust_catalog_identifiers=True,
            workers=4,
        )
        holder["ingestor"] = ingestor
        result = ingestor.run()

        self.assertFalse(result["ok"])
        self.assertEqual(result["run"]["status"], "FAILED")
        self.assertEqual(calls, 4)
        with self.catalog.connect() as connection:
            in_progress = connection.execute(
                "SELECT COUNT(*) FROM topology_progress WHERE status='IN_PROGRESS'"
            ).fetchone()[0]
            failure = connection.execute(
                "SELECT error_code,error_message FROM topology_progress "
                "WHERE error_code='UNEXPECTED_COLLECTOR_ERROR'"
            ).fetchone()
        self.assertEqual(in_progress, 0)
        self.assertEqual(dict(failure), {
            "error_code": "UNEXPECTED_COLLECTOR_ERROR",
            "error_message": "Unexpected collector failure",
        })

    def test_parallel_budget_resume_starts_at_each_targets_next_page(self):
        self.seed_targets(4)
        first_wave = threading.Barrier(4)
        first_calls = []
        first_lock = threading.Lock()

        def paged_response(parameters):
            route_id = parameters["routeId"]
            page = int(parameters["pageNo"])
            suffix = route_id.rsplit("_", 1)[-1]
            node = "A" if page == 1 else "B"
            return response(
                [
                    {
                        "citycode": "25",
                        "routeid": route_id,
                        "nodeid": f"N{suffix}{node}",
                        "nodenm": node,
                        "nodeord": page,
                    }
                ],
                total=2,
                page=page,
                size=1,
            )

        def first_fetch(operation, parameters):
            with first_lock:
                first_calls.append((parameters["routeId"], parameters["pageNo"]))
            first_wave.wait(timeout=3)
            return paged_response(parameters)

        first = self.ingestor(
            first_fetch,
            request_budget=4,
            page_size=1,
            target_source="catalog",
            trust_catalog_identifiers=True,
            workers=4,
        ).run()
        self.assertEqual(first["run"]["status"], "BUDGET_EXHAUSTED")
        self.assertEqual({page for _, page in first_calls}, {"1"})

        second_calls = []

        def second_fetch(operation, parameters):
            second_calls.append((parameters["routeId"], parameters["pageNo"]))
            return paged_response(parameters)

        second = self.ingestor(
            second_fetch,
            request_budget=4,
            page_size=1,
            target_source="catalog",
            trust_catalog_identifiers=True,
            workers=4,
        ).run()
        self.assertEqual(second["run"]["status"], "COMPLETE")
        self.assertEqual(second["run"]["succeeded"], 4)
        self.assertEqual(len(second_calls), 4)
        self.assertEqual({page for _, page in second_calls}, {"2"})

    def test_parallel_refresh_records_unchanged_without_duplicate_sequences(self):
        self.seed_targets(4)

        def fetch(operation, parameters):
            return self.one_page(parameters)

        first = self.ingestor(
            fetch,
            request_budget=8,
            target_source="catalog",
            trust_catalog_identifiers=True,
            workers=4,
        ).run()
        self.assertEqual(first["run"]["succeeded"], 4)
        refreshed = self.ingestor(
            fetch,
            request_budget=8,
            target_source="catalog",
            trust_catalog_identifiers=True,
            refresh_complete=True,
            workers=4,
        ).run()
        self.assertEqual(refreshed["run"]["unchanged"], 4)
        self.assertEqual(len(self.catalog.planning_snapshot().route_sequences), 4)

    def test_global_request_start_rate_is_serialized_with_manual_clock(self):
        self.seed_targets(6)
        clock_lock = threading.Lock()
        now = 0.0
        sleeps = []
        starts = []

        def monotonic():
            with clock_lock:
                return now

        def sleep(duration):
            nonlocal now
            with clock_lock:
                sleeps.append(duration)
                now += duration

        ingestor = TopologyIngestor(
            catalog=self.catalog,
            fetcher=lambda operation, parameters: self.one_page(parameters),
            config=self.config(
                request_budget=10,
                requests_per_second=4,
                target_source="catalog",
                trust_catalog_identifiers=True,
                workers=4,
            ),
            clock=lambda: FIXED_NOW,
            monotonic=monotonic,
            sleeper=sleep,
        )
        ingestor._on_request_started = starts.append
        result = ingestor.run()

        self.assertEqual(result["run"]["succeeded"], 6)
        self.assertEqual(len(starts), 6)
        self.assertTrue(
            all(later - earlier >= 0.25 for earlier, later in zip(starts, starts[1:]))
        )
        self.assertAlmostEqual(sum(sleeps), 1.25)

    def test_fatal_access_stops_after_inflight_wave_without_orphan_claims(self):
        self.seed_targets(40)
        first_wave = threading.Barrier(4)
        lock = threading.Lock()
        calls = 0
        holder = {}

        def fetch(operation, parameters):
            nonlocal calls
            with lock:
                calls += 1
                ordinal = calls
            first_wave.wait(timeout=3)
            if ordinal == 1:
                raise TagoError("30", "secret provider response")
            self.assertTrue(holder["ingestor"]._stop_event.wait(timeout=3))
            return self.one_page(parameters)

        ingestor = self.ingestor(
            fetch,
            request_budget=40,
            target_source="catalog",
            trust_catalog_identifiers=True,
            workers=4,
        )
        holder["ingestor"] = ingestor
        result = ingestor.run()

        self.assertFalse(result["ok"])
        self.assertEqual(result["run"]["status"], "DATA_GAP")
        self.assertEqual(calls, 4)
        with self.catalog.connect() as connection:
            in_progress = connection.execute(
                "SELECT COUNT(*) FROM topology_progress WHERE status='IN_PROGRESS'"
            ).fetchone()[0]
            fatal = connection.execute(
                "SELECT COUNT(*) FROM topology_progress WHERE error_code='30'"
            ).fetchone()[0]
        self.assertEqual(in_progress, 0)
        self.assertEqual(fatal, 1)

    def test_daily_quota_stops_after_inflight_wave_and_defers_target(self):
        self.seed_targets(40)
        first_wave = threading.Barrier(4)
        lock = threading.Lock()
        calls = 0
        holder = {}

        def fetch(operation, parameters):
            nonlocal calls
            with lock:
                calls += 1
                ordinal = calls
            first_wave.wait(timeout=3)
            if ordinal == 1:
                raise TagoError(
                    "22", "LIMITED NUMBER OF SERVICE REQUESTS EXCEEDS ERROR"
                )
            self.assertTrue(holder["ingestor"]._stop_event.wait(timeout=3))
            return self.one_page(parameters)

        ingestor = self.ingestor(
            fetch,
            request_budget=40,
            target_source="catalog",
            trust_catalog_identifiers=True,
            workers=4,
        )
        holder["ingestor"] = ingestor
        result = ingestor.run()

        self.assertEqual(result["run"]["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(result["run"]["failed"], 0)
        self.assertEqual(calls, 4)
        with self.catalog.connect() as connection:
            in_progress = connection.execute(
                "SELECT COUNT(*) FROM topology_progress WHERE status='IN_PROGRESS'"
            ).fetchone()[0]
            quota = connection.execute(
                "SELECT status,error_code FROM topology_progress "
                "WHERE error_code='22'"
            ).fetchone()
        self.assertEqual(in_progress, 0)
        self.assertEqual(tuple(quota), ("DEFERRED", "22"))

    def test_os_lock_rejects_a_second_cli_and_releases_for_next_run(self):
        catalog_path = Path(self.temp.name) / "catalog.sqlite3"
        with _catalog_process_lock(catalog_path):
            with self.assertRaises(TopologyProcessLocked):
                with _catalog_process_lock(catalog_path):
                    self.fail("second process lock must not be acquired")
        with _catalog_process_lock(catalog_path):
            pass

    def test_stale_database_run_does_not_permanently_block_recovery(self):
        self.catalog.create_topology_run(
            run_id="ing_first",
            provider="TAGO",
            target_source="tago",
            request_budget=10,
            target_limit=None,
        )
        self.catalog.create_topology_run(
            run_id="ing_second",
            provider="TAGO",
            target_source="tago",
            request_budget=10,
            target_limit=None,
        )
        self.catalog.finish_topology_run("ing_first", "FAILED")
        self.catalog.finish_topology_run("ing_second", "FAILED")


class LocalLiveApiTests(unittest.TestCase):
    def test_cli_requires_exactly_one_access_mode(self):
        base = ["--catalog-db", "catalog.sqlite3"]
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _parser().parse_args(base)
            with self.assertRaises(SystemExit):
                _parser().parse_args(
                    base
                    + [
                        "--service-key-stdin",
                        "--local-live-api",
                        "http://127.0.0.1:8791",
                    ]
                )
        args = _parser().parse_args(base + ["--local-live-api", "http://127.0.0.1:8791"])
        self.assertEqual(args.local_live_api, "http://127.0.0.1:8791")
        self.assertFalse(args.service_key_stdin)
        self.assertEqual(args.workers, 1)
        self.assertFalse(args.fill_loop)
        self.assertEqual(args.max_cycles, 4)
        self.assertEqual(args.max_no_progress_cycles, 2)
        worker_args = _parser().parse_args(
            base
            + [
                "--local-live-api",
                "http://127.0.0.1:8791",
                "--workers",
                "7",
                "--fill-loop",
                "--max-cycles",
                "9",
                "--retry-exhausted-transient",
            ]
        )
        self.assertEqual(worker_args.workers, 7)
        self.assertTrue(worker_args.fill_loop)
        self.assertEqual(worker_args.max_cycles, 9)
        self.assertTrue(worker_args.retry_exhausted_transient)
        selected_args = _parser().parse_args(
            base
            + [
                "--local-live-api",
                "http://127.0.0.1:8791",
                "--only-route",
                "12:SJB293000331",
                "--only-route",
                "25:DJB30300128",
                "--only-route",
                "25:DJB30300074",
                "--repair-corrupt-retries",
            ]
        )
        self.assertEqual(
            selected_args.only_route,
            [
                ("12", "SJB293000331"),
                ("25", "DJB30300128"),
                ("25", "DJB30300074"),
            ],
        )
        self.assertTrue(selected_args.repair_corrupt_retries)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _parser().parse_args(
                base
                + [
                    "--local-live-api",
                    "http://127.0.0.1:8791",
                    "--only-route",
                    "25:DJB30300128",
                    "--only-route",
                    "25:DJB30300128",
                ]
            )
        self.assertEqual(_only_route_target("25:GMB수점10"), ("25", "GMB수점10"))
        for value in ["25", ":ROUTE", "25:", "25:BAD/ROUTE", "2 5:ROUTE"]:
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                _only_route_target(value)
        with self.assertRaisesRegex(ValueError, "duplicate --only-route"):
            IngestConfig(
                only_routes=(("25", "DJB30300128"), ("25", "DJB30300128"))
            ).validate()
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            IngestConfig(
                target_limit=1,
                only_routes=(("25", "DJB30300128"),),
            ).validate()
        with self.assertRaises(ValueError):
            IngestConfig(workers=0).validate()
        IngestConfig(workers=16).validate()
        with self.assertRaises(ValueError):
            IngestConfig(workers=17).validate()
        with self.assertRaises(ValueError):
            IngestConfig(requests_per_second=0.09).validate()

    def test_local_origin_is_literal_loopback_http_without_url_components(self):
        self.assertEqual(
            _local_live_api_origin("http://127.0.0.2:8791"),
            "http://127.0.0.2:8791",
        )
        self.assertEqual(_local_live_api_origin("http://[::1]:8791"), "http://[::1]:8791")
        invalid = [
            "https://127.0.0.1:8791",
            "http://localhost:8791",
            "http://192.168.0.2:8791",
            "http://user@127.0.0.1:8791",
            "http://127.0.0.1:8791/",
            "http://127.0.0.1:8791?x=1",
            "http://127.0.0.1:8791#fragment",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                _local_live_api_origin(value)

    def test_ready_live_api_maps_only_fixed_endpoints_to_raw_contract(self):
        payloads = {
            "/api/status": {
                "ok": True,
                "service": "busro-itda-data-service",
                "mode": "live",
                "tago": {"configured": True, "state": "ready", "key_exposed": False},
            },
            "/api/cities": {
                "ok": True,
                "mode": "live",
                "cities": [{"city_code": "25", "city_name": "대전광역시"}],
                "upstream": {"total_count": 1, "page_no": 1, "num_rows": 1},
            },
            "/api/routes": {
                "ok": True,
                "mode": "live",
                "routes": [
                    {
                        "city_code": "25",
                        "route_id": "DJB_ROUTE_1",
                        "route_no": "101",
                        "route_type": "간선",
                        "start_node_name": "기점",
                        "end_node_name": "종점",
                    }
                ],
                "upstream": {"total_count": 3, "page_no": 2, "num_rows": 1},
            },
            "/api/routes/stops": {
                "ok": True,
                "mode": "live",
                "stops": [
                    {
                        "city_code": "25",
                        "route_id": "DJB_ROUTE_1",
                        "node_id": "DJB_A",
                        "node_no": "1001",
                        "node_name": "기점",
                        "node_order": 1,
                        "latitude": 36.3,
                        "longitude": 127.3,
                        "up_down_code": "0",
                    }
                ],
                "upstream": {"total_count": 1, "page_no": 1, "num_rows": 100},
            },
        }
        local_api = FakeLocalApi(payloads)
        fetcher = LocalLiveApiFetcher(
            "http://127.0.0.1:8791", timeout_seconds=4.0, open_url=local_api
        )
        fetcher.verify_ready()
        cities, city_meta = normalize_catalog(fetcher("cities", {}), operation="cities")
        routes, route_meta = normalize_catalog(
            fetcher(
                "routes",
                {"cityCode": "25", "pageNo": "2", "numOfRows": "1"},
            ),
            operation="routes",
            fallback_city_code="25",
        )
        stops, stop_meta = normalize_catalog(
            fetcher(
                "route_stops",
                {
                    "cityCode": "25",
                    "routeId": "DJB_ROUTE_1",
                    "pageNo": "1",
                    "numOfRows": "100",
                },
            ),
            operation="route_stops",
            fallback_city_code="25",
            fallback_route_id="DJB_ROUTE_1",
        )
        self.assertEqual(cities, [{"city_code": "25", "city_name": "대전광역시"}])
        self.assertEqual(city_meta["total_count"], 1)
        self.assertEqual(routes[0]["route_id"], "DJB_ROUTE_1")
        self.assertEqual(routes[0]["start_node_name"], "기점")
        self.assertEqual(route_meta["page_no"], 2)
        self.assertEqual(stops[0]["node_id"], "DJB_A")
        self.assertEqual(stops[0]["node_order"], 1)
        self.assertEqual(stop_meta["total_count"], 1)

        urls = [urlsplit(call[0].full_url) for call in local_api.calls]
        self.assertEqual(
            [item.path for item in urls],
            ["/api/status", "/api/cities", "/api/routes", "/api/routes/stops"],
        )
        self.assertEqual(
            parse_qs(urls[2].query),
            {"city_code": ["25"], "page": ["2"], "limit": ["1"]},
        )
        self.assertNotIn("serviceKey", "".join(item.query for item in urls))
        self.assertTrue(all(timeout == 4.0 for _, timeout in local_api.calls))

        before = len(local_api.calls)
        with self.assertRaises(TagoError):
            fetcher("route_info", {})
        with self.assertRaises(TagoError):
            fetcher("cities", {"url": "http://example.invalid"})
        self.assertEqual(len(local_api.calls), before)

    def test_status_failure_does_not_echo_response_or_secret(self):
        secret = "NEVER-ECHO-THIS-KEY"
        local_api = FakeLocalApi(
            {
                "/api/status": {
                    "ok": True,
                    "service": "busro-itda-data-service",
                    "mode": "fixture",
                    "tago": {
                        "configured": False,
                        "state": "missing_key",
                        "key_exposed": False,
                        "unexpected_secret": secret,
                    },
                }
            }
        )
        fetcher = LocalLiveApiFetcher(
            "http://127.0.0.1:8791", timeout_seconds=4.0, open_url=local_api
        )
        with self.assertRaises(TagoError) as raised:
            fetcher.verify_ready()
        self.assertEqual(raised.exception.code, "LOCAL_API_NOT_READY")
        self.assertNotIn(secret, str(raised.exception))

    def test_http_error_restores_bounded_fatal_code_without_echoing_body(self):
        secret = "NEVER-ECHO-HTTP-BODY"

        def denied(request, *, timeout):
            body = json.dumps(
                {"ok": False, "error": {"code": "30", "message": secret}}
            ).encode("utf-8")
            raise HTTPError(
                request.full_url,
                403,
                "Forbidden",
                {"Content-Type": "application/json"},
                io.BytesIO(body),
            )

        fetcher = LocalLiveApiFetcher(
            "http://127.0.0.1:8791", timeout_seconds=4.0, open_url=denied
        )
        with self.assertRaises(TagoError) as raised:
            fetcher.verify_ready()
        self.assertEqual(raised.exception.code, "30")
        self.assertNotIn(secret, str(raised.exception))

    def test_timeout_and_response_size_are_bounded(self):
        with self.assertRaises(ValueError):
            LocalLiveApiFetcher("http://127.0.0.1:8791", timeout_seconds=0.49)
        with self.assertRaises(ValueError):
            LocalLiveApiFetcher("http://127.0.0.1:8791", timeout_seconds=30.01)

        oversized = FakeHttpResponse(
            b"{}", content_length=False
        )
        oversized.headers["Content-Length"] = str(MAX_LOCAL_API_RESPONSE_BYTES + 1)
        fetcher = LocalLiveApiFetcher(
            "http://127.0.0.1:8791",
            timeout_seconds=4.0,
            open_url=lambda request, timeout: oversized,
        )
        with self.assertRaises(TagoError) as raised:
            fetcher.verify_ready()
        self.assertEqual(raised.exception.code, "LOCAL_API_RESPONSE_TOO_LARGE")
        self.assertFalse(oversized.read_called)

        chunked = FakeHttpResponse(
            b"x" * (MAX_LOCAL_API_RESPONSE_BYTES + 1), content_length=False
        )
        fetcher = LocalLiveApiFetcher(
            "http://127.0.0.1:8791",
            timeout_seconds=4.0,
            open_url=lambda request, timeout: chunked,
        )
        with self.assertRaises(TagoError) as raised:
            fetcher.verify_ready()
        self.assertEqual(raised.exception.code, "LOCAL_API_RESPONSE_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
