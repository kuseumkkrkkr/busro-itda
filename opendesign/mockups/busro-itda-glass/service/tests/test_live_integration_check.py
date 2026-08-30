from __future__ import annotations

from pathlib import Path
import argparse
import sys
import unittest


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from live_integration_check import local_api_base  # noqa: E402


class LiveIntegrationCheckCase(unittest.TestCase):
    def test_only_local_http_api_targets_are_accepted(self) -> None:
        self.assertEqual(local_api_base("http://127.0.0.1:8791"), "http://127.0.0.1:8791/api")
        self.assertEqual(local_api_base("http://localhost:8791/api"), "http://localhost:8791/api")
        self.assertEqual(local_api_base("http://[::1]:8791/api"), "http://[::1]:8791/api")

        for value in (
            "https://127.0.0.1:8791/api",
            "http://example.com/api",
            "http://user:pass@127.0.0.1:8791/api",
            "http://127.0.0.1:8791/other",
            "http://127.0.0.1:8791/api?key=secret",
        ):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    local_api_base(value)


if __name__ == "__main__":
    unittest.main()
