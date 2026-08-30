from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from source_registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    MAX_FILE_BYTES,
    RegistryError,
    SourceRegistry,
    load_default_registry,
)


def _fixture_document() -> dict:
    return json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))


class SourceRegistryCase(unittest.TestCase):
    def test_default_registry_preserves_evidence_and_priority(self) -> None:
        registry = load_default_registry()
        self.assertEqual(registry.priority_order[0]["id"], "TAGO")
        self.assertEqual([item["tier"] for item in registry.priority_order], [1, 2, 3, 4, 5])

        tago = registry.search("TAGO", limit=10)
        self.assertEqual(len(tago), 5)
        self.assertTrue(all(source["priority_tier"] == 1 for source in tago))
        self.assertTrue(all(source["license"] == "NO_USE_RESTRICTION" for source in tago))
        core = [source for source in tago if source["id"] != "tago-route-specific-stops"]
        self.assertEqual({source["id"] for source in core}, {"tago-routes", "tago-stops", "tago-arrivals", "tago-positions"})
        self.assertTrue(all(source["development_daily_quota"] == 10000 for source in core))
        supplementary = next(source for source in tago if source["id"] == "tago-route-specific-stops")
        self.assertEqual(supplementary["development_daily_quota"], 1000)
        self.assertEqual(supplementary["refresh"]["policy"], "SUPPLEMENTARY_NOT_NATIONWIDE")

        ktdb = registry.search("KTDB", limit=1)[0]
        self.assertEqual(ktdb["id"], "ktdb-gtfs-2024")
        self.assertEqual(ktdb["status"], "VERIFIED_SCHEDULE_ORIGIN")
        self.assertEqual(ktdb["refresh"]["basis_date"], "2024-03")
        self.assertEqual(
            ktdb["refresh"]["id_namespace"],
            "KTDB_NONSTANDARD_NEVER_JOIN_BY_NAME",
        )

        yeongdong = registry.search("영동군", limit=1)[0]
        self.assertEqual(yeongdong["status"], "VERIFIED_SCHEDULE_ORIGIN")
        self.assertEqual(yeongdong["refresh"]["effective_date"], "2026-04-01")
        self.assertEqual(yeongdong["urls"][1]["robots"], "BLOCKED")

        seongju = registry.search("성주군", limit=1)[0]
        self.assertEqual(seongju["license"], "KOGL_TYPE_1")
        self.assertEqual(seongju["refresh"]["effective_date"], "2026-03-20")

        gimcheon_bis = registry.search("김천시 버스정보시스템", limit=1)[0]
        self.assertEqual(gimcheon_bis["status"], "SOURCE_DOWN")

    def test_list_and_search_are_bounded_and_return_copies(self) -> None:
        registry = load_default_registry()
        first_page = registry.list_sources(limit=2)
        self.assertEqual(len(first_page), 2)
        self.assertTrue(all(source["priority_tier"] == 1 for source in first_page))
        first_page[0]["name"] = "mutated"
        self.assertNotEqual(registry.list_sources(limit=1)[0]["name"], "mutated")

        schedules = registry.list_sources(status="VERIFIED_SCHEDULE_ORIGIN", limit=10)
        self.assertTrue(schedules)
        self.assertTrue(all(source["status"] == "VERIFIED_SCHEDULE_ORIGIN" for source in schedules))
        self.assertEqual(len(registry.search("영천", limit=1)), 1)

        for call in (
            lambda: registry.list_sources(limit=101),
            lambda: registry.list_sources(offset=10001),
            lambda: registry.list_sources(status="LIVE"),
            lambda: registry.search("x" * 101),
            lambda: registry.search("TAGO", limit=0),
        ):
            with self.subTest(call=call):
                with self.assertRaises(RegistryError):
                    call()

    def test_rejects_duplicate_ids_control_characters_and_bad_status(self) -> None:
        duplicate = _fixture_document()
        duplicate["sources"].append(deepcopy(duplicate["sources"][0]))
        with self.assertRaisesRegex(RegistryError, "duplicate source id"):
            SourceRegistry(duplicate)

        controlled = _fixture_document()
        controlled["sources"][0]["name"] = "bad\nname"
        with self.assertRaisesRegex(RegistryError, "control"):
            SourceRegistry(controlled)

        bad_status = _fixture_document()
        bad_status["sources"][0]["status"] = "LIVE"
        with self.assertRaisesRegex(RegistryError, "unsupported source status"):
            SourceRegistry(bad_status)

    def test_rejects_non_https_unlisted_and_traversing_urls(self) -> None:
        variants = (
            "http://www.data.go.kr/data/15098529/openapi.do",
            "https://evil.example/data",
            "https://user@www.data.go.kr/data/15098529/openapi.do",
            "https://www.data.go.kr:444/data/15098529/openapi.do",
            "https://www.data.go.kr/data/%252e%252e/private",
        )
        for url in variants:
            document = _fixture_document()
            document["sources"][0]["urls"][0]["url"] = url
            with self.subTest(url=url):
                with self.assertRaises(RegistryError):
                    SourceRegistry(document)

    def test_file_loader_rejects_path_escape_and_large_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / "allowed"
            allowed.mkdir()
            outside = root / "outside.json"
            outside.write_text(json.dumps(_fixture_document()), encoding="utf-8")
            with self.assertRaisesRegex(RegistryError, "allowed root"):
                SourceRegistry.from_file(outside, allowed_root=allowed)

            oversized = allowed / "oversized.json"
            oversized.write_bytes(b" " * (MAX_FILE_BYTES + 1))
            with self.assertRaisesRegex(RegistryError, "between 1"):
                SourceRegistry.from_file(oversized, allowed_root=allowed)

            valid = allowed / "valid.json"
            valid.write_text(json.dumps(_fixture_document(), ensure_ascii=False), encoding="utf-8")
            loaded = SourceRegistry.from_file(valid, allowed_root=allowed)
            self.assertTrue(loaded.search("울산"))


if __name__ == "__main__":
    unittest.main()
