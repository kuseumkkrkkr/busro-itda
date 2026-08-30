from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
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
from db import Store  # noqa: E402
from server import BusroHTTPServer, Handler  # noqa: E402
from tago import POSITIONS_URL, TagoError, fetch_positions  # noqa: E402


FIXED_NOW = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)


class SlowDripResponse:
    """Keep ``read()`` active while making progress below an inactivity timeout."""

    def __init__(self, entered: threading.Event, release: threading.Event, exited: threading.Event):
        self.entered = entered
        self.release = release
        self.exited = exited
        self.drips = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        self.entered.set()
        try:
            while not self.release.wait(timeout=0.005):
                self.drips += 1
            return b'{"elements": []}'[:limit]
        finally:
            self.exited.set()


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

    def test_guard_settings_are_bounded_and_operator_token_is_validated(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BUSRO_TAGO_MAX_CONCURRENT_CALLS": "999",
                "BUSRO_TAGO_ADMISSION_TIMEOUT_SECONDS": "-4",
                "BUSRO_TAGO_DAILY_CALL_BUDGET": "999999",
                "BUSRO_OPERATOR_TOKEN": "0123456789abcdef",
            },
            clear=True,
        ):
            settings = Settings.from_env(fixture_override=True)
        self.assertEqual(settings.tago_max_concurrent_calls, 32)
        self.assertEqual(settings.tago_admission_timeout_seconds, 0.01)
        self.assertEqual(settings.tago_daily_call_budget, 100_000)
        self.assertEqual(settings.operator_token, "0123456789abcdef")

        with patch.dict(os.environ, {"BUSRO_OPERATOR_TOKEN": "too-short"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env(fixture_override=True)

    def test_daily_tago_attempt_reservation_is_atomic(self) -> None:
        stores = (self.service.store, Store(self.service.settings.db_path))

        def reserve(index):
            return stores[index % len(stores)].reserve_tago_attempt(
                service_date="2026-09-01",
                attempted_at="2026-08-31T15:00:00Z",
                daily_limit=7,
            )

        with ThreadPoolExecutor(max_workers=50) as executor:
            reservations = list(executor.map(reserve, range(50)))

        self.assertEqual(sum(1 for allowed, _count in reservations if allowed), 7)
        self.assertEqual(self.service.store.tago_attempt_count("2026-09-01"), 7)

    def test_live_tago_budget_counts_only_singleflight_leader_and_not_cache_hits(self) -> None:
        live = BusroService(
            replace(
                self.service.settings,
                fixture_mode=False,
                tago_service_key="unit-test-key",
                tago_daily_call_budget=1,
                db_path=Path(self.temp.name) / "live-budget.sqlite3",
            ),
            clock=lambda: FIXED_NOW,
        )
        upstream = json.loads(
            self.service.settings.fixture_path.read_text(encoding="utf-8")
        )
        barrier = threading.Barrier(20)
        calls = 0
        calls_lock = threading.Lock()

        def slow_fetch(**_kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return upstream

        def request(_index):
            barrier.wait(timeout=3)
            return live.arrivals({"city_code": "25", "node_id": "DJB8001793"})

        with patch("app.fetch_arrivals", side_effect=slow_fetch):
            with ThreadPoolExecutor(max_workers=20) as executor:
                results = list(executor.map(request, range(20)))
            cached = live.arrivals({"city_code": "25", "node_id": "DJB8001793"})
            with self.assertRaises(AppError) as exhausted:
                live.arrivals({"city_code": "25", "node_id": "DJB8001794"})

        self.assertEqual(calls, 1)
        self.assertEqual(live.store.tago_attempt_count("2026-08-31"), 1)
        self.assertTrue(cached["cached"])
        self.assertEqual(len(results), 20)
        self.assertEqual(exhausted.exception.status, 429)
        self.assertEqual(exhausted.exception.code, "TAGO_DAILY_BUDGET_EXHAUSTED")
        self.assertEqual(exhausted.exception.details["retry_after_seconds"], 43_200)
        self.assertEqual(
            exhausted.exception.details["resets_at"],
            "2026-09-01T00:00:00+09:00",
        )

    def test_osm_slow_drip_obeys_hard_deadline_and_releases_singleflight(self) -> None:
        body = {
            "route_ref": "601",
            "stops": [
                {"latitude": 36.601, "longitude": 127.298},
                {"latitude": 36.565, "longitude": 127.315},
            ],
            "allow_road_estimate": False,
        }
        entered = threading.Event()
        release = threading.Event()
        exited = threading.Event()
        response = SlowDripResponse(entered, release, exited)
        geometry_gate = threading.BoundedSemaphore(1)
        upstream_gate = threading.BoundedSemaphore(1)

        def request_geometry(request_body=body) -> AppError:
            try:
                self.service.route_geometry(request_body)
            except AppError as exc:
                return exc
            self.fail("slow-drip geometry request unexpectedly succeeded")

        started = time.monotonic()
        try:
            with (
                patch("osm.MAX_RESOLVE_TIMEOUT_SECONDS", 0.08),
                patch("osm.GEOMETRY_ADMISSION_WAIT_SECONDS", 0.03),
                patch("osm._GEOMETRY_ADMISSION", geometry_gate),
                patch("osm._UPSTREAM_HTTP_ADMISSION", upstream_gate),
                patch("osm.urlopen", return_value=response) as opened,
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                leader = executor.submit(request_geometry)
                self.assertTrue(entered.wait(timeout=0.5))
                follower = executor.submit(request_geometry)
                errors = [leader.result(timeout=0.5), follower.result(timeout=0.5)]
                distinct_started = time.monotonic()
                distinct_error = request_geometry({**body, "route_ref": "602"})
                distinct_elapsed = time.monotonic() - distinct_started

            elapsed = time.monotonic() - started
            self.assertTrue(all(error.code == "OSM_DEADLINE_EXCEEDED" for error in errors))
            self.assertTrue(all(error.status == 504 for error in errors))
            self.assertEqual(distinct_error.code, "OSM_BUSY")
            self.assertEqual(distinct_error.status, 429)
            self.assertLess(distinct_elapsed, 0.15)
            self.assertLess(elapsed, 0.45)
            self.assertEqual(opened.call_count, 1)
            self.assertGreater(response.drips, 1)
            self.assertEqual(self.service._singleflight._entries, {})
            self.assertTrue(geometry_gate.acquire(blocking=False))
            geometry_gate.release()
            self.assertFalse(upstream_gate.acquire(blocking=False))
        finally:
            release.set()
            self.assertTrue(exited.wait(timeout=0.5))

        self.assertTrue(upstream_gate.acquire(timeout=0.5))
        upstream_gate.release()

    def test_tago_budget_rolls_over_at_kst_midnight(self) -> None:
        now = [datetime(2026, 8, 31, 14, 59, tzinfo=timezone.utc)]
        live = BusroService(
            replace(
                self.service.settings,
                fixture_mode=False,
                tago_service_key="unit-test-key",
                tago_daily_call_budget=1,
                db_path=Path(self.temp.name) / "kst-budget.sqlite3",
            ),
            clock=lambda: now[0],
        )
        upstream = json.loads(
            self.service.settings.fixture_path.read_text(encoding="utf-8")
        )
        with patch("app.fetch_arrivals", return_value=upstream) as fetched:
            live.arrivals({"city_code": "25", "node_id": "DJB8001793"})
            with self.assertRaises(AppError):
                live.arrivals({"city_code": "25", "node_id": "DJB8001794"})
            now[0] = datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)
            live.arrivals({"city_code": "25", "node_id": "DJB8001795"})

        self.assertEqual(fetched.call_count, 2)
        self.assertEqual(live.store.tago_attempt_count("2026-08-31"), 1)
        self.assertEqual(live.store.tago_attempt_count("2026-09-01"), 1)

    def test_failed_live_tago_fetch_still_consumes_one_attempt(self) -> None:
        live = BusroService(
            replace(
                self.service.settings,
                fixture_mode=False,
                tago_service_key="unit-test-key",
                db_path=Path(self.temp.name) / "failed-attempt.sqlite3",
            ),
            clock=lambda: FIXED_NOW,
        )
        with patch("app.fetch_arrivals", side_effect=TagoError("UPSTREAM", "failed")):
            with self.assertRaises(AppError):
                live.arrivals({"city_code": "25", "node_id": "DJB8001793"})
        self.assertEqual(live.store.tago_attempt_count("2026-08-31"), 1)

    def test_process_wide_tago_semaphore_rejects_a_second_service_quickly(self) -> None:
        first = BusroService(
            replace(
                self.service.settings,
                fixture_mode=False,
                tago_service_key="unit-test-key",
                tago_max_concurrent_calls=1,
                tago_admission_timeout_seconds=0.02,
                db_path=Path(self.temp.name) / "guard-first.sqlite3",
            ),
            clock=lambda: FIXED_NOW,
        )
        second = BusroService(
            replace(first.settings, db_path=Path(self.temp.name) / "guard-second.sqlite3"),
            clock=lambda: FIXED_NOW,
        )
        upstream = json.loads(
            self.service.settings.fixture_path.read_text(encoding="utf-8")
        )
        entered = threading.Event()
        release = threading.Event()

        def blocked_fetch(**_kwargs):
            entered.set()
            release.wait(timeout=2)
            return upstream

        with (
            patch("app._TAGO_UPSTREAM_SEMAPHORE", threading.BoundedSemaphore(1)),
            patch("app._TAGO_UPSTREAM_LIMIT", 1),
            patch("app.fetch_arrivals", side_effect=blocked_fetch),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(
                first.arrivals,
                {"city_code": "25", "node_id": "DJB8001793"},
            )
            self.assertTrue(entered.wait(timeout=1))
            try:
                with self.assertRaises(AppError) as busy:
                    second.arrivals({"city_code": "25", "node_id": "DJB8001794"})
            finally:
                release.set()
            self.assertTrue(future.result()["ok"])

        self.assertEqual(busy.exception.status, 429)
        self.assertEqual(busy.exception.code, "TAGO_UPSTREAM_BUSY")
        self.assertEqual(first.store.tago_attempt_count("2026-08-31"), 1)
        self.assertEqual(second.store.tago_attempt_count("2026-08-31"), 0)

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

    def test_live_replay_requires_registered_official_schedule_origin(self) -> None:
        live = BusroService(
            replace(
                self.service.settings,
                fixture_mode=False,
                tago_service_key="not-used-by-replay",
                db_path=Path(self.temp.name) / "live-replay.sqlite3",
            ),
            clock=lambda: FIXED_NOW,
        )
        leg = {
            "id": "official-source-leg",
            "route_id": "DJB30300052",
            "node_id": "DJB8005622",
            "node_order": 2,
            "scheduled_arrival": "12:01",
            "next_departure": "12:10",
            "minimum_transfer_minutes": 5,
        }

        with self.assertRaises(AppError) as missing:
            live.replay({"route": "route-source", "legs": [leg], "dates": ["2026-08-31"]})
        self.assertEqual(missing.exception.code, "VERIFIED_TIMETABLE_SOURCE_REQUIRED")
        self.assertEqual(missing.exception.status, 422)

        with self.assertRaises(AppError) as route_only:
            live.replay(
                {
                    "route": "route-source",
                    "legs": [{**leg, "time_evidence_source": "tago-routes"}],
                    "dates": ["2026-08-31"],
                }
            )
        self.assertEqual(route_only.exception.code, "UNVERIFIED_TIMETABLE_SOURCE")
        self.assertEqual(route_only.exception.details["required_status"], "VERIFIED_SCHEDULE_ORIGIN")

        with self.assertRaises(AppError) as registry_only:
            live.replay(
                {
                    "route": "route-source",
                    "legs": [{**leg, "time_evidence_source": "yeongdong-timetable"}],
                    "dates": ["2026-08-31"],
                }
            )
        self.assertEqual(registry_only.exception.status, 422)
        self.assertEqual(
            registry_only.exception.code, "OFFICIAL_SCHEDULE_RECORD_REQUIRED"
        )
        self.assertEqual(
            registry_only.exception.details["reason"],
            "NO_SERVER_SCHEDULE_STORE_FOR_SOURCE",
        )

        with self.assertRaises(AppError) as missing_metadata:
            live.replay(
                {
                    "route": "route-source",
                    "legs": [{**leg, "time_evidence_source": "ktdb-gtfs-2024"}],
                    "dates": ["2026-08-31"],
                }
            )
        self.assertEqual(missing_metadata.exception.status, 422)
        self.assertEqual(
            missing_metadata.exception.code, "OFFICIAL_SCHEDULE_RECORD_REQUIRED"
        )
        self.assertEqual(
            missing_metadata.exception.details["reason"],
            "LIVE_SCHEDULE_METADATA_REQUIRED",
        )
        self.assertIn(
            "next_route_id", missing_metadata.exception.details["missing_fields"]
        )

        complete_leg = {
            **leg,
            "time_evidence_source": "ktdb-gtfs-2024",
            "time_evidence_trip_id": "GTFS:KTDB:TARRIVAL000000000001",
            "next_route_id": "GTFS:KTDB:RNEXT0000000000001:PNEXT000000000000000000000000000000000001",
            "next_node_id": "GTFS:KTDB:SNEXT0000000000001",
            "next_node_order": 1,
            "next_time_evidence_trip_id": "GTFS:KTDB:TNEXT000000000000001",
        }
        with self.assertRaises(AppError) as missing_feed:
            live.replay(
                {
                    "route": "route-source",
                    "legs": [complete_leg],
                    "dates": ["2026-08-31"],
                }
            )
        self.assertEqual(missing_feed.exception.status, 422)
        self.assertEqual(
            missing_feed.exception.code, "OFFICIAL_SCHEDULE_RECORD_REQUIRED"
        )
        self.assertEqual(
            missing_feed.exception.details["reason"], "ACTIVE_GTFS_FEED_REQUIRED"
        )
        self.assertEqual(missing_feed.exception.details["phase"], "arrival")

    def test_live_replay_uses_two_bound_gtfs_records_instead_of_request_times(self) -> None:
        live = BusroService(
            replace(
                self.service.settings,
                fixture_mode=False,
                tago_service_key="not-used-by-replay",
                db_path=Path(self.temp.name) / "live-bound-schedule.sqlite3",
            ),
            clock=lambda: FIXED_NOW,
        )
        route_id = "GTFS:KTDB:R0123456789abcdef0123:P0123456789abcdef0123456789abcdef01234567"
        node_id = "GTFS:KTDB:S0123456789abcdef0123"
        trip_id = "GTFS:KTDB:T0123456789abcdef0123"
        next_route_id = "GTFS:KTDB:Rfedcba98765432100123:Pfedcba9876543210fedcba9876543210fedcba98"
        next_node_id = "GTFS:KTDB:Sfedcba98765432100123"
        next_trip_id = "GTFS:KTDB:Tfedcba98765432100123"
        arrival_evidence = {
            "data_gap": False,
            "basis": "OFFICIAL_STATIC_GTFS_RAW_EVIDENCE",
            "feed": {"feed_id": "official-feed-bound"},
            "trips": [
                {
                    "trip_namespace_id": trip_id,
                    "calendar": {"operates_on_date": True},
                    "stop_times": [
                        {
                            "stop_sequence": 2,
                            "node_id": node_id,
                            "arrival_time": "12:01:00",
                            "arrival_seconds": 12 * 3600 + 60,
                            "departure_time": "12:02:00",
                            "departure_seconds": 12 * 3600 + 2 * 60,
                        }
                    ],
                }
            ],
        }
        next_departure_evidence = {
            "data_gap": False,
            "basis": "OFFICIAL_STATIC_GTFS_RAW_EVIDENCE",
            "feed": {"feed_id": "official-feed-bound"},
            "trips": [
                {
                    "trip_namespace_id": next_trip_id,
                    "calendar": {"operates_on_date": True},
                    "stop_times": [
                        {
                            "stop_sequence": 1,
                            "node_id": next_node_id,
                            "arrival_time": "12:09:00",
                            "arrival_seconds": 12 * 3600 + 9 * 60,
                            "departure_time": "12:10:00",
                            "departure_seconds": 12 * 3600 + 10 * 60,
                        }
                    ],
                }
            ],
        }
        passage = {
            "passage_id": "passage-official-record",
            "city_code": "GTFS-KTDB",
            "route_id": route_id,
            "node_id": node_id,
            "vehicle_no": "TEST-1",
            "service_date": "2026-08-31",
            "observed_from": "2026-08-31T03:00:00Z",
            "observed_to": "2026-08-31T03:01:00Z",
            "from_node_order": 1,
            "node_order": 2,
            "node_order_delta": 1,
            "precision": "polling_window",
            "status": "PASSAGE",
        }
        request_body = {
            "route": "route-bound-record",
            "legs": [
                {
                    "id": "bound-record-leg",
                    "route_id": route_id,
                    "node_id": node_id,
                    "node_order": 2,
                    # These client values would miss by many hours if trusted.
                    "scheduled_arrival": "00:01",
                    "next_departure": "00:02",
                    "minimum_transfer_minutes": 5,
                    "time_evidence_source": "ktdb-gtfs-2024",
                    "time_evidence_trip_id": trip_id,
                    "next_route_id": next_route_id,
                    "next_node_id": next_node_id,
                    "next_node_order": 1,
                    "next_time_evidence_trip_id": next_trip_id,
                }
            ],
            "dates": ["2026-08-31"],
        }

        def schedule_evidence_lookup(**kwargs):
            if kwargs["graph_route_id"] == route_id:
                return arrival_evidence
            if kwargs["graph_route_id"] == next_route_id:
                return next_departure_evidence
            raise AssertionError(f"unexpected route lookup: {kwargs['graph_route_id']}")

        with (
            patch.object(
                live.network_catalog,
                "gtfs_schedule_evidence",
                side_effect=schedule_evidence_lookup,
            ) as schedule_lookup,
            patch.object(live.store, "replay_events", return_value=[passage]),
        ):
            replays = {
                minutes: live.replay(
                    {
                        **request_body,
                        "legs": [
                            {
                                **request_body["legs"][0],
                                "minimum_transfer_minutes": minutes,
                            }
                        ],
                    }
                )
                for minutes in (0, 60)
            }

        self.assertEqual(schedule_lookup.call_count, 4)
        self.assertEqual(
            [item.kwargs["graph_route_id"] for item in schedule_lookup.call_args_list],
            [route_id, next_route_id, route_id, next_route_id],
        )
        self.assertEqual(
            schedule_lookup.call_args_list[0].kwargs,
            {
                "provider": "KTDB",
                "graph_route_id": route_id,
                "service_date": "2026-08-31",
                "limit": 100,
            },
        )
        self.assertEqual(
            schedule_lookup.call_args_list[1].kwargs,
            {
                "provider": "KTDB",
                "graph_route_id": next_route_id,
                "service_date": "2026-08-31",
                "limit": 100,
            },
        )
        replay = replays[0]
        self.assertEqual(replay, replays[60])
        self.assertEqual(replay["daily"][0]["status"], "success")
        self.assertEqual(
            replay["basis"]["minimum_transfer_source"], "server_safety_policy"
        )
        self.assertEqual(replay["basis"]["minimum_transfer_minutes"], 5)
        self.assertEqual(
            replay["daily"][0]["legs"][0]["minimum_transfer_source"],
            "server_safety_policy",
        )
        self.assertEqual(
            replay["daily"][0]["legs"][0]["minimum_transfer_minutes"], 5
        )
        self.assertEqual(
            replay["daily"][0]["legs"][0]["schedule_records"]["arrival"]["trip_id"],
            trip_id,
        )
        self.assertEqual(
            replay["daily"][0]["legs"][0]["schedule_records"]["arrival"]["arrival_time"],
            "12:01:00",
        )
        self.assertEqual(
            replay["daily"][0]["legs"][0]["schedule_records"]["next_departure"]["trip_id"],
            next_trip_id,
        )
        self.assertEqual(
            replay["daily"][0]["legs"][0]["schedule_records"]["next_departure"]["departure_time"],
            "12:10:00",
        )
        self.assertEqual(
            replay["basis"]["schedule_evidence"], "server_gtfs_schedule_records"
        )
        self.assertEqual(replay["basis"]["schedule_value_scope"], "server_record_values_only")

        with patch.object(
            live.network_catalog,
            "gtfs_schedule_evidence",
            side_effect=[arrival_evidence, {**next_departure_evidence, "trips": []}],
        ):
            with self.assertRaises(AppError) as missing_next_record:
                live.replay(request_body)
        self.assertEqual(missing_next_record.exception.status, 422)
        self.assertEqual(
            missing_next_record.exception.code, "OFFICIAL_SCHEDULE_RECORD_REQUIRED"
        )
        self.assertEqual(
            missing_next_record.exception.details["phase"], "next_departure"
        )
        self.assertEqual(missing_next_record.exception.details["matching_records"], 0)

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

    def test_operator_endpoints_require_token_off_loopback_and_when_configured(self) -> None:
        protected_paths = (
            "/api/collect",
            "/api/positions/collect",
            "/api/mappings/validate",
            "/api/network/hydrate",
        )
        with patch.object(Handler, "_client_is_loopback", return_value=False):
            for path in protected_paths:
                with self.subTest(path=path):
                    status, payload, _ = self.request("POST", path, body={})
                    self.assertEqual(status, 403)
                    self.assertEqual(payload["error"]["code"], "OPERATOR_AUTH_REQUIRED")

            operator_token = "0123456789abcdef"
            self.server.service.settings = replace(
                self.server.service.settings,
                operator_token=operator_token,
            )
            denied_status, denied_payload, _ = self.request(
                "POST",
                "/api/collect",
                body={"city_code": "25", "node_id": "DJB8001793"},
                headers={"Authorization": "Bearer incorrect-token"},
            )
            allowed_status, allowed_payload, _ = self.request(
                "POST",
                "/api/collect",
                body={"city_code": "25", "node_id": "DJB8001793"},
                headers={
                    "Authorization": f"Bearer {operator_token}",
                    "Idempotency-Key": "operator-collect-0001",
                },
            )
            options_status, _options_payload, options_headers = self.request(
                "OPTIONS",
                "/api/collect",
                headers={"Origin": "http://127.0.0.1:8290"},
            )

        with patch.object(Handler, "_client_is_loopback", return_value=True):
            loopback_missing_status, loopback_missing_payload, _ = self.request(
                "POST",
                "/api/collect",
                body={"city_code": "25", "node_id": "DJB8001794"},
            )
            loopback_wrong_status, loopback_wrong_payload, _ = self.request(
                "POST",
                "/api/collect",
                body={"city_code": "25", "node_id": "DJB8001794"},
                headers={"X-Busro-Operator-Token": "incorrect-token"},
            )
            loopback_allowed_status, loopback_allowed_payload, _ = self.request(
                "POST",
                "/api/collect",
                body={"city_code": "25", "node_id": "DJB8001794"},
                headers={
                    "X-Busro-Operator-Token": operator_token,
                    "Idempotency-Key": "operator-loopback-0001",
                },
            )

        self.assertEqual(denied_status, 403)
        self.assertEqual(denied_payload["error"]["code"], "OPERATOR_AUTH_REQUIRED")
        self.assertEqual(allowed_status, 201)
        self.assertTrue(allowed_payload["created"])
        self.assertNotIn(operator_token, json.dumps(allowed_payload))
        self.assertNotIn(operator_token, json.dumps(self.server.service.status()))
        self.assertEqual(options_status, 204)
        self.assertIn("Authorization", options_headers["Access-Control-Allow-Headers"])
        self.assertEqual(loopback_missing_status, 403)
        self.assertEqual(loopback_missing_payload["error"]["code"], "OPERATOR_AUTH_REQUIRED")
        self.assertEqual(loopback_wrong_status, 403)
        self.assertEqual(loopback_wrong_payload["error"]["code"], "OPERATOR_AUTH_REQUIRED")
        self.assertEqual(loopback_allowed_status, 201)
        self.assertTrue(loopback_allowed_payload["created"])

    def test_tago_budget_exhaustion_is_http_429_with_retry_after(self) -> None:
        service = self.server.service
        service.settings = replace(
            service.settings,
            fixture_mode=False,
            tago_service_key="unit-test-key",
            tago_daily_call_budget=1,
        )
        upstream = json.loads(service.settings.fixture_path.read_text(encoding="utf-8"))
        with patch("app.fetch_arrivals", return_value=upstream) as fetched:
            first_status, _first_payload, _ = self.request(
                "GET", "/api/arrivals?city_code=25&node_id=DJB8001793"
            )
            second_status, second_payload, second_headers = self.request(
                "GET", "/api/arrivals?city_code=25&node_id=DJB8001794"
            )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 429)
        self.assertEqual(second_payload["error"]["code"], "TAGO_DAILY_BUDGET_EXHAUSTED")
        self.assertEqual(second_headers["Retry-After"], "43200")
        self.assertEqual(fetched.call_count, 1)
        self.assertEqual(service.store.tago_attempt_count("2026-08-31"), 1)
        self.assertNotIn("unit-test-key", json.dumps(second_payload))

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
