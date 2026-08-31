from __future__ import annotations

import argparse
from contextlib import redirect_stderr
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
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
    TopologyIngestor,
    _local_live_api_origin,
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
