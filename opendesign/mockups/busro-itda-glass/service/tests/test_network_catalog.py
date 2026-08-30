from __future__ import annotations

import csv
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


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
