from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time
import unittest

SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

from loopback_live_api import (  # noqa: E402
    GET_PATHS,
    LoopbackApiError,
    LoopbackLiveApiClient,
    MAX_RESPONSE_BYTES,
    validate_loopback_origin,
)


class FakeResponse:
    def __init__(self, status: int, payload: bytes):
        self.status = status
        self.payload = payload

    def read(self, maximum: int) -> bytes:
        return self.payload[:maximum]


class RecordingConnection:
    calls: list[dict] = []
    status = 200
    payload = b'{"ok":true}'
    delay = 0.0

    def __init__(self, host: str, port: int, timeout: float):
        self.host, self.port, self.timeout = host, port, timeout

    def request(self, method: str, target: str, body=None, headers=None):
        type(self).calls.append(
            {
                "host": self.host,
                "port": self.port,
                "timeout": self.timeout,
                "method": method,
                "target": target,
                "body": body,
                "headers": dict(headers or {}),
            }
        )

    def getresponse(self):
        if type(self).delay:
            time.sleep(type(self).delay)
        return FakeResponse(type(self).status, type(self).payload)

    def close(self):
        return None


class LoopbackLiveApiTests(unittest.TestCase):
    def setUp(self):
        RecordingConnection.calls = []
        RecordingConnection.status = 200
        RecordingConnection.payload = b'{"ok":true}'
        RecordingConnection.delay = 0.0

    def client(self, **kwargs) -> LoopbackLiveApiClient:
        return LoopbackLiveApiClient(
            "http://127.0.0.1:8791",
            listener_port=8792,
            connection_factory=RecordingConnection,
            **kwargs,
        )

    def test_origin_requires_canonical_literal_explicit_different_port(self):
        self.assertEqual(
            validate_loopback_origin("http://127.0.0.1:8791", listener_port=8792).serialized,
            "http://127.0.0.1:8791",
        )
        self.assertEqual(
            validate_loopback_origin("http://[::1]:8791", listener_port=8792).serialized,
            "http://[::1]:8791",
        )
        rejected = (
            "http://localhost:8791",
            "https://127.0.0.1:8791",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://user@127.0.0.1:8791",
            "http://127.0.0.1:8791/",
            "http://127.0.0.1:8791?x=1",
            "http://127.0.0.1:8791#x",
            "http://127.0.0.1:8792",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(LoopbackApiError):
                validate_loopback_origin(value, listener_port=8792)

    def test_get_allowlist_and_no_forwarded_sensitive_headers(self):
        client = self.client()
        for path in sorted(GET_PATHS):
            with self.subTest(path=path):
                self.assertTrue(client.get(path + "?city_code=25")["ok"])
        call = RecordingConnection.calls[0]
        self.assertEqual(call["headers"], {"Accept": "application/json", "User-Agent": "busro-loopback-live/1"})
        for target in ("/api/status", "http://127.0.0.1:8791/api/cities", "//127.0.0.1/api/cities"):
            with self.subTest(target=target), self.assertRaises(LoopbackApiError):
                client.get(target)
        self.assertTrue(client.probe_status()["ok"])
        self.assertEqual(RecordingConnection.calls[-1]["target"], "/api/status")

    def test_post_requires_explicit_mutation_and_only_adds_idempotency_header(self):
        client = self.client()
        with self.assertRaises(LoopbackApiError) as disabled:
            client.post("/api/collect", {"city_code": "25"}, allow_mutation=False)
        self.assertEqual(disabled.exception.code, "LOOPBACK_MUTATION_DISABLED")

        result = client.post(
            "/api/positions/collect",
            {"city_code": "25", "route_id": "R"},
            allow_mutation=True,
            idempotency_key="loopback-post-0001",
        )
        self.assertTrue(result["ok"])
        call = RecordingConnection.calls[-1]
        self.assertEqual(
            call["headers"],
            {
                "Accept": "application/json",
                "User-Agent": "busro-loopback-live/1",
                "Content-Type": "application/json",
                "Idempotency-Key": "loopback-post-0001",
            },
        )
        self.assertEqual(json.loads(call["body"]), {"city_code": "25", "route_id": "R"})
        with self.assertRaises(LoopbackApiError):
            client.post("/api/collect?x=1", {}, allow_mutation=True)
        for target in ("/api/mappings/validate", "/api/network/hydrate"):
            with self.subTest(target=target):
                self.assertTrue(client.post(target, {}, allow_mutation=True)["ok"])
        with self.assertRaises(LoopbackApiError):
            client.post("/api/replay", {}, allow_mutation=True)

    def test_upstream_error_preserves_safe_contract_and_redacts_sensitive_details(self):
        RecordingConnection.status = 429
        RecordingConnection.payload = json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "잠시 후 다시 시도\n하세요",
                    "details": {"retry_after_seconds": 2, "service_key": "must-not-escape"},
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        with self.assertRaises(LoopbackApiError) as raised:
            self.client().get("/api/cities")
        error = raised.exception
        self.assertEqual((error.code, error.status), ("RATE_LIMITED", 429))
        self.assertEqual(error.message, "잠시 후 다시 시도 하세요")
        self.assertEqual(error.details, {"retry_after_seconds": 2})
        self.assertEqual(error.payload()["error"]["code"], "RATE_LIMITED")

    def test_response_must_be_bounded_json_object(self):
        for payload, expected in (
            (b"[]", "INVALID_LOOPBACK_RESPONSE"),
            (b"not json", "INVALID_LOOPBACK_RESPONSE"),
            (b"x" * (MAX_RESPONSE_BYTES + 1), "LOOPBACK_RESPONSE_TOO_LARGE"),
        ):
            RecordingConnection.payload = payload
            with self.subTest(expected=expected), self.assertRaises(LoopbackApiError) as raised:
                self.client().get("/api/cities")
            self.assertEqual(raised.exception.code, expected)

    def test_redirect_is_not_followed_and_request_is_not_retried(self):
        RecordingConnection.status = 302
        RecordingConnection.payload = b'{"ok":false,"error":{"message":"moved"}}'
        with self.assertRaises(LoopbackApiError) as raised:
            self.client().get("/api/cities")
        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(len(RecordingConnection.calls), 1)

    def test_total_deadline_is_capped_and_returns_without_waiting_for_worker(self):
        RecordingConnection.delay = 0.15
        client = self.client(timeout_seconds=99, max_concurrency=1, admission_wait_seconds=0.01)
        self.assertEqual(client.timeout_seconds, 8.0)
        started = time.monotonic()
        with self.assertRaises(LoopbackApiError) as raised:
            client.get("/api/cities", timeout_seconds=0.03)
        elapsed = time.monotonic() - started
        self.assertEqual(raised.exception.code, "LOOPBACK_API_DEADLINE_EXCEEDED")
        self.assertLess(elapsed, 0.12)

        # The timed-out worker retains the sole permit until it really exits.
        with self.assertRaises(LoopbackApiError) as busy:
            client.get("/api/cities", timeout_seconds=0.1)
        self.assertEqual(busy.exception.code, "LOOPBACK_API_BUSY")


if __name__ == "__main__":
    unittest.main()
