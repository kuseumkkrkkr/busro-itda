from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from app import BusroService  # noqa: E402
from config import Settings  # noqa: E402
from loopback_live_api import LoopbackApiError  # noqa: E402
from server import BusroHTTPServer, Handler, _require_direct_live_upstream  # noqa: E402


class FakeLiveApi:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.snapshot_id = "snap_proxy_visible"
        self.available = True
        self.proxied = False
        self.transport_safe = True
        self.probe_count = 0
        self._probe_lock = threading.Lock()

    def probe_status(self):
        with self._probe_lock:
            self.probe_count += 1
        if not self.available:
            raise LoopbackApiError("LOOPBACK_API_UNAVAILABLE", "unavailable", status=503)
        status = {
            "ok": True,
            "mode": "live",
            "tago": {"configured": True, "state": "ready", "key_exposed": False},
            "capabilities": {
                "transport_route_identifiers": (
                    "hangul_ascii_safe" if self.transport_safe else "legacy_ascii_only"
                )
            },
        }
        if self.proxied:
            status["tago"]["connection"] = "loopback_proxy"
        return status

    def get(self, target):
        self.calls.append(("GET", target))
        return {"ok": True, "source": "TAGO", "arrivals": []}

    def post(self, target, body, *, allow_mutation, idempotency_key=None):
        self.calls.append(("POST", target, body, allow_mutation, idempotency_key))
        if target == "/api/mappings/validate":
            return {"ok": True, "valid": True, "reason": "ROUTE_CONTAINS_NODE"}
        return {
            "ok": True,
            "created": True,
            "snapshot": {"snapshot_id": self.snapshot_id},
            "passages": [],
        }


class LoopbackServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        settings = Settings(
            fixture_mode=False,
            tago_service_key=None,
            db_path=Path(self.temp.name) / "shared.sqlite3",
            network_catalog_path=Path(self.temp.name) / "shared-catalog.sqlite3",
        )
        self.service = BusroService(settings)
        self.service.store.create_snapshot(
            snapshot_id="snap_proxy_visible",
            idempotency_key="proxy-visible-0001",
            request_hash="request-hash",
            payload_hash="payload-hash",
            source="TAGO",
            city_code="25",
            node_id="DJB8001793",
            captured_at="2026-08-31T00:00:00Z",
            upstream={"result_code": "00"},
            arrivals=[],
        )
        self.live_api = FakeLiveApi()
        self.server = BusroHTTPServer(
            ("127.0.0.1", 0),
            Handler,
            service=self.service,
            live_api=self.live_api,
            shared_live_storage=True,
            shared_storage_baseline_consistent=True,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def concurrent_requests(self, count, method, path):
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(self.request, method, path) for _ in range(count)]
            return [future.result(timeout=4) for future in futures]

    def test_status_is_ready_without_claiming_a_local_credential(self) -> None:
        status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["tago"]["state"], "ready")
        self.assertFalse(payload["tago"]["configured"])
        self.assertEqual(payload["tago"]["credential_scope"], "loopback_upstream")
        self.assertFalse(payload["tago"]["key_exposed"])
        self.assertTrue(payload["loopback_live_api"]["baseline_consistent"])
        self.assertFalse(payload["loopback_live_api"]["write_verified"])

    def test_get_and_mapping_are_fixed_pass_throughs(self) -> None:
        status, payload = self.request(
            "GET", "/api/arrivals?node_id=DJB8001793&city_code=25"
        )
        self.assertEqual((status, payload["source"]), (200, "TAGO"))
        self.assertEqual(
            self.live_api.calls[-1],
            ("GET", "/api/arrivals?node_id=DJB8001793&city_code=25"),
        )

        status, payload = self.request(
            "POST",
            "/api/mappings/validate",
            body={"city_code": "25", "route_id": "R1", "node_id": "N1"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])

    def test_collection_must_be_visible_in_the_local_shared_store(self) -> None:
        status, payload = self.request(
            "POST",
            "/api/collect",
            body={"city_code": "25", "node_id": "DJB8001793"},
            headers={"Idempotency-Key": "proxy-visible-0001"},
        )
        self.assertEqual(status, 201)
        self.assertTrue(payload["created"])
        status, payload = self.request("GET", "/api/status")
        self.assertTrue(payload["loopback_live_api"]["write_verified"])

        self.live_api.snapshot_id = "snap_proxy_missing"
        calls_before_mismatch = len(self.live_api.calls)
        status, payload = self.request(
            "POST",
            "/api/collect",
            body={"city_code": "25", "node_id": "DJB8001794"},
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "LOOPBACK_SHARED_STORAGE_MISMATCH")
        self.assertEqual(len(self.live_api.calls), calls_before_mismatch + 1)
        status, payload = self.request(
            "POST",
            "/api/collect",
            body={"city_code": "25", "node_id": "DJB8001795"},
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "LOOPBACK_SHARED_STORAGE_FAILED")
        self.assertEqual(len(self.live_api.calls), calls_before_mismatch + 1)

        status, payload = self.request("GET", "/api/status")
        self.assertFalse(payload["loopback_live_api"]["write_verified"])
        self.assertTrue(payload["loopback_live_api"]["failed"])
        self.assertFalse(payload["capabilities"]["snapshot_collection"])

    def test_status_downgrades_when_upstream_stops(self) -> None:
        self.live_api.available = False
        status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["tago"]["state"], "upstream_unavailable")
        self.assertFalse(payload["capabilities"]["live_arrivals"])
        self.assertFalse(payload["capabilities"]["route_stop_mapping_validation"])
        self.assertFalse(payload["capabilities"]["transport_route_identifiers"])

    def test_proxy_chain_status_is_rejected(self) -> None:
        with self.assertRaises(LoopbackApiError):
            _require_direct_live_upstream(
                {
                    "mode": "live",
                    "tago": {
                        "configured": True,
                        "state": "ready",
                        "key_exposed": False,
                        "connection": "loopback_proxy",
                    },
                }
            )

    def test_runtime_proxy_chain_latches_and_blocks_dispatch(self) -> None:
        self.live_api.proxied = True
        status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["tago"]["state"], "upstream_unavailable")
        calls_before = len(self.live_api.calls)
        status, payload = self.request(
            "GET", "/api/arrivals?city_code=25&node_id=DJB8001793"
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "LOOPBACK_UPSTREAM_NOT_ATTESTED")
        self.assertEqual(len(self.live_api.calls), calls_before)

    def test_mutation_capabilities_are_false_without_shared_storage(self) -> None:
        self.server.shared_live_storage = False
        status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["capabilities"]["snapshot_collection"])
        self.assertFalse(payload["capabilities"]["position_snapshot_collection"])
        self.assertFalse(payload["capabilities"]["verified_route_hydration"])
        calls_before = len(self.live_api.calls)
        status, payload = self.request(
            "POST",
            "/api/collect",
            body={"city_code": "25", "node_id": "DJB8001793"},
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "LOOPBACK_SHARED_STORAGE_REQUIRED")
        self.assertEqual(len(self.live_api.calls), calls_before)

    def test_concurrent_forced_status_reuses_one_attestation(self) -> None:
        count = 12
        gate = CountingLock()
        gate.acquire()
        self.server._live_attestation_lock = gate
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(self.request, "GET", "/api/status") for _ in range(count)]
            self.assertTrue(gate.wait_for_attempts(count + 1, timeout=2))
            gate.release()
            results = [future.result(timeout=4) for future in futures]
        self.assertTrue(all(status == 200 for status, _ in results))
        self.assertTrue(all(payload["tago"]["state"] == "ready" for _, payload in results))
        self.assertEqual(self.live_api.probe_count, 1)

    def test_concurrent_failed_get_reuses_one_attestation(self) -> None:
        count = 12
        self.live_api.available = False
        gate = CountingLock()
        gate.acquire()
        self.server._live_attestation_lock = gate
        path = "/api/arrivals?city_code=25&node_id=DJB8001793"
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(self.request, "GET", path) for _ in range(count)]
            self.assertTrue(gate.wait_for_attempts(count + 1, timeout=2))
            gate.release()
            results = [future.result(timeout=4) for future in futures]
        self.assertTrue(all(status == 503 for status, _ in results))
        self.assertTrue(
            all(
                payload["error"]["code"]
                in {"LOOPBACK_API_UNAVAILABLE", "LOOPBACK_UPSTREAM_NOT_ATTESTED"}
                for _, payload in results
            )
        )
        self.assertEqual(self.live_api.probe_count, 1)
        self.assertEqual(self.live_api.calls, [])

    def test_hangul_route_id_requires_current_upstream_capability(self) -> None:
        route_id = "GMB수점10"
        path = "/api/routes/stops?" + urlencode(
            {"city_code": "25", "route_id": route_id, "page": 1, "limit": 100}
        )
        self.live_api.transport_safe = False
        status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["capabilities"]["transport_route_identifiers"],
            "legacy_ascii_only",
        )
        status, payload = self.request("GET", path)
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "LOOPBACK_UPSTREAM_ROUTE_ID_UNSUPPORTED")
        self.assertEqual(self.live_api.calls, [])

        self.live_api.transport_safe = True
        status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["loopback_live_api"]["transport_route_identifiers"],
            "hangul_ascii_safe",
        )
        self.assertEqual(
            payload["capabilities"]["transport_route_identifiers"],
            "hangul_ascii_safe",
        )
        status, payload = self.request("GET", path)
        self.assertEqual((status, payload["source"]), (200, "TAGO"))


class CountingLock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition()
        self.attempts = 0

    def acquire(self, blocking=True, timeout=-1):
        with self._condition:
            self.attempts += 1
            self._condition.notify_all()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    def wait_for_attempts(self, count: int, *, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self.attempts >= count, timeout=timeout)


if __name__ == "__main__":
    unittest.main()
