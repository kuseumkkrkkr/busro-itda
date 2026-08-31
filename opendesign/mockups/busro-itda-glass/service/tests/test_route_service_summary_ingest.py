from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from route_service_summary_ingest import (
    RouteServiceSummaryError,
    RouteServiceSummaryProfile,
    import_route_service_summaries,
    parse_route_service_summaries,
)


HEADER = (
    "노선 아이디,요일,일일운행횟수,기점첫차출발시각,기점막차출발시각,"
    "종점첫차출발시각,종점막차출발시각,최소배차간격,최대배차간격\r\n"
)


def fixture(*rows: str) -> tuple[bytes, RouteServiceSummaryProfile]:
    data = (HEADER + "\r\n".join(rows) + "\r\n").encode("cp949")
    route_count = len({row.split(",", 1)[0] for row in rows})
    profile = RouteServiceSummaryProfile(
        dataset_name="fixture",
        official_page_url="https://example.invalid/official",
        source_date="2026-07-16",
        published_date="2026-07-22",
        expected_sha256=hashlib.sha256(data).hexdigest().upper(),
        expected_file_bytes=len(data),
        expected_rows=len(rows),
        expected_unique_routes=route_count,
    )
    return data, profile


class RouteServiceSummaryIngestTests(unittest.TestCase):
    def test_parse_preserves_summary_semantics_and_blank_destination_times(self) -> None:
        data, profile = fixture(
            "254000001,토요일,1,07:20,07:20,,,0,0",
            "254000001,평일,4,06:00,21:00,06:30,21:30,30,90",
        )
        parsed = parse_route_service_summaries(data, profile=profile)
        self.assertEqual(len(parsed.rows), 2)
        self.assertEqual(parsed.rows[0].service_day_code, "2")
        self.assertIsNone(parsed.rows[0].destination_first_departure)
        self.assertEqual(parsed.summary(profile)["semantic_role"],
                         "route_service_summary_not_stop_timetable")

    def test_rejects_duplicate_route_and_service_day(self) -> None:
        data, profile = fixture(
            "254000001,평일,1,07:20,07:20,,,0,0",
            "254000001,평일,2,08:20,08:20,,,0,0",
        )
        with self.assertRaisesRegex(RouteServiceSummaryError, "duplicate"):
            parse_route_service_summaries(data, profile=profile)

    def test_preserves_and_flags_published_invalid_headway_order(self) -> None:
        data, profile = fixture(
            "254000001,매일,1,07:20,07:20,,,60,30",
        )
        parsed = parse_route_service_summaries(data, profile=profile)
        self.assertEqual(
            parsed.rows[0].quality_flags, ("PUBLISHED_MIN_HEADWAY_GT_MAX",)
        )
        self.assertEqual(parsed.summary(profile)["quality_flagged_rows"], 1)

    def test_import_is_idempotent_and_does_not_create_gtfs_rows(self) -> None:
        data, profile = fixture(
            "254000001,평일,4,06:00,21:00,06:30,21:30,30,90",
            "254000002,매일,8,05:30,22:30,,,10,45",
        )
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog.sqlite3"
            with closing(sqlite3.connect(catalog)) as connection:
                connection.execute("CREATE TABLE catalog_meta(key TEXT PRIMARY KEY,value TEXT)")
                connection.commit()
            first = import_route_service_summaries(
                catalog_path=catalog, data=data, profile=profile
            )
            second = import_route_service_summaries(
                catalog_path=catalog, data=data, profile=profile
            )
            self.assertEqual(first["source_id"], second["source_id"])
            with closing(sqlite3.connect(catalog)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM route_service_summaries"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM route_service_summary_meta "
                        "WHERE key='active_source_id'"
                    ).fetchone()[0],
                    first["source_id"],
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='gtfs_stop_times'"
                    ).fetchone()
                )

    def test_validate_only_requires_no_database(self) -> None:
        data, profile = fixture(
            "254000001,공휴일,1,07:20,07:20,,,0,0",
        )
        result = parse_route_service_summaries(data, profile=profile).summary(profile)
        self.assertEqual(result["mode"], "validate_only")
        self.assertEqual(result["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
