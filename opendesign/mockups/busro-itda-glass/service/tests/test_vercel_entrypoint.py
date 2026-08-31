from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[5]
ENTRYPOINT = ROOT / "api" / "index.py"
SPEC = importlib.util.spec_from_file_location("busro_vercel_entrypoint", ENTRYPOINT)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import contract guard
    raise RuntimeError("Vercel entrypoint could not be loaded")
entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entrypoint)


class VercelEntrypointResponseCase(unittest.TestCase):
    @staticmethod
    def _write(payload: dict, *, status: int = 200):
        instance = entrypoint.handler.__new__(entrypoint.handler)
        instance.headers = {}
        instance.wfile = io.BytesIO()
        statuses: list[int] = []
        headers: list[tuple[str, str]] = []
        instance.send_response = statuses.append
        instance.send_header = lambda name, value: headers.append((name, value))
        instance.end_headers = lambda: None

        instance._write_json(status, payload)

        return statuses, dict(headers), instance.wfile.getvalue()

    def test_normal_journey_response_passes_unchanged(self) -> None:
        payload = {
            "ok": True,
            "status": "READY",
            "candidates": [
                {
                    "route_ids": ["991", "B1", "607"],
                    "transfers": 2,
                    "steps": [{"kind": "ride", "route_id": "991"}],
                }
            ],
        }

        statuses, headers, body = self._write(payload)

        self.assertEqual(statuses, [200])
        self.assertEqual(json.loads(body), payload)
        self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_oversized_response_is_replaced_by_bounded_public_error(self) -> None:
        secret_marker = "must-not-leak"
        payload = {
            "ok": True,
            "candidates": [secret_marker + ("x" * entrypoint.MAX_RESPONSE_BYTES)],
        }

        statuses, headers, body = self._write(payload)
        decoded = json.loads(body)

        self.assertEqual(statuses, [413])
        self.assertEqual(decoded["error"]["code"], "RESPONSE_TOO_LARGE")
        self.assertNotIn(secret_marker.encode("utf-8"), body)
        self.assertLessEqual(len(body), entrypoint.MAX_RESPONSE_BYTES)
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertEqual(int(headers["Content-Length"]), len(body))


if __name__ == "__main__":
    unittest.main()
