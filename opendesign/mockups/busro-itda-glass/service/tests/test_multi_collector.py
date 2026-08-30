from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

from multi_collector import (  # noqa: E402
    CollectorError,
    LocalApi,
    RateLimiter,
    Target,
    collect_once,
    load_targets,
    local_origin,
    make_idempotency_key,
)


class MultiCollectorTests(unittest.TestCase):
    def test_loads_strict_json_and_deduplicates(self):
        document = {
            "arrivals": [
                {"city_code": "25", "node_id": "A"},
                {"city_code": "25", "node_id": "A"},
            ],
            "positions": [{"city_code": "25", "route_id": "R"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            targets = load_targets(path)
        self.assertEqual([target.label for target in targets], ["arrival:25:A", "position:25:R"])

    def test_rejects_non_loopback_api(self):
        self.assertEqual(local_origin("http://127.0.0.1:8791/"), "http://127.0.0.1:8791")
        for value in ("https://127.0.0.1", "http://example.com", "http://user@localhost"):
            with self.subTest(value=value), self.assertRaises(CollectorError):
                local_origin(value)

    def test_idempotency_key_is_stable_per_target_bucket(self):
        target = Target("arrival", "25", "A")
        first = make_idempotency_key(target, 10, 300)
        self.assertEqual(first, make_idempotency_key(target, 10, 300))
        self.assertNotEqual(first, make_idempotency_key(target, 11, 300))
        self.assertLessEqual(len(first), 128)

    def test_budget_and_error_isolation(self):
        targets = (
            Target("arrival", "25", "bad"),
            Target("arrival", "25", "good"),
            Target("position", "25", "skipped"),
        )

        class FakeApi:
            def __init__(self):
                self.calls = []

            def collect(self, target, key):
                self.calls.append((target, key))
                if target.target_id == "bad":
                    raise CollectorError("UPSTREAM", "failed", 502)
                return {"created": True, "snapshot": {"snapshot_id": "ok"}}

        api = FakeApi()
        outcomes, used = collect_once(
            targets,
            api,
            budget=2,
            used=0,
            interval=300,
            bucket=10,
            limiter=RateLimiter(20),
        )
        self.assertEqual([outcome["ok"] for outcome in outcomes], [False, True])
        self.assertEqual([call[0].target_id for call in api.calls], ["bad", "good"])
        self.assertEqual(used, 2)


if __name__ == "__main__":
    unittest.main()
