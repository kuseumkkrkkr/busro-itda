from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from app import AppError, BusroService  # noqa: E402
from config import Settings  # noqa: E402
from server import BusroHTTPServer, Handler  # noqa: E402
from tago import (  # noqa: E402
    CATALOG_OPERATIONS,
    TagoError,
    fetch_catalog,
    normalize_catalog,
)


FIXED_NOW = datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)


class CatalogCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            fixture_mode=True,
            db_path=Path(self.temp.name) / "catalog.sqlite3",
            network_catalog_path=Path(self.temp.name) / "network-catalog.sqlite3",
            catalog_fixture_path=SERVICE_DIR / "fixtures" / "tago_catalog.json",
        )
        self.service = BusroService(self.settings, clock=lambda: FIXED_NOW)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_object_and_list_items_normalize_to_stable_fields(self) -> None:
        route_object = {
            "response": {
                "header": {"resultCode": "00"},
                "body": {
                    "items": {
                        "item": {
                            "citycode": "25",
                            "routeid": "DJB1",
                            "routeno": "101",
                            "routetp": "간선",
                        }
                    },
                    "totalCount": 1,
                },
            }
        }
        routes, route_meta = normalize_catalog(route_object, operation="routes")
        self.assertEqual(routes[0]["route_id"], "DJB1")
        self.assertEqual(route_meta["normalized_count"], 1)

        stop_list = {
            "response": {
                "header": {"resultCode": "00"},
                "body": {
                    "items": {
                        "item": [
                            {
                                "nodeid": "NODE1",
                                "nodenm": "첫 정류장",
                                "nodeord": "1",
                                "gpslati": "36.1",
                                "gpslong": "127.1",
                            },
                            {"invalid": True},
                        ]
                    },
                    "totalCount": "2",
                },
            }
        }
        stops, stop_meta = normalize_catalog(
            stop_list,
            operation="route_stops",
            fallback_city_code="25",
            fallback_route_id="DJB1",
        )
        self.assertEqual(stops[0]["node_order"], 1)
        self.assertEqual(stops[0]["route_id"], "DJB1")
        self.assertEqual(stop_meta["total_count"], 2)

    def test_fixture_catalog_is_explicitly_not_live_and_persisted(self) -> None:
        cities = self.service.cities({})
        routes = self.service.routes({"city_code": "25"})
        route = self.service.route_info(
            {"city_code": "25", "route_id": "DJB_FIXTURE_001"}
        )
        stops = self.service.route_stops(
            {"city_code": "25", "route_id": "DJB_FIXTURE_001"}
        )
        self.assertGreaterEqual(len(cities["cities"]), 5)
        self.assertEqual(routes["routes"][0]["route_id"], "DJB_FIXTURE_001")
        self.assertEqual(route["route"]["route_no"], "샘플1")
        self.assertEqual(stops["stops"][1]["node_order"], 2)
        self.assertEqual(cities["provenance"]["fixture_notice"], "SCHEMA_ONLY_NOT_LIVE")
        self.assertEqual(self.service.store.counts()["catalog_snapshots"], 4)

    def test_live_key_absence_is_clear_and_key_is_never_exposed(self) -> None:
        live = BusroService(
            replace(
                self.settings,
                fixture_mode=False,
                tago_service_key=None,
                db_path=Path(self.temp.name) / "live.sqlite3",
            ),
            clock=lambda: FIXED_NOW,
        )
        with self.assertRaises(AppError) as raised:
            live.cities({})
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.code, "TAGO_KEY_REQUIRED")
        self.assertNotIn("service_key", json.dumps(live.status()).lower())

    def test_fixed_operation_and_decoded_key_are_encoded_once(self) -> None:
        response_body = {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                "body": {"items": {"item": []}, "totalCount": 0},
            }
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(response_body).encode("utf-8")

        with patch("tago.urlopen", return_value=FakeResponse()) as opened:
            fetch_catalog(
                operation="routes",
                parameters={"cityCode": "25", "routeNo": "101"},
                service_key="abc+def/ghi=",
                timeout_seconds=3,
                fixture_mode=False,
                fixture_path=SERVICE_DIR / "fixtures" / "tago_catalog.json",
            )
        url = opened.call_args.args[0].full_url
        self.assertTrue(url.startswith(CATALOG_OPERATIONS["routes"] + "?"))
        self.assertIn("serviceKey=abc%2Bdef%2Fghi%3D", url)
        self.assertIn("routeNo=101", url)
        self.assertNotIn("abc+def/ghi=", url)

    def test_catalog_rejects_http_200_envelope_without_result_code(self) -> None:
        response_body = {
            "response": {
                "header": {"resultCode": "", "resultMsg": ""},
                "body": {"items": "", "totalCount": 0},
            }
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(response_body).encode("utf-8")

        with patch("tago.urlopen", return_value=FakeResponse()):
            with self.assertRaises(TagoError) as raised:
                fetch_catalog(
                    operation="routes",
                    parameters={"cityCode": "25", "routeNo": "607"},
                    service_key="decoded-key",
                    timeout_seconds=3,
                    fixture_mode=False,
                    fixture_path=SERVICE_DIR / "fixtures" / "tago_catalog.json",
                )
        self.assertEqual(raised.exception.code, "UPSTREAM_MALFORMED_RESPONSE")

    def test_catalog_preserves_http_200_service_error_envelope(self) -> None:
        response_body = {
            "OpenAPI_ServiceResponse": {
                "cmmMsgHeader": {
                    "errMsg": "HTTP_ERROR",
                    "returnAuthMsg": "HTTP 에러",
                    "returnReasonCode": "04",
                }
            }
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(response_body).encode("utf-8")

        with patch("tago.urlopen", return_value=FakeResponse()):
            with self.assertRaises(TagoError) as raised:
                fetch_catalog(
                    operation="routes",
                    parameters={"cityCode": "25"},
                    service_key="decoded-key",
                    timeout_seconds=3,
                    fixture_mode=False,
                    fixture_path=SERVICE_DIR / "fixtures" / "tago_catalog.json",
                )
        self.assertEqual(raised.exception.code, "04")
        self.assertNotIn("decoded-key", raised.exception.message)

    def _live_service(self, name: str) -> BusroService:
        return BusroService(
            replace(
                self.settings,
                fixture_mode=False,
                tago_service_key="decoded-key",
                db_path=Path(self.temp.name) / f"{name}.sqlite3",
                network_catalog_path=Path(self.temp.name) / f"{name}-network.sqlite3",
            ),
            clock=lambda: FIXED_NOW,
        )

    @staticmethod
    def _route_payload(*items: dict[str, str]) -> dict:
        return {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                "body": {"items": {"item": list(items)}, "totalCount": len(items)},
            }
        }

    def test_route_number_malformed_response_uses_one_exact_unfiltered_retry(self) -> None:
        service = self._live_service("route-number-fallback")
        fallback = self._route_payload(
            {"citycode": "25", "routeid": "DJB_OTHER", "routeno": "607-1"},
            {"citycode": "25", "routeid": "DJB_607", "routeno": "607"},
        )
        calls: list[dict] = []

        def fake_fetch(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TagoError(
                    "UPSTREAM_MALFORMED_RESPONSE",
                    "TAGO catalog response did not include a result code",
                )
            return fallback

        with patch("app.fetch_catalog", side_effect=fake_fetch):
            result = service.routes({"city_code": "25", "route_no": "607"})

        self.assertEqual(len(calls), 2)
        self.assertIn("routeNo", calls[0]["parameters"])
        self.assertNotIn("routeNo", calls[1]["parameters"])
        self.assertEqual([route["route_id"] for route in result["routes"]], ["DJB_607"])
        fallback_meta = result["upstream"]["verification_fallback"]
        self.assertEqual(fallback_meta["bounded_additional_calls"], 1)
        self.assertEqual(fallback_meta["strategy"], "same_city_page_exact_route_no")
        self.assertEqual(result["provenance"]["operation"], "routes")

    def test_route_info_malformed_response_requires_exact_official_route_id(self) -> None:
        service = self._live_service("route-info-fallback")
        fallback = self._route_payload(
            {"citycode": "26", "routeid": "USB_OTHER", "routeno": "114"},
            {
                "citycode": "26",
                "routeid": "USB192000006",
                "routeno": "115",
                "startnodenm": "첫 정류장",
                "endnodenm": "종점",
            },
        )
        calls: list[dict] = []

        def fake_fetch(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TagoError(
                    "UPSTREAM_MALFORMED_RESPONSE",
                    "TAGO catalog response did not include a result code",
                )
            return fallback

        with patch("app.fetch_catalog", side_effect=fake_fetch):
            result = service.route_info(
                {"city_code": "26", "route_id": "USB192000006"}
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["operation"], "route_info")
        self.assertEqual(calls[1]["operation"], "routes")
        self.assertEqual(calls[1]["parameters"]["numOfRows"], "100")
        self.assertEqual(result["route"]["route_id"], "USB192000006")
        self.assertEqual(result["route"]["route_no"], "115")
        self.assertEqual(result["provenance"]["operation"], "route_info")
        self.assertEqual(result["provenance"]["upstream_operation"], "routes")
        self.assertEqual(
            result["upstream"]["verification_fallback"]["strategy"],
            "same_city_first_page_exact_route_id",
        )

    def test_bounded_fallback_does_not_claim_absence_without_exact_match(self) -> None:
        service = self._live_service("route-fallback-no-match")
        calls = 0

        def fake_fetch(**_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TagoError("UPSTREAM_MALFORMED_RESPONSE", "missing result code")
            return self._route_payload(
                {"citycode": "25", "routeid": "DJB_607_1", "routeno": "607-1"}
            )

        with patch("app.fetch_catalog", side_effect=fake_fetch):
            with self.assertRaises(AppError) as raised:
                service.routes({"city_code": "25", "route_no": "607"})

        self.assertEqual(calls, 2)
        self.assertEqual(raised.exception.code, "UPSTREAM_MALFORMED_RESPONSE")
        self.assertEqual(service.store.counts()["catalog_snapshots"], 0)

    def test_official_tago_error_is_not_retried_as_malformed(self) -> None:
        service = self._live_service("route-no-error-retry")
        calls = 0

        def fake_fetch(**_kwargs):
            nonlocal calls
            calls += 1
            raise TagoError("22", "LIMITED NUMBER OF SERVICE REQUESTS EXCEEDS ERROR")

        with patch("app.fetch_catalog", side_effect=fake_fetch):
            with self.assertRaises(AppError) as raised:
                service.routes({"city_code": "25", "route_no": "607"})

        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.code, "22")

    def test_malicious_and_unbounded_catalog_inputs_are_rejected(self) -> None:
        cases = (
            lambda: self.service.routes({"city_code": "25/../../secret"}),
            lambda: self.service.routes({"city_code": "25", "route_no": "101\nInjected: yes"}),
            lambda: self.service.route_stops({"city_code": "25", "route_id": "https://evil.test"}),
            lambda: self.service.nearby_stops({"latitude": "nan", "longitude": "127.1"}),
            lambda: self.service.stops({"city_code": "25", "limit": "101"}),
            lambda: self.service.cities({"url": "https://evil.test"}),
        )
        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaises(AppError):
                    operation()

    def test_tago_transport_route_ids_preserve_hangul(self) -> None:
        route_id = "GMB수점10"
        self.assertIsNone(
            self.service.route_info({"city_code": "25", "route_id": route_id})["route"]
        )
        self.assertEqual(
            self.service.route_stops({"city_code": "25", "route_id": route_id})["stops"],
            [],
        )
        self.assertFalse(
            self.service.validate_mapping(
                {"city_code": "25", "route_id": route_id, "node_id": "NODE1"}
            )["valid"]
        )
        positions = self.service.positions({"city_code": "25", "route_id": route_id})[
            "positions"
        ]
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["route_id"], route_id)

    def test_mapping_validation_proves_route_contains_exact_node(self) -> None:
        valid = self.service.validate_mapping(
            {
                "city_code": "25",
                "route_id": "DJB_FIXTURE_001",
                "node_id": "DJB_FIXTURE_STOP_002",
            }
        )
        invalid = self.service.validate_mapping(
            {
                "city_code": "25",
                "route_id": "DJB_FIXTURE_001",
                "node_id": "DJB_FIXTURE_STOP_999",
            }
        )
        repeated = self.service.validate_mapping(
            {
                "city_code": "25",
                "route_id": "DJB_FIXTURE_001",
                "node_id": "DJB_FIXTURE_STOP_002",
            }
        )
        self.assertTrue(valid["valid"])
        self.assertEqual(valid["match"]["node_order"], 2)
        self.assertEqual(valid["reason"], "ROUTE_CONTAINS_NODE")
        self.assertFalse(invalid["valid"])
        self.assertEqual(invalid["reason"], "NODE_NOT_ON_ROUTE")
        self.assertFalse(repeated["validation"]["created"])
        self.assertEqual(self.service.store.counts()["mapping_validations"], 2)

    def test_fifty_concurrent_catalog_misses_share_one_upstream_call(self) -> None:
        from app import fetch_catalog as real_fetch

        barrier = threading.Barrier(50)
        lock = threading.Lock()
        calls = 0

        def slow_fetch(**kwargs):
            nonlocal calls
            with lock:
                calls += 1
            time.sleep(0.05)
            return real_fetch(**kwargs)

        def request(_index):
            barrier.wait(timeout=3)
            return self.service.cities({})

        with patch("app.fetch_catalog", side_effect=slow_fetch):
            with ThreadPoolExecutor(max_workers=50) as executor:
                results = list(executor.map(request, range(50)))
        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 50)
        self.assertTrue(all(result["cities"] for result in results))
        self.assertEqual(self.service.store.counts()["catalog_snapshots"], 1)


class CatalogHTTPCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        service = BusroService(
            Settings(
                fixture_mode=True,
                db_path=Path(self.temp.name) / "http-catalog.sqlite3",
                network_catalog_path=Path(self.temp.name) / "http-network-catalog.sqlite3",
                catalog_fixture_path=SERVICE_DIR / "fixtures" / "tago_catalog.json",
            ),
            clock=lambda: FIXED_NOW,
        )
        self.server = BusroHTTPServer(("127.0.0.1", 0), Handler, service=service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, result

    def test_all_catalog_http_routes_are_reachable(self) -> None:
        requests = (
            ("GET", "/api/cities", None, "cities"),
            ("GET", "/api/routes?city_code=25", None, "routes"),
            ("GET", "/api/routes/info?city_code=25&route_id=DJB_FIXTURE_001", None, "route"),
            ("GET", "/api/routes/stops?city_code=25&route_id=DJB_FIXTURE_001", None, "stops"),
            ("GET", "/api/stops?city_code=25&node_name=%EC%83%98%ED%94%8C", None, "stops"),
            ("GET", "/api/stops/nearby?latitude=36.35&longitude=127.38", None, "stops"),
            ("GET", "/api/stops/routes?city_code=25&node_id=DJB_FIXTURE_STOP_002", None, "routes"),
            (
                "POST",
                "/api/mappings/validate",
                {
                    "city_code": "25",
                    "route_id": "DJB_FIXTURE_001",
                    "node_id": "DJB_FIXTURE_STOP_002",
                },
                "valid",
            ),
        )
        for method, path, body, field in requests:
            with self.subTest(path=path):
                status, result = self.request(method, path, body)
                self.assertEqual(status, 200)
                self.assertIn(field, result)


if __name__ == "__main__":
    unittest.main()
