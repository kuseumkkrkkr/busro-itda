"""Disk-backed, bounded importer for an official static GTFS ZIP.

The ZIP is verified and streamed into a temporary SQLite staging database on
the catalog drive.  No stop_times-sized Python collection is materialized.
Only after the complete feed, references, calendars, and directed patterns are
valid does ``NetworkCatalog`` copy and activate it in one main-DB transaction.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
from typing import Any, BinaryIO, Callable, Iterable
import zipfile

from network_catalog import CatalogError, CatalogLimitError, NetworkCatalog


REQUIRED_FILES = (
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "calendar.txt",
)
OPTIONAL_FILES = ("calendar_dates.txt",)
TABLE_COLUMNS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "stops.txt": (("stop_id", "stop_name"), ("stop_lat", "stop_lon")),
    "routes.txt": (
        ("route_id", "route_type"),
        ("route_short_name", "route_long_name"),
    ),
    "trips.txt": (
        ("route_id", "service_id", "trip_id"),
        ("direction_id", "trip_headsign"),
    ),
    "stop_times.txt": (
        ("trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"),
        ("pickup_type", "drop_off_type"),
    ),
    "calendar.txt": (
        (
            "service_id", "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday", "start_date", "end_date",
        ),
        (),
    ),
    "calendar_dates.txt": (("service_id", "date", "exception_type"), ()),
}
DEFAULT_FILE_ROW_LIMITS = {
    "stops.txt": 1_000_000,
    "routes.txt": 500_000,
    "trips.txt": 3_000_000,
    "stop_times.txt": 25_000_000,
    "calendar.txt": 1_000_000,
    "calendar_dates.txt": 2_000_000,
}
MAX_PATTERN_STOPS = 10_000
MAX_PATTERNS = 500_000
MIN_STAGE_PREFLIGHT_BYTES = 16 * 1024 * 1024
STAGE_DISK_RESERVE_BYTES = 512 * 1024 * 1024
STAGE_DISK_COPIES = 3  # staging DB, main DB growth, and main WAL during activation
_CSV_FIELD_LIMIT_LOCK = threading.Lock()
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_PROVIDER = re.compile(r"^[0-9A-Za-z_.:-]{1,24}$")
_GTFS_TIME = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")


class GtfsImportError(ValueError):
    """Unsafe archive, invalid GTFS, or incomplete staging data."""


@dataclass(frozen=True, slots=True)
class GtfsImportLimits:
    max_zip_bytes: int = 2 * 1024 * 1024 * 1024
    max_uncompressed_bytes: int = 12 * 1024 * 1024 * 1024
    max_member_bytes: int = 6 * 1024 * 1024 * 1024
    max_stage_bytes: int = 64 * 1024 * 1024 * 1024
    max_members: int = 128
    max_rows_per_table: int = 25_000_000
    max_total_rows: int = 30_000_000
    max_columns: int = 128
    max_cell_chars: int = 2_048
    max_compression_ratio: float = 250.0
    insert_batch_rows: int = 10_000

    def validate(self) -> None:
        values = (
            self.max_zip_bytes, self.max_uncompressed_bytes, self.max_member_bytes,
            self.max_stage_bytes, self.max_members, self.max_rows_per_table,
            self.max_total_rows, self.max_columns, self.max_cell_chars,
            self.insert_batch_rows,
        )
        if any(not isinstance(value, int) or value <= 0 for value in values):
            raise GtfsImportError("all GTFS import limits must be positive integers")
        if not 1.0 <= float(self.max_compression_ratio) <= 1_000.0:
            raise GtfsImportError("max_compression_ratio must be 1..1000")
        if self.insert_batch_rows > 50_000:
            raise GtfsImportError("insert_batch_rows cannot exceed 50000")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _expected_sha256(value: str) -> str:
    text = "" if value is None else str(value).strip().lower()
    if not _SHA256.fullmatch(text):
        raise GtfsImportError(
            "expected_sha256 is required and must contain 64 hexadecimal characters"
        )
    return text


def _provider(value: str) -> str:
    text = "" if value is None else str(value)
    if not _SAFE_PROVIDER.fullmatch(text):
        raise GtfsImportError("provider must be a 1..24 character safe namespace")
    return text


def _raw_id(value: Any, field: str) -> str:
    text = "" if value is None else str(value)
    if not text:
        raise GtfsImportError(f"{field} is required")
    if len(text) > 512:
        raise CatalogLimitError(f"{field} exceeds 512 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise GtfsImportError(f"{field} contains control characters")
    return text


def _text(value: Any, field: str, *, required: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise GtfsImportError(f"{field} is required")
    if len(text) > 2_048:
        raise CatalogLimitError(f"{field} exceeds 2048 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise GtfsImportError(f"{field} contains control characters")
    return text


def _coordinate(value: Any, field: str, minimum: float, maximum: float) -> float | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise GtfsImportError(f"{field} is not numeric") from exc
    if not minimum <= number <= maximum:
        raise GtfsImportError(f"{field} is outside its geographic range")
    return number


def _date(value: Any, field: str) -> str:
    text = "" if value is None else str(value).strip()
    if not re.fullmatch(r"\d{8}", text):
        raise GtfsImportError(f"{field} must use YYYYMMDD")
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise GtfsImportError(f"{field} is not a valid date") from exc


def _time(value: Any, field: str) -> tuple[str | None, int | None]:
    text = "" if value is None else str(value)
    if not text:
        return None, None
    match = _GTFS_TIME.fullmatch(text)
    if not match:
        raise GtfsImportError(f"{field} must use HH:MM:SS")
    hour, minute, second = (int(part) for part in match.groups())
    if hour > 47:
        raise GtfsImportError(f"{field} hour must be 0..47")
    return text, hour * 3600 + minute * 60 + second


def _optional_type(value: Any, field: str) -> int | None:
    text = "" if value is None else str(value)
    if text == "":
        return None
    try:
        parsed = int(text)
    except ValueError as exc:
        raise GtfsImportError(f"{field} must be 0..3 or empty") from exc
    if parsed not in (0, 1, 2, 3):
        raise GtfsImportError(f"{field} must be 0..3 or empty")
    return parsed


def _namespaced_id(provider: str, kind: str, raw_id: str) -> str:
    marker = {"STOP": "S", "ROUTE": "R", "TRIP": "T", "SERVICE": "V"}[kind]
    digest = hashlib.sha256(
        _canonical(["GTFS", provider, kind, raw_id]).encode("utf-8")
    ).hexdigest()
    return f"GTFS:{provider}:{marker}{digest[:20]}"


def _file_sha256(handle: BinaryIO, *, maximum: int) -> tuple[str, int]:
    """Hash one already-open regular file and rewind the same descriptor."""
    try:
        file_stat = os.fstat(handle.fileno())
        handle.seek(0)
    except (AttributeError, OSError) as exc:
        raise GtfsImportError(f"cannot inspect GTFS ZIP descriptor: {exc}") from exc
    size = int(file_stat.st_size)
    if not stat.S_ISREG(file_stat.st_mode) or size <= 0:
        raise GtfsImportError("GTFS ZIP path must be a non-empty regular file")
    if size > maximum:
        raise CatalogLimitError(f"GTFS ZIP exceeds {maximum} bytes")
    digest = hashlib.sha256()
    total = 0
    while chunk := handle.read(1024 * 1024):
        total += len(chunk)
        if total > maximum:
            raise CatalogLimitError(f"GTFS ZIP exceeds {maximum} bytes")
        digest.update(chunk)
    if total != size:
        raise GtfsImportError("GTFS ZIP size changed while it was being verified")
    handle.seek(0)
    return digest.hexdigest(), total


def _safe_member_name(info: zipfile.ZipInfo) -> str | None:
    raw_name = info.filename
    if (
        "\\" in raw_name
        or "\x00" in raw_name
        or raw_name.startswith("/")
        or len(raw_name) > 512
    ):
        raise GtfsImportError("ZIP member contains an unsafe path separator or NUL")
    normalized_name = raw_name[:-1] if raw_name.endswith("/") else raw_name
    parts = normalized_name.split("/")
    if not 1 <= len(parts) <= 8:
        raise GtfsImportError("ZIP member nesting exceeds eight safe components")
    if any(
        part in {"", ".", ".."}
        or part != part.strip()
        or len(part) > 128
        or ":" in part
        or any(ord(char) < 32 or ord(char) == 127 for char in part)
        for part in parts
    ):
        raise GtfsImportError("ZIP member path is unsafe")
    path = PurePosixPath(*parts)
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        raise GtfsImportError("ZIP symbolic links are not accepted")
    if info.flag_bits & 0x1:
        raise GtfsImportError("encrypted ZIP members are not accepted")
    return None if info.is_dir() else path.as_posix()


def _inspect_archive(
    archive: zipfile.ZipFile, limits: GtfsImportLimits
) -> tuple[dict[str, zipfile.ZipInfo], list[dict[str, Any]]]:
    infos = archive.infolist()
    if len(infos) > limits.max_members:
        raise CatalogLimitError(f"GTFS ZIP exceeds {limits.max_members} members")
    allowed_compression = {
        zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA
    }
    table_names_folded = {name.casefold() for name in TABLE_COLUMNS}
    relevant: dict[str, zipfile.ZipInfo] = {}
    relevant_parents: set[str] = set()
    seen_paths: set[str] = set()
    manifest: list[dict[str, Any]] = []
    total_uncompressed = 0
    for info in infos:
        safe_name = _safe_member_name(info)
        if safe_name is None:
            continue
        folded = safe_name.casefold()
        if folded in seen_paths:
            raise GtfsImportError("ZIP contains duplicate member paths")
        seen_paths.add(folded)
        if info.compress_type not in allowed_compression:
            raise GtfsImportError("ZIP contains an unsupported compression method")
        if info.file_size < 0 or info.compress_size < 0:
            raise GtfsImportError("ZIP member has an invalid size")
        if info.file_size > limits.max_member_bytes:
            raise CatalogLimitError(f"ZIP member exceeds {limits.max_member_bytes} bytes")
        total_uncompressed += info.file_size
        if total_uncompressed > limits.max_uncompressed_bytes:
            raise CatalogLimitError(
                f"GTFS ZIP expands beyond {limits.max_uncompressed_bytes} bytes"
            )
        ratio = info.file_size / max(1, info.compress_size)
        if ratio > limits.max_compression_ratio:
            raise CatalogLimitError(
                f"ZIP member compression ratio exceeds {limits.max_compression_ratio:g}"
            )
        manifest.append(
            {"name": safe_name, "byte_count": info.file_size,
             "compressed_bytes": info.compress_size}
        )
        path = PurePosixPath(safe_name)
        base_name = path.name
        if base_name in TABLE_COLUMNS:
            if base_name in relevant:
                raise GtfsImportError(f"GTFS ZIP contains duplicate {base_name}")
            relevant[base_name] = info
            relevant_parents.add("" if len(path.parts) == 1 else path.parent.as_posix())
        elif base_name.casefold() in table_names_folded:
            raise GtfsImportError("GTFS table names must use standard lower-case spelling")
    missing = set(REQUIRED_FILES) - set(relevant)
    if missing:
        raise GtfsImportError(
            "GTFS ZIP is missing required files: " + ", ".join(sorted(missing))
        )
    if len(relevant_parents) != 1:
        raise GtfsImportError("GTFS tables must share one archive root")
    return relevant, sorted(manifest, key=lambda item: item["name"])


class _DigestingReader(io.RawIOBase):
    """Hash and byte-bound one decompressed ZIP member while CSV consumes it."""

    def __init__(self, source: BinaryIO, *, maximum: int, member_name: str):
        super().__init__()
        self._source = source
        self._maximum = maximum
        self._member_name = member_name
        self.digest = hashlib.sha256()
        self.byte_count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int:
        data = self._source.read(len(buffer))
        if not data:
            return 0
        self.byte_count += len(data)
        if self.byte_count > self._maximum:
            raise CatalogLimitError(
                f"{self._member_name} exceeds the member byte limit"
            )
        self.digest.update(data)
        buffer[:len(data)] = data
        return len(data)


def _stage_disk_limit(
    directory: Path,
    *,
    relevant: dict[str, zipfile.ZipInfo],
    limits: GtfsImportLimits,
) -> int:
    """Return a disk-safe page cap and reject an obviously impossible import."""
    try:
        free_bytes = int(shutil.disk_usage(directory).free)
    except OSError as exc:
        raise GtfsImportError(f"cannot inspect staging disk capacity: {exc}") from exc
    usable_bytes = max(
        0, (free_bytes - STAGE_DISK_RESERVE_BYTES) // STAGE_DISK_COPIES
    )
    source_bytes = sum(info.file_size for info in relevant.values())
    estimated_minimum = min(
        limits.max_stage_bytes,
        max(MIN_STAGE_PREFLIGHT_BYTES, source_bytes * 2),
    )
    if usable_bytes < estimated_minimum:
        raise CatalogLimitError(
            "insufficient free space for bounded GTFS staging "
            "(need at least "
            f"{estimated_minimum * STAGE_DISK_COPIES + STAGE_DISK_RESERVE_BYTES} "
            "bytes free including activation headroom)"
        )
    return min(limits.max_stage_bytes, usable_bytes)


def _initialize_stage(path: Path, *, maximum_bytes: int) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        maximum_pages = maximum_bytes // page_size
        if maximum_pages < 1:
            raise CatalogLimitError(
                f"GTFS staging limit must allow at least one {page_size}-byte page"
            )
        actual_pages = int(
            connection.execute(f"PRAGMA max_page_count={maximum_pages}").fetchone()[0]
        )
        if actual_pages > maximum_pages:
            raise GtfsImportError("SQLite did not accept the bounded staging page limit")
        connection.executescript(
            """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA cache_size=-131072;
        CREATE TABLE stage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE stops (
            raw_stop_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL UNIQUE,
            stop_name TEXT NOT NULL,
            latitude REAL,
            longitude REAL
        );
        CREATE TABLE routes (
            raw_route_id TEXT PRIMARY KEY,
            route_namespace_id TEXT NOT NULL UNIQUE,
            route_short_name TEXT NOT NULL,
            route_long_name TEXT NOT NULL,
            route_type INTEGER NOT NULL,
            is_bus INTEGER NOT NULL CHECK(is_bus IN (0,1))
        );
        CREATE TABLE services (
            raw_service_id TEXT PRIMARY KEY,
            service_namespace_id TEXT NOT NULL UNIQUE,
            monday INTEGER NOT NULL, tuesday INTEGER NOT NULL,
            wednesday INTEGER NOT NULL, thursday INTEGER NOT NULL,
            friday INTEGER NOT NULL, saturday INTEGER NOT NULL, sunday INTEGER NOT NULL,
            start_date TEXT NOT NULL, end_date TEXT NOT NULL
        );
        CREATE TABLE calendar_dates (
            raw_service_id TEXT NOT NULL,
            service_date TEXT NOT NULL,
            exception_type INTEGER NOT NULL,
            PRIMARY KEY(raw_service_id,service_date)
        ) WITHOUT ROWID;
        CREATE TABLE trips (
            raw_trip_id TEXT PRIMARY KEY,
            trip_namespace_id TEXT NOT NULL UNIQUE,
            raw_route_id TEXT NOT NULL,
            raw_service_id TEXT NOT NULL,
            pattern_id TEXT,
            direction_id INTEGER,
            trip_headsign TEXT NOT NULL
        );
        CREATE TABLE stop_times (
            raw_trip_id TEXT NOT NULL,
            stop_sequence INTEGER NOT NULL,
            raw_stop_id TEXT NOT NULL,
            arrival_time TEXT,
            arrival_seconds INTEGER,
            departure_time TEXT,
            departure_seconds INTEGER,
            pickup_type INTEGER,
            drop_off_type INTEGER,
            PRIMARY KEY(raw_trip_id,stop_sequence)
        ) WITHOUT ROWID;
        CREATE TABLE patterns (
            pattern_id TEXT PRIMARY KEY,
            raw_route_id TEXT NOT NULL,
            graph_city_code TEXT NOT NULL,
            graph_route_id TEXT NOT NULL UNIQUE,
            pattern_sha256 TEXT NOT NULL UNIQUE,
            direction_mask INTEGER NOT NULL,
            stop_count INTEGER NOT NULL,
            representative_trip_id TEXT NOT NULL
        );
        CREATE TABLE pattern_stops (
            pattern_id TEXT NOT NULL,
            node_order INTEGER NOT NULL,
            raw_stop_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            direction TEXT NOT NULL,
            pickup_type INTEGER NOT NULL CHECK(pickup_type BETWEEN 0 AND 3),
            drop_off_type INTEGER NOT NULL CHECK(drop_off_type BETWEEN 0 AND 3),
            can_board INTEGER NOT NULL CHECK(can_board IN (0,1)),
            can_alight INTEGER NOT NULL CHECK(can_alight IN (0,1)),
            PRIMARY KEY(pattern_id,node_order)
        ) WITHOUT ROWID;
        """
        )
        return connection
    except sqlite3.DatabaseError as exc:
        connection.close()
        if "full" in str(exc).lower():
            raise CatalogLimitError(
                f"GTFS staging database exceeds its {maximum_bytes}-byte page limit"
            ) from exc
        raise GtfsImportError("cannot initialize bounded GTFS staging database") from exc
    except Exception:
        connection.close()
        raise


def _table_transform(
    canonical_name: str, provider: str
) -> tuple[str, Callable[[dict[str, str], int], tuple[Any, ...]]]:
    if canonical_name == "stops.txt":
        sql = "INSERT INTO stops VALUES(?,?,?,?,?)"

        def transform(row: dict[str, str], number: int) -> tuple[Any, ...]:
            stop_id = _raw_id(row["stop_id"], f"stops row {number} stop_id")
            latitude = _coordinate(row.get("stop_lat"), "stop_lat", -90.0, 90.0)
            longitude = _coordinate(row.get("stop_lon"), "stop_lon", -180.0, 180.0)
            if (latitude is None) != (longitude is None):
                raise GtfsImportError("GTFS stop coordinates must be supplied together")
            return (
                stop_id, _namespaced_id(provider, "STOP", stop_id),
                _text(row["stop_name"], "stop_name", required=True), latitude, longitude,
            )

    elif canonical_name == "routes.txt":
        sql = "INSERT INTO routes VALUES(?,?,?,?,?,?)"

        def transform(row: dict[str, str], number: int) -> tuple[Any, ...]:
            route_id = _raw_id(row["route_id"], f"routes row {number} route_id")
            short_name = _text(row.get("route_short_name"), "route_short_name")
            long_name = _text(row.get("route_long_name"), "route_long_name")
            if not short_name and not long_name:
                raise GtfsImportError("each GTFS route requires a short or long name")
            try:
                route_type = int(row["route_type"])
            except ValueError as exc:
                raise GtfsImportError("route_type must be an integer") from exc
            if not 0 <= route_type <= 9999:
                raise GtfsImportError("route_type is outside the supported range")
            return (
                route_id, _namespaced_id(provider, "ROUTE", route_id), short_name,
                long_name, route_type, int(route_type == 3 or 700 <= route_type <= 799),
            )

    elif canonical_name == "calendar.txt":
        sql = "INSERT INTO services VALUES(?,?,?,?,?,?,?,?,?,?,?)"

        def transform(row: dict[str, str], number: int) -> tuple[Any, ...]:
            service_id = _raw_id(row["service_id"], f"calendar row {number} service_id")
            flags: list[int] = []
            for name in (
                "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
            ):
                try:
                    flag = int(row[name])
                except ValueError as exc:
                    raise GtfsImportError(f"calendar {name} must be 0 or 1") from exc
                if flag not in (0, 1):
                    raise GtfsImportError(f"calendar {name} must be 0 or 1")
                flags.append(flag)
            start = _date(row["start_date"], "calendar start_date")
            end = _date(row["end_date"], "calendar end_date")
            if start > end:
                raise GtfsImportError("calendar start_date cannot follow end_date")
            return (
                service_id, _namespaced_id(provider, "SERVICE", service_id),
                *flags, start, end,
            )

    elif canonical_name == "calendar_dates.txt":
        sql = "INSERT INTO calendar_dates VALUES(?,?,?)"

        def transform(row: dict[str, str], number: int) -> tuple[Any, ...]:
            service_id = _raw_id(
                row["service_id"], f"calendar_dates row {number} service_id"
            )
            try:
                exception_type = int(row["exception_type"])
            except ValueError as exc:
                raise GtfsImportError("exception_type must be 1 or 2") from exc
            if exception_type not in (1, 2):
                raise GtfsImportError("exception_type must be 1 or 2")
            return service_id, _date(row["date"], "calendar_dates date"), exception_type

    elif canonical_name == "trips.txt":
        sql = "INSERT INTO trips VALUES(?,?,?,?,?,?,?)"

        def transform(row: dict[str, str], number: int) -> tuple[Any, ...]:
            trip_id = _raw_id(row["trip_id"], f"trips row {number} trip_id")
            route_id = _raw_id(row["route_id"], f"trips row {number} route_id")
            service_id = _raw_id(row["service_id"], f"trips row {number} service_id")
            raw_direction = row.get("direction_id", "")
            if raw_direction == "":
                direction_id = None
            else:
                try:
                    direction_id = int(raw_direction)
                except ValueError as exc:
                    raise GtfsImportError("direction_id must be 0, 1, or empty") from exc
                if direction_id not in (0, 1):
                    raise GtfsImportError("direction_id must be 0, 1, or empty")
            return (
                trip_id, _namespaced_id(provider, "TRIP", trip_id), route_id,
                service_id, None, direction_id,
                _text(row.get("trip_headsign"), "trip_headsign"),
            )

    elif canonical_name == "stop_times.txt":
        sql = "INSERT INTO stop_times VALUES(?,?,?,?,?,?,?,?,?)"

        def transform(row: dict[str, str], number: int) -> tuple[Any, ...]:
            trip_id = _raw_id(row["trip_id"], f"stop_times row {number} trip_id")
            stop_id = _raw_id(row["stop_id"], f"stop_times row {number} stop_id")
            try:
                sequence = int(row["stop_sequence"])
            except ValueError as exc:
                raise GtfsImportError("stop_sequence must be a non-negative integer") from exc
            if sequence < 0:
                raise GtfsImportError("stop_sequence must be a non-negative integer")
            arrival, arrival_seconds = _time(row["arrival_time"], "arrival_time")
            departure, departure_seconds = _time(row["departure_time"], "departure_time")
            return (
                trip_id, sequence, stop_id, arrival, arrival_seconds, departure,
                departure_seconds, _optional_type(row.get("pickup_type"), "pickup_type"),
                _optional_type(row.get("drop_off_type"), "drop_off_type"),
            )

    else:  # pragma: no cover - all callers use a fixed table map
        raise GtfsImportError("unsupported GTFS table")
    return sql, transform


def _load_table(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    canonical_name: str,
    provider: str,
    limits: GtfsImportLimits,
    stage: sqlite3.Connection,
    remaining_total_rows: int,
) -> tuple[int, str]:
    required, optional = TABLE_COLUMNS[canonical_name]
    selected = required + optional
    insert_sql, transform = _table_transform(canonical_name, provider)
    file_limit = min(limits.max_rows_per_table, DEFAULT_FILE_ROW_LIMITS[canonical_name])
    count = 0
    batch: list[tuple[Any, ...]] = []
    digesting: _DigestingReader | None = None
    try:
        stage.execute("BEGIN")
        with archive.open(info, "r") as raw_handle:
            digesting = _DigestingReader(
                raw_handle, maximum=limits.max_member_bytes,
                member_name=info.filename,
            )
            with io.BufferedReader(digesting, buffer_size=1024 * 1024) as buffered:
                with io.TextIOWrapper(
                    buffered, encoding="utf-8-sig", errors="strict", newline=""
                ) as text_handle:
                    reader = csv.reader(text_handle)
                    try:
                        header = next(reader)
                    except StopIteration as exc:
                        raise GtfsImportError(f"{canonical_name} is empty") from exc
                    if not header or len(header) > limits.max_columns:
                        raise CatalogLimitError(
                            f"{canonical_name} header exceeds {limits.max_columns} columns"
                        )
                    if any(not name or len(name) > 128 for name in header):
                        raise GtfsImportError(f"{canonical_name} contains an invalid header")
                    if len(header) != len(set(header)):
                        raise GtfsImportError(f"{canonical_name} contains duplicate columns")
                    missing = set(required) - set(header)
                    if missing:
                        raise GtfsImportError(
                            f"{canonical_name} is missing columns: {', '.join(sorted(missing))}"
                        )
                    indexes = {name: header.index(name) for name in selected if name in header}
                    for row_number, values in enumerate(reader, start=2):
                        if not values or all(value == "" for value in values):
                            continue
                        if len(values) != len(header):
                            raise GtfsImportError(
                                f"{canonical_name} row {row_number} does not match its header"
                            )
                        if count >= file_limit:
                            raise CatalogLimitError(
                                f"{canonical_name} exceeds {file_limit} rows"
                            )
                        if count >= remaining_total_rows:
                            raise CatalogLimitError(
                                f"GTFS tables exceed {limits.max_total_rows} total rows"
                            )
                        for value in values:
                            if len(value) > limits.max_cell_chars:
                                raise CatalogLimitError(
                                    f"{canonical_name} row {row_number} exceeds the cell limit"
                                )
                            if "\x00" in value:
                                raise GtfsImportError(
                                    f"{canonical_name} row {row_number} contains NUL"
                                )
                        row = {
                            name: values[indexes[name]] if name in indexes else ""
                            for name in selected
                        }
                        batch.append(transform(row, row_number))
                        count += 1
                        if len(batch) >= limits.insert_batch_rows:
                            stage.executemany(insert_sql, batch)
                            batch.clear()
                    if batch:
                        stage.executemany(insert_sql, batch)
        if digesting is None or digesting.byte_count != info.file_size:
            raise GtfsImportError(
                f"ZIP member size changed while reading {info.filename!r}"
            )
        if count == 0 and canonical_name != "calendar_dates.txt":
            raise GtfsImportError(f"{canonical_name} contains no data rows")
        stage.commit()
    except (UnicodeDecodeError, csv.Error, sqlite3.IntegrityError) as exc:
        stage.rollback()
        if isinstance(exc, UnicodeDecodeError):
            raise GtfsImportError(f"{canonical_name} must be UTF-8") from exc
        if isinstance(exc, csv.Error):
            raise GtfsImportError(f"{canonical_name} is not valid bounded CSV") from exc
        raise GtfsImportError(
            f"{canonical_name} contains duplicate or conflicting identifiers"
        ) from exc
    except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
        stage.rollback()
        raise GtfsImportError(f"cannot read ZIP member {info.filename!r}") from exc
    except Exception:
        stage.rollback()
        raise
    assert digesting is not None
    return count, digesting.digest.hexdigest()


def _unknown_reference(stage: sqlite3.Connection, sql: str) -> str | None:
    row = stage.execute(sql).fetchone()
    return None if row is None else str(row[0])


def _validate_references(stage: sqlite3.Connection) -> None:
    checks = (
        (
            "trips route_id",
            "SELECT t.raw_trip_id FROM trips t LEFT JOIN routes r ON r.raw_route_id=t.raw_route_id WHERE r.raw_route_id IS NULL LIMIT 1",
        ),
        (
            "trips service_id",
            "SELECT t.raw_trip_id FROM trips t LEFT JOIN services s ON s.raw_service_id=t.raw_service_id WHERE s.raw_service_id IS NULL LIMIT 1",
        ),
        (
            "stop_times trip_id",
            "SELECT st.raw_trip_id FROM stop_times st LEFT JOIN trips t ON t.raw_trip_id=st.raw_trip_id WHERE t.raw_trip_id IS NULL LIMIT 1",
        ),
        (
            "stop_times stop_id",
            "SELECT st.raw_stop_id FROM stop_times st LEFT JOIN stops s ON s.raw_stop_id=st.raw_stop_id WHERE s.raw_stop_id IS NULL LIMIT 1",
        ),
        (
            "calendar_dates service_id",
            "SELECT c.raw_service_id FROM calendar_dates c LEFT JOIN services s ON s.raw_service_id=c.raw_service_id WHERE s.raw_service_id IS NULL LIMIT 1",
        ),
    )
    for field, sql in checks:
        invalid = _unknown_reference(stage, sql)
        if invalid is not None:
            raise GtfsImportError(f"{field} references an unknown raw ID")


def _validate_stop_time_order(stage: sqlite3.Connection) -> None:
    """Reject explicit backwards time records before atomic activation."""
    current_trip: str | None = None
    previous_time: int | None = None
    for row in stage.execute(
        "SELECT raw_trip_id,stop_sequence,arrival_seconds,departure_seconds "
        "FROM stop_times ORDER BY raw_trip_id,stop_sequence"
    ):
        trip_id = str(row["raw_trip_id"])
        if trip_id != current_trip:
            current_trip = trip_id
            previous_time = None
        arrival = row["arrival_seconds"]
        departure = row["departure_seconds"]
        if arrival is not None:
            arrival = int(arrival)
        if departure is not None:
            departure = int(departure)
        if arrival is not None and departure is not None and arrival > departure:
            raise GtfsImportError(
                f"trip {trip_id!r} stop_sequence {row['stop_sequence']} arrives after departure"
            )
        first_time = arrival if arrival is not None else departure
        last_time = departure if departure is not None else arrival
        if (
            previous_time is not None
            and first_time is not None
            and first_time < previous_time
        ):
            raise GtfsImportError(
                f"trip {trip_id!r} stop_times are not time-monotonic"
            )
        if last_time is not None:
            previous_time = last_time


def _derive_patterns(stage: sqlite3.Connection, provider: str) -> int:
    graph_city_code = f"GTFS-{provider}"
    candidate_sql = (
        "INSERT INTO patterns(pattern_id,raw_route_id,graph_city_code,graph_route_id,"
        "pattern_sha256,direction_mask,stop_count,representative_trip_id) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(pattern_id) DO UPDATE SET "
        "direction_mask=(patterns.direction_mask | excluded.direction_mask),"
        "representative_trip_id=MIN(patterns.representative_trip_id,excluded.representative_trip_id) "
        "WHERE patterns.pattern_sha256=excluded.pattern_sha256 "
        "AND patterns.raw_route_id=excluded.raw_route_id "
        "AND patterns.stop_count=excluded.stop_count"
    )
    stage.execute("BEGIN")
    candidates: list[tuple[Any, ...]] = []
    trip_updates: list[tuple[str, str]] = []
    ordered_trip_sql = (
        "SELECT t.raw_trip_id,t.raw_route_id,t.direction_id,r.is_bus,"
        "st.stop_sequence,st.raw_stop_id,st.pickup_type,st.drop_off_type "
        "FROM stop_times st "
        "CROSS JOIN trips t ON t.raw_trip_id=st.raw_trip_id "
        "CROSS JOIN routes r ON r.raw_route_id=t.raw_route_id "
        "ORDER BY st.raw_trip_id,st.stop_sequence"
    )
    plan = " ".join(
        str(row[3]) for row in stage.execute("EXPLAIN QUERY PLAN " + ordered_trip_sql)
    ).upper()
    if "USE TEMP B-TREE" in plan:
        raise GtfsImportError("GTFS pattern derivation would require an unbounded temp sort")
    cursor = stage.execute(ordered_trip_sql)
    current_trip: str | None = None
    current_route = ""
    current_direction: int | None = None
    current_is_bus = False
    current_stops: list[tuple[str, int, int]] = []
    processed_trips = 0

    def flush_batches() -> None:
        if candidates:
            stage.executemany(candidate_sql, candidates)
            candidates.clear()
        if trip_updates:
            stage.executemany(
                "UPDATE trips SET pattern_id=? WHERE raw_trip_id=?", trip_updates
            )
            trip_updates.clear()

    def finish_trip() -> None:
        nonlocal processed_trips
        if current_trip is None:
            return
        processed_trips += 1
        if len(current_stops) < 2:
            raise GtfsImportError(f"trip {current_trip!r} must contain at least two stop_times")
        if not current_is_bus:
            return
        # Access types are part of the route-state identity. This prevents two
        # trips with the same stop order but different boarding/alighting rules
        # from being collapsed into an unsafe representative pattern.
        pattern_sha = hashlib.sha256(
            _canonical(
                ["GTFS_BUS_PATTERN", provider, current_route, current_stops]
            ).encode("utf-8")
        ).hexdigest()
        route_sha = hashlib.sha256(
            _canonical([provider, current_route]).encode("utf-8")
        ).hexdigest()
        pattern_id = "gpat_" + pattern_sha
        graph_route_id = (
            f"GTFS:{provider}:R{route_sha[:20]}:P{pattern_sha[:40]}"
        )
        direction_mask = 0 if current_direction is None else (1 << current_direction)
        candidates.append(
            (
                pattern_id, current_route, graph_city_code, graph_route_id,
                pattern_sha, direction_mask, len(current_stops), current_trip,
            )
        )
        trip_updates.append((pattern_id, current_trip))
        if len(candidates) >= 10_000:
            flush_batches()

    try:
        for row in cursor:
            trip_id = row["raw_trip_id"]
            if trip_id != current_trip:
                finish_trip()
                current_trip = trip_id
                current_route = row["raw_route_id"]
                current_direction = row["direction_id"]
                current_is_bus = bool(row["is_bus"])
                current_stops = []
            if row["stop_sequence"] is not None:
                if len(current_stops) >= MAX_PATTERN_STOPS:
                    raise CatalogLimitError(
                        f"trip {current_trip!r} exceeds {MAX_PATTERN_STOPS} stops"
                    )
                current_stops.append(
                    (
                        row["raw_stop_id"],
                        int(row["pickup_type"] or 0),
                        int(row["drop_off_type"] or 0),
                    )
                )
        finish_trip()
        flush_batches()
        total_trips = int(stage.execute("SELECT COUNT(*) FROM trips").fetchone()[0])
        if processed_trips != total_trips:
            raise GtfsImportError("one or more trips contain no stop_times")
        pattern_count = int(stage.execute("SELECT COUNT(*) FROM patterns").fetchone()[0])
        if not 0 <= pattern_count <= MAX_PATTERNS:
            raise CatalogLimitError(f"GTFS bus patterns cannot exceed {MAX_PATTERNS} rows")
        stage.execute(
            "INSERT INTO pattern_stops(pattern_id,node_order,raw_stop_id,node_id,node_name,latitude,longitude,direction,pickup_type,drop_off_type,can_board,can_alight) "
            "SELECT p.pattern_id,st.stop_sequence,"
            "st.raw_stop_id,s.node_id,s.stop_name,s.latitude,s.longitude,"
            "CASE p.direction_mask WHEN 1 THEN 'GTFS:0' WHEN 2 THEN 'GTFS:1' ELSE 'GTFS' END,"
            "COALESCE(st.pickup_type,0),COALESCE(st.drop_off_type,0),"
            "CASE WHEN COALESCE(st.pickup_type,0)=0 THEN 1 ELSE 0 END,"
            "CASE WHEN COALESCE(st.drop_off_type,0)=0 THEN 1 ELSE 0 END "
            "FROM patterns p JOIN stop_times st ON st.raw_trip_id=p.representative_trip_id "
            "JOIN stops s ON s.raw_stop_id=st.raw_stop_id"
        )
        expected_stops = int(
            stage.execute("SELECT COALESCE(SUM(stop_count),0) FROM patterns").fetchone()[0]
        )
        actual_stops = int(stage.execute("SELECT COUNT(*) FROM pattern_stops").fetchone()[0])
        if expected_stops != actual_stops:
            raise GtfsImportError("derived GTFS pattern stops are incomplete")
        stage.commit()
        return pattern_count
    except Exception:
        stage.rollback()
        raise


def _build_stage(
    stage_path: Path,
    *,
    archive: zipfile.ZipFile,
    relevant: dict[str, zipfile.ZipInfo],
    provider: str,
    limits: GtfsImportLimits,
    stage_maximum_bytes: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    stage = _initialize_stage(stage_path, maximum_bytes=stage_maximum_bytes)
    provenance: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    total_rows = 0
    try:
        with _CSV_FIELD_LIMIT_LOCK:
            previous_field_limit = csv.field_size_limit()
            csv.field_size_limit(limits.max_cell_chars)
            try:
                for name in REQUIRED_FILES + OPTIONAL_FILES:
                    info = relevant.get(name)
                    if info is None:
                        continue
                    row_count, member_digest = _load_table(
                        archive, info, canonical_name=name, provider=provider,
                        limits=limits, stage=stage,
                        remaining_total_rows=limits.max_total_rows - total_rows,
                    )
                    total_rows += row_count
                    counts[name] = row_count
                    provenance[name] = {
                        "sha256": member_digest,
                        "byte_count": info.file_size,
                        "row_count": row_count,
                    }
            finally:
                csv.field_size_limit(previous_field_limit)
        _validate_references(stage)
        _validate_stop_time_order(stage)
        counts["bus_patterns"] = _derive_patterns(stage, provider)
        stage.executemany(
            "INSERT INTO stage_meta(key,value) VALUES(?,?)",
            (
                ("complete", "1"),
                ("provider", provider),
                ("counts_json", _canonical(counts)),
            ),
        )
        stage.commit()
        integrity = stage.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise GtfsImportError("GTFS staging database failed integrity_check")
    except sqlite3.DatabaseError as exc:
        if "full" in str(exc).lower():
            raise CatalogLimitError(
                f"GTFS staging database reached its {stage_maximum_bytes}-byte page limit"
            ) from exc
        raise GtfsImportError("GTFS staging database operation failed") from exc
    finally:
        stage.close()
    if stage_path.stat().st_size > stage_maximum_bytes:
        raise CatalogLimitError(
            f"GTFS staging database exceeds {stage_maximum_bytes} bytes"
        )
    return provenance, counts


def import_gtfs_zip(
    catalog: NetworkCatalog,
    *,
    zip_path: Path,
    expected_sha256: str,
    source_url: str,
    source_date: str,
    provider: str,
    topology_role: str = "historical_model",
    limits: GtfsImportLimits | None = None,
) -> dict[str, Any]:
    """Stream and atomically store GTFS, historical-model-only by default."""
    selected_limits = limits or GtfsImportLimits()
    selected_limits.validate()
    provider_id = _provider(provider)
    expected = _expected_sha256(expected_sha256)
    path = Path(zip_path).resolve()
    catalog.path.parent.mkdir(parents=True, exist_ok=True)
    try:
        zip_handle = path.open("rb")
    except OSError as exc:
        raise GtfsImportError(f"cannot open GTFS ZIP: {exc}") from exc
    with zip_handle:
        actual_sha256, zip_bytes = _file_sha256(
            zip_handle, maximum=selected_limits.max_zip_bytes
        )
        if actual_sha256 != expected:
            raise GtfsImportError(
                f"GTFS ZIP SHA-256 mismatch: expected {expected}, got {actual_sha256}"
            )
        try:
            archive = zipfile.ZipFile(zip_handle, "r")
        except (zipfile.BadZipFile, OSError) as exc:
            raise GtfsImportError("GTFS input is not a readable ZIP") from exc
        with archive:
            relevant, manifest = _inspect_archive(archive, selected_limits)
            stage_maximum_bytes = _stage_disk_limit(
                catalog.path.parent, relevant=relevant, limits=selected_limits
            )
            with tempfile.TemporaryDirectory(
                prefix="gtfs-stage-", dir=str(catalog.path.parent)
            ) as temporary:
                stage_path = Path(temporary) / "validated.sqlite3"
                provenance, counts = _build_stage(
                    stage_path, archive=archive, relevant=relevant,
                    provider=provider_id, limits=selected_limits,
                    stage_maximum_bytes=stage_maximum_bytes,
                )
                stable_sha256, stable_bytes = _file_sha256(
                    zip_handle, maximum=selected_limits.max_zip_bytes
                )
                if stable_sha256 != expected or stable_bytes != zip_bytes:
                    raise GtfsImportError("GTFS ZIP changed during staging")
                result = catalog.activate_gtfs_staged_feed(
                    stage_path=stage_path,
                    provider=provider_id,
                    source_url=source_url,
                    source_date=source_date,
                    feed_sha256=actual_sha256,
                    member_manifest=manifest,
                    table_provenance=provenance,
                    topology_role=topology_role,
                )
    return {
        **result,
        "zip_bytes": zip_bytes,
        "staging": "DISK_BACKED_VALIDATED_THEN_ATOMICALLY_ACTIVATED",
        "table_provenance": provenance,
        "counts": {**result["counts"], "source_rows_total": sum(
            count for name, count in counts.items() if name.endswith(".txt")
        )},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stream and atomically import an official UTF-8 GTFS ZIP"
    )
    parser.add_argument("--catalog-db", type=Path, required=True)
    parser.add_argument("--zip", dest="zip_path", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-date", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument(
        "--topology-role",
        choices=("historical_model", "active_topology"),
        default="historical_model",
        help="historical_model stores model evidence without activating current routes",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = import_gtfs_zip(
            NetworkCatalog(args.catalog_db), zip_path=args.zip_path,
            expected_sha256=args.expected_sha256, source_url=args.source_url,
            source_date=args.source_date, provider=args.provider,
            topology_role=args.topology_role,
        )
    except (CatalogError, GtfsImportError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GtfsImportError", "GtfsImportLimits", "REQUIRED_FILES", "import_gtfs_zip", "main"
]
