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
    VALID_COLLECTION_POLICIES,
    VALID_SCHEDULE_GRANULARITIES,
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
        self.assertTrue(all(source["origin_status"] == "VERIFIED_ROUTE_ONLY" for source in tago))
        self.assertTrue(all(source["ingestion_status"] == "DISCOVERED_ONLY" for source in tago))
        self.assertTrue(all(source["schedule_granularity"] == "NONE" for source in tago))
        self.assertTrue(all(source["collection_policy"] == "BOUNDED_API" for source in tago))
        core = [source for source in tago if source["id"] != "tago-route-specific-stops"]
        self.assertEqual({source["id"] for source in core}, {"tago-routes", "tago-stops", "tago-arrivals", "tago-positions"})
        self.assertTrue(all(source["development_daily_quota"] == 10000 for source in core))
        supplementary = next(source for source in tago if source["id"] == "tago-route-specific-stops")
        self.assertEqual(supplementary["development_daily_quota"], 1000)
        self.assertEqual(supplementary["refresh"]["policy"], "SUPPLEMENTARY_NOT_NATIONWIDE")

        ktdb = registry.search("KTDB", limit=1)[0]
        self.assertEqual(ktdb["id"], "ktdb-gtfs-2024")
        self.assertEqual(ktdb["origin_status"], "VERIFIED_PRIOR_ONLY")
        self.assertEqual(ktdb["ingestion_status"], "DISCOVERED_ONLY")
        self.assertEqual(ktdb["schedule_granularity"], "STOP_LEVEL_TIMES")
        self.assertEqual(ktdb["collection_policy"], "MANUAL_APPLICATION_ONLY")
        self.assertFalse(ktdb["projection_allowed"])
        self.assertIn("RANKING_PRIOR_TIE_BREAK_ONLY", ktdb["allowed_uses"])
        self.assertIn("CURRENT_TIMETABLE", ktdb["prohibited_uses"])
        self.assertIn(
            "STANDALONE_DELAY_OR_RELIABILITY_PROBABILITY",
            ktdb["prohibited_uses"],
        )
        self.assertEqual(ktdb["refresh"]["basis_date"], "2024-03")
        self.assertEqual(
            ktdb["refresh"]["id_namespace"],
            "KTDB_NONSTANDARD_NEVER_JOIN_BY_NAME",
        )

        gwangju = registry.search("광주 시내버스 최신", limit=1)[0]
        self.assertEqual(gwangju["origin_status"], "VERIFIED_SCHEDULE_ORIGIN")
        self.assertEqual(gwangju["ingestion_status"], "DISCOVERED_ONLY")
        self.assertEqual(gwangju["schedule_granularity"], "TERMINAL_DEPARTURES")
        self.assertFalse(gwangju["projection_allowed"])
        self.assertEqual(gwangju["refresh"]["observed_notice_id"], "1209")
        self.assertEqual(gwangju["refresh"]["effective_date"], "2026-08-22")
        self.assertIn("EXACT_TAGO_ROUTE_MAPPING", gwangju["refresh"]["activation"])

        yeongdong = registry.search("영동군", limit=1)[0]
        self.assertEqual(yeongdong["origin_status"], "VERIFIED_SCHEDULE_ORIGIN")
        self.assertEqual(yeongdong["schedule_granularity"], "UNKNOWN")
        self.assertEqual(yeongdong["collection_policy"], "PERMISSION_REQUIRED")
        self.assertEqual(yeongdong["refresh"]["effective_date"], "2026-04-01")
        self.assertEqual(yeongdong["urls"][1]["robots"], "BLOCKED")

        seongju = registry.search("성주군", limit=1)[0]
        self.assertEqual(seongju["license"], "KOGL_TYPE_1")
        self.assertEqual(seongju["refresh"]["effective_date"], "2026-03-20")

        gimcheon_bis = registry.search("김천시 버스정보시스템", limit=1)[0]
        self.assertEqual(gimcheon_bis["origin_status"], "SOURCE_DOWN")

        all_sources = registry.list_sources(limit=100)
        self.assertTrue(
            all(source["schedule_granularity"] in VALID_SCHEDULE_GRANULARITIES for source in all_sources)
        )
        self.assertTrue(
            all(source["collection_policy"] in VALID_COLLECTION_POLICIES for source in all_sources)
        )

    def test_list_and_search_are_bounded_and_return_copies(self) -> None:
        registry = load_default_registry()
        first_page = registry.list_sources(limit=2)
        self.assertEqual(len(first_page), 2)
        self.assertTrue(all(source["priority_tier"] == 1 for source in first_page))
        first_page[0]["name"] = "mutated"
        self.assertNotEqual(registry.list_sources(limit=1)[0]["name"], "mutated")

        schedules = registry.list_sources(origin_status="VERIFIED_SCHEDULE_ORIGIN", limit=10)
        self.assertTrue(schedules)
        self.assertTrue(
            all(source["origin_status"] == "VERIFIED_SCHEDULE_ORIGIN" for source in schedules)
        )
        priors = registry.list_sources(status="VERIFIED_PRIOR_ONLY", limit=10)
        self.assertEqual([source["id"] for source in priors], ["ktdb-gtfs-2024"])
        self.assertEqual(len(registry.search("영천", limit=1)), 1)

        for call in (
            lambda: registry.list_sources(limit=101),
            lambda: registry.list_sources(offset=10001),
            lambda: registry.list_sources(status="LIVE"),
            lambda: registry.list_sources(origin_status="LIVE"),
            lambda: registry.list_sources(
                status="VERIFIED_ROUTE_ONLY",
                origin_status="VERIFIED_SCHEDULE_ORIGIN",
            ),
            lambda: registry.search("x" * 101),
            lambda: registry.search("TAGO", limit=0),
        ):
            with self.subTest(call=call):
                with self.assertRaises(RegistryError):
                    call()

    def test_rejects_duplicate_ids_control_characters_and_bad_origin_status(self) -> None:
        duplicate = _fixture_document()
        duplicate["sources"].append(deepcopy(duplicate["sources"][0]))
        with self.assertRaisesRegex(RegistryError, "duplicate source id"):
            SourceRegistry(duplicate)

        controlled = _fixture_document()
        controlled["sources"][0]["name"] = "bad\nname"
        with self.assertRaisesRegex(RegistryError, "control"):
            SourceRegistry(controlled)

        bad_status = _fixture_document()
        bad_status["sources"][0]["origin_status"] = "LIVE"
        with self.assertRaisesRegex(RegistryError, "unsupported source origin_status"):
            SourceRegistry(bad_status)

    def test_v2_requires_safe_schedule_and_collection_contracts(self) -> None:
        old_schema = _fixture_document()
        old_schema["schema_version"] = 1
        with self.assertRaisesRegex(RegistryError, "unsupported municipal source registry schema"):
            SourceRegistry(old_schema)

        variants = (
            ("schedule_granularity", None, "schedule_granularity"),
            ("schedule_granularity", "PER_STOP_MAYBE", "schedule_granularity"),
            ("collection_policy", None, "collection_policy"),
            ("collection_policy", "UNBOUNDED_CRAWL", "collection_policy"),
            ("ingestion_status", "READY_MAYBE", "ingestion_status"),
        )
        for field, value, message in variants:
            document = _fixture_document()
            if value is None:
                document["sources"][0].pop(field)
            else:
                document["sources"][0][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(RegistryError, message):
                    SourceRegistry(document)

        explicit = _fixture_document()
        explicit["sources"][0]["ingestion_status"] = "ACTIVE"
        self.assertEqual(
            SourceRegistry(explicit).search("tago-routes", limit=1)[0]["ingestion_status"],
            "ACTIVE",
        )

    def test_prior_only_sources_fail_closed(self) -> None:
        projection = _fixture_document()
        projection["sources"][5]["projection_allowed"] = True
        with self.assertRaisesRegex(RegistryError, "projection_allowed=false"):
            SourceRegistry(projection)

        missing_uses = _fixture_document()
        missing_uses["sources"][5].pop("allowed_uses")
        with self.assertRaisesRegex(RegistryError, "allowed_uses"):
            SourceRegistry(missing_uses)

        overlapping = _fixture_document()
        overlapping["sources"][5]["prohibited_uses"].append(
            overlapping["sources"][5]["allowed_uses"][0]
        )
        with self.assertRaisesRegex(RegistryError, "must be disjoint"):
            SourceRegistry(overlapping)

        no_granularity = _fixture_document()
        no_granularity["sources"][5]["schedule_granularity"] = "NONE"
        with self.assertRaisesRegex(RegistryError, "must declare schedule granularity"):
            SourceRegistry(no_granularity)

    def test_current_projection_requires_active_ingestion(self) -> None:
        document = _fixture_document()
        gwangju = next(
            source
            for source in document["sources"]
            if source["id"] == "gwangju-current-timetable"
        )
        gwangju["projection_allowed"] = True
        with self.assertRaisesRegex(RegistryError, "ingestion_status=ACTIVE"):
            SourceRegistry(document)

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
