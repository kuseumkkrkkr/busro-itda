from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import http.client
from http.server import BaseHTTPRequestHandler
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
from tago import POSITIONS_URL, TagoError, fetch_positions  # noqa: E402


FIXED_NOW = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        settings = Settings(
            fixture_mode=True,
            db_path=Path(self.temp.name) / "test.sqlite3",
            network_catalog_path=Path(self.temp.name) / "network-catalog.sqlite3",
            fixture_path=SERVICE_DIR / "fixtures" / "tago_arrivals.json",
            fixture_delays_path=SERVICE_DIR / "fixtures" / "delay_samples.json",
            allowed_origins=("http://127.0.0.1:8290",),
        )
        self.service = BusroService(settings, clock=lambda: FIXED_NOW)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def position_payload(node_order: int, node_id: str, node_name: str) -> dict:
        payload = json.loads(
            (SERVICE_DIR / "fixtures" / "tago_positions.json").read_text(encoding="utf-8")
        )
        item = payload["response"]["body"]["items"]["item"][0]
        item["nodeord"] = node_order
        item["nodeid"] = node_id
        item["nodenm"] = node_name
        return payload

    def test_status_is_explicitly_fixture_and_does_not_expose_key(self) -> None:
        status = self.service.status()
        self.assertEqual(status["mode"], "fixture")
        self.assertEqual(status["tago"]["state"], "fixture")
        self.assertFalse(status["tago"]["key_exposed"])
        self.assertNotIn("service_key", json.dumps(status).lower())

    def test_arrivals_normalize_and_cache(self) -> None:
        first = self.service.arrivals({"city_code": "25", "node_id": "DJB8001793"})
        second = self.service.arrivals({"city_code": "25", "node_id": "DJB8001793"})
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["arrivals"][0]["arrival_seconds"], 420)
        self.assertEqual(first["arrivals"][0]["route_id"], "DJB30300002")

    def test_collect_bypasses_stale_arrival_cache_before_persisting(self) -> None:
        original = json.loads(
            (SERVICE_DIR / "fixtures" / "tago_arrivals.json").read_text(encoding="utf-8")
        )
        refreshed = json.loads(json.dumps(original))
        refreshed["response"]["body"]["items"]["item"][0]["arrtime"] = "9999"

        with patch("app.fetch_arrivals", side_effect=[original, refreshed]) as fetch:
            cached = self.service.arrivals({"city_code": "25", "node_id": "DJB8001793"})
            collected, status = self.service.collect(
                {"city_code": "25", "node_id": "DJB8001793"},
                header_idempotency_key="fresh-arrival-0001",
            )

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(cached["arrivals"][0]["arrival_seconds"], 420)
        self.assertEqual(status, 201)
        self.assertTrue(collected["created"])
        self.assertEqual(collected["snapshot"]["arrivals"][0]["arrival_seconds"], 9999)

    def test_fifty_simultaneous_arrival_misses_fetch_upstream_once(self) -> None:
        from app import fetch_arrivals as real_fetch

        barrier = threading.Barrier(50)
        counter_lock = threading.Lock()
        calls = 0

        def slow_fetch(**kwargs):
            nonlocal calls
            with counter_lock:
                calls += 1
            time.sleep(0.05)
            return real_fetch(**kwargs)

        def request_arrivals(_index):
            barrier.wait(timeout=3)
            return self.service.arrivals({"city_code": "25", "node_id": "DJB8001793"})

        with patch("app.fetch_arrivals", side_effect=slow_fetch):
            with ThreadPoolExecutor(max_workers=50) as executor:
                results = list(executor.map(request_arrivals, range(50)))

        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 50)
        self.assertTrue(all(len(result["arrivals"]) == 2 for result in results))

    def test_fifty_simultaneous_arrival_failures_share_one_upstream_attempt(self) -> None:
        barrier = threading.Barrier(50)
        counter_lock = threading.Lock()
        calls = 0

        def failing_fetch(**_kwargs):
            nonlocal calls
            with counter_lock:
                calls += 1
            time.sleep(0.05)
            raise TagoError("TEST_UPSTREAM_FAILURE", "fixture failure")

        def request_arrivals(_index):
            barrier.wait(timeout=3)
            try:
                self.service.arrivals({"city_code": "25", "node_id": "DJB8001793"})
            except AppError as exc:
                return exc.code
            return "unexpected-success"

        with patch("app.fetch_arrivals", side_effect=failing_fetch):
            with ThreadPoolExecutor(max_workers=50) as executor:
                results = list(executor.map(request_arrivals, range(50)))

        self.assertEqual(calls, 1)
        self.assertEqual(results, ["TEST_UPSTREAM_FAILURE"] * 50)

    def test_fifty_simultaneous_collects_fetch_once_and_create_one_snapshot(self) -> None:
        from app import fetch_arrivals as real_fetch

        barrier = threading.Barrier(50)
        counter_lock = threading.Lock()
        calls = 0

        def slow_fetch(**kwargs):
            nonlocal calls
            with counter_lock:
                calls += 1
            time.sleep(0.05)
            return real_fetch(**kwargs)

        def collect(_index):
            barrier.wait(timeout=3)
            return self.service.collect({"city_code": "25", "node_id": "DJB8001793"})

        with patch("app.fetch_arrivals", side_effect=slow_fetch):
            with ThreadPoolExecutor(max_workers=50) as executor:
                results = list(executor.map(collect, range(50)))

        self.assertEqual(calls, 1)
        self.assertEqual(self.service.status()["storage"]["snapshots"], 1)
        self.assertEqual(sum(1 for payload, status in results if status == 201 and payload["created"]), 1)
        self.assertEqual(sum(1 for payload, status in results if status == 200 and not payload["created"]), 49)

    def test_two_hundred_distinct_collect_writes_are_serialized_without_lock_errors(self) -> None:
        barrier = threading.Barrier(200)

        def collect(index):
            barrier.wait(timeout=10)
            return self.service.collect(
                {"city_code": "25", "node_id": f"LOAD{index:04d}"},
                header_idempotency_key=f"distinct-load-{index:04d}",
            )

        with ThreadPoolExecutor(max_workers=200) as executor:
            results = list(executor.map(collect, range(200)))

        self.assertEqual(len(results), 200)
        self.assertTrue(all(status == 201 and payload["created"] for payload, status in results))
        self.assertEqual(self.service.status()["storage"]["snapshots"], 200)

    def test_live_mode_without_key_is_clear_503(self) -> None:
        live = BusroService(
            replace(
                self.service.settings,
                fixture_mode=False,
                tago_service_key=None,
                db_path=Path(self.temp.name) / "live.sqlite3",
            ),
            clock=lambda: FIXED_NOW,
        )
        with self.assertRaises(AppError) as raised:
            live.arrivals({"city_code": "25", "node_id": "DJB8001793"})
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.code, "TAGO_KEY_REQUIRED")
        with self.assertRaises(AppError) as position_error:
            live.positions({"city_code": "25", "route_id": "DJB30300052"})
        self.assertEqual(position_error.exception.status, 503)
        self.assertEqual(position_error.exception.code, "TAGO_KEY_REQUIRED")

    def test_collect_is_idempotent_and_history_is_filterable(self) -> None:
        body = {"city_code": "25", "node_id": "DJB8001793"}
        first, first_status = self.service.collect(body, header_idempotency_key="collect-test-0001")
        second, second_status = self.service.collect(body, header_idempotency_key="collect-test-0001")
        self.assertEqual(first_status, 201)
        self.assertEqual(second_status, 200)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["snapshot"]["snapshot_id"], second["snapshot"]["snapshot_id"])
        self.assertEqual(self.service.status()["storage"]["snapshots"], 1)

        history = self.service.history(
            {"route_id": "DJB30300002", "from": "2026-08-31", "to": "2026-08-31"}
        )
        self.assertEqual(history["count"], 1)
        self.assertEqual(len(history["snapshots"][0]["arrivals"]), 1)
        self.assertEqual(history["snapshots"][0]["arrivals"][0]["route_id"], "DJB30300002")

    def test_idempotency_key_cannot_be_reused_for_another_request(self) -> None:
        self.service.collect(
            {"city_code": "25", "node_id": "DJB8001793"},
            header_idempotency_key="collect-test-0002",
        )
        with self.assertRaises(AppError) as raised:
            self.service.collect(
                {"city_code": "26", "node_id": "DJB8001793"},
                header_idempotency_key="collect-test-0002",
            )
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_daily_simulation_is_seeded_and_has_stable_schema(self) -> None:
        request = {
            "route": {"id": "B", "name": "조치원-구포 안전 경로"},
            "legs": [
                {
                    "id": "transfer-daejeon",
                    "route_id": "DJB30300002",
                    "node_id": "DJB8001793",
                    "scheduled_arrival": "09:10",
                    "next_departure": "09:28",
                    "minimum_transfer_minutes": 5,
                },
                {
                    "id": "transfer-yeongcheon",
                    "route_id": "DEMO55",
                    "scheduled_arrival": "19:05",
                    "next_departure": "19:24",
                    "minimum_transfer_minutes": 5,
                },
            ],
            "dates": {"from": "2026-09-01", "to": "2026-09-03"},
            "trials": 500,
            "seed": 7,
        }
        first = self.service.simulate(request)
        second = self.service.simulate(request)
        self.assertEqual(first, second)
        self.assertEqual(len(first["daily"]), 3)
        self.assertEqual(first["summary"]["days"], 3)
        self.assertIn(first["daily"][0]["status"], {"success", "failure"})
        self.assertIn("success_probability", first["daily"][0])
        self.assertTrue(all(leg["source"] == "fixture_model" for leg in first["basis"]["legs"]))

    def test_live_simulation_requires_reconstructed_passage_history(self) -> None:
        live = BusroService(
            replace(
                self.service.settings,
                fixture_mode=False,
                tago_service_key="not-used-by-simulation",
                db_path=Path(self.temp.name) / "empty-live.sqlite3",
            ),
            clock=lambda: FIXED_NOW,
        )
        with self.assertRaises(AppError) as raised:
            live.simulate(
                {
                    "route": "B",
                    "legs": [
                        {
                            "id": "xfer",
                            "route_id": "DJB30300002",
                            "scheduled_arrival": "09:10",
                            "next_departure": "09:30",
                            "fallback_delay_minutes": [0, 1, 2, 3, 4, 5, 6, 7],
                        }
                    ],
                    "dates": ["2026-09-01"],
                    "trials": 100,
                }
            )
        self.assertEqual(raised.exception.status, 422)
        self.assertEqual(raised.exception.code, "PASSAGE_HISTORY_REQUIRED")

    def test_eta_projection_diagnostic_compares_schedule_in_asia_seoul(self) -> None:
        arrival = {
            "node_id": "DJB8001793",
            "node_name": "북대전농협",
            "route_id": "DJB30300002",
            "route_no": "5",
            "route_type": "마을버스",
            "arrival_seconds": 600,
            "remaining_stops": 3,
            "vehicle_type": "저상버스",
        }
        self.service.store.create_snapshot(
            snapshot_id="snap_kst_diagnostic",
            idempotency_key="kst-diagnostic-0001",
            request_hash="request-hash",
            payload_hash="payload-hash",
            source="TEST",
            city_code="25",
            node_id="DJB8001793",
            captured_at="2026-08-31T00:00:00Z",
            upstream={"result_code": "00"},
            arrivals=[arrival],
        )
        leg = self.service._simulation_leg(
            {
                "id": "kst-leg",
                "route_id": "DJB30300002",
                "node_id": "DJB8001793",
                "scheduled_arrival": "09:10",
                "next_departure": "09:30",
            },
            0,
        )
        self.assertEqual(self.service._historical_delay_samples(leg), [0.0])

    def test_input_validation_rejects_bad_node(self) -> None:
        with self.assertRaises(AppError) as raised:
            self.service.arrivals({"city_code": "25", "node_id": "../secret"})
        self.assertEqual(raised.exception.code, "INVALID_NODE_ID")

    def test_positions_normalize_official_fixture_fields(self) -> None:
        result = self.service.positions(
            {"city_code": "25", "route_id": "DJB30300052"}
        )
        self.assertEqual(result["source"], "TAGO_POSITION_FIXTURE")
        self.assertEqual(result["positions"][0]["vehicle_no"], "대전99가9999")
        self.assertEqual(result["positions"][0]["route_id"], "DJB30300052")
        self.assertEqual(result["positions"][0]["node_order"], 1)

    def test_position_client_uses_fixed_official_host_and_encodes_decoded_key_once(self) -> None:
        response_body = self.position_payload(1, "DJB8005621", "정류장1")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(response_body).encode("utf-8")

        with patch("tago.urlopen", return_value=FakeResponse()) as opened:
            fetch_positions(
                city_code="25",
                route_id="DJB30300052",
                service_key="abc+def/ghi=",
                timeout_seconds=3,
                fixture_mode=False,
                fixture_path=SERVICE_DIR / "fixtures" / "tago_positions.json",
            )
        request = opened.call_args.args[0]
        self.assertTrue(request.full_url.startswith(POSITIONS_URL + "?serviceKey="))
        self.assertIn("serviceKey=abc%2Bdef%2Fghi%3D", request.full_url)
        self.assertIn("cityCode=25", request.full_url)
        self.assertIn("routeId=DJB30300052", request.full_url)
        self.assertNotIn("abc+def/ghi=", request.full_url)

    def test_two_position_polls_reconstruct_passage_and_replay_excludes_gap_day(self) -> None:
        now = [datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)]
        service = BusroService(self.service.settings, clock=lambda: now[0])
        first = self.position_payload(1, "DJB8005621", "대전역동광장종점")
        second = self.position_payload(2, "DJB8005622", "대전역")
        with patch("app.fetch_positions", side_effect=[first, second]):
            initial, initial_status = service.collect_positions(
                {"city_code": "25", "route_id": "DJB30300052"},
                header_idempotency_key="position-poll-0001",
            )
            now[0] += timedelta(minutes=1)
            moved, moved_status = service.collect_positions(
                {"city_code": "25", "route_id": "DJB30300052"},
                header_idempotency_key="position-poll-0002",
            )

        self.assertEqual(initial_status, 201)
        self.assertEqual(initial["passages"], [])
        self.assertEqual(moved_status, 201)
        self.assertEqual(moved["reconstruction"]["passage_count"], 1)
        event = moved["passages"][0]
        self.assertEqual(event["status"], "PASSAGE")
        self.assertEqual(event["node_order"], 2)
        self.assertEqual(event["service_date"], "2026-08-31")
        self.assertEqual(event["precision"], "polling_window")
        self.assertEqual(event["observed_from"], "2026-08-31T03:00:00Z")
        self.assertEqual(event["observed_to"], "2026-08-31T03:01:00Z")

        restarted = BusroService(self.service.settings, clock=lambda: now[0])
        self.assertEqual(restarted.status()["storage"]["position_snapshots"], 2)
        self.assertEqual(restarted.status()["storage"]["passages"], 1)
        history = restarted.passage_history(
            {"route_id": "DJB30300052", "from": "2026-08-31", "to": "2026-08-31"}
        )
        self.assertEqual(history["count"], 1)
        replay = restarted.replay(
            {
                "route": "route-B",
                "legs": [
                    {
                        "id": "daejeon-transfer",
                        "route_id": "DJB30300052",
                        "node_id": "DJB8005622",
                        "node_order": 2,
                        "scheduled_arrival": "12:01",
                        "next_departure": "12:10",
                        "minimum_transfer_minutes": 5,
                    }
                ],
                "dates": ["2026-08-31", "2026-09-01"],
            }
        )
        self.assertEqual(replay["daily"][0]["status"], "success")
        self.assertEqual(replay["daily"][1]["status"], "data_gap")
        self.assertEqual(replay["summary"]["eligible_days"], 1)
        self.assertEqual(replay["summary"]["gap_days"], 1)
        self.assertEqual(replay["summary"]["success_rate"], 1.0)

    def test_jump_regression_and_uncertain_polling_window_are_preserved(self) -> None:
        now = [datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)]
        service = BusroService(self.service.settings, clock=lambda: now[0])
        payloads = [
            self.position_payload(1, "DJB8005621", "정류장1"),
            self.position_payload(3, "DJB8005623", "정류장3"),
            self.position_payload(2, "DJB8005622", "정류장2"),
        ]
        with patch("app.fetch_positions", side_effect=payloads):
            for index in range(3):
                service.collect_positions(
                    {"city_code": "25", "route_id": "DJB30300052"},
                    header_idempotency_key=f"position-anomaly-{index:04d}",
                )
                now[0] += timedelta(minutes=1)
        history = service.passage_history({"route_id": "DJB30300052"})
        by_status = {item["status"]: item for item in history["passages"]}
        self.assertEqual(by_status["DATA_GAP"]["gap_reason"], "NODE_ORDER_JUMP")
        self.assertEqual(by_status["REGRESSION"]["gap_reason"], "NODE_ORDER_REGRESSION")

        # A sequential window crossing the connection cutoff is uncertain, not failure.
        isolated_db = Path(self.temp.name) / "uncertain.sqlite3"
        isolated = BusroService(replace(self.service.settings, db_path=isolated_db), clock=lambda: now[0])
        now[0] = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)
        with patch(
            "app.fetch_positions",
            side_effect=[
                self.position_payload(1, "DJB8005621", "정류장1"),
                self.position_payload(2, "DJB8005622", "정류장2"),
            ],
        ):
            isolated.collect_positions(
                {"city_code": "25", "route_id": "DJB30300052"},
                header_idempotency_key="position-window-0001",
            )
            now[0] += timedelta(minutes=1)
            isolated.collect_positions(
                {"city_code": "25", "route_id": "DJB30300052"},
                header_idempotency_key="position-window-0002",
            )
        replay = isolated.replay(
            {
                "route": "route-window",
                "legs": [
                    {
                        "id": "window-leg",
                        "route_id": "DJB30300052",
                        "node_id": "DJB8005622",
                        "node_order": 2,
                        "scheduled_arrival": "12:00",
                        "next_departure": "12:05",
                        "minimum_transfer_minutes": 5,
                    }
                ],
                "dates": ["2026-08-31"],
            }
        )
        self.assertEqual(replay["daily"][0]["status"], "data_gap")
        self.assertEqual(
            replay["daily"][0]["legs"][0]["reason"],
            "PASSAGE_WINDOW_CROSSES_DEADLINE",
        )
        self.assertEqual(replay["summary"]["eligible_days"], 0)

    def test_position_service_date_and_filters_use_kst(self) -> None:
        now = [datetime(2026, 8, 31, 14, 59, tzinfo=timezone.utc)]
        service = BusroService(self.service.settings, clock=lambda: now[0])
        with patch(
            "app.fetch_positions",
            side_effect=[
                self.position_payload(1, "DJB8005621", "정류장1"),
                self.position_payload(2, "DJB8005622", "정류장2"),
            ],
        ):
            service.collect_positions(
                {"city_code": "25", "route_id": "DJB30300052"},
                header_idempotency_key="position-kst-0001",
            )
            now[0] += timedelta(minutes=2)
            result, _status = service.collect_positions(
                {"city_code": "25", "route_id": "DJB30300052"},
                header_idempotency_key="position-kst-0002",
            )
        self.assertEqual(result["passages"][0]["service_date"], "2026-09-01")
        history = service.passage_history(
            {"from": "2026-08-31T15:01:00Z", "to": "2026-08-31T15:01:00Z"}
        )
        self.assertEqual(history["filters"]["from"], "2026-09-01")
        self.assertEqual(history["count"], 1)

    def test_position_collect_is_idempotent_singleflight_and_conflict_safe(self) -> None:
        barrier = threading.Barrier(30)
        calls = 0
        lock = threading.Lock()

        def slow_fetch(**_kwargs):
            nonlocal calls
            with lock:
                calls += 1
            time.sleep(0.03)
            return self.position_payload(1, "DJB8005621", "정류장1")

        def collect(_index):
            barrier.wait(timeout=3)
            return self.service.collect_positions(
                {"city_code": "25", "route_id": "DJB30300052"},
                header_idempotency_key="position-singleflight-0001",
            )

        with patch("app.fetch_positions", side_effect=slow_fetch):
            with ThreadPoolExecutor(max_workers=30) as executor:
                responses = list(executor.map(collect, range(30)))
        self.assertEqual(calls, 1)
        self.assertEqual(sum(status == 201 for _payload, status in responses), 1)
        self.assertEqual(sum(status == 200 for _payload, status in responses), 29)
        with self.assertRaises(AppError) as raised:
            self.service.collect_positions(
                {"city_code": "26", "route_id": "DJB30300052"},
                header_idempotency_key="position-singleflight-0001",
            )
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_replay_cpu_workload_limit_is_enforced(self) -> None:
        legs = [
            {
                "id": f"leg-{index}",
                "route_id": f"ROUTE{index}",
                "node_id": f"NODE{index:02d}",
                "node_order": index + 1,
                "scheduled_arrival": "12:00",
                "next_departure": "12:10",
            }
            for index in range(12)
        ]
        with self.assertRaises(AppError) as raised:
            self.service.replay(
                {
                    "route": "too-large",
                    "legs": legs,
                    "dates": {"from": "2026-08-01", "to": "2026-08-31"},
                }
            )
        self.assertEqual(raised.exception.status, 413)
        self.assertEqual(raised.exception.code, "REPLAY_TOO_LARGE")


class HTTPCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        settings = Settings(
            fixture_mode=True,
            db_path=Path(self.temp.name) / "http.sqlite3",
            network_catalog_path=Path(self.temp.name) / "http-network-catalog.sqlite3",
            fixture_path=SERVICE_DIR / "fixtures" / "tago_arrivals.json",
            fixture_delays_path=SERVICE_DIR / "fixtures" / "delay_samples.json",
            allowed_origins=("http://127.0.0.1:8290",),
            max_body_bytes=1024,
        )
        service = BusroService(settings, clock=lambda: FIXED_NOW)
        self.server = BusroHTTPServer(("127.0.0.1", 0), Handler, service=service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, *, body=None, headers=None, timeout=3):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8")) if response.status != 204 else None
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, data, response_headers

    def test_required_endpoints_and_cors(self) -> None:
        self.assertEqual(self.server.request_queue_size, 256)
        self.assertEqual(self.server.max_concurrent_requests, 200)
        self.assertEqual(self.server.request_timeout_seconds, 10.0)
        status, payload, headers = self.request(
            "GET", "/api/status", headers={"Origin": "http://127.0.0.1:8290"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://127.0.0.1:8290")

        status, payload, _ = self.request(
            "GET", "/api/arrivals?city_code=25&node_id=DJB8001793"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["arrivals"]), 2)

        status, payload, _ = self.request(
            "POST",
            "/api/collect",
            body={"city_code": "25", "node_id": "DJB8001793"},
            headers={"Idempotency-Key": "http-collect-0001"},
        )
        self.assertEqual(status, 201)
        self.assertTrue(payload["created"])

        status, payload, _ = self.request(
            "GET", "/api/history?route_id=DJB30300002&from=2026-08-31&to=2026-08-31"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)

        status, payload, _ = self.request(
            "POST",
            "/api/simulate",
            body={
                "route": "B",
                "legs": [
                    {
                        "id": "http-transfer",
                        "route_id": "DJB30300002",
                        "scheduled_arrival": "09:10",
                        "next_departure": "09:28",
                        "minimum_transfer_minutes": 5,
                    }
                ],
                "dates": ["2026-09-01", "2026-09-02"],
                "trials": 100,
                "seed": 9,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["daily"]), 2)
        self.assertEqual(payload["summary"]["days"], 2)

        status, payload, _ = self.request(
            "GET", "/api/positions?city_code=25&route_id=DJB30300052"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["positions"][0]["vehicle_no"], "대전99가9999")

        status, payload, _ = self.request(
            "POST",
            "/api/positions/collect",
            body={"city_code": "25", "route_id": "DJB30300052"},
            headers={"Idempotency-Key": "http-position-0001"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["snapshot"]["observation_kind"], "vehicle_position")

        status, payload, _ = self.request(
            "GET", "/api/passages?route_id=DJB30300052&from=2026-08-31"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 0)

        status, payload, _ = self.request(
            "POST",
            "/api/replay",
            body={
                "route": "route-http",
                "legs": [
                    {
                        "id": "http-position-leg",
                        "route_id": "DJB30300052",
                        "node_id": "DJB8005622",
                        "node_order": 2,
                        "scheduled_arrival": "12:00",
                        "next_departure": "12:10",
                    }
                ],
                "dates": ["2026-08-31"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["daily"][0]["status"], "data_gap")
        self.assertEqual(payload["summary"]["eligible_days"], 0)

    def test_server_rejects_unbounded_concurrency_and_socket_timeouts(self) -> None:
        with self.assertRaises(ValueError):
            BusroHTTPServer(
                ("127.0.0.1", 0),
                Handler,
                service=self.server.service,
                max_concurrent_requests=201,
            )
        with self.assertRaises(ValueError):
            BusroHTTPServer(
                ("127.0.0.1", 0),
                Handler,
                service=self.server.service,
                request_timeout_seconds=31,
            )

    def test_server_never_runs_more_than_configured_active_handlers(self) -> None:
        class SlowHandler(BaseHTTPRequestHandler):
            active = 0
            peak = 0
            lock = threading.Lock()

            def do_GET(self):
                with self.lock:
                    type(self).active += 1
                    type(self).peak = max(type(self).peak, type(self).active)
                try:
                    time.sleep(0.1)
                    self.send_response(204)
                    self.end_headers()
                finally:
                    with self.lock:
                        type(self).active -= 1

            def log_message(self, _format, *_args):
                return

        limited = BusroHTTPServer(
            ("127.0.0.1", 0),
            SlowHandler,
            service=self.server.service,
            max_concurrent_requests=2,
        )
        thread = threading.Thread(target=limited.serve_forever, daemon=True)
        thread.start()
        port = limited.server_address[1]

        def request(_index):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                response.read()
                return response.status
            finally:
                connection.close()

        try:
            with ThreadPoolExecutor(max_workers=6) as executor:
                statuses = list(executor.map(request, range(6)))
        finally:
            limited.shutdown()
            limited.server_close()
            thread.join(timeout=2)

        self.assertEqual(statuses, [204] * 6)
        self.assertEqual(SlowHandler.peak, 2)

    def test_production_compiled_assets_are_served(self) -> None:
        for name in (
            "components.compiled.js",
            "nationwide.compiled.js",
            "screens.compiled.js",
            "app.compiled.js",
        ):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            connection.request("GET", f"/{name}")
            response = connection.getresponse()
            body = response.read()
            headers = dict(response.getheaders())
            connection.close()

            self.assertEqual(response.status, 200, name)
            self.assertIn("text/javascript", headers["Content-Type"])
            self.assertGreater(len(body), 100, name)

    def test_disconnected_client_does_not_raise_or_retry_response_write(self) -> None:
        class DisconnectedWriter:
            def write(self, _payload):
                raise ConnectionAbortedError("client closed the connection")

        handler = object.__new__(Handler)
        handler.wfile = DisconnectedWriter()
        handler.close_connection = False

        handler._write_response(b"already-computed-response")

        self.assertTrue(handler.close_connection)

    def test_fifty_http_collects_are_singleflight_and_one_snapshot(self) -> None:
        from app import fetch_arrivals as real_fetch

        barrier = threading.Barrier(50)
        counter_lock = threading.Lock()
        calls = 0

        def slow_fetch(**kwargs):
            nonlocal calls
            with counter_lock:
                calls += 1
            time.sleep(0.05)
            return real_fetch(**kwargs)

        def collect(_index):
            barrier.wait(timeout=3)
            return self.request(
                "POST",
                "/api/collect",
                body={"city_code": "25", "node_id": "DJB8001793"},
            )[:2]

        with patch("app.fetch_arrivals", side_effect=slow_fetch):
            with ThreadPoolExecutor(max_workers=50) as executor:
                responses = list(executor.map(collect, range(50)))

        self.assertEqual(calls, 1)
        self.assertEqual(self.server.service.store.counts()["snapshots"], 1)
        self.assertEqual(sum(1 for status, payload in responses if status == 201 and payload["created"]), 1)
        self.assertEqual(sum(1 for status, payload in responses if status == 200 and not payload["created"]), 49)

    def test_two_hundred_distinct_http_collects_complete_without_database_lock(self) -> None:
        barrier = threading.Barrier(200)

        def collect(index):
            barrier.wait(timeout=10)
            return self.request(
                "POST",
                "/api/collect",
                body={"city_code": "25", "node_id": f"HTTPLOAD{index:04d}"},
                headers={"Idempotency-Key": f"http-distinct-{index:04d}"},
                timeout=20,
            )[:2]

        with patch("builtins.print"):
            with ThreadPoolExecutor(max_workers=200) as executor:
                responses = list(executor.map(collect, range(200)))

        self.assertEqual(len(responses), 200)
        self.assertTrue(all(status == 201 and payload["created"] for status, payload in responses))
        self.assertEqual(self.server.service.store.counts()["snapshots"], 200)

    def test_unlisted_origin_is_rejected(self) -> None:
        status, payload, _ = self.request(
            "GET", "/api/status", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "ORIGIN_NOT_ALLOWED")

    def test_json_body_limit_is_enforced(self) -> None:
        status, payload, _ = self.request(
            "POST",
            "/api/collect",
            body={"city_code": "25", "node_id": "DJB8001793", "padding": "x" * 1100},
        )
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "BODY_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
