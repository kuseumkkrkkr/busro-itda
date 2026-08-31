"""Strict importer for the official TS-BIS route service summary snapshot.

This dataset contains route-level service-day, first/last departure, run-count,
and headway summaries.  It is intentionally stored outside ``gtfs_*`` because
it does not contain trips or stop-level departure times.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable


TS_BIS_COLUMNS = (
    "노선 아이디",
    "요일",
    "일일운행횟수",
    "기점첫차출발시각",
    "기점막차출발시각",
    "종점첫차출발시각",
    "종점막차출발시각",
    "최소배차간격",
    "최대배차간격",
)
SERVICE_DAY_CODES = {
    "비정기": "0",
    "평일": "1",
    "토요일": "2",
    "공휴일": "3",
    "매일": "4",
}
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_ROWS = 20_000
_DIGITS = re.compile(r"^[0-9]+$")
_TIME = re.compile(r"^([0-9]{2}):([0-9]{2})$")


class RouteServiceSummaryError(ValueError):
    """The source snapshot is unsafe, altered, or structurally invalid."""


@dataclass(frozen=True, slots=True)
class RouteServiceSummaryProfile:
    dataset_name: str
    official_page_url: str
    source_date: str
    published_date: str
    expected_sha256: str
    expected_file_bytes: int
    expected_rows: int
    expected_unique_routes: int
    encoding: str = "cp949"


TS_BIS_PROFILE = RouteServiceSummaryProfile(
    dataset_name="한국교통안전공단_버스 노선 및 시간표 정보",
    official_page_url="https://www.data.go.kr/data/15150451/fileData.do",
    source_date="2026-07-16",
    published_date="2026-07-22",
    expected_sha256=(
        "7F1435826D73EC70E80F603603784C900AB48D8ABB1DC44767912D791E11580A"
    ),
    expected_file_bytes=430_571,
    expected_rows=10_563,
    expected_unique_routes=7_043,
)


@dataclass(frozen=True, slots=True)
class RouteServiceSummaryRow:
    route_id: str
    service_day_code: str
    service_day_label: str
    daily_runs: int
    origin_first_departure: str | None
    origin_last_departure: str | None
    destination_first_departure: str | None
    destination_last_departure: str | None
    min_headway_minutes: int
    max_headway_minutes: int
    quality_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedRouteServiceSummaries:
    rows: tuple[RouteServiceSummaryRow, ...]
    file_sha256: str
    unique_route_count: int

    def summary(
        self, profile: RouteServiceSummaryProfile = TS_BIS_PROFILE
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "validate_only",
            "dataset": profile.dataset_name,
            "source_date": profile.source_date,
            "published_date": profile.published_date,
            "file_sha256": self.file_sha256,
            "row_count": len(self.rows),
            "unique_route_count": self.unique_route_count,
            "quality_flagged_rows": sum(bool(row.quality_flags) for row in self.rows),
            "semantic_role": "route_service_summary_not_stop_timetable",
        }


def _bounded_text(
    value: Any, field: str, *, maximum: int, required: bool = True
) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise RouteServiceSummaryError(f"{field} is required")
    if len(text) > maximum:
        raise RouteServiceSummaryError(f"{field} exceeds {maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise RouteServiceSummaryError(f"{field} contains control characters")
    return text


def _iso_date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise RouteServiceSummaryError(f"{field} must be YYYY-MM-DD") from exc


def _integer(
    value: Any, field: str, *, minimum: int, maximum: int
) -> int:
    text = _bounded_text(value, field, maximum=12)
    if not _DIGITS.fullmatch(text):
        raise RouteServiceSummaryError(f"{field} must be published integer digits")
    parsed = int(text)
    if not minimum <= parsed <= maximum:
        raise RouteServiceSummaryError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return parsed


def _departure_time(value: Any, field: str) -> str | None:
    text = _bounded_text(value, field, maximum=5, required=False)
    if not text:
        return None
    match = _TIME.fullmatch(text)
    if match is None:
        raise RouteServiceSummaryError(f"{field} must be HH:MM or blank")
    hour, minute = (int(part) for part in match.groups())
    if hour > 47 or minute > 59:
        raise RouteServiceSummaryError(f"{field} is outside the service-day clock")
    return f"{hour:02d}:{minute:02d}"


def parse_route_service_summaries(
    data: bytes,
    *,
    profile: RouteServiceSummaryProfile = TS_BIS_PROFILE,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ParsedRouteServiceSummaries:
    if not isinstance(data, bytes):
        raise RouteServiceSummaryError("CSV input must be bytes")
    if not 1 <= len(data) <= max_bytes:
        raise RouteServiceSummaryError(f"CSV must be 1..{max_bytes} bytes")
    digest = hashlib.sha256(data).hexdigest().upper()
    if len(data) != profile.expected_file_bytes:
        raise RouteServiceSummaryError("CSV byte size does not match the snapshot")
    if digest != profile.expected_sha256.upper():
        raise RouteServiceSummaryError("CSV SHA-256 does not match the snapshot")
    _iso_date(profile.source_date, "source_date")
    _iso_date(profile.published_date, "published_date")
    try:
        text = data.decode(profile.encoding)
    except UnicodeDecodeError as exc:
        raise RouteServiceSummaryError(
            f"CSV is not valid {profile.encoding}"
        ) from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != TS_BIS_COLUMNS:
        raise RouteServiceSummaryError("CSV columns do not match the official schema")

    parsed: list[RouteServiceSummaryRow] = []
    keys: set[tuple[str, str]] = set()
    for line_number, raw in enumerate(reader, start=2):
        if len(parsed) >= max_rows:
            raise RouteServiceSummaryError(f"CSV exceeds the {max_rows} row limit")
        if None in raw:
            raise RouteServiceSummaryError(f"line {line_number} has extra columns")
        route_id = _bounded_text(raw["노선 아이디"], "노선 아이디", maximum=32)
        if not _DIGITS.fullmatch(route_id):
            raise RouteServiceSummaryError("노선 아이디 must contain digits only")
        label = _bounded_text(raw["요일"], "요일", maximum=8)
        code = SERVICE_DAY_CODES.get(label)
        if code is None:
            raise RouteServiceSummaryError(f"unsupported 요일 value: {label}")
        key = (route_id, code)
        if key in keys:
            raise RouteServiceSummaryError(
                f"duplicate route/service-day key at line {line_number}"
            )
        keys.add(key)
        min_headway = _integer(
            raw["최소배차간격"], "최소배차간격", minimum=0, maximum=2_880
        )
        max_headway = _integer(
            raw["최대배차간격"], "최대배차간격", minimum=0, maximum=2_880
        )
        quality_flags = (
            ("PUBLISHED_MIN_HEADWAY_GT_MAX",)
            if min_headway > max_headway
            else ()
        )
        parsed.append(
            RouteServiceSummaryRow(
                route_id=route_id,
                service_day_code=code,
                service_day_label=label,
                daily_runs=_integer(
                    raw["일일운행횟수"], "일일운행횟수", minimum=0, maximum=10_000
                ),
                origin_first_departure=_departure_time(
                    raw["기점첫차출발시각"], "기점첫차출발시각"
                ),
                origin_last_departure=_departure_time(
                    raw["기점막차출발시각"], "기점막차출발시각"
                ),
                destination_first_departure=_departure_time(
                    raw["종점첫차출발시각"], "종점첫차출발시각"
                ),
                destination_last_departure=_departure_time(
                    raw["종점막차출발시각"], "종점막차출발시각"
                ),
                min_headway_minutes=min_headway,
                max_headway_minutes=max_headway,
                quality_flags=quality_flags,
            )
        )
    if len(parsed) != profile.expected_rows:
        raise RouteServiceSummaryError("CSV row count does not match the snapshot")
    unique_routes = len({row.route_id for row in parsed})
    if unique_routes != profile.expected_unique_routes:
        raise RouteServiceSummaryError(
            "CSV unique route count does not match the snapshot"
        )
    return ParsedRouteServiceSummaries(
        rows=tuple(parsed), file_sha256=digest, unique_route_count=unique_routes
    )


def _source_id(profile: RouteServiceSummaryProfile, digest: str) -> str:
    compact_date = profile.source_date.replace("-", "")
    return f"ts_bis_route_service_{compact_date}_{digest[:12].lower()}"


def import_route_service_summaries(
    *,
    catalog_path: str | Path,
    data: bytes,
    profile: RouteServiceSummaryProfile = TS_BIS_PROFILE,
    **limits: Any,
) -> dict[str, Any]:
    parsed = parse_route_service_summaries(data, profile=profile, **limits)
    path = Path(catalog_path).resolve()
    if not path.is_file():
        raise RouteServiceSummaryError("catalog database does not exist")
    source_id = _source_id(profile, parsed.file_sha256)
    imported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS route_service_summary_sources (
                source_id TEXT PRIMARY KEY,
                dataset_name TEXT NOT NULL,
                official_page_url TEXT NOT NULL,
                source_date TEXT NOT NULL,
                published_date TEXT NOT NULL,
                file_sha256 TEXT NOT NULL UNIQUE,
                file_bytes INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                unique_route_count INTEGER NOT NULL,
                semantic_role TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS route_service_summaries (
                source_id TEXT NOT NULL REFERENCES route_service_summary_sources(source_id)
                    ON DELETE CASCADE,
                route_id TEXT NOT NULL,
                service_day_code TEXT NOT NULL,
                service_day_label TEXT NOT NULL,
                daily_runs INTEGER NOT NULL,
                origin_first_departure TEXT,
                origin_last_departure TEXT,
                destination_first_departure TEXT,
                destination_last_departure TEXT,
                min_headway_minutes INTEGER NOT NULL,
                max_headway_minutes INTEGER NOT NULL,
                quality_flags_json TEXT NOT NULL,
                PRIMARY KEY(source_id,route_id,service_day_code)
            );
            CREATE INDEX IF NOT EXISTS idx_route_service_summaries_route
                ON route_service_summaries(route_id,service_day_code);
            CREATE TABLE IF NOT EXISTS route_service_summary_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO route_service_summary_sources VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET imported_at=excluded.imported_at",
            (
                source_id,
                profile.dataset_name,
                profile.official_page_url,
                profile.source_date,
                profile.published_date,
                parsed.file_sha256,
                len(data),
                len(parsed.rows),
                parsed.unique_route_count,
                "route_service_summary_not_stop_timetable",
                imported_at,
            ),
        )
        connection.execute(
            "DELETE FROM route_service_summaries WHERE source_id=?", (source_id,)
        )
        connection.executemany(
            "INSERT INTO route_service_summaries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    source_id,
                    row.route_id,
                    row.service_day_code,
                    row.service_day_label,
                    row.daily_runs,
                    row.origin_first_departure,
                    row.origin_last_departure,
                    row.destination_first_departure,
                    row.destination_last_departure,
                    row.min_headway_minutes,
                    row.max_headway_minutes,
                    json.dumps(row.quality_flags, separators=(",", ":")),
                )
                for row in parsed.rows
            ],
        )
        inserted = connection.execute(
            "SELECT COUNT(*) FROM route_service_summaries WHERE source_id=?",
            (source_id,),
        ).fetchone()[0]
        if int(inserted) != len(parsed.rows):
            raise RouteServiceSummaryError("imported summary row count is inconsistent")
        connection.execute(
            "INSERT INTO route_service_summary_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("active_source_id", source_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        **parsed.summary(profile),
        "mode": "import",
        "source_id": source_id,
        "active": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or import the official TS-BIS route service summary CSV"
    )
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--catalog-db", type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.apply != (args.catalog_db is not None):
        print(
            json.dumps(
                {"ok": False, "error": "--apply and --catalog-db must be supplied together"},
                ensure_ascii=False,
            )
        )
        return 2
    try:
        data = args.csv.read_bytes()
        result = (
            import_route_service_summaries(
                catalog_path=args.catalog_db, data=data
            )
            if args.apply
            else parse_route_service_summaries(data).summary()
        )
    except (OSError, sqlite3.Error, RouteServiceSummaryError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "RouteServiceSummaryError",
    "RouteServiceSummaryProfile",
    "TS_BIS_COLUMNS",
    "TS_BIS_PROFILE",
    "import_route_service_summaries",
    "parse_route_service_summaries",
]
