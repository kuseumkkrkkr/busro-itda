from __future__ import annotations

import csv
from contextlib import closing, contextmanager
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

from network_catalog import (  # noqa: E402
    CatalogLimitError,
    CatalogValidationError,
    NetworkCatalog,
    ROUTE_COLUMNS,
    STOP_COLUMNS,
)


FIXED_NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


def csv_bytes(columns, rows, encoding="utf-8-sig"):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode(encoding)


def stop_row(**overrides):
    row = {
        "정류장번호": "NODE-A",
        "정류장명": "시청 앞",
        "위도": "36.5000",
        "경도": "127.3000",
        "정보수집일": "2026-08-30",
        "모바일단축번호": "101",
        "도시코드": "25",
        "도시명": "세종",
        "관리도시명": "세종특별자치시",
    }
    row.update(overrides)
    return row


def route_row(**overrides):
    row = {
        "노선 아이디": "ROUTE-A",
        "노선명": "601",
        "기점노드 아이디": "NODE-A",
        "종점노드 아이디": "NODE-C",
        "기점정류장": "시청 앞",
        "종점정류장": "터미널",
        "지자체코드": "25",
        "지자체명": "세종",
    }
    row.update(overrides)
    return row


class NetworkCatalogCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = NetworkCatalog(self.root / "catalog.sqlite3", clock=lambda: FIXED_NOW)

    def tearDown(self):
        self.temp.cleanup()

    def test_cp949_stops_utf8_routes_and_provenance_are_persisted(self):
        stops = csv_bytes(
            STOP_COLUMNS,
            [stop_row(), stop_row(**{"정류장번호": "NODE-B", "정류장명": "한글 정류장", "모바일단축번호": "102"})],
            "cp949",
        )
        routes = csv_bytes(ROUTE_COLUMNS, [route_row()])
        stop_result = self.catalog.import_stops_csv(
            stops,
            source_url="https://data.go.kr/download/nationwide-stops.csv",
            source_date="2026-08-30",
        )
        route_result = self.catalog.import_routes_csv(
            routes,
            source_url="https://tago.go.kr/download/routes.csv",
            source_date="2026-08-30",
        )
        self.assertEqual(stop_result["encoding"], "cp949")
        self.assertEqual(route_result["encoding"], "utf-8-sig")
        self.assertEqual(stop_result["sha256"], hashlib.sha256(stops).hexdigest())
        self.assertEqual(self.catalog.search_cities("세종")[0]["stop_count"], 2)
        self.assertEqual(self.catalog.search_stops("한글")[0]["node_id"], "NODE-B")
        self.assertEqual(self.catalog.search_routes("601")[0]["route_id"], "ROUTE-A")
        provenance = self.catalog.provenance(limit=10)
        self.assertEqual({item["dataset_kind"] for item in provenance}, {"stops", "routes"})
        self.assertTrue(all(item["source_url"].startswith("https://") for item in provenance))

    def test_stop_resolution_fallback_has_source_node_index(self):
        with closing(sqlite3.connect(self.catalog.path)) as connection:
            plan = " ".join(
                row[3]
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT city_code,node_id FROM catalog_stops "
                    "WHERE source_id=? AND node_id=? "
                    "ORDER BY city_code,node_id LIMIT 2",
                    ("source", "node"),
                )
            )
        self.assertIn("idx_catalog_stops_source_node", plan)

    def test_catalog_import_does_not_touch_existing_runtime_database(self):
        runtime_db = self.root / "busro_itda.sqlite3"
        runtime_db.write_bytes(b"existing-runtime-db-sentinel")
        before = hashlib.sha256(runtime_db.read_bytes()).hexdigest()
        self.catalog.import_stops_csv(
            csv_bytes(STOP_COLUMNS, [stop_row()]),
            source_url="https://data.go.kr/stops.csv",
            source_date="2026-08-30",
        )
        self.assertEqual(hashlib.sha256(runtime_db.read_bytes()).hexdigest(), before)
        self.assertNotEqual(self.catalog.path, runtime_db)

        actual_runtime = self.root / "actual-runtime.sqlite3"
        connection = sqlite3.connect(actual_runtime)
        try:
            connection.execute("CREATE TABLE runtime_snapshots(id TEXT PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
        actual_before = hashlib.sha256(actual_runtime.read_bytes()).hexdigest()
        with self.assertRaises(CatalogValidationError):
            NetworkCatalog(actual_runtime, clock=lambda: FIXED_NOW)
        self.assertEqual(hashlib.sha256(actual_runtime.read_bytes()).hexdigest(), actual_before)

    def test_nonempty_legacy_active_gtfs_feed_table_fails_closed(self):
        legacy_path = self.root / "legacy-nonempty.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO catalog_meta VALUES('revision','7');
                CREATE TABLE active_gtfs_feeds (
                    provider TEXT PRIMARY KEY,
                    feed_id TEXT NOT NULL
                );
                INSERT INTO active_gtfs_feeds VALUES('KTDB','legacy-feed');
                CREATE TABLE active_route_sequences (
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    sequence_id TEXT NOT NULL,
                    PRIMARY KEY(city_code,route_id)
                );
                INSERT INTO active_route_sequences VALUES('11','R1','legacy-sequence');
                """
            )
        finally:
            connection.close()

        with self.assertRaisesRegex(
            CatalogValidationError, "legacy active GTFS feed roles are ambiguous"
        ):
            NetworkCatalog(legacy_path, clock=lambda: FIXED_NOW)

        connection = sqlite3.connect(legacy_path)
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(active_gtfs_feeds)"
                )
            }
            feed = connection.execute(
                "SELECT provider,feed_id FROM active_gtfs_feeds"
            ).fetchone()
            sequence = connection.execute(
                "SELECT city_code,route_id,sequence_id FROM active_route_sequences"
            ).fetchone()
        finally:
            connection.close()
        self.assertNotIn("topology_role", columns)
        self.assertEqual(feed, ("KTDB", "legacy-feed"))
        self.assertEqual(sequence, ("11", "R1", "legacy-sequence"))

    def test_empty_legacy_active_gtfs_feed_table_migrates_as_historical(self):
        legacy_path = self.root / "legacy-empty.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO catalog_meta VALUES('revision','0');
                CREATE TABLE active_gtfs_feeds (
                    provider TEXT PRIMARY KEY,
                    feed_id TEXT NOT NULL
                );
                """
            )
        finally:
            connection.close()

        NetworkCatalog(legacy_path, clock=lambda: FIXED_NOW)

        connection = sqlite3.connect(legacy_path)
        try:
            role_column = next(
                row
                for row in connection.execute(
                    "PRAGMA table_info(active_gtfs_feeds)"
                )
                if row[1] == "topology_role"
            )
            connection.execute(
                "INSERT INTO active_gtfs_feeds(provider,feed_id) VALUES('KTDB','feed')"
            )
            role = connection.execute(
                "SELECT topology_role FROM active_gtfs_feeds WHERE provider='KTDB'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(role_column[3], 1)
        self.assertEqual(role_column[4], "'historical_model'")
        self.assertEqual(role, "historical_model")

    def test_csv_ids_do_not_create_route_topology_until_explicit_hydration(self):
        self.catalog.import_stops_csv(
            csv_bytes(STOP_COLUMNS, [stop_row(), stop_row(**{"정류장번호": "NODE-C", "정류장명": "터미널"})]),
            source_url="https://data.go.kr/stops.csv",
            source_date="2026-08-30",
        )
        self.catalog.import_routes_csv(
            csv_bytes(ROUTE_COLUMNS, [route_row()]),
            source_url="https://tago.go.kr/routes.csv",
            source_date="2026-08-30",
        )
        self.assertEqual(self.catalog.snapshot().route_sequences, ())
        result = self.catalog.hydrate_route_sequence(
            city_code="25",
            route_id="ROUTE-A",
            ordered_stops=[
                {"node_id": "NODE-A", "node_name": "시청 앞", "node_order": 1, "latitude": 36.5, "longitude": 127.3},
                {"node_id": "NODE-X", "node_name": "공식 경유점", "node_order": 4, "latitude": 36.51, "longitude": 127.31},
                {"node_id": "NODE-C", "node_name": "터미널", "node_order": 9, "latitude": 36.52, "longitude": 127.32},
            ],
            source="TAGO getRouteAcctoThrghSttnList",
            captured_at="2026-08-31T00:00:00Z",
        )
        snapshot = self.catalog.snapshot()
        sequence = snapshot.route_sequences[0]
        self.assertEqual(result["stop_count"], 3)
        self.assertEqual([item.node_id for item in sequence.stops], ["NODE-A", "NODE-X", "NODE-C"])
        self.assertEqual([item.node_order for item in sequence.stops], [1, 4, 9])
        self.assertEqual(sequence.source, "TAGO getRouteAcctoThrghSttnList")
        self.assertEqual(sequence.captured_at, "2026-08-31T00:00:00Z")

    def test_preserve_newer_activation_policy_keeps_live_topology_active(self):
        def stops(label: str) -> list[dict[str, object]]:
            return [
                {
                    "node_id": f"{label}-A",
                    "node_name": f"{label} A",
                    "node_order": 1,
                    "latitude": 36.5,
                    "longitude": 127.3,
                },
                {
                    "node_id": f"{label}-B",
                    "node_name": f"{label} B",
                    "node_order": 2,
                    "latitude": 36.51,
                    "longitude": 127.31,
                },
            ]

        live = self.catalog.hydrate_route_sequence(
            city_code="32010",
            route_id="CCB250000100",
            ordered_stops=stops("LIVE"),
            source="TAGO_ROUTE_STOPS_LIVE_BATCH",
            captured_at="2026-08-31T04:06:18Z",
        )
        revision_after_live = live["revision"]

        older = self.catalog.hydrate_route_sequence(
            city_code="32010",
            route_id="CCB250000100",
            ordered_stops=stops("BULK"),
            source="CHUNCHEON_MUNICIPAL_FILE",
            captured_at="2026-03-26T00:00:00Z",
            activation_policy="preserve_newer",
        )

        active = self.catalog.active_route_sequence_info(
            city_code="32010", route_id="CCB250000100"
        )
        self.assertTrue(older["created"])
        self.assertFalse(older["activated"])
        self.assertTrue(older["skipped_older"])
        self.assertEqual(older["revision"], revision_after_live)
        self.assertEqual(active["sequence_id"], live["sequence_id"])
        self.assertEqual(active["captured_at"], "2026-08-31T04:06:18Z")

        fresher = self.catalog.hydrate_route_sequence(
            city_code="32010",
            route_id="CCB250000100",
            ordered_stops=stops("FRESH"),
            source="TAGO_ROUTE_STOPS_LIVE_BATCH",
            captured_at="2026-09-01T00:00:00Z",
            activation_policy="preserve_newer",
        )
        active = self.catalog.active_route_sequence_info(
            city_code="32010", route_id="CCB250000100"
        )
        self.assertTrue(fresher["activated"])
        self.assertFalse(fresher["skipped_older"])
        self.assertEqual(active["sequence_id"], fresher["sequence_id"])

        with self.assertRaisesRegex(CatalogValidationError, "different active topology hash"):
            self.catalog.hydrate_route_sequence(
                city_code="32010",
                route_id="CCB250000100",
                ordered_stops=stops("SAME-DATE-CONFLICT"),
                source="MOLIT_REGION_BATCH",
                captured_at="2026-09-01T00:00:00Z",
                activation_policy="preserve_newer",
            )
        active_after_conflict = self.catalog.active_route_sequence_info(
            city_code="32010", route_id="CCB250000100"
        )
        self.assertEqual(active_after_conflict["sequence_id"], fresher["sequence_id"])

        with self.assertRaisesRegex(CatalogValidationError, "activation_policy"):
            self.catalog.hydrate_route_sequences_batch(
                [
                    {
                        "city_code": "32010",
                        "route_id": "R-INVALID-POLICY",
                        "ordered_stops": stops("INVALID"),
                        "source": "TEST",
                        "captured_at": "2026-09-01T00:00:00Z",
                    }
                ],
                activation_policy="guess",
            )

    def test_transport_route_id_preserves_hangul_and_rejects_unsafe_characters(self):
        route_id = "GMB수점10"
        result = self.catalog.hydrate_route_sequence(
            city_code="37050",
            route_id=route_id,
            ordered_stops=[
                {"node_id": "A", "node_name": "A", "node_order": 1},
                {"node_id": "B", "node_name": "B", "node_order": 2},
            ],
            source="TAGO_ROUTE_STOPS_LIVE_BATCH",
            captured_at="2026-08-31T00:00:00Z",
        )
        self.assertEqual(result["route_id"], route_id)
        self.assertIsNotNone(
            self.catalog.active_route_sequence_info(
                city_code="37050", route_id=route_id
            )
        )

        for unsafe in ("BAD ROUTE", " BAD", "BAD/ROUTE", "BAD'ROUTE", 'BAD"ROUTE'):
            with self.subTest(route_id=unsafe), self.assertRaises(
                CatalogValidationError
            ):
                self.catalog.hydrate_route_sequence(
                    city_code="37050",
                    route_id=unsafe,
                    ordered_stops=[
                        {"node_id": "A", "node_name": "A", "node_order": 1},
                        {"node_id": "B", "node_name": "B", "node_order": 2},
                    ],
                    source="TEST",
                    captured_at="2026-08-31T00:00:00Z",
                )

        with self.assertRaises(CatalogValidationError):
            self.catalog.upsert_topology_targets(
                provider="한글",
                routes=[],
                discovery_source="TEST",
            )

    def test_small_topology_batch_spans_cities_deterministically(self):
        self.catalog.upsert_topology_targets(
            provider="TAGO",
            routes=[
                {"city_code": city, "route_id": route, "route_no": route}
                for city in ("12", "13", "14")
                for route in (f"R{city}A", f"R{city}B")
            ],
            discovery_source="TEST",
        )

        claimed: list[tuple[str, str]] = []
        for _ in range(4):
            target = self.catalog.claim_topology_target(
                provider="TAGO", run_id="coverage-run"
            )
            self.assertIsNotNone(target)
            claimed.append((target["city_code"], target["route_id"]))
            with self.catalog.connect() as connection:
                connection.execute(
                    "UPDATE topology_progress SET status='COMPLETE' "
                    "WHERE provider='TAGO' AND city_code=? AND route_id=?",
                    claimed[-1],
                )
                connection.commit()

        self.assertEqual(
            claimed,
            [
                ("12", "R12A"),
                ("13", "R13A"),
                ("14", "R14A"),
                ("12", "R12B"),
            ],
        )

    def test_topology_claim_preserves_resume_status_priority(self):
        routes = [
            {"city_code": city, "route_id": route, "route_no": route}
            for city, route in (
                ("10", "PENDING_ROUTE"),
                ("15", "EXHAUSTED_FAILED_ROUTE"),
                ("20", "FAILED_ROUTE"),
                ("30", "DEFERRED_ROUTE"),
                ("40", "RESUME_ROUTE"),
                ("40", "COMPLETE_ROUTE"),
            )
        ]
        self.catalog.upsert_topology_targets(
            provider="TAGO", routes=routes, discovery_source="TEST"
        )
        with self.catalog.connect() as connection:
            connection.execute(
                "UPDATE topology_progress SET status='FAILED',attempts=2 WHERE route_id='FAILED_ROUTE'"
            )
            connection.execute(
                "UPDATE topology_progress SET status='FAILED',attempts=3 "
                "WHERE route_id='EXHAUSTED_FAILED_ROUTE'"
            )
            connection.execute(
                "UPDATE topology_progress SET status='DEFERRED' WHERE route_id='DEFERRED_ROUTE'"
            )
            connection.execute(
                "UPDATE topology_progress SET status='IN_PROGRESS',last_run_id='old-run' "
                "WHERE route_id='RESUME_ROUTE'"
            )
            connection.execute(
                "UPDATE topology_progress SET status='COMPLETE' WHERE route_id='COMPLETE_ROUTE'"
            )
            connection.commit()

        claimed = [
            self.catalog.claim_topology_target(provider="TAGO", run_id="resume-run")
            for _ in range(4)
        ]
        self.assertEqual(
            [target["route_id"] for target in claimed],
            ["RESUME_ROUTE", "DEFERRED_ROUTE", "FAILED_ROUTE", "PENDING_ROUTE"],
        )
        self.assertIsNone(
            self.catalog.claim_topology_target(provider="TAGO", run_id="resume-run")
        )
        with self.catalog.connect() as connection:
            attempts = dict(
                connection.execute(
                    "SELECT route_id,attempts FROM topology_progress "
                    "WHERE route_id IN ('FAILED_ROUTE','EXHAUSTED_FAILED_ROUTE')"
                ).fetchall()
            )
        self.assertEqual(attempts, {"FAILED_ROUTE": 3, "EXHAUSTED_FAILED_ROUTE": 3})

    def test_failed_retry_discards_staging_while_checkpoints_resume(self):
        self.catalog.upsert_topology_targets(
            provider="TAGO",
            routes=[
                {"city_code": "25", "route_id": route, "route_no": route}
                for route in ("FAILED", "DEFERRED", "INTERRUPTED")
            ],
            discovery_source="TEST",
        )
        for route in ("FAILED", "DEFERRED", "INTERRUPTED"):
            self.catalog.stage_topology_page(
                provider="TAGO",
                city_code="25",
                route_id=route,
                page_no=1,
                items=[{"node_id": route + "-A"}],
                total_count=2,
            )
        with self.catalog.connect() as connection:
            connection.execute(
                "UPDATE topology_progress SET status='FAILED',attempts=1 "
                "WHERE route_id='FAILED'"
            )
            connection.execute(
                "UPDATE topology_progress SET status='DEFERRED' "
                "WHERE route_id='DEFERRED'"
            )
            connection.execute(
                "UPDATE topology_progress SET status='IN_PROGRESS',last_run_id='old-run' "
                "WHERE route_id='INTERRUPTED'"
            )
            connection.commit()

        claimed = [
            self.catalog.claim_topology_target(provider="TAGO", run_id="new-run")
            for _ in range(3)
        ]
        self.assertEqual(
            [target["route_id"] for target in claimed],
            ["INTERRUPTED", "DEFERRED", "FAILED"],
        )
        self.assertEqual([target["next_page"] for target in claimed], [2, 2, 1])
        self.assertEqual([target["staged_count"] for target in claimed], [1, 1, 0])
        with self.catalog.connect() as connection:
            pages = dict(
                connection.execute(
                    "SELECT route_id,COUNT(*) FROM topology_pages GROUP BY route_id"
                ).fetchall()
            )
        self.assertEqual(pages, {"DEFERRED": 1, "INTERRUPTED": 1})

    def test_repair_requeues_only_proven_mixed_retry_pages(self):
        routes = (
            "CORRUPT", "CORRUPT_EMPTY_FINAL", "GENUINE_ZERO", "GENUINE_ONE", "UNPROVEN"
        )
        self.catalog.upsert_topology_targets(
            provider="TAGO",
            routes=[
                {"city_code": "25", "route_id": route, "route_no": route}
                for route in routes
            ],
            discovery_source="TEST",
        )
        self.catalog.stage_topology_page(
            provider="TAGO", city_code="25", route_id="CORRUPT",
            page_no=1, items=[], total_count=0,
        )
        self.catalog.stage_topology_page(
            provider="TAGO", city_code="25", route_id="CORRUPT",
            page_no=2, items=[], total_count=45,
        )
        self.catalog.stage_topology_page(
            provider="TAGO", city_code="25", route_id="CORRUPT_EMPTY_FINAL",
            page_no=1, items=[], total_count=0,
        )
        self.catalog.stage_topology_page(
            provider="TAGO", city_code="25", route_id="CORRUPT_EMPTY_FINAL",
            page_no=2, items=[], total_count=3,
        )
        self.catalog.stage_topology_page(
            provider="TAGO", city_code="25", route_id="GENUINE_ZERO",
            page_no=1, items=[], total_count=0,
        )
        self.catalog.stage_topology_page(
            provider="TAGO", city_code="25", route_id="GENUINE_ONE",
            page_no=1, items=[{"node_id": "ONLY"}], total_count=1,
        )
        self.catalog.stage_topology_page(
            provider="TAGO", city_code="25", route_id="UNPROVEN",
            page_no=1, items=[], total_count=0,
        )
        with self.catalog.connect() as connection:
            connection.execute(
                "UPDATE topology_progress SET status='FAILED',attempts=3,"
                "error_code='INVALID_ROUTE_TOPOLOGY',"
                "error_message='complete route-stop sequence was not staged' "
                "WHERE route_id IN ('CORRUPT','UNPROVEN')"
            )
            connection.execute(
                "UPDATE topology_progress SET status='FAILED',attempts=3,"
                "error_code='INVALID_ROUTE_TOPOLOGY',"
                "error_message='ordered_stops must contain 2..10000 rows' "
                "WHERE route_id IN ('CORRUPT_EMPTY_FINAL','GENUINE_ZERO','GENUINE_ONE')"
            )
            connection.commit()

        self.assertEqual(
            self.catalog.repair_corrupt_topology_retries(provider="TAGO"), 2
        )
        self.assertEqual(
            self.catalog.repair_corrupt_topology_retries(provider="TAGO"), 0
        )
        with self.catalog.connect() as connection:
            state = {
                row["route_id"]: (
                    row["status"], row["attempts"], row["next_page"],
                    row["pages_fetched"], row["staged_count"], row["error_code"],
                )
                for row in connection.execute(
                    "SELECT route_id,status,attempts,next_page,pages_fetched,"
                    "staged_count,error_code FROM topology_progress "
                    "WHERE route_id IN ('CORRUPT','CORRUPT_EMPTY_FINAL',"
                    "'GENUINE_ZERO','GENUINE_ONE','UNPROVEN')"
                )
            }
            page_counts = dict(
                connection.execute(
                    "SELECT route_id,COUNT(*) FROM topology_pages GROUP BY route_id"
                ).fetchall()
            )
        self.assertEqual(state["CORRUPT"], ("PENDING", 0, 1, 0, 0, None))
        self.assertEqual(
            state["CORRUPT_EMPTY_FINAL"], ("PENDING", 0, 1, 0, 0, None)
        )
        self.assertEqual(state["GENUINE_ZERO"][0:2], ("FAILED", 3))
        self.assertEqual(state["GENUINE_ONE"][0:2], ("FAILED", 3))
        self.assertEqual(state["UNPROVEN"][0:2], ("FAILED", 3))
        self.assertNotIn("CORRUPT", page_counts)
        self.assertNotIn("CORRUPT_EMPTY_FINAL", page_counts)
        self.assertEqual(
            {key: page_counts[key] for key in ("GENUINE_ZERO", "GENUINE_ONE", "UNPROVEN")},
            {"GENUINE_ZERO": 1, "GENUINE_ONE": 1, "UNPROVEN": 1},
        )

    def test_snapshot_and_cache_are_immutable(self):
        self.catalog.hydrate_route_sequence(
            city_code="25",
            route_id="ROUTE-A",
            ordered_stops=[
                {"node_id": "A", "node_name": "A", "node_order": 1},
                {"node_id": "B", "node_name": "B", "node_order": 2},
            ],
            source="TEST",
            captured_at="2026-08-31T00:00:00Z",
        )
        first = self.catalog.snapshot()
        second = self.catalog.snapshot()
        self.assertIs(first, second)
        with self.assertRaises(FrozenInstanceError):
            first.version = "mutated"
        with self.assertRaises(TypeError):
            first.route_sequences[0].stops[0]["node_id"] = "mutated"

    def test_search_is_literal_and_limits_are_enforced(self):
        self.catalog.import_routes_csv(
            csv_bytes(
                ROUTE_COLUMNS,
                [route_row(**{"노선 아이디": "ROUTE-PERCENT", "노선명": "10%순환"}), route_row(**{"노선 아이디": "ROUTE-PLAIN", "노선명": "100순환"})],
            ),
            source_url="https://tago.go.kr/routes.csv",
            source_date="2026-08-30",
        )
        self.assertEqual([item["route_id"] for item in self.catalog.search_routes("%")], ["ROUTE-PERCENT"])
        with self.assertRaises(CatalogLimitError):
            self.catalog.search_routes("", limit=101)
        with self.assertRaises(CatalogLimitError):
            self.catalog.search_routes("x" * 101)

    def test_planning_stop_reference_resolves_one_selected_static_stop(self):
        self.catalog.import_stops_csv(
            csv_bytes(
                STOP_COLUMNS,
                [
                    stop_row(
                        **{
                            "정류장번호": "OCB276000024",
                            "정류장명": "옥천버스앞",
                            "도시코드": "33330",
                            "위도": "36.299573",
                            "경도": "127.566392",
                        }
                    )
                ],
            ),
            source_url="https://data.go.kr/stops.csv",
            source_date="2026-08-30",
        )

        reference = self.catalog.planning_stop_reference(
            node_id="OCB276000024", city_code="33330"
        )

        self.assertEqual(reference["node_name"], "옥천버스앞")
        self.assertEqual(reference["latitude"], 36.299573)
        self.assertIsNone(
            self.catalog.planning_stop_reference(
                node_id="OCB276000024", city_code="25"
            )
        )

    def test_planning_stop_reference_preserves_exact_topology_stop_without_coordinates(self):
        self.catalog.hydrate_route_sequence(
            city_code="25",
            route_id="NO-COORDINATES",
            ordered_stops=[
                {"node_id": "EXACT-A", "node_name": "좌표 미제공 기점", "node_order": 1},
                {"node_id": "EXACT-B", "node_name": "좌표 미제공 종점", "node_order": 2},
            ],
            source="TEST",
            captured_at="2026-08-31T00:00:00Z",
        )

        reference = self.catalog.planning_stop_reference(
            node_id="EXACT-A", city_code="25"
        )

        self.assertEqual(reference["node_name"], "좌표 미제공 기점")
        self.assertIsNone(reference["latitude"])
        self.assertIsNone(reference["longitude"])

    def test_sqlite_planning_context_is_lightweight_and_indexes_are_present(self):
        self.catalog.hydrate_route_sequence(
            city_code="25",
            route_id="ROUTE-A",
            ordered_stops=[
                {
                    "node_id": "NODE-A",
                    "node_name": "기점",
                    "node_order": 1,
                    "latitude": 36.5,
                    "longitude": 127.3,
                },
                {
                    "node_id": "NODE-B",
                    "node_name": "종점",
                    "node_order": 2,
                    "latitude": 36.51,
                    "longitude": 127.31,
                },
            ],
            source="TEST",
            captured_at="2026-08-31T00:00:00Z",
        )

        context = self.catalog.planning_route_context([("25", "ROUTE-A")])

        self.assertEqual(context.route_sequences, ())
        self.assertEqual(context.stops, ())
        self.assertEqual(context.routes[0].route_no, "ROUTE-A")
        with self.catalog.connect() as connection:
            indexes = {
                str(row["name"])
                for table in ("route_sequence_stops", "active_route_sequences")
                for row in connection.execute(f"PRAGMA index_list({table})")
            }
        self.assertTrue(
            {
                "idx_active_route_sequences_sequence",
                "idx_route_sequence_stops_node_lookup",
                "idx_route_sequence_stops_coordinate_lookup",
            }.issubset(indexes)
        )

    def test_stop_search_uses_sequential_filter_plans_for_contains_queries(self):
        """Avoid cold-cache random reads through composite PK indexes.

        Stop autocomplete uses a leading-wildcard contains query.  Seeking the
        active source in ``catalog_stops`` or looping over every sequence via
        the composite route-stop index causes one table lookup per row.  The
        explicit scans below are bounded by the catalog size and keep reads
        sequential; this plan assertion prevents SQLite statistics from
        silently reintroducing that I/O amplification.
        """

        plans: dict[str, list[str]] = {}
        original_connect = self.catalog.connect

        class PlanRecordingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, params=()):
                label = None
                if "FROM catalog_stops cs NOT INDEXED WHERE" in sql:
                    label = "static"
                elif "FROM route_sequence_stops s NOT INDEXED" in sql:
                    label = "hydrated"
                if label:
                    plans[label] = [
                        str(row[3])
                        for row in self.connection.execute(
                            "EXPLAIN QUERY PLAN " + sql, params
                        )
                    ]
                return self.connection.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        @contextmanager
        def recording_connect():
            with original_connect() as connection:
                yield PlanRecordingConnection(connection)

        with patch.object(self.catalog, "connect", recording_connect):
            results = self.catalog.search_stops("역", limit=8)

        self.assertLessEqual(len(results), 8)
        self.assertIn("static", plans)
        self.assertIn("hydrated", plans)
        self.assertTrue(any(detail == "SCAN cs" for detail in plans["static"]))
        self.assertTrue(any(detail == "SCAN s" for detail in plans["hydrated"]))
        self.assertFalse(
            any(
                "SEARCH cs USING INDEX sqlite_autoindex_catalog_stops_1" in detail
                for detail in plans["static"]
            )
        )
        self.assertFalse(
            any(
                "SEARCH s USING INDEX sqlite_autoindex_route_sequence_stops_2" in detail
                for detail in plans["hydrated"]
            )
        )

    def test_sqlite_planning_context_uses_official_municipal_route_label(self):
        source = json.dumps(
            {
                "dataset": "서울시 버스 노선별 정류소 정보",
                "route_name": "N61",
                "source_date": "2026-08-04",
            },
            ensure_ascii=False,
        )
        self.catalog.hydrate_route_sequence(
            city_code="11",
            route_id="100100589",
            ordered_stops=[
                {"node_id": "100000001", "node_name": "기점", "node_order": 1},
                {"node_id": "100000002", "node_name": "종점", "node_order": 2},
            ],
            source=source,
            captured_at="2026-08-04T00:00:00Z",
        )

        context = self.catalog.planning_route_context([("11", "100100589")])

        self.assertEqual(context.routes[0].route_no, "N61")
        self.assertEqual(self.catalog.planning_snapshot().routes[0].route_no, "N61")

    def test_malicious_or_conflicting_csv_is_rejected_transactionally(self):
        self.catalog.import_stops_csv(
            csv_bytes(STOP_COLUMNS, [stop_row()]),
            source_url="https://data.go.kr/stops.csv",
            source_date="2026-08-30",
        )
        malicious = csv_bytes(STOP_COLUMNS, [stop_row(**{"정류장번호": "NODE');DROP_TABLE--"})])
        with self.assertRaises(CatalogValidationError):
            self.catalog.import_stops_csv(
                malicious,
                source_url="https://data.go.kr/malicious.csv",
                source_date="2026-08-31",
            )
        duplicates = csv_bytes(STOP_COLUMNS, [stop_row(), stop_row(**{"정류장명": "충돌"})])
        with self.assertRaises(CatalogValidationError):
            self.catalog.import_stops_csv(
                duplicates,
                source_url="https://data.go.kr/duplicates.csv",
                source_date="2026-08-31",
            )
        self.assertEqual(self.catalog.search_stops("시청")[0]["node_id"], "NODE-A")
        with self.assertRaises(CatalogValidationError):
            self.catalog.import_routes_csv(
                csv_bytes(ROUTE_COLUMNS, [route_row()]),
                source_url="file:///etc/passwd",
                source_date="2026-08-31",
            )

    def test_size_row_cell_and_sequence_bounds_fail_before_unbounded_work(self):
        tiny = NetworkCatalog(self.root / "tiny.sqlite3", max_csv_bytes=256, max_rows=2, clock=lambda: FIXED_NOW)
        row_bounded = NetworkCatalog(self.root / "row-bounded.sqlite3", max_csv_bytes=4096, max_rows=2, clock=lambda: FIXED_NOW)
        with self.assertRaises(CatalogLimitError):
            row_bounded.import_stops_csv(
                b"x" * 257,
                source_url="https://data.go.kr/too-large.csv",
                source_date="2026-08-31",
            )
        with self.assertRaises(CatalogLimitError):
            tiny.import_stops_csv(
                csv_bytes(STOP_COLUMNS, [stop_row(**{"정류장번호": "A"}), stop_row(**{"정류장번호": "B"}), stop_row(**{"정류장번호": "C"})]),
                source_url="https://data.go.kr/too-many.csv",
                source_date="2026-08-31",
            )
        with self.assertRaises(CatalogLimitError):
            self.catalog.import_stops_csv(
                csv_bytes(STOP_COLUMNS, [stop_row(**{"정류장명": "가" * 513})]),
                source_url="https://data.go.kr/long-cell.csv",
                source_date="2026-08-31",
            )
        with self.assertRaises(CatalogValidationError):
            self.catalog.hydrate_route_sequence(
                city_code="25",
                route_id="ROUTE-A",
                ordered_stops=[
                    {"node_id": "A", "node_name": "A", "node_order": 2},
                    {"node_id": "B", "node_name": "B", "node_order": 1},
                ],
                source="TEST",
                captured_at="2026-08-31T00:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
