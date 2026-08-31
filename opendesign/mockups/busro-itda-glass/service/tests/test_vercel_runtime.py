from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vercel_runtime import dispatch_request, reset_runtime_for_tests


class VercelRuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "BUSRO_FIXTURE_MODE": "1",
                "BUSRO_DB_PATH": str(root / "observations.sqlite3"),
                "BUSRO_NETWORK_CATALOG_PATH": str(root / "catalog.sqlite3"),
            },
            clear=False,
        )
        self.environment.start()
        reset_runtime_for_tests()

    def tearDown(self) -> None:
        reset_runtime_for_tests()
        self.environment.stop()
        self.temporary.cleanup()

    def test_status_discloses_serverless_storage_boundary_without_secret(self) -> None:
        response = dispatch_request("GET", "/api/status")
        self.assertEqual(response.status, 200)
        self.assertFalse(response.payload["tago"]["key_exposed"])
        self.assertEqual(response.payload["deployment"]["platform"], "vercel")
        self.assertEqual(response.payload["deployment"]["observation_storage"], "ephemeral")
        self.assertFalse(response.payload["capabilities"]["snapshot_collection"])
        self.assertFalse(response.payload["capabilities"]["verified_route_hydration"])

    def test_persistent_mutations_fail_closed(self) -> None:
        for path in ("/api/collect", "/api/positions/collect", "/api/network/hydrate"):
            with self.subTest(path=path):
                response = dispatch_request("POST", path, body={})
                self.assertEqual(response.status, 503)
                self.assertEqual(response.payload["error"]["code"], "PERSISTENT_STORAGE_REQUIRED")

    def test_static_search_and_unknown_route_are_bounded(self) -> None:
        search = dispatch_request("GET", "/api/network/stops", {"q": "서울", "limit": "8"})
        self.assertEqual(search.status, 200)
        self.assertIn("s-maxage", search.cache_control)

        missing = dispatch_request("GET", "/api/not-real")
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.payload["error"]["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
