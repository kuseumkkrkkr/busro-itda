"""Bounded, provenance-preserving nationwide bus catalog storage.

CSV catalogs and authoritative route-stop sequences are deliberately separate.
Imported IDs are never joined by resemblance, name, or geographic proximity.
Only explicit authoritative hydration or atomic GTFS activation can create
ordered route topology.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections import Counter, OrderedDict
import copy
import heapq
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlparse

from route_topology_anomalies import (
    SINGLE_POINT_ROUTE_SPIKE_ERROR_CODE,
    SinglePointRouteSpike,
    single_point_route_spike,
)


STOP_COLUMNS = (
    "정류장번호",
    "정류장명",
    "위도",
    "경도",
    "정보수집일",
    "모바일단축번호",
    "도시코드",
    "도시명",
    "관리도시명",
)
ROUTE_COLUMNS = (
    "노선 아이디",
    "노선명",
    "기점노드 아이디",
    "종점노드 아이디",
    "기점정류장",
    "종점정류장",
    "지자체코드",
    "지자체명",
)

DEFAULT_MAX_CSV_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_ROWS = 500_000
MAX_CELL_CHARS = 512
MAX_SEQUENCE_STOPS = 10_000
MAX_SEQUENCE_BATCH = 20_000
MAX_SEARCH_LIMIT = 100
MAX_QUERY_CHARS = 100
MAX_TOPOLOGY_PAGE_ITEMS = 100
MAX_TOPOLOGY_PAGE_BYTES = 512 * 1024
MAX_GTFS_ID_CHARS = 512
MAX_GTFS_EVIDENCE_TRIPS = 100
MAX_GTFS_EVIDENCE_STOP_TIMES = 10_000
MAX_GTFS_PATTERNS = 500_000
MAX_GTFS_STAGE_BYTES = 64 * 1024 * 1024 * 1024
MAX_GTFS_SCHEDULE_EXPANSIONS = 20_000
MAX_GTFS_SCHEDULE_DEPARTURES_PER_STOP = 256
MAX_GTFS_SCHEDULE_TRIP_STOPS = 512
MAX_GTFS_SCHEDULE_HORIZON_SECONDS = 24 * 60 * 60
MAX_GTFS_SCHEDULE_PARALLEL_SEARCHES = 4
MAX_GTFS_SCHEDULE_CACHED_TRIPS = 4_096
MAX_GTFS_SCHEDULE_CACHED_STOP_ROWS = 250_000
MAX_GTFS_SCHEDULE_ACTIVE_SERVICE_ROWS = 750_000
GTFS_SCHEDULE_WALL_CLOCK_SECONDS = 12.0
GTFS_SCHEDULE_CACHE_TTL_SECONDS = 30.0
MAX_GTFS_SCHEDULE_CACHE_ENTRIES = 32
GTFS_SCHEDULE_ADMISSION_TIMEOUT_SECONDS = 0.25
GTFS_TRANSFER_BUFFER_SECONDS = 5 * 60
SEOUL_TIMEZONE = timezone(timedelta(hours=9), name="Asia/Seoul")
_CODE = re.compile(r"^[0-9A-Za-z_.:-]{1,96}$")
_TRANSPORT_IDENTIFIER = re.compile(r"^[0-9A-Za-z가-힣_.:-]{1,96}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")

_SCHEDULE_COORDINATOR_LOCK = threading.Lock()
_SCHEDULE_PROCESS_SLOTS = threading.BoundedSemaphore(
    MAX_GTFS_SCHEDULE_PARALLEL_SEARCHES
)
_SCHEDULE_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_SCHEDULE_FLIGHTS: dict[str, dict[str, Any]] = {}


class CatalogError(ValueError):
    """Base error for unsafe or invalid catalog input."""


class CatalogLimitError(CatalogError):
    """Raised when bounded work or input limits are exceeded."""


class CatalogValidationError(CatalogError):
    """Raised when source data does not satisfy the explicit schema."""


@dataclass(frozen=True, slots=True)
class StopRecord:
    city_code: str
    node_id: str
    node_name: str
    latitude: float
    longitude: float
    collected_date: str
    mobile_short_no: str
    city_name: str
    managing_city_name: str
    source_id: str


@dataclass(frozen=True, slots=True)
class RouteRecord:
    city_code: str
    route_id: str
    route_no: str
    start_node_id: str
    end_node_id: str
    start_stop_name: str
    end_stop_name: str
    municipality_name: str
    source_id: str


@dataclass(frozen=True, slots=True)
class RouteStopRecord:
    city_code: str
    route_id: str
    node_id: str
    node_order: int
    node_name: str
    latitude: float | None
    longitude: float | None
    direction: str
    can_board: bool = True
    can_alight: bool = True


@dataclass(frozen=True, slots=True)
class RouteSequence:
    city_code: str
    route_id: str
    source: str
    captured_at: str
    sha256: str
    stops: tuple[RouteStopRecord, ...]


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Immutable input for a planner graph build."""

    version: str
    revision: int
    stops: tuple[StopRecord, ...]
    routes: tuple[RouteRecord, ...]
    route_sequences: tuple[RouteSequence, ...]
    catalog_route_count: int | None = None
    topology_target_count: int | None = None
    topology_complete_count: int | None = None
    topology_discovery_complete: bool | None = None
    topology_hydrated_count: int | None = None


def _route_stop_payload(stop: RouteStopRecord) -> dict[str, Any]:
    """Keep legacy all-access sequence hashes stable while hashing restrictions."""
    payload = asdict(stop)
    if stop.can_board and stop.can_alight:
        payload.pop("can_board")
        payload.pop("can_alight")
    return payload


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_text(value: Any, field: str, *, required: bool = False, maximum: int = MAX_CELL_CHARS) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise CatalogValidationError(f"{field} is required")
    if len(text) > maximum:
        raise CatalogLimitError(f"{field} exceeds {maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise CatalogValidationError(f"{field} contains control characters")
    return text


def _safe_code(value: Any, field: str) -> str:
    text = _safe_text(value, field, required=True, maximum=96)
    if not _CODE.fullmatch(text):
        raise CatalogValidationError(f"{field} has an invalid identifier")
    return text


def _public_route_label(source: Any, fallback: Any) -> str:
    """Read an optional official label from bounded provenance JSON.

    Municipal sequences do not necessarily have a matching TAGO target row.
    Their importer retains the published route name in immutable provenance,
    so presentation can use it without inventing a cross-provider ID join.
    Malformed and legacy sources safely keep the route-ID fallback.
    """

    default = str(fallback or "")
    try:
        payload = json.loads(str(source))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    if not isinstance(payload, dict):
        return default
    candidates: list[Any] = [payload.get("route_no"), payload.get("route_name")]
    route_numbers = payload.get("route_numbers")
    if isinstance(route_numbers, list):
        candidates.extend(route_numbers[:20])
    for candidate in candidates:
        try:
            label = _safe_text(candidate, "route label", maximum=80)
        except CatalogError:
            continue
        if label:
            return label
    return default


def _safe_transport_identifier(value: Any, field: str) -> str:
    """Validate an exact provider-owned route identifier.

    TAGO route IDs can contain Hangul (for example ``GMB수점10``). Keep
    that raw identifier unchanged while allowing only ASCII alphanumerics,
    modern Hangul syllables, underscore, dot, colon, and hyphen. Whitespace,
    path separators, quotes, controls, and every other character stay invalid.
    """
    text = "" if value is None else str(value)
    if not text:
        raise CatalogValidationError(f"{field} is required")
    if len(text) > 96:
        raise CatalogLimitError(f"{field} exceeds 96 characters")
    if any(char.isspace() for char in text):
        raise CatalogValidationError(f"{field} contains whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise CatalogValidationError(f"{field} contains control characters")
    if not _TRANSPORT_IDENTIFIER.fullmatch(text):
        raise CatalogValidationError(f"{field} has an invalid transport identifier")
    return text


def _raw_gtfs_id(value: Any, field: str) -> str:
    """Preserve a provider-owned GTFS ID exactly without guessing joins."""
    text = "" if value is None else str(value)
    if not text:
        raise CatalogValidationError(f"{field} is required")
    if len(text) > MAX_GTFS_ID_CHARS:
        raise CatalogLimitError(f"{field} exceeds {MAX_GTFS_ID_CHARS} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise CatalogValidationError(f"{field} contains control characters")
    return text


def _sha256(value: Any, field: str = "sha256") -> str:
    text = _safe_text(value, field, required=True, maximum=64).lower()
    if not _SHA256.fullmatch(text):
        raise CatalogValidationError(f"{field} must be a 64-character SHA-256")
    return text


def _gtfs_namespaced_id(provider: str, kind: str, raw_id: str) -> str:
    marker = {"STOP": "S", "ROUTE": "R", "TRIP": "T", "SERVICE": "V"}[kind]
    digest = hashlib.sha256(
        _canonical(["GTFS", provider, kind, raw_id]).encode("utf-8")
    ).hexdigest()
    return f"GTFS:{provider}:{marker}{digest[:20]}"


def _coordinate(value: Any, field: str, minimum: float, maximum: float) -> float:
    text = _safe_text(value, field, required=True, maximum=32)
    try:
        number = float(text)
    except ValueError as exc:
        raise CatalogValidationError(f"{field} is not numeric") from exc
    if not minimum <= number <= maximum:
        raise CatalogValidationError(f"{field} is outside its geographic range")
    return number


def _optional_coordinate(value: Any, field: str, minimum: float, maximum: float) -> float | None:
    text = _safe_text(value, field, maximum=32)
    return None if not text else _coordinate(text, field, minimum, maximum)


def _source_url(value: str) -> str:
    text = _safe_text(value, "source_url", required=True, maximum=2048)
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CatalogValidationError("source_url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise CatalogValidationError("source_url cannot contain credentials")
    return text


def _source_date(value: str) -> str:
    text = _safe_text(value, "source_date", required=True, maximum=10)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise CatalogValidationError("source_date must use YYYY-MM-DD") from exc


def _timestamp(value: str) -> str:
    text = _safe_text(value, "captured_at", required=True, maximum=40)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogValidationError("captured_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise CatalogValidationError("captured_at must include an offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class NetworkCatalog:
    """Owns a dedicated SQLite catalog database, never the runtime history DB."""

    def __init__(
        self,
        path: Path,
        *,
        max_csv_bytes: int = DEFAULT_MAX_CSV_BYTES,
        max_rows: int = DEFAULT_MAX_ROWS,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.path = Path(path)
        self.max_csv_bytes = max(1, min(int(max_csv_bytes), DEFAULT_MAX_CSV_BYTES))
        self.max_rows = max(1, min(int(max_rows), DEFAULT_MAX_ROWS))
        self.clock = clock
        self._cache_lock = threading.RLock()
        self._snapshot_cache: CatalogSnapshot | None = None
        self._planning_cache: CatalogSnapshot | None = None
        self._schedule_slots = _SCHEDULE_PROCESS_SLOTS
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            existing_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if existing_tables and "catalog_meta" not in existing_tables:
                raise CatalogValidationError(
                    "database path already contains a non-catalog schema; use a dedicated catalog DB"
                )
            if "active_gtfs_feeds" in existing_tables:
                active_feed_columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(active_gtfs_feeds)"
                    ).fetchall()
                }
                if (
                    "topology_role" not in active_feed_columns
                    and connection.execute(
                        "SELECT 1 FROM active_gtfs_feeds LIMIT 1"
                    ).fetchone()
                    is not None
                ):
                    raise CatalogValidationError(
                        "legacy active GTFS feed roles are ambiguous; rebuild the "
                        "catalog before migration"
                    )
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS catalog_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO catalog_meta(key,value) VALUES('revision','0');
                CREATE TABLE IF NOT EXISTS catalog_sources (
                    source_id TEXT PRIMARY KEY,
                    dataset_kind TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_date TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    encoding TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(dataset_kind,source_url,source_date,sha256)
                );
                CREATE TABLE IF NOT EXISTS catalog_import_quality (
                    source_id TEXT PRIMARY KEY REFERENCES catalog_sources(source_id) ON DELETE CASCADE,
                    input_row_count INTEGER NOT NULL,
                    imported_row_count INTEGER NOT NULL,
                    rejected_row_count INTEGER NOT NULL,
                    rejection_reasons_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_stops (
                    source_id TEXT NOT NULL REFERENCES catalog_sources(source_id) ON DELETE CASCADE,
                    city_code TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    collected_date TEXT NOT NULL,
                    mobile_short_no TEXT NOT NULL,
                    city_name TEXT NOT NULL,
                    managing_city_name TEXT NOT NULL,
                    PRIMARY KEY(source_id,city_code,node_id)
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_stops_search
                    ON catalog_stops(city_code,node_name,node_id);
                CREATE INDEX IF NOT EXISTS idx_catalog_stops_source_node
                    ON catalog_stops(source_id,node_id,city_code);
                CREATE TABLE IF NOT EXISTS catalog_routes (
                    source_id TEXT NOT NULL REFERENCES catalog_sources(source_id) ON DELETE CASCADE,
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    route_no TEXT NOT NULL,
                    start_node_id TEXT NOT NULL,
                    end_node_id TEXT NOT NULL,
                    start_stop_name TEXT NOT NULL,
                    end_stop_name TEXT NOT NULL,
                    municipality_name TEXT NOT NULL,
                    PRIMARY KEY(source_id,city_code,route_id)
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_routes_search
                    ON catalog_routes(city_code,route_no,route_id);
                CREATE TABLE IF NOT EXISTS route_sequence_versions (
                    sequence_id TEXT PRIMARY KEY,
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    stop_count INTEGER NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(city_code,route_id,source,captured_at,sha256)
                );
                CREATE TABLE IF NOT EXISTS route_sequence_stops (
                    sequence_id TEXT NOT NULL REFERENCES route_sequence_versions(sequence_id) ON DELETE CASCADE,
                    node_order INTEGER NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    latitude REAL,
                    longitude REAL,
                    direction TEXT NOT NULL,
                    can_board INTEGER NOT NULL DEFAULT 1 CHECK(can_board IN (0,1)),
                    can_alight INTEGER NOT NULL DEFAULT 1 CHECK(can_alight IN (0,1)),
                    PRIMARY KEY(sequence_id,node_order),
                    UNIQUE(sequence_id,node_id,node_order)
                );
                CREATE TABLE IF NOT EXISTS active_route_sequences (
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    sequence_id TEXT NOT NULL REFERENCES route_sequence_versions(sequence_id),
                    PRIMARY KEY(city_code,route_id)
                );
                CREATE INDEX IF NOT EXISTS idx_active_route_sequences_sequence
                    ON active_route_sequences(sequence_id,city_code,route_id);
                CREATE INDEX IF NOT EXISTS idx_route_sequence_stops_node_lookup
                    ON route_sequence_stops(node_id,sequence_id,node_order);
                CREATE INDEX IF NOT EXISTS idx_route_sequence_stops_coordinate_lookup
                    ON route_sequence_stops(latitude,longitude,sequence_id,node_order)
                    WHERE latitude IS NOT NULL AND longitude IS NOT NULL;
                CREATE TABLE IF NOT EXISTS gtfs_feed_versions (
                    feed_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_date TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    member_manifest_json TEXT NOT NULL,
                    UNIQUE(provider,source_url,source_date,sha256)
                );
                CREATE TABLE IF NOT EXISTS active_gtfs_feeds (
                    provider TEXT PRIMARY KEY,
                    feed_id TEXT NOT NULL REFERENCES gtfs_feed_versions(feed_id),
                    topology_role TEXT NOT NULL DEFAULT 'historical_model'
                        CHECK(topology_role IN ('historical_model','active_topology'))
                );
                CREATE TABLE IF NOT EXISTS gtfs_feed_tables (
                    feed_id TEXT NOT NULL REFERENCES gtfs_feed_versions(feed_id) ON DELETE CASCADE,
                    file_name TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_count INTEGER NOT NULL,
                    row_count INTEGER NOT NULL,
                    PRIMARY KEY(feed_id,file_name),
                    CHECK(byte_count >= 0 AND row_count >= 0)
                );
                CREATE TABLE IF NOT EXISTS gtfs_id_aliases (
                    feed_id TEXT NOT NULL REFERENCES gtfs_feed_versions(feed_id) ON DELETE CASCADE,
                    entity_type TEXT NOT NULL,
                    raw_id TEXT NOT NULL,
                    namespaced_id TEXT NOT NULL,
                    PRIMARY KEY(feed_id,entity_type,raw_id),
                    CHECK(entity_type IN ('STOP','ROUTE','TRIP','SERVICE'))
                );
                CREATE INDEX IF NOT EXISTS idx_gtfs_alias_namespace
                    ON gtfs_id_aliases(feed_id,entity_type,namespaced_id);
                CREATE TABLE IF NOT EXISTS gtfs_stops (
                    feed_id TEXT NOT NULL REFERENCES gtfs_feed_versions(feed_id) ON DELETE CASCADE,
                    raw_stop_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    stop_name TEXT NOT NULL,
                    latitude REAL,
                    longitude REAL,
                    PRIMARY KEY(feed_id,raw_stop_id),
                    UNIQUE(feed_id,node_id)
                );
                CREATE TABLE IF NOT EXISTS gtfs_routes (
                    feed_id TEXT NOT NULL REFERENCES gtfs_feed_versions(feed_id) ON DELETE CASCADE,
                    raw_route_id TEXT NOT NULL,
                    route_namespace_id TEXT NOT NULL,
                    route_short_name TEXT NOT NULL,
                    route_long_name TEXT NOT NULL,
                    route_type INTEGER NOT NULL,
                    PRIMARY KEY(feed_id,raw_route_id),
                    UNIQUE(feed_id,route_namespace_id)
                );
                CREATE TABLE IF NOT EXISTS gtfs_services (
                    feed_id TEXT NOT NULL REFERENCES gtfs_feed_versions(feed_id) ON DELETE CASCADE,
                    raw_service_id TEXT NOT NULL,
                    service_namespace_id TEXT NOT NULL,
                    monday INTEGER NOT NULL,
                    tuesday INTEGER NOT NULL,
                    wednesday INTEGER NOT NULL,
                    thursday INTEGER NOT NULL,
                    friday INTEGER NOT NULL,
                    saturday INTEGER NOT NULL,
                    sunday INTEGER NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    PRIMARY KEY(feed_id,raw_service_id),
                    UNIQUE(feed_id,service_namespace_id),
                    CHECK(monday IN (0,1) AND tuesday IN (0,1) AND wednesday IN (0,1)
                      AND thursday IN (0,1) AND friday IN (0,1) AND saturday IN (0,1)
                      AND sunday IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS gtfs_calendar_dates (
                    feed_id TEXT NOT NULL REFERENCES gtfs_feed_versions(feed_id) ON DELETE CASCADE,
                    raw_service_id TEXT NOT NULL,
                    service_date TEXT NOT NULL,
                    exception_type INTEGER NOT NULL,
                    PRIMARY KEY(feed_id,raw_service_id,service_date),
                    FOREIGN KEY(feed_id,raw_service_id)
                      REFERENCES gtfs_services(feed_id,raw_service_id) ON DELETE CASCADE,
                    CHECK(exception_type IN (1,2))
                );
                CREATE INDEX IF NOT EXISTS idx_gtfs_calendar_dates_day
                    ON gtfs_calendar_dates(feed_id,service_date,raw_service_id);
                CREATE TABLE IF NOT EXISTS gtfs_patterns (
                    feed_id TEXT NOT NULL REFERENCES gtfs_feed_versions(feed_id) ON DELETE CASCADE,
                    pattern_id TEXT NOT NULL,
                    raw_route_id TEXT NOT NULL,
                    graph_city_code TEXT NOT NULL,
                    graph_route_id TEXT NOT NULL,
                    pattern_sha256 TEXT NOT NULL,
                    direction_id INTEGER,
                    stop_count INTEGER NOT NULL,
                    representative_trip_id TEXT NOT NULL,
                    sequence_id TEXT NOT NULL REFERENCES route_sequence_versions(sequence_id),
                    PRIMARY KEY(feed_id,pattern_id),
                    UNIQUE(feed_id,graph_city_code,graph_route_id),
                    FOREIGN KEY(feed_id,raw_route_id)
                      REFERENCES gtfs_routes(feed_id,raw_route_id) ON DELETE CASCADE,
                    CHECK(direction_id IS NULL OR direction_id IN (0,1)),
                    CHECK(stop_count >= 2)
                );
                CREATE TABLE IF NOT EXISTS gtfs_trips (
                    feed_id TEXT NOT NULL REFERENCES gtfs_feed_versions(feed_id) ON DELETE CASCADE,
                    raw_trip_id TEXT NOT NULL,
                    trip_namespace_id TEXT NOT NULL,
                    raw_route_id TEXT NOT NULL,
                    raw_service_id TEXT NOT NULL,
                    pattern_id TEXT,
                    direction_id INTEGER,
                    trip_headsign TEXT NOT NULL,
                    PRIMARY KEY(feed_id,raw_trip_id),
                    UNIQUE(feed_id,trip_namespace_id),
                    FOREIGN KEY(feed_id,raw_route_id)
                      REFERENCES gtfs_routes(feed_id,raw_route_id) ON DELETE CASCADE,
                    FOREIGN KEY(feed_id,raw_service_id)
                      REFERENCES gtfs_services(feed_id,raw_service_id) ON DELETE CASCADE,
                    FOREIGN KEY(feed_id,pattern_id)
                      REFERENCES gtfs_patterns(feed_id,pattern_id) ON DELETE CASCADE,
                    CHECK(direction_id IS NULL OR direction_id IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS gtfs_stop_times (
                    feed_id TEXT NOT NULL REFERENCES gtfs_feed_versions(feed_id) ON DELETE CASCADE,
                    raw_trip_id TEXT NOT NULL,
                    stop_sequence INTEGER NOT NULL,
                    raw_stop_id TEXT NOT NULL,
                    arrival_time TEXT,
                    arrival_seconds INTEGER,
                    departure_time TEXT,
                    departure_seconds INTEGER,
                    pickup_type INTEGER,
                    drop_off_type INTEGER,
                    PRIMARY KEY(feed_id,raw_trip_id,stop_sequence),
                    FOREIGN KEY(feed_id,raw_trip_id)
                      REFERENCES gtfs_trips(feed_id,raw_trip_id) ON DELETE CASCADE,
                    FOREIGN KEY(feed_id,raw_stop_id)
                      REFERENCES gtfs_stops(feed_id,raw_stop_id) ON DELETE CASCADE,
                    CHECK(stop_sequence >= 0),
                    CHECK(arrival_seconds IS NULL OR arrival_seconds BETWEEN 0 AND 172799),
                    CHECK(departure_seconds IS NULL OR departure_seconds BETWEEN 0 AND 172799),
                    CHECK(pickup_type IS NULL OR pickup_type BETWEEN 0 AND 3),
                    CHECK(drop_off_type IS NULL OR drop_off_type BETWEEN 0 AND 3)
                );
                CREATE INDEX IF NOT EXISTS idx_gtfs_trips_pattern
                    ON gtfs_trips(feed_id,pattern_id,raw_trip_id);
                CREATE INDEX IF NOT EXISTS idx_gtfs_stop_times_stop
                    ON gtfs_stop_times(feed_id,raw_stop_id,raw_trip_id);
                CREATE INDEX IF NOT EXISTS idx_gtfs_stop_times_departure
                    ON gtfs_stop_times(
                      feed_id,raw_stop_id,departure_seconds,raw_trip_id,stop_sequence
                    );
                CREATE INDEX IF NOT EXISTS idx_gtfs_trips_service_pattern
                    ON gtfs_trips(feed_id,raw_service_id,pattern_id,raw_trip_id);
                CREATE TABLE IF NOT EXISTS topology_targets (
                    provider TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    route_no TEXT NOT NULL,
                    discovery_source TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    PRIMARY KEY(provider,city_code,route_id)
                );
                CREATE INDEX IF NOT EXISTS idx_topology_targets_city
                    ON topology_targets(provider,city_code,route_id);
                CREATE TABLE IF NOT EXISTS topology_progress (
                    provider TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    next_page INTEGER NOT NULL DEFAULT 1,
                    total_count INTEGER,
                    pages_fetched INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    requests_used INTEGER NOT NULL DEFAULT 0,
                    staged_count INTEGER NOT NULL DEFAULT 0,
                    content_sha256 TEXT,
                    sequence_id TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    last_run_id TEXT,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY(provider,city_code,route_id),
                    FOREIGN KEY(provider,city_code,route_id)
                      REFERENCES topology_targets(provider,city_code,route_id)
                      ON DELETE CASCADE,
                    CHECK(status IN ('PENDING','IN_PROGRESS','COMPLETE','UNCHANGED','FAILED','DEFERRED')),
                    CHECK(next_page >= 1),
                    CHECK(pages_fetched >= 0 AND requests_used >= 0 AND staged_count >= 0)
                );
                CREATE INDEX IF NOT EXISTS idx_topology_progress_queue
                    ON topology_progress(provider,status,city_code,route_id);
                CREATE TABLE IF NOT EXISTS topology_pages (
                    provider TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    page_no INTEGER NOT NULL,
                    item_count INTEGER NOT NULL,
                    total_count INTEGER NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(provider,city_code,route_id,page_no),
                    FOREIGN KEY(provider,city_code,route_id)
                      REFERENCES topology_targets(provider,city_code,route_id)
                      ON DELETE CASCADE,
                    CHECK(page_no >= 1 AND item_count >= 0 AND total_count >= 0)
                );
                CREATE TABLE IF NOT EXISTS topology_runs (
                    run_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    target_source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_budget INTEGER NOT NULL,
                    requests_used INTEGER NOT NULL DEFAULT 0,
                    target_limit INTEGER,
                    targets_processed INTEGER NOT NULL DEFAULT 0,
                    succeeded INTEGER NOT NULL DEFAULT 0,
                    unchanged INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    deferred INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS topology_discovered_cities (
                    provider TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    city_name TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    PRIMARY KEY(provider,city_code)
                );
                CREATE TABLE IF NOT EXISTS topology_discovery_progress (
                    provider TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    next_page INTEGER NOT NULL DEFAULT 1,
                    total_count INTEGER,
                    requests_used INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    error_message TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider,scope_key),
                    CHECK(status IN ('PENDING','IN_PROGRESS','COMPLETE','FAILED','DEFERRED'))
                );
                """
            )
            route_stop_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(route_sequence_stops)"
                ).fetchall()
            }
            missing_access_columns = {
                "can_board", "can_alight"
            } - route_stop_columns
            if missing_access_columns:
                legacy_gtfs = connection.execute(
                    "SELECT 1 FROM gtfs_patterns LIMIT 1"
                ).fetchone()
                if legacy_gtfs is not None:
                    raise CatalogValidationError(
                        "legacy GTFS catalog must be rebuilt before access-rule migration"
                    )
                if "can_board" in missing_access_columns:
                    connection.execute(
                        "ALTER TABLE route_sequence_stops ADD COLUMN "
                        "can_board INTEGER NOT NULL DEFAULT 1 CHECK(can_board IN (0,1))"
                    )
                if "can_alight" in missing_access_columns:
                    connection.execute(
                        "ALTER TABLE route_sequence_stops ADD COLUMN "
                        "can_alight INTEGER NOT NULL DEFAULT 1 CHECK(can_alight IN (0,1))"
                    )
            active_feed_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(active_gtfs_feeds)"
                ).fetchall()
            }
            if "topology_role" not in active_feed_columns:
                connection.execute(
                    "ALTER TABLE active_gtfs_feeds ADD COLUMN topology_role TEXT "
                    "NOT NULL DEFAULT 'historical_model' "
                    "CHECK(topology_role IN ('historical_model','active_topology'))"
                )
            connection.commit()

    def _read_source(self, source: Path | bytes | bytearray) -> bytes:
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        else:
            path = Path(source)
            if path.stat().st_size > self.max_csv_bytes:
                raise CatalogLimitError(f"CSV exceeds {self.max_csv_bytes} bytes")
            with path.open("rb") as handle:
                data = handle.read(self.max_csv_bytes + 1)
        if len(data) > self.max_csv_bytes:
            raise CatalogLimitError(f"CSV exceeds {self.max_csv_bytes} bytes")
        if not data:
            raise CatalogValidationError("CSV is empty")
        return data

    @staticmethod
    def _decode(data: bytes) -> tuple[str, str]:
        try:
            return data.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            try:
                return data.decode("cp949"), "cp949"
            except UnicodeDecodeError as exc:
                raise CatalogValidationError("CSV must be UTF-8 or CP949") from exc

    def _rows(self, text: str, required_columns: Sequence[str]) -> Iterator[tuple[int, Mapping[str, str]]]:
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""))
            if reader.fieldnames is None:
                raise CatalogValidationError("CSV header is missing")
            fields = [_safe_text(item, "CSV header", required=True, maximum=80) for item in reader.fieldnames]
            if len(fields) != len(set(fields)):
                raise CatalogValidationError("CSV contains duplicate headers")
            reader.fieldnames = fields
            missing = [field for field in required_columns if field not in fields]
            if missing:
                raise CatalogValidationError(f"CSV is missing columns: {', '.join(missing)}")
            for row_number, row in enumerate(reader, start=2):
                if row_number - 1 > self.max_rows:
                    raise CatalogLimitError(f"CSV exceeds {self.max_rows} rows")
                if None in row:
                    raise CatalogValidationError(f"row {row_number} has extra columns")
                if not any(str(value or "").strip() for value in row.values()):
                    continue
                yield row_number, row
        except csv.Error as exc:
            raise CatalogValidationError(f"malformed CSV: {exc}") from exc

    def import_stops_csv(
        self,
        source: Path | bytes | bytearray,
        *,
        source_url: str,
        source_date: str,
        quarantine_invalid_rows: bool = False,
    ) -> dict[str, Any]:
        data = self._read_source(source)
        text, encoding = self._decode(data)
        records: list[StopRecord] = []
        input_row_count = 0
        rejection_reasons: Counter[str] = Counter()
        for row_number, row in self._rows(text, STOP_COLUMNS):
            input_row_count += 1
            try:
                records.append(
                    StopRecord(
                        city_code=_safe_code(row["도시코드"], "도시코드"),
                        node_id=_safe_code(row["정류장번호"], "정류장번호"),
                        node_name=_safe_text(row["정류장명"], "정류장명", required=True),
                        latitude=_coordinate(row["위도"], "위도", -90.0, 90.0),
                        longitude=_coordinate(row["경도"], "경도", -180.0, 180.0),
                        collected_date=_safe_text(row["정보수집일"], "정보수집일", maximum=32),
                        mobile_short_no=_safe_text(row["모바일단축번호"], "모바일단축번호", maximum=64),
                        city_name=_safe_text(row["도시명"], "도시명", required=True, maximum=160),
                        managing_city_name=_safe_text(row["관리도시명"], "관리도시명", maximum=160),
                        source_id="",
                    )
                )
            except CatalogError as exc:
                if quarantine_invalid_rows:
                    message = str(exc).lower()
                    reason = "INVALID_COORDINATE" if "coordinate" in message or "geographic range" in message else "INVALID_ROW"
                    rejection_reasons[reason] += 1
                    continue
                raise type(exc)(f"row {row_number}: {exc}") from exc
        return self._persist_catalog(
            "stops",
            records,
            data,
            encoding,
            source_url,
            source_date,
            input_row_count=input_row_count,
            rejection_reasons=rejection_reasons,
        )

    def import_routes_csv(self, source: Path | bytes | bytearray, *, source_url: str, source_date: str) -> dict[str, Any]:
        data = self._read_source(source)
        text, encoding = self._decode(data)
        records: list[RouteRecord] = []
        for row_number, row in self._rows(text, ROUTE_COLUMNS):
            try:
                records.append(
                    RouteRecord(
                        city_code=_safe_code(row["지자체코드"], "지자체코드"),
                        route_id=_safe_transport_identifier(row["노선 아이디"], "노선 아이디"),
                        route_no=_safe_text(row["노선명"], "노선명", required=True, maximum=160),
                        start_node_id=_safe_code(row["기점노드 아이디"], "기점노드 아이디"),
                        end_node_id=_safe_code(row["종점노드 아이디"], "종점노드 아이디"),
                        start_stop_name=_safe_text(row["기점정류장"], "기점정류장", required=True, maximum=160),
                        end_stop_name=_safe_text(row["종점정류장"], "종점정류장", required=True, maximum=160),
                        municipality_name=_safe_text(row["지자체명"], "지자체명", required=True, maximum=160),
                        source_id="",
                    )
                )
            except CatalogError as exc:
                raise type(exc)(f"row {row_number}: {exc}") from exc
        return self._persist_catalog("routes", records, data, encoding, source_url, source_date)

    def _persist_catalog(
        self,
        kind: str,
        records: Sequence[StopRecord] | Sequence[RouteRecord],
        data: bytes,
        encoding: str,
        source_url: str,
        source_date: str,
        *,
        input_row_count: int | None = None,
        rejection_reasons: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        if not records:
            raise CatalogValidationError("CSV contains no data rows")
        url = _source_url(source_url)
        dated = _source_date(source_date)
        sha256 = hashlib.sha256(data).hexdigest()
        identity = hashlib.sha256(_canonical([kind, url, dated, sha256]).encode("utf-8")).hexdigest()
        source_id = f"{kind}_{identity[:24]}"
        imported_at = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        table = "catalog_stops" if kind == "stops" else "catalog_routes"
        quality_reasons = dict(sorted((rejection_reasons or {}).items()))
        total_rows = int(input_row_count if input_row_count is not None else len(records))
        rejected_rows = max(0, total_rows - len(records))
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT 1 FROM catalog_sources WHERE source_id=?", (source_id,)).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO catalog_sources(source_id,dataset_kind,source_url,source_date,sha256,encoding,row_count,imported_at) VALUES(?,?,?,?,?,?,?,?)",
                        (source_id, kind, url, dated, sha256, encoding, len(records), imported_at),
                    )
                    if kind == "stops":
                        connection.executemany(
                            "INSERT INTO catalog_stops VALUES(?,?,?,?,?,?,?,?,?,?)",
                            [
                                (
                                    source_id,
                                    item.city_code,
                                    item.node_id,
                                    item.node_name,
                                    item.latitude,
                                    item.longitude,
                                    item.collected_date,
                                    item.mobile_short_no,
                                    item.city_name,
                                    item.managing_city_name,
                                )
                                for item in records
                            ],
                        )
                    else:
                        connection.executemany(
                            "INSERT INTO catalog_routes VALUES(?,?,?,?,?,?,?,?,?)",
                            [
                                (
                                    source_id,
                                    item.city_code,
                                    item.route_id,
                                    item.route_no,
                                    item.start_node_id,
                                    item.end_node_id,
                                    item.start_stop_name,
                                    item.end_stop_name,
                                    item.municipality_name,
                                )
                                for item in records
                            ],
                        )
                connection.execute(
                    "INSERT INTO catalog_import_quality(source_id,input_row_count,imported_row_count,rejected_row_count,rejection_reasons_json) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(source_id) DO UPDATE SET input_row_count=excluded.input_row_count,imported_row_count=excluded.imported_row_count,rejected_row_count=excluded.rejected_row_count,rejection_reasons_json=excluded.rejection_reasons_json",
                    (source_id, total_rows, len(records), rejected_rows, _canonical(quality_reasons)),
                )
                connection.execute(
                    "INSERT INTO catalog_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"active_{kind}_source_id", source_id),
                )
                revision = self._bump_revision(connection)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise CatalogValidationError(f"duplicate or conflicting {kind} identifiers") from exc
        self._invalidate_cache()
        return {
            "dataset_kind": kind,
            "source_id": source_id,
            "source_url": url,
            "source_date": dated,
            "sha256": sha256,
            "encoding": encoding,
            "row_count": len(records),
            "quality": {
                "input_row_count": total_rows,
                "imported_row_count": len(records),
                "rejected_row_count": rejected_rows,
                "rejection_reasons": quality_reasons,
                "coordinates_corrected": 0,
            },
            "revision": revision,
            "table": table,
        }

    def hydrate_route_sequence(
        self,
        *,
        city_code: str,
        route_id: str,
        ordered_stops: Iterable[Mapping[str, Any]],
        source: str,
        captured_at: str,
        activation_policy: str = "replace",
    ) -> dict[str, Any]:
        result = self.hydrate_route_sequences_batch(
            [
                {
                    "city_code": city_code,
                    "route_id": route_id,
                    "ordered_stops": ordered_stops,
                    "source": source,
                    "captured_at": captured_at,
                }
            ],
            activation_policy=activation_policy,
        )
        return result["sequences"][0]

    def hydrate_route_sequences_batch(
        self,
        sequences: Iterable[Mapping[str, Any]],
        *,
        activation_policy: str = "replace",
    ) -> dict[str, Any]:
        """Validate and activate multiple authoritative routes atomically.

        Municipal file imports use this path so a bad or conflicting route can
        never leave only the first part of a file active.  All input is fully
        normalized before SQLite is opened for writes, and the catalog revision
        changes at most once for the whole batch.
        """
        policy = _safe_text(
            activation_policy,
            "activation_policy",
            required=True,
            maximum=32,
        )
        if policy not in {"replace", "preserve_newer"}:
            raise CatalogValidationError(
                "activation_policy must be replace or preserve_newer"
            )
        raw_sequences = list(sequences)
        if not 1 <= len(raw_sequences) <= MAX_SEQUENCE_BATCH:
            raise CatalogLimitError(
                f"sequences must contain 1..{MAX_SEQUENCE_BATCH} routes"
            )
        normalized: list[dict[str, Any]] = []
        seen_routes: set[tuple[str, str]] = set()
        for index, item in enumerate(raw_sequences):
            if not isinstance(item, Mapping):
                raise CatalogValidationError(f"sequences[{index}] must be an object")
            city = _safe_code(item.get("city_code"), "city_code")
            route = _safe_transport_identifier(item.get("route_id"), "route_id")
            route_key = (city, route)
            if route_key in seen_routes:
                raise CatalogValidationError("batch contains a duplicate city_code/route_id")
            seen_routes.add(route_key)
            provenance = _safe_text(
                item.get("source"), "source", required=True, maximum=512
            )
            captured = _timestamp(item.get("captured_at"))
            stops = self._route_stop_records(city, route, item.get("ordered_stops") or ())
            spike = next(
                (
                    evidence
                    for stop_index in range(2, len(stops))
                    if (
                        evidence := single_point_route_spike(
                            stops[stop_index - 2],
                            stops[stop_index - 1],
                            stops[stop_index],
                        )
                    )
                    is not None
                ),
                None,
            )
            if spike is not None:
                raise CatalogValidationError(spike.bounded_evidence())
            canonical_stops = [_route_stop_payload(stop) for stop in stops]
            digest = hashlib.sha256(
                _canonical(canonical_stops).encode("utf-8")
            ).hexdigest()
            identity = hashlib.sha256(
                _canonical([city, route, provenance, captured, digest]).encode("utf-8")
            ).hexdigest()
            normalized.append(
                {
                    "sequence_id": "seq_" + identity[:24],
                    "city_code": city,
                    "route_id": route,
                    "source": provenance,
                    "captured_at": captured,
                    "sha256": digest,
                    "stops": stops,
                }
            )

        imported_at = self.clock().astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        any_activated = False
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for sequence in normalized:
                sequence_id = sequence["sequence_id"]
                exists = connection.execute(
                    "SELECT 1 FROM route_sequence_versions WHERE sequence_id=?",
                    (sequence_id,),
                ).fetchone()
                sequence["created"] = exists is None
                if exists is None:
                    stops = sequence["stops"]
                    connection.execute(
                        "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                        (
                            sequence_id,
                            sequence["city_code"],
                            sequence["route_id"],
                            sequence["source"],
                            sequence["captured_at"],
                            sequence["sha256"],
                            len(stops),
                            imported_at,
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO route_sequence_stops("
                        "sequence_id,node_order,node_id,node_name,latitude,longitude,"
                        "direction,can_board,can_alight) VALUES(?,?,?,?,?,?,?,?,?)",
                        [
                            (
                                sequence_id,
                                stop.node_order,
                                stop.node_id,
                                stop.node_name,
                                stop.latitude,
                                stop.longitude,
                                stop.direction,
                                int(stop.can_board),
                                int(stop.can_alight),
                            )
                            for stop in stops
                        ],
                    )
                active = connection.execute(
                    "SELECT a.sequence_id,v.captured_at,v.sha256 "
                    "FROM active_route_sequences a "
                    "JOIN route_sequence_versions v ON v.sequence_id=a.sequence_id "
                    "WHERE a.city_code=? AND a.route_id=?",
                    (sequence["city_code"], sequence["route_id"]),
                ).fetchone()
                if (
                    active is not None
                    and policy == "preserve_newer"
                    and sequence["captured_at"] == active["captured_at"]
                    and sequence["sha256"] != active["sha256"]
                ):
                    raise CatalogValidationError(
                        "same captured_at has a different active topology hash"
                    )
                skipped_older = bool(
                    active is not None
                    and policy == "preserve_newer"
                    and sequence["captured_at"] < active["captured_at"]
                )
                activated = bool(
                    not skipped_older
                    and (active is None or active["sequence_id"] != sequence_id)
                )
                sequence["skipped_older"] = skipped_older
                sequence["activated"] = activated
                if activated:
                    connection.execute(
                        "INSERT INTO active_route_sequences(city_code,route_id,sequence_id) VALUES(?,?,?) ON CONFLICT(city_code,route_id) DO UPDATE SET sequence_id=excluded.sequence_id",
                        (sequence["city_code"], sequence["route_id"], sequence_id),
                    )
                    any_activated = True
            if any_activated:
                revision = self._bump_revision(connection)
            else:
                revision_row = connection.execute(
                    "SELECT value FROM catalog_meta WHERE key='revision'"
                ).fetchone()
                revision = int(revision_row[0] if revision_row else 0)
            connection.commit()
        if any_activated:
            self._invalidate_cache()
        results = [
            {
                "sequence_id": sequence["sequence_id"],
                "city_code": sequence["city_code"],
                "route_id": sequence["route_id"],
                "source": sequence["source"],
                "captured_at": sequence["captured_at"],
                "sha256": sequence["sha256"],
                "stop_count": len(sequence["stops"]),
                "revision": revision,
                "created": sequence["created"],
                "activated": sequence["activated"],
                "skipped_older": sequence["skipped_older"],
            }
            for sequence in normalized
        ]
        return {
            "route_count": len(results),
            "created": sum(1 for result in results if result["created"]),
            "activated": sum(1 for result in results if result["activated"]),
            "skipped_older": sum(1 for result in results if result["skipped_older"]),
            "activation_policy": policy,
            "revision": revision,
            "sequences": results,
        }

    @staticmethod
    def _route_stop_records(
        city: str,
        route: str,
        ordered_stops: Iterable[Mapping[str, Any]],
    ) -> list[RouteStopRecord]:
        raw_stops = list(ordered_stops)
        if not 2 <= len(raw_stops) <= MAX_SEQUENCE_STOPS:
            raise CatalogLimitError(f"ordered_stops must contain 2..{MAX_SEQUENCE_STOPS} rows")
        stops: list[RouteStopRecord] = []
        previous_order: int | None = None
        for index, item in enumerate(raw_stops):
            try:
                order = int(item.get("node_order"))
            except (TypeError, ValueError) as exc:
                raise CatalogValidationError(f"ordered_stops[{index}].node_order must be an integer") from exc
            if order < 0 or (previous_order is not None and order <= previous_order):
                raise CatalogValidationError("node_order must be unique and strictly increasing")
            previous_order = order
            latitude = _optional_coordinate(item.get("latitude"), "latitude", -90.0, 90.0)
            longitude = _optional_coordinate(item.get("longitude"), "longitude", -180.0, 180.0)
            if (latitude is None) != (longitude is None):
                raise CatalogValidationError("latitude and longitude must be supplied together")
            access: dict[str, bool] = {}
            for field in ("can_board", "can_alight"):
                if field not in item:
                    access[field] = True
                    continue
                value = item.get(field)
                if isinstance(value, bool):
                    access[field] = value
                elif value in (0, 1, "0", "1"):
                    access[field] = bool(int(value))
                else:
                    raise CatalogValidationError(
                        f"ordered_stops[{index}].{field} must be boolean"
                    )
            stops.append(
                RouteStopRecord(
                    city_code=city,
                    route_id=route,
                    node_id=_safe_code(item.get("node_id"), "node_id"),
                    node_order=order,
                    node_name=_safe_text(item.get("node_name"), "node_name", required=True, maximum=160),
                    latitude=latitude,
                    longitude=longitude,
                    direction=_safe_text(item.get("direction"), "direction", maximum=32),
                    can_board=access["can_board"],
                    can_alight=access["can_alight"],
                )
            )
        return stops

    def route_sequence_sha256(
        self,
        *,
        city_code: str,
        route_id: str,
        ordered_stops: Iterable[Mapping[str, Any]],
    ) -> str:
        city = _safe_code(city_code, "city_code")
        route = _safe_transport_identifier(route_id, "route_id")
        stops = self._route_stop_records(city, route, ordered_stops)
        return hashlib.sha256(
            _canonical([_route_stop_payload(item) for item in stops]).encode("utf-8")
        ).hexdigest()

    def active_route_sequence_info(self, *, city_code: str, route_id: str) -> dict[str, Any] | None:
        city = _safe_code(city_code, "city_code")
        route = _safe_transport_identifier(route_id, "route_id")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT v.sequence_id,v.sha256,v.stop_count,v.captured_at,v.source "
                "FROM active_route_sequences a JOIN route_sequence_versions v ON v.sequence_id=a.sequence_id "
                "WHERE a.city_code=? AND a.route_id=?",
                (city, route),
            ).fetchone()
        return dict(row) if row else None

    def activate_gtfs_staged_feed(
        self,
        *,
        stage_path: Path,
        provider: str,
        source_url: str,
        source_date: str,
        feed_sha256: str,
        member_manifest: Iterable[Mapping[str, Any]],
        table_provenance: Mapping[str, Mapping[str, Any]],
        topology_role: str = "historical_model",
    ) -> dict[str, Any]:
        """Atomically activate a fully validated, disk-backed GTFS stage."""
        provider_id = _safe_code(provider, "provider")
        if len(provider_id) > 24:
            raise CatalogLimitError("provider exceeds 24 characters for GTFS namespacing")
        url = _source_url(source_url)
        dated = _source_date(source_date)
        digest = _sha256(feed_sha256, "feed_sha256")
        role = _safe_text(
            topology_role, "topology_role", required=True, maximum=32
        ).lower()
        if role not in {"historical_model", "active_topology"}:
            raise CatalogValidationError(
                "topology_role must be historical_model or active_topology"
            )
        identity = hashlib.sha256(
            _canonical(["GTFS", provider_id, url, dated, digest]).encode("utf-8")
        ).hexdigest()
        feed_id = "gtfs_" + identity[:24]
        graph_city_code = f"GTFS-{provider_id}"
        captured_at = f"{dated}T00:00:00Z"
        sequence_source = f"GTFS:{provider_id}:{feed_id}"
        path = Path(stage_path).resolve()
        if not path.is_file() or path.stat().st_size <= 0:
            raise CatalogValidationError("GTFS staging path must be a non-empty file")
        if path.stat().st_size > MAX_GTFS_STAGE_BYTES:
            raise CatalogLimitError(f"GTFS staging database exceeds {MAX_GTFS_STAGE_BYTES} bytes")
        if path == self.path.resolve():
            raise CatalogValidationError("GTFS staging database must differ from the catalog DB")

        manifest: list[dict[str, Any]] = []
        seen_members: set[str] = set()
        for index, raw in enumerate(member_manifest):
            if index >= 128 or not isinstance(raw, Mapping):
                raise CatalogLimitError("member_manifest exceeds 128 valid entries")
            name = _safe_text(raw.get("name"), "member name", required=True, maximum=512)
            parts = name.split("/")
            if (
                not 1 <= len(parts) <= 8
                or any(
                    part in {"", ".", ".."}
                    or part != part.strip()
                    or len(part) > 128
                    or ":" in part
                    or "\\" in part
                    for part in parts
                )
                or name.casefold() in seen_members
            ):
                raise CatalogValidationError("member_manifest contains an unsafe or duplicate name")
            seen_members.add(name.casefold())
            try:
                byte_count = int(raw.get("byte_count"))
                compressed_bytes = int(raw.get("compressed_bytes"))
            except (TypeError, ValueError) as exc:
                raise CatalogValidationError("member sizes must be integers") from exc
            if byte_count < 0 or compressed_bytes < 0:
                raise CatalogValidationError("member sizes cannot be negative")
            manifest.append(
                {"name": name, "byte_count": byte_count, "compressed_bytes": compressed_bytes}
            )
        if not manifest:
            raise CatalogValidationError("member_manifest is required")
        manifest_json = _canonical(sorted(manifest, key=lambda item: item["name"]))
        if len(manifest_json) > 128 * 1024:
            raise CatalogLimitError("member_manifest is too large")

        required_files = {
            "stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt"
        }
        allowed_files = required_files | {"calendar_dates.txt"}
        if not required_files.issubset(table_provenance):
            missing = ", ".join(sorted(required_files - set(table_provenance)))
            raise CatalogValidationError(f"GTFS table provenance is missing: {missing}")
        if set(table_provenance) - allowed_files:
            raise CatalogValidationError("GTFS table provenance contains an unsupported file")
        tables: list[dict[str, Any]] = []
        for name in sorted(table_provenance):
            raw = table_provenance[name]
            if not isinstance(raw, Mapping):
                raise CatalogValidationError("table provenance entries must be objects")
            try:
                byte_count = int(raw.get("byte_count"))
                row_count = int(raw.get("row_count"))
            except (TypeError, ValueError) as exc:
                raise CatalogValidationError("GTFS table counts must be integers") from exc
            if byte_count < 0 or row_count < 0:
                raise CatalogValidationError("GTFS table counts cannot be negative")
            tables.append(
                {
                    "file_name": name,
                    "sha256": _sha256(raw.get("sha256"), f"{name} sha256"),
                    "byte_count": byte_count,
                    "row_count": row_count,
                }
            )

        imported_at = self.clock().astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        created = False
        activated = False
        revision = 0
        counts: dict[str, int] = {}
        required_stage_tables = {
            "stage_meta", "stops", "routes", "services", "calendar_dates",
            "trips", "stop_times", "patterns", "pattern_stops",
        }
        stage_count_map = {
            "stops.txt": "stops",
            "routes.txt": "routes",
            "calendar.txt": "services",
            "calendar_dates.txt": "calendar_dates",
            "trips.txt": "trips",
            "stop_times.txt": "stop_times",
        }
        try:
            with self.connect() as connection:
                connection.execute("PRAGMA temp_store=MEMORY")
                connection.execute("ATTACH DATABASE ? AS gtfs_stage", (str(path),))
                try:
                    stage_tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM gtfs_stage.sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if not required_stage_tables.issubset(stage_tables):
                        raise CatalogValidationError("GTFS staging schema is incomplete")
                    meta = {
                        row["key"]: row["value"]
                        for row in connection.execute(
                            "SELECT key,value FROM gtfs_stage.stage_meta"
                        ).fetchall()
                    }
                    if meta.get("complete") != "1" or meta.get("provider") != provider_id:
                        raise CatalogValidationError("GTFS staging completion marker is invalid")
                    integrity = connection.execute(
                        "PRAGMA gtfs_stage.integrity_check"
                    ).fetchone()[0]
                    if integrity != "ok":
                        raise CatalogValidationError("GTFS staging database failed integrity_check")
                    declared_counts = {
                        item["file_name"]: item["row_count"] for item in tables
                    }
                    for file_name, stage_table in stage_count_map.items():
                        stage_count = int(
                            connection.execute(
                                f"SELECT COUNT(*) FROM gtfs_stage.{stage_table}"
                            ).fetchone()[0]
                        )
                        if file_name in declared_counts:
                            if stage_count != declared_counts[file_name]:
                                raise CatalogValidationError(
                                    "GTFS staging counts do not match table provenance"
                                )
                        elif stage_count:
                            raise CatalogValidationError(
                                "GTFS staging contains undeclared optional rows"
                            )
                        counts[stage_table] = stage_count
                    counts["bus_patterns"] = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM gtfs_stage.patterns"
                        ).fetchone()[0]
                    )
                    if not 0 <= counts["bus_patterns"] <= MAX_GTFS_PATTERNS:
                        raise CatalogLimitError(
                            f"GTFS bus patterns cannot exceed {MAX_GTFS_PATTERNS} rows"
                        )
                    try:
                        staged_meta_counts = json.loads(meta.get("counts_json", "{}"))
                    except json.JSONDecodeError as exc:
                        raise CatalogValidationError("GTFS staging counts marker is invalid") from exc
                    for file_name, value in declared_counts.items():
                        if staged_meta_counts.get(file_name) != value:
                            raise CatalogValidationError("GTFS staging counts marker is stale")
                    if staged_meta_counts.get("bus_patterns") != counts["bus_patterns"]:
                        raise CatalogValidationError("GTFS staging pattern count is stale")

                    connection.execute(
                        "CREATE TEMP TABLE gtfs_sequence_map("
                        "pattern_id TEXT PRIMARY KEY,sequence_id TEXT NOT NULL,"
                        "sequence_sha256 TEXT NOT NULL,stop_count INTEGER NOT NULL)"
                    )
                    ordered_pattern_sql = (
                        "SELECT p.pattern_id,p.raw_route_id,p.graph_city_code,p.graph_route_id,"
                        "p.pattern_sha256,p.direction_mask,p.stop_count,"
                        "ps.node_order,ps.raw_stop_id,ps.node_id,ps.node_name,"
                        "ps.latitude,ps.longitude,ps.direction,"
                        "ps.pickup_type,ps.drop_off_type,ps.can_board,ps.can_alight "
                        "FROM gtfs_stage.pattern_stops ps "
                        "JOIN gtfs_stage.patterns p ON p.pattern_id=ps.pattern_id "
                        "ORDER BY ps.pattern_id,ps.node_order"
                    )
                    query_plan = " ".join(
                        str(row[3])
                        for row in connection.execute(
                            "EXPLAIN QUERY PLAN " + ordered_pattern_sql
                        ).fetchall()
                    ).upper()
                    if "USE TEMP B-TREE" in query_plan:
                        raise CatalogValidationError(
                            "GTFS sequence derivation would require an unbounded temp sort"
                        )
                    current_pattern: dict[str, Any] | None = None
                    pattern_stops: list[Mapping[str, Any]] = []
                    access_vector: list[tuple[str, int, int]] = []
                    sequence_batch: list[tuple[Any, ...]] = []

                    def finish_pattern() -> None:
                        nonlocal current_pattern, pattern_stops, access_vector
                        if current_pattern is None:
                            return
                        expected_count = int(current_pattern["stop_count"])
                        if len(pattern_stops) != expected_count:
                            raise CatalogValidationError("GTFS staged pattern stops are incomplete")
                        pattern_sha = hashlib.sha256(
                            _canonical(
                                ["GTFS_BUS_PATTERN", provider_id,
                                 current_pattern["raw_route_id"], access_vector]
                            ).encode("utf-8")
                        ).hexdigest()
                        route_sha = hashlib.sha256(
                            _canonical(
                                [provider_id, current_pattern["raw_route_id"]]
                            ).encode("utf-8")
                        ).hexdigest()
                        expected_pattern_id = "gpat_" + pattern_sha
                        expected_graph_route = (
                            f"GTFS:{provider_id}:R{route_sha[:20]}:P{pattern_sha[:40]}"
                        )
                        if (
                            current_pattern["pattern_id"] != expected_pattern_id
                            or current_pattern["pattern_sha256"] != pattern_sha
                            or current_pattern["graph_city_code"] != graph_city_code
                            or current_pattern["graph_route_id"] != expected_graph_route
                        ):
                            raise CatalogValidationError("GTFS staged pattern namespace is invalid")
                        route_id = _safe_transport_identifier(
                            current_pattern["graph_route_id"], "graph_route_id"
                        )
                        route_records = self._route_stop_records(
                            graph_city_code, route_id, pattern_stops
                        )
                        sequence_sha = hashlib.sha256(
                            _canonical(
                                [_route_stop_payload(item) for item in route_records]
                            ).encode("utf-8")
                        ).hexdigest()
                        sequence_identity = hashlib.sha256(
                            _canonical(
                                [graph_city_code, route_id, sequence_source,
                                 captured_at, sequence_sha]
                            ).encode("utf-8")
                        ).hexdigest()
                        sequence_batch.append(
                            (
                                current_pattern["pattern_id"],
                                "seq_" + sequence_identity[:24],
                                sequence_sha,
                                expected_count,
                            )
                        )
                        if len(sequence_batch) >= 2_000:
                            connection.executemany(
                                "INSERT INTO temp.gtfs_sequence_map VALUES(?,?,?,?)",
                                sequence_batch,
                            )
                            sequence_batch.clear()
                        current_pattern = None
                        pattern_stops = []
                        access_vector = []

                    for row in connection.execute(ordered_pattern_sql):
                        if current_pattern is None or row["pattern_id"] != current_pattern["pattern_id"]:
                            finish_pattern()
                            current_pattern = {
                                key: row[key]
                                for key in (
                                    "pattern_id", "raw_route_id", "graph_city_code",
                                    "graph_route_id", "pattern_sha256", "direction_mask",
                                    "stop_count",
                                )
                            }
                        raw_stop_id = _raw_gtfs_id(row["raw_stop_id"], "raw_stop_id")
                        expected_node_id = _gtfs_namespaced_id(
                            provider_id, "STOP", raw_stop_id
                        )
                        direction_mask = int(current_pattern["direction_mask"])
                        expected_direction = (
                            "GTFS:0" if direction_mask == 1
                            else "GTFS:1" if direction_mask == 2 else "GTFS"
                        )
                        if row["node_id"] != expected_node_id or row["direction"] != expected_direction:
                            raise CatalogValidationError("GTFS staged stop namespace is invalid")
                        pattern_stops.append(
                            {
                                "node_id": row["node_id"],
                                "node_name": row["node_name"],
                                "node_order": row["node_order"],
                                "latitude": row["latitude"],
                                "longitude": row["longitude"],
                                "direction": row["direction"],
                                "can_board": row["can_board"],
                                "can_alight": row["can_alight"],
                            }
                        )
                        access_vector.append(
                            (
                                raw_stop_id,
                                int(row["pickup_type"]),
                                int(row["drop_off_type"]),
                            )
                        )
                    finish_pattern()
                    if sequence_batch:
                        connection.executemany(
                            "INSERT INTO temp.gtfs_sequence_map VALUES(?,?,?,?)",
                            sequence_batch,
                        )
                    mapped_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM temp.gtfs_sequence_map"
                        ).fetchone()[0]
                    )
                    if mapped_count != counts["bus_patterns"]:
                        raise CatalogValidationError("GTFS sequence map is incomplete")

                    connection.commit()
                    connection.execute("BEGIN IMMEDIATE")
                    existing = connection.execute(
                        "SELECT * FROM gtfs_feed_versions WHERE feed_id=?", (feed_id,)
                    ).fetchone()
                    if existing is None:
                        created = True
                        connection.execute(
                            "INSERT INTO gtfs_feed_versions VALUES(?,?,?,?,?,?,?)",
                            (feed_id, provider_id, url, dated, digest, imported_at, manifest_json),
                        )
                        connection.executemany(
                            "INSERT INTO gtfs_feed_tables VALUES(?,?,?,?,?)",
                            [
                                (feed_id, item["file_name"], item["sha256"],
                                 item["byte_count"], item["row_count"])
                                for item in tables
                            ],
                        )
                        connection.execute(
                            "INSERT INTO gtfs_stops "
                            "SELECT ?,raw_stop_id,node_id,stop_name,latitude,longitude FROM gtfs_stage.stops",
                            (feed_id,),
                        )
                        connection.execute(
                            "INSERT INTO gtfs_routes "
                            "SELECT ?,raw_route_id,route_namespace_id,route_short_name,route_long_name,route_type FROM gtfs_stage.routes",
                            (feed_id,),
                        )
                        connection.execute(
                            "INSERT INTO gtfs_services "
                            "SELECT ?,raw_service_id,service_namespace_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date FROM gtfs_stage.services",
                            (feed_id,),
                        )
                        connection.execute(
                            "INSERT INTO gtfs_calendar_dates "
                            "SELECT ?,raw_service_id,service_date,exception_type FROM gtfs_stage.calendar_dates",
                            (feed_id,),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO route_sequence_versions "
                            "SELECT m.sequence_id,p.graph_city_code,p.graph_route_id,?,?,"
                            "m.sequence_sha256,m.stop_count,? "
                            "FROM gtfs_stage.patterns p JOIN temp.gtfs_sequence_map m ON m.pattern_id=p.pattern_id",
                            (sequence_source, captured_at, imported_at),
                        )
                        collision = connection.execute(
                            "SELECT 1 FROM gtfs_stage.patterns p "
                            "JOIN temp.gtfs_sequence_map m ON m.pattern_id=p.pattern_id "
                            "JOIN route_sequence_versions v ON v.sequence_id=m.sequence_id "
                            "WHERE v.city_code<>p.graph_city_code OR v.route_id<>p.graph_route_id "
                            "OR v.source<>? OR v.captured_at<>? OR v.sha256<>m.sequence_sha256 "
                            "OR v.stop_count<>m.stop_count LIMIT 1",
                            (sequence_source, captured_at),
                        ).fetchone()
                        if collision is not None:
                            raise CatalogValidationError("GTFS route sequence identity conflicts")
                        connection.execute(
                            "INSERT OR IGNORE INTO route_sequence_stops("
                            "sequence_id,node_order,node_id,node_name,latitude,longitude,"
                            "direction,can_board,can_alight) "
                            "SELECT m.sequence_id,ps.node_order,ps.node_id,ps.node_name,"
                            "ps.latitude,ps.longitude,ps.direction,"
                            "ps.can_board,ps.can_alight "
                            "FROM gtfs_stage.pattern_stops ps "
                            "JOIN temp.gtfs_sequence_map m ON m.pattern_id=ps.pattern_id"
                        )
                        missing_sequence_stops = connection.execute(
                            "SELECT 1 FROM temp.gtfs_sequence_map m WHERE "
                            "(SELECT COUNT(*) FROM route_sequence_stops s WHERE s.sequence_id=m.sequence_id)<>m.stop_count LIMIT 1"
                        ).fetchone()
                        if missing_sequence_stops is not None:
                            raise CatalogValidationError("GTFS route sequence stops are incomplete")
                        connection.execute(
                            "INSERT INTO gtfs_patterns "
                            "SELECT ?,p.pattern_id,p.raw_route_id,p.graph_city_code,p.graph_route_id,"
                            "p.pattern_sha256,CASE p.direction_mask WHEN 1 THEN 0 WHEN 2 THEN 1 ELSE NULL END,"
                            "p.stop_count,p.representative_trip_id,m.sequence_id "
                            "FROM gtfs_stage.patterns p JOIN temp.gtfs_sequence_map m ON m.pattern_id=p.pattern_id",
                            (feed_id,),
                        )
                        connection.execute(
                            "INSERT INTO gtfs_trips "
                            "SELECT ?,raw_trip_id,trip_namespace_id,raw_route_id,raw_service_id,"
                            "pattern_id,direction_id,trip_headsign FROM gtfs_stage.trips",
                            (feed_id,),
                        )
                        connection.execute(
                            "INSERT INTO gtfs_stop_times "
                            "SELECT ?,raw_trip_id,stop_sequence,raw_stop_id,arrival_time,arrival_seconds,"
                            "departure_time,departure_seconds,pickup_type,drop_off_type FROM gtfs_stage.stop_times",
                            (feed_id,),
                        )
                        for entity_type, table_name, raw_column, namespace_column in (
                            ("STOP", "stops", "raw_stop_id", "node_id"),
                            ("ROUTE", "routes", "raw_route_id", "route_namespace_id"),
                            ("TRIP", "trips", "raw_trip_id", "trip_namespace_id"),
                            ("SERVICE", "services", "raw_service_id", "service_namespace_id"),
                        ):
                            connection.execute(
                                f"INSERT INTO gtfs_id_aliases "
                                f"SELECT ?,?,{raw_column},{namespace_column} FROM gtfs_stage.{table_name}",
                                (feed_id, entity_type),
                            )
                    else:
                        if (
                            existing["provider"] != provider_id
                            or existing["source_url"] != url
                            or existing["source_date"] != dated
                            or existing["sha256"] != digest
                            or existing["member_manifest_json"] != manifest_json
                        ):
                            raise CatalogValidationError("existing GTFS feed identity conflicts")
                        existing_tables = {
                            row["file_name"]: (
                                row["sha256"], row["byte_count"], row["row_count"]
                            )
                            for row in connection.execute(
                                "SELECT file_name,sha256,byte_count,row_count "
                                "FROM gtfs_feed_tables WHERE feed_id=?",
                                (feed_id,),
                            ).fetchall()
                        }
                        expected_tables = {
                            item["file_name"]: (
                                item["sha256"], item["byte_count"], item["row_count"]
                            )
                            for item in tables
                        }
                        if existing_tables != expected_tables:
                            raise CatalogValidationError("existing GTFS provenance conflicts")
                        final_count_map = {
                            "stops": "gtfs_stops", "routes": "gtfs_routes",
                            "services": "gtfs_services", "calendar_dates": "gtfs_calendar_dates",
                            "trips": "gtfs_trips", "stop_times": "gtfs_stop_times",
                            "bus_patterns": "gtfs_patterns",
                        }
                        for count_key, final_table in final_count_map.items():
                            final_count = int(
                                connection.execute(
                                    f"SELECT COUNT(*) FROM {final_table} WHERE feed_id=?",
                                    (feed_id,),
                                ).fetchone()[0]
                            )
                            if final_count != counts[count_key]:
                                raise CatalogValidationError("existing GTFS feed rows are incomplete")

                    previous = connection.execute(
                        "SELECT feed_id,topology_role FROM active_gtfs_feeds WHERE provider=?",
                        (provider_id,),
                    ).fetchone()
                    previous_feed_id = previous["feed_id"] if previous else None
                    previous_role = previous["topology_role"] if previous else None
                    if previous_feed_id and (
                        previous_feed_id != feed_id
                        or previous_role == "active_topology" and role != "active_topology"
                    ):
                        for old in connection.execute(
                            "SELECT graph_city_code,graph_route_id "
                            "FROM gtfs_patterns WHERE feed_id=?",
                            (previous_feed_id,),
                        ):
                            deleted = connection.execute(
                                "DELETE FROM active_route_sequences WHERE city_code=? AND route_id=?",
                                (old["graph_city_code"], old["graph_route_id"]),
                            ).rowcount
                            activated = activated or bool(deleted)
                    if role == "active_topology":
                        for pattern in connection.execute(
                            "SELECT p.graph_city_code,p.graph_route_id,m.sequence_id "
                            "FROM gtfs_stage.patterns p JOIN temp.gtfs_sequence_map m "
                            "ON m.pattern_id=p.pattern_id"
                        ):
                            active = connection.execute(
                                "SELECT sequence_id FROM active_route_sequences WHERE city_code=? AND route_id=?",
                                (pattern["graph_city_code"], pattern["graph_route_id"]),
                            ).fetchone()
                            if active is None or active["sequence_id"] != pattern["sequence_id"]:
                                connection.execute(
                                    "INSERT INTO active_route_sequences VALUES(?,?,?) "
                                    "ON CONFLICT(city_code,route_id) DO UPDATE SET sequence_id=excluded.sequence_id",
                                    (pattern["graph_city_code"], pattern["graph_route_id"], pattern["sequence_id"]),
                                )
                                activated = True
                    if previous_feed_id != feed_id or previous_role != role:
                        connection.execute(
                            "INSERT INTO active_gtfs_feeds(provider,feed_id,topology_role) "
                            "VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                            "feed_id=excluded.feed_id,topology_role=excluded.topology_role",
                            (provider_id, feed_id, role),
                        )
                        activated = True
                    if activated:
                        revision = self._bump_revision(connection)
                    else:
                        row = connection.execute(
                            "SELECT value FROM catalog_meta WHERE key='revision'"
                        ).fetchone()
                        revision = int(row[0] if row else 0)
                    connection.commit()
                except Exception:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                finally:
                    connection.execute("DETACH DATABASE gtfs_stage")
        except sqlite3.IntegrityError as exc:
            raise CatalogValidationError(
                "GTFS staged feed contains duplicate or conflicting identifiers"
            ) from exc
        if activated:
            self._invalidate_cache()
        return {
            "feed_id": feed_id,
            "provider": provider_id,
            "topology_role": role,
            "source_url": url,
            "source_date": dated,
            "sha256": digest,
            "created": created,
            "activated": activated,
            "revision": revision,
            "counts": counts,
            "graph_namespace": graph_city_code,
            "schedule_evidence_only": True,
            "eligible_for_success_rate": False,
        }

    def gtfs_feed_evidence(self, *, provider: str) -> dict[str, Any]:
        """Return provenance for the active feed, never a validated success model."""
        provider_id = _safe_code(provider, "provider")
        with self.connect() as connection:
            feed = connection.execute(
                "SELECT f.*,a.topology_role FROM active_gtfs_feeds a "
                "JOIN gtfs_feed_versions f ON f.feed_id=a.feed_id WHERE a.provider=?",
                (provider_id,),
            ).fetchone()
            if feed is None:
                return {
                    "provider": provider_id,
                    "data_gap": True,
                    "reason": "ACTIVE_GTFS_FEED_REQUIRED",
                    "eligible_for_success_rate": False,
                }
            tables = connection.execute(
                "SELECT file_name,sha256,byte_count,row_count FROM gtfs_feed_tables "
                "WHERE feed_id=? ORDER BY file_name",
                (feed["feed_id"],),
            ).fetchall()
        historical = feed["topology_role"] != "active_topology"
        return {
            "provider": provider_id,
            "data_gap": False,
            "feed": {
                "feed_id": feed["feed_id"],
                "source_url": feed["source_url"],
                "source_date": feed["source_date"],
                "sha256": feed["sha256"],
                "imported_at": feed["imported_at"],
                "topology_role": feed["topology_role"],
                "members": json.loads(feed["member_manifest_json"]),
                "tables": [dict(row) for row in tables],
            },
            "basis": (
                "HISTORICAL_GTFS_PRIOR_ONLY"
                if historical
                else "OFFICIAL_STATIC_GTFS_RAW_EVIDENCE"
            ),
            "projection_allowed": not historical,
            "model_role": "historical_prior" if historical else None,
            "eligible_for_success_rate": False,
            "validation_required": [
                "service_calendar_and_exceptions",
                "publisher_timetable_semantics",
                "multi_day_observed_passage_history",
            ],
        }

    def gtfs_schedule_evidence(
        self,
        *,
        provider: str,
        graph_route_id: str,
        service_date: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read active GTFS trip/calendar rows as evidence only.

        This deliberately does not return a probability or claim that a listed
        static time operated.  Product replay must independently validate the
        publisher semantics and combine them with observed passage history.
        """
        provider_id = _safe_code(provider, "provider")
        route_id = _safe_transport_identifier(graph_route_id, "graph_route_id")
        bounded = max(1, min(int(limit), MAX_GTFS_EVIDENCE_TRIPS))
        day = _source_date(service_date) if service_date is not None else None
        feed_evidence = self.gtfs_feed_evidence(provider=provider_id)
        if feed_evidence.get("data_gap"):
            return {
                **feed_evidence,
                "graph_route_id": route_id,
                "service_date": day,
                "trips": [],
            }
        if (
            day is not None
            and feed_evidence["feed"].get("topology_role") != "active_topology"
        ):
            return {
                **feed_evidence,
                "data_gap": True,
                "reason": "HISTORICAL_GTFS_PRIOR_ONLY",
                "projection_allowed": False,
                "graph_route_id": route_id,
                "service_date": day,
                "trips": [],
                "success_probability": None,
            }
        feed_id = feed_evidence["feed"]["feed_id"]
        with self.connect() as connection:
            pattern = connection.execute(
                "SELECT p.pattern_id,p.raw_route_id,p.graph_city_code,p.graph_route_id,"
                "p.pattern_sha256,p.direction_id,p.stop_count,p.sequence_id,"
                "r.route_namespace_id,r.route_short_name,r.route_long_name,r.route_type "
                "FROM gtfs_patterns p JOIN gtfs_routes r "
                "ON r.feed_id=p.feed_id AND r.raw_route_id=p.raw_route_id "
                "WHERE p.feed_id=? AND p.graph_route_id=?",
                (feed_id, route_id),
            ).fetchone()
            if pattern is None:
                return {
                    **feed_evidence,
                    "data_gap": True,
                    "reason": "ACTIVE_GTFS_ROUTE_PATTERN_REQUIRED",
                    "graph_route_id": route_id,
                    "service_date": day,
                    "trips": [],
                }
            trip_rows = connection.execute(
                "SELECT t.raw_trip_id,t.trip_namespace_id,t.raw_service_id,t.direction_id,t.trip_headsign,"
                "s.service_namespace_id,s.monday,s.tuesday,s.wednesday,s.thursday,s.friday,s.saturday,s.sunday,"
                "s.start_date,s.end_date "
                "FROM gtfs_trips t JOIN gtfs_services s "
                "ON s.feed_id=t.feed_id AND s.raw_service_id=t.raw_service_id "
                "WHERE t.feed_id=? AND t.pattern_id=? ORDER BY t.raw_trip_id LIMIT ?",
                (feed_id, pattern["pattern_id"], bounded),
            ).fetchall()
            trips_result: list[dict[str, Any]] = []
            remaining_stop_times = MAX_GTFS_EVIDENCE_STOP_TIMES
            weekday_names = (
                "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
            )
            for trip_row in trip_rows:
                operates: bool | None = None
                exception_type: int | None = None
                if day is not None:
                    calendar_day = date.fromisoformat(day)
                    operates = (
                        trip_row["start_date"] <= day <= trip_row["end_date"]
                        and int(trip_row[weekday_names[calendar_day.weekday()]]) == 1
                    )
                    exception = connection.execute(
                        "SELECT exception_type FROM gtfs_calendar_dates "
                        "WHERE feed_id=? AND raw_service_id=? AND service_date=?",
                        (feed_id, trip_row["raw_service_id"], day),
                    ).fetchone()
                    if exception is not None:
                        exception_type = int(exception["exception_type"])
                        operates = exception_type == 1
                if remaining_stop_times <= 0:
                    stop_rows = []
                else:
                    stop_rows = connection.execute(
                        "SELECT st.stop_sequence,st.arrival_time,st.arrival_seconds,"
                        "st.departure_time,st.departure_seconds,st.pickup_type,st.drop_off_type,"
                        "st.raw_stop_id,s.node_id,s.stop_name,s.latitude,s.longitude "
                        "FROM gtfs_stop_times st JOIN gtfs_stops s "
                        "ON s.feed_id=st.feed_id AND s.raw_stop_id=st.raw_stop_id "
                        "WHERE st.feed_id=? AND st.raw_trip_id=? "
                        "ORDER BY st.stop_sequence LIMIT ?",
                        (feed_id, trip_row["raw_trip_id"], remaining_stop_times),
                    ).fetchall()
                remaining_stop_times -= len(stop_rows)
                trips_result.append(
                    {
                        "raw_trip_id": trip_row["raw_trip_id"],
                        "trip_namespace_id": trip_row["trip_namespace_id"],
                        "raw_service_id": trip_row["raw_service_id"],
                        "service_namespace_id": trip_row["service_namespace_id"],
                        "direction_id": trip_row["direction_id"],
                        "trip_headsign": trip_row["trip_headsign"],
                        "calendar": {
                            "start_date": trip_row["start_date"],
                            "end_date": trip_row["end_date"],
                            "weekdays": {
                                name: bool(trip_row[name]) for name in weekday_names
                            },
                            "service_date": day,
                            "exception_type": exception_type,
                            "operates_on_date": operates,
                        },
                        "stop_times": [dict(row) for row in stop_rows],
                    }
                )
        pattern_result = dict(pattern)
        return {
            **feed_evidence,
            "data_gap": False,
            "graph_route_id": route_id,
            "service_date": day,
            "pattern": pattern_result,
            "trips": trips_result,
            "truncated": len(trip_rows) >= bounded or remaining_stop_times <= 0,
            "basis": feed_evidence["basis"],
            "projection_allowed": feed_evidence["projection_allowed"],
            "eligible_for_success_rate": False,
            "success_probability": None,
        }

    @staticmethod
    def _gtfs_operating_predicate(weekday_column: str) -> str:
        if weekday_column not in {
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        }:
            raise CatalogValidationError("service_date weekday is invalid")
        return (
            "CASE WHEN cd.exception_type IS NOT NULL THEN cd.exception_type=1 "
            f"ELSE (s.start_date<=? AND s.end_date>=? AND s.{weekday_column}=1) END"
        )

    def gtfs_exact_stop_time_record(
        self,
        *,
        provider: str,
        service_date: str,
        graph_route_id: str,
        node_id: str,
        node_order: int,
        trip_namespace_id: str,
        expected_feed_id: str | None = None,
    ) -> dict[str, Any]:
        """Read one exact active-feed trip/route/stop/date record.

        This avoids the evidence-list LIMIT used by human-facing inspection.
        Every identifier remains in the provider-owned GTFS namespace.
        """
        provider_id = _safe_code(provider, "provider")
        day = _source_date(service_date)
        route_id = _safe_transport_identifier(graph_route_id, "graph_route_id")
        stop_id = _safe_transport_identifier(node_id, "node_id")
        trip_id = _safe_transport_identifier(trip_namespace_id, "trip_namespace_id")
        pinned_feed_id = (
            _safe_code(expected_feed_id, "expected_feed_id")
            if expected_feed_id is not None
            else None
        )
        try:
            sequence = int(node_order)
        except (TypeError, ValueError) as exc:
            raise CatalogValidationError("node_order must be an integer") from exc
        if not 0 <= sequence <= 100_000:
            raise CatalogValidationError("node_order is outside its range")
        calendar_day = date.fromisoformat(day)
        weekday = (
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        )[calendar_day.weekday()]
        predicate = self._gtfs_operating_predicate(weekday)
        with self.connect() as connection:
            connection.execute("BEGIN")
            active_feed = connection.execute(
                "SELECT feed_id,topology_role FROM active_gtfs_feeds WHERE provider=?",
                (provider_id,),
            ).fetchone()
            if active_feed is None:
                return {
                    "data_gap": True,
                    "reason": "ACTIVE_GTFS_FEED_REQUIRED",
                    "provider": provider_id,
                    "service_date": day,
                    "graph_route_id": route_id,
                    "node_id": stop_id,
                    "node_order": sequence,
                    "trip_id": trip_id,
                }
            if active_feed["topology_role"] != "active_topology":
                return {
                    "data_gap": True,
                    "reason": "HISTORICAL_GTFS_PRIOR_ONLY",
                    "provider": provider_id,
                    "active_feed_id": active_feed["feed_id"],
                    "topology_role": active_feed["topology_role"],
                    "projection_allowed": False,
                    "service_date": day,
                    "graph_route_id": route_id,
                    "node_id": stop_id,
                    "node_order": sequence,
                    "trip_id": trip_id,
                }
            if pinned_feed_id is not None and active_feed["feed_id"] != pinned_feed_id:
                return {
                    "data_gap": True,
                    "reason": "ACTIVE_GTFS_FEED_VERSION_MISMATCH",
                    "provider": provider_id,
                    "expected_feed_id": pinned_feed_id,
                    "active_feed_id": active_feed["feed_id"],
                    "service_date": day,
                    "graph_route_id": route_id,
                    "node_id": stop_id,
                    "node_order": sequence,
                    "trip_id": trip_id,
                }
            row = connection.execute(
                "SELECT f.feed_id,f.source_url,f.source_date,f.sha256,f.imported_at,"
                "p.graph_city_code,p.graph_route_id,t.trip_namespace_id,"
                "st.stop_sequence,st.arrival_time,st.arrival_seconds,"
                "st.departure_time,st.departure_seconds,st.pickup_type,st.drop_off_type,"
                "gs.node_id,gs.stop_name,gs.latitude,gs.longitude "
                "FROM active_gtfs_feeds a "
                "JOIN gtfs_feed_versions f ON f.feed_id=a.feed_id "
                "JOIN gtfs_trips t ON t.feed_id=f.feed_id AND t.trip_namespace_id=? "
                "JOIN gtfs_patterns p ON p.feed_id=t.feed_id AND p.pattern_id=t.pattern_id "
                "JOIN gtfs_services s ON s.feed_id=t.feed_id AND s.raw_service_id=t.raw_service_id "
                "LEFT JOIN gtfs_calendar_dates cd ON cd.feed_id=s.feed_id "
                "AND cd.raw_service_id=s.raw_service_id AND cd.service_date=? "
                "JOIN gtfs_stop_times st ON st.feed_id=t.feed_id "
                "AND st.raw_trip_id=t.raw_trip_id AND st.stop_sequence=? "
                "JOIN gtfs_stops gs ON gs.feed_id=st.feed_id AND gs.raw_stop_id=st.raw_stop_id "
                "WHERE a.provider=? AND p.graph_route_id=? AND gs.node_id=? AND "
                + predicate,
                (trip_id, day, sequence, provider_id, route_id, stop_id, day, day),
            ).fetchone()
        if row is None:
            return {
                "data_gap": True,
                "reason": "EXACT_ACTIVE_GTFS_STOP_TIME_REQUIRED",
                "provider": provider_id,
                "service_date": day,
                "graph_route_id": route_id,
                "node_id": stop_id,
                "node_order": sequence,
                "trip_id": trip_id,
            }
        return {
            "data_gap": False,
            "basis": "OFFICIAL_STATIC_GTFS_RAW_EVIDENCE",
            "provider": provider_id,
            "feed": {
                "feed_id": row["feed_id"],
                "source_date": row["source_date"],
                "sha256": row["sha256"],
                "imported_at": row["imported_at"],
            },
            "service_date": day,
            "graph_city_code": row["graph_city_code"],
            "graph_route_id": row["graph_route_id"],
            "trip_id": row["trip_namespace_id"],
            "node_id": row["node_id"],
            "node_order": int(row["stop_sequence"]),
            "stop_name": row["stop_name"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "arrival_time": row["arrival_time"],
            "arrival_seconds": row["arrival_seconds"],
            "departure_time": row["departure_time"],
            "departure_seconds": row["departure_seconds"],
            "pickup_type": row["pickup_type"],
            "drop_off_type": row["drop_off_type"],
        }

    def plan_gtfs_schedule(
        self,
        *,
        provider: str,
        schedule_source_id: str,
        origin_node_id: str,
        destination_node_id: str,
        service_date: str,
        departure_time: str,
    ) -> dict[str, Any]:
        """Singleflight and briefly cache one exact schedule search."""
        provider_id = _safe_code(provider, "provider")
        source_id = _safe_code(schedule_source_id, "schedule_source_id")
        origin_id = _safe_transport_identifier(origin_node_id, "origin_node_id")
        destination_id = _safe_transport_identifier(
            destination_node_id, "destination_node_id"
        )
        day = _source_date(service_date)
        clock = str(departure_time or "")
        if re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])", clock) is None:
            raise CatalogValidationError("departure_time must use HH:MM")
        with self.connect() as connection:
            revision_row = connection.execute(
                "SELECT value FROM catalog_meta WHERE key='revision'"
            ).fetchone()
            active_row = connection.execute(
                "SELECT feed_id,topology_role FROM active_gtfs_feeds WHERE provider=?",
                (provider_id,),
            ).fetchone()
        cache_key = hashlib.sha256(
            _canonical(
                [
                    str(self.path.resolve()),
                    int(revision_row[0] if revision_row else 0),
                    active_row["feed_id"] if active_row else None,
                    active_row["topology_role"] if active_row else None,
                    provider_id, source_id, origin_id, destination_id, day, clock,
                ]
            ).encode("utf-8")
        ).hexdigest()
        now = time.monotonic()
        with _SCHEDULE_COORDINATOR_LOCK:
            stale = [
                key for key, (expires, _value) in _SCHEDULE_CACHE.items()
                if expires <= now
            ]
            for key in stale:
                _SCHEDULE_CACHE.pop(key, None)
            cached = _SCHEDULE_CACHE.get(cache_key)
            if cached is not None:
                _SCHEDULE_CACHE.move_to_end(cache_key)
                return copy.deepcopy(cached[1])
            flight = _SCHEDULE_FLIGHTS.get(cache_key)
            leader = flight is None
            if leader:
                flight = {
                    "event": threading.Event(), "value": None, "error": None
                }
                _SCHEDULE_FLIGHTS[cache_key] = flight

        assert flight is not None
        if not leader:
            if not flight["event"].wait(GTFS_SCHEDULE_WALL_CLOCK_SECONDS + 1.0):
                return {
                    "status": "DATA_GAP",
                    "reason": "SCHEDULE_BUSY",
                    "schedule": {
                        "status": "DATA_GAP", "reason": "SCHEDULE_BUSY",
                        "service_date": day, "departure_time": clock,
                        "provider": provider_id, "basis": None, "feed_id": None,
                        "timezone": "Asia/Seoul",
                    },
                    "graph": {
                        "algorithm": "bounded_time_dependent_dijkstra",
                        "search_complete": False,
                    },
                    "alternatives": [],
                }
            if flight["error"] is not None:
                raise flight["error"]
            return copy.deepcopy(flight["value"])

        try:
            result = self._plan_gtfs_schedule_uncached(
                provider=provider_id,
                schedule_source_id=source_id,
                origin_node_id=origin_id,
                destination_node_id=destination_id,
                service_date=day,
                departure_time=clock,
            )
            flight["value"] = copy.deepcopy(result)
            if result.get("reason") != "SCHEDULE_BUSY":
                with _SCHEDULE_COORDINATOR_LOCK:
                    _SCHEDULE_CACHE[cache_key] = (
                        time.monotonic() + GTFS_SCHEDULE_CACHE_TTL_SECONDS,
                        copy.deepcopy(result),
                    )
                    _SCHEDULE_CACHE.move_to_end(cache_key)
                    while len(_SCHEDULE_CACHE) > MAX_GTFS_SCHEDULE_CACHE_ENTRIES:
                        _SCHEDULE_CACHE.popitem(last=False)
            return result
        except BaseException as exc:
            flight["error"] = exc
            raise
        finally:
            flight["event"].set()
            with _SCHEDULE_COORDINATOR_LOCK:
                if _SCHEDULE_FLIGHTS.get(cache_key) is flight:
                    del _SCHEDULE_FLIGHTS[cache_key]

    def _plan_gtfs_schedule_uncached(
        self,
        *,
        provider: str,
        schedule_source_id: str,
        origin_node_id: str,
        destination_node_id: str,
        service_date: str,
        departure_time: str,
    ) -> dict[str, Any]:
        """Bounded earliest-arrival Dijkstra over one active official GTFS feed.

        ``service_date`` and ``departure_time`` are a civil local date/time.
        The previous GTFS service day's 24:xx-47:xx records and the selected
        day's 00:xx records therefore compete on one absolute timeline.  No
        TAGO, name, or geographic stop join is attempted.
        """
        provider_id = _safe_code(provider, "provider")
        source_id = _safe_code(schedule_source_id, "schedule_source_id")
        origin_id = _safe_transport_identifier(origin_node_id, "origin_node_id")
        destination_id = _safe_transport_identifier(
            destination_node_id, "destination_node_id"
        )
        if origin_id == destination_id:
            raise CatalogValidationError("origin and destination must differ")
        civil_day_text = _source_date(service_date)
        civil_day = date.fromisoformat(civil_day_text)
        match = re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])", str(departure_time or ""))
        if match is None:
            raise CatalogValidationError("departure_time must use HH:MM")
        requested_seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60
        horizon_end = requested_seconds + MAX_GTFS_SCHEDULE_HORIZON_SECONDS
        service_days = tuple(civil_day + timedelta(days=offset) for offset in (-1, 0, 1))

        admitted = self._schedule_slots.acquire(
            timeout=GTFS_SCHEDULE_ADMISSION_TIMEOUT_SECONDS
        )
        if not admitted:
            return {
                "status": "DATA_GAP",
                "reason": "SCHEDULE_BUSY",
                "schedule": {
                    "status": "DATA_GAP", "reason": "SCHEDULE_BUSY",
                    "service_date": civil_day_text, "departure_time": departure_time,
                    "provider": provider_id, "basis": None, "feed_id": None,
                    "timezone": "Asia/Seoul",
                },
                "graph": {
                    "algorithm": "bounded_time_dependent_dijkstra",
                    "search_complete": False,
                },
                "alternatives": [],
            }
        deadline_at = time.monotonic() + GTFS_SCHEDULE_WALL_CLOCK_SECONDS
        deadline_hit = threading.Event()
        feed_id_for_gap: str | None = None
        try:
            with self.connect() as connection:
                def abort_long_sql() -> int:
                    if time.monotonic() >= deadline_at:
                        deadline_hit.set()
                        return 1
                    return 0

                connection.set_progress_handler(abort_long_sql, 10_000)
                connection.execute("BEGIN")
                feed = connection.execute(
                    "SELECT f.*,a.topology_role FROM active_gtfs_feeds a "
                    "JOIN gtfs_feed_versions f ON f.feed_id=a.feed_id WHERE a.provider=?",
                    (provider_id,),
                ).fetchone()
                if feed is None:
                    return self._gtfs_schedule_gap(
                        provider_id, civil_day_text, departure_time,
                        "ACTIVE_GTFS_FEED_REQUIRED",
                    )
                feed_id = str(feed["feed_id"])
                feed_id_for_gap = feed_id
                if feed["topology_role"] != "active_topology":
                    gap = self._gtfs_schedule_gap(
                        provider_id,
                        civil_day_text,
                        departure_time,
                        "HISTORICAL_GTFS_PRIOR_ONLY",
                        feed_id=feed_id,
                    )
                    gap["schedule"].update(
                        {
                            "basis": "HISTORICAL_GTFS_PRIOR_ONLY",
                            "topology_role": "historical_model",
                            "projection_allowed": False,
                            "success_probability": None,
                        }
                    )
                    gap["schedule"]["limitations"] = [
                        "NOT_A_CURRENT_TIMETABLE",
                        "MUST_MATCH_CURRENT_TAGO_IDENTIFIERS_AND_ACTUAL_OBSERVATIONS",
                        "CANNOT_INDEPENDENTLY_PRODUCE_RELIABILITY_PROBABILITY",
                    ]
                    return gap
                endpoints = {}
                for key, node_id in (("origin", origin_id), ("destination", destination_id)):
                    rows = connection.execute(
                        "SELECT raw_stop_id,node_id,stop_name,latitude,longitude "
                        "FROM gtfs_stops WHERE feed_id=? AND node_id=? LIMIT 2",
                        (feed_id, node_id),
                    ).fetchall()
                    if len(rows) != 1:
                        return self._gtfs_schedule_gap(
                            provider_id, civil_day_text, departure_time,
                            "STOP_NOT_IN_ACTIVE_GTFS_FEED", feed_id=feed_id,
                        )
                    endpoints[key] = dict(rows[0])

                weekday_names = (
                    "monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday",
                )
                # Resolve each civil-adjacent service day once. Exceptions
                # override the base weekday calendar before any stop search.
                connection.execute(
                    "CREATE TEMP TABLE active_schedule_services("
                    "service_date TEXT NOT NULL,raw_service_id TEXT NOT NULL,"
                    "PRIMARY KEY(service_date,raw_service_id)) WITHOUT ROWID"
                )
                active_service_rows = 0
                for operating_day in service_days:
                    if time.monotonic() >= deadline_at:
                        deadline_hit.set()
                        break
                    day_text = operating_day.isoformat()
                    predicate = self._gtfs_operating_predicate(
                        weekday_names[operating_day.weekday()]
                    )
                    connection.execute(
                        "INSERT INTO temp.active_schedule_services "
                        "SELECT ?,s.raw_service_id FROM gtfs_services s "
                        "LEFT JOIN gtfs_calendar_dates cd ON cd.feed_id=s.feed_id "
                        "AND cd.raw_service_id=s.raw_service_id AND cd.service_date=? "
                        "WHERE s.feed_id=? AND " + predicate,
                        (day_text, day_text, feed_id, day_text, day_text),
                    )
                    active_service_rows = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM temp.active_schedule_services"
                        ).fetchone()[0]
                    )
                    if active_service_rows > MAX_GTFS_SCHEDULE_ACTIVE_SERVICE_ROWS:
                        gap = self._gtfs_schedule_gap(
                            provider_id, civil_day_text, departure_time,
                            "ACTIVE_SERVICE_SET_LIMIT_REACHED", feed_id=feed_id,
                        )
                        gap["graph"]["active_service_rows"] = active_service_rows
                        return gap
                if deadline_hit.is_set():
                    return self._gtfs_schedule_gap(
                        provider_id, civil_day_text, departure_time,
                        "SCHEDULE_DEADLINE_REACHED", feed_id=feed_id,
                    )
                trip_cache: dict[str, tuple[dict[str, Any], ...] | None] = {}
                best: dict[str, int] = {
                    str(endpoints["origin"]["raw_stop_id"]): requested_seconds
                }
                previous: dict[str, tuple[str, dict[str, Any]]] = {}
                queue: list[tuple[int, str]] = [
                    (requested_seconds, str(endpoints["origin"]["raw_stop_id"]))
                ]
                goal_raw = str(endpoints["destination"]["raw_stop_id"])
                expansions = 0
                departures_scanned = 0
                timetable_rows_skipped = 0
                trip_stop_limit_hits = 0
                invalid_trip_time_hits = 0
                boarding_limit_hit = False
                cache_limit_hit = False
                cached_stop_rows = 0
                goal_finalized = False

                while queue and expansions < MAX_GTFS_SCHEDULE_EXPANSIONS:
                    if time.monotonic() >= deadline_at:
                        deadline_hit.set()
                        break
                    if cache_limit_hit:
                        break
                    arrival_absolute, raw_stop_id = heapq.heappop(queue)
                    if best.get(raw_stop_id) != arrival_absolute:
                        continue
                    expansions += 1
                    if raw_stop_id == goal_raw:
                        goal_finalized = True
                        break
                    ready_absolute = arrival_absolute + (
                        GTFS_TRANSFER_BUFFER_SECONDS if raw_stop_id in previous else 0
                    )
                    boarding_rows: list[dict[str, Any]] = []
                    for operating_day in service_days:
                        if time.monotonic() >= deadline_at:
                            deadline_hit.set()
                            break
                        offset_seconds = (operating_day - civil_day).days * 86_400
                        lower = max(0, ready_absolute - offset_seconds)
                        upper = min(172_799, horizon_end - offset_seconds)
                        if lower > upper:
                            continue
                        day_text = operating_day.isoformat()
                        rows = connection.execute(
                            "SELECT st.raw_trip_id,st.stop_sequence,st.departure_time,"
                            "st.departure_seconds,t.trip_namespace_id,p.graph_city_code,"
                            "p.graph_route_id,r.route_short_name,r.route_long_name "
                            "FROM gtfs_stop_times st "
                            "JOIN gtfs_trips t ON t.feed_id=st.feed_id "
                            "AND t.raw_trip_id=st.raw_trip_id AND t.pattern_id IS NOT NULL "
                            "JOIN temp.active_schedule_services active_service "
                            "ON active_service.service_date=? "
                            "AND active_service.raw_service_id=t.raw_service_id "
                            "JOIN gtfs_patterns p ON p.feed_id=t.feed_id AND p.pattern_id=t.pattern_id "
                            "JOIN gtfs_routes r ON r.feed_id=t.feed_id AND r.raw_route_id=t.raw_route_id "
                            "WHERE st.feed_id=? AND st.raw_stop_id=? "
                            "AND st.departure_seconds BETWEEN ? AND ? "
                            "AND COALESCE(st.pickup_type,0)=0 "
                            "ORDER BY st.departure_seconds,st.raw_trip_id,st.stop_sequence LIMIT ?",
                            (
                                day_text, feed_id, raw_stop_id, lower, upper,
                                MAX_GTFS_SCHEDULE_DEPARTURES_PER_STOP + 1,
                            ),
                        ).fetchall()
                        if len(rows) > MAX_GTFS_SCHEDULE_DEPARTURES_PER_STOP:
                            boarding_limit_hit = True
                            rows = rows[:MAX_GTFS_SCHEDULE_DEPARTURES_PER_STOP]
                        for row in rows:
                            item = dict(row)
                            item["gtfs_service_date"] = day_text
                            item["day_offset_seconds"] = offset_seconds
                            item["departure_absolute"] = (
                                int(item["departure_seconds"]) + offset_seconds
                            )
                            boarding_rows.append(item)
                    boarding_rows.sort(
                        key=lambda item: (
                            item["departure_absolute"], item["trip_namespace_id"],
                            item["stop_sequence"],
                        )
                    )
                    if len(boarding_rows) > MAX_GTFS_SCHEDULE_DEPARTURES_PER_STOP:
                        boarding_limit_hit = True
                        boarding_rows = boarding_rows[:MAX_GTFS_SCHEDULE_DEPARTURES_PER_STOP]
                    departures_scanned += len(boarding_rows)

                    for board in boarding_rows:
                        if time.monotonic() >= deadline_at:
                            deadline_hit.set()
                            break
                        raw_trip_id = str(board["raw_trip_id"])
                        if raw_trip_id not in trip_cache:
                            if len(trip_cache) >= MAX_GTFS_SCHEDULE_CACHED_TRIPS:
                                cache_limit_hit = True
                                break
                            stop_rows = connection.execute(
                                "SELECT st.stop_sequence,st.raw_stop_id,st.arrival_time,"
                                "st.arrival_seconds,st.departure_time,st.departure_seconds,"
                                "st.pickup_type,st.drop_off_type,s.node_id,s.stop_name,"
                                "s.latitude,s.longitude "
                                "FROM gtfs_stop_times st JOIN gtfs_stops s "
                                "ON s.feed_id=st.feed_id AND s.raw_stop_id=st.raw_stop_id "
                                "WHERE st.feed_id=? AND st.raw_trip_id=? "
                                "ORDER BY st.stop_sequence LIMIT ?",
                                (feed_id, raw_trip_id, MAX_GTFS_SCHEDULE_TRIP_STOPS + 1),
                            ).fetchall()
                            if len(stop_rows) > MAX_GTFS_SCHEDULE_TRIP_STOPS:
                                trip_cache[raw_trip_id] = None
                                trip_stop_limit_hits += 1
                            else:
                                previous_departure: int | None = None
                                monotonic = True
                                for stop_row in stop_rows:
                                    arrival_value = stop_row["arrival_seconds"]
                                    departure_value = stop_row["departure_seconds"]
                                    if arrival_value is None or departure_value is None:
                                        monotonic = False
                                        break
                                    arrival_value = int(arrival_value)
                                    departure_value = int(departure_value)
                                    if (
                                        arrival_value > departure_value
                                        or (
                                            previous_departure is not None
                                            and arrival_value < previous_departure
                                        )
                                    ):
                                        monotonic = False
                                        break
                                    previous_departure = departure_value
                                if not monotonic:
                                    trip_cache[raw_trip_id] = None
                                    invalid_trip_time_hits += 1
                                    continue
                                if (
                                    cached_stop_rows + len(stop_rows)
                                    > MAX_GTFS_SCHEDULE_CACHED_STOP_ROWS
                                ):
                                    cache_limit_hit = True
                                    trip_cache[raw_trip_id] = None
                                    break
                                trip_cache[raw_trip_id] = tuple(dict(row) for row in stop_rows)
                                cached_stop_rows += len(stop_rows)
                        trip_rows = trip_cache[raw_trip_id]
                        if trip_rows is None:
                            continue
                        board_sequence = int(board["stop_sequence"])
                        board_absolute = int(board["departure_absolute"])
                        for downstream in trip_rows:
                            if time.monotonic() >= deadline_at:
                                deadline_hit.set()
                                break
                            sequence = int(downstream["stop_sequence"])
                            if sequence <= board_sequence:
                                continue
                            arrival_seconds = downstream.get("arrival_seconds")
                            if arrival_seconds is None or downstream.get("drop_off_type") not in (None, 0):
                                continue
                            downstream_absolute = (
                                int(arrival_seconds) + int(board["day_offset_seconds"])
                            )
                            if downstream_absolute < board_absolute or downstream_absolute > horizon_end:
                                timetable_rows_skipped += 1
                                continue
                            target_raw = str(downstream["raw_stop_id"])
                            if downstream_absolute >= best.get(target_raw, horizon_end + 1):
                                continue
                            best[target_raw] = downstream_absolute
                            previous[target_raw] = (
                                raw_stop_id,
                                {
                                    "raw_trip_id": raw_trip_id,
                                    "trip_id": board["trip_namespace_id"],
                                    "route_id": board["graph_route_id"],
                                    "city_code": board["graph_city_code"],
                                    "route_no": board["route_short_name"] or board["route_long_name"] or board["graph_route_id"],
                                    "gtfs_service_date": board["gtfs_service_date"],
                                    "day_offset_seconds": board["day_offset_seconds"],
                                    "board_sequence": board_sequence,
                                    "alight_sequence": sequence,
                                    "departure_time": board["departure_time"],
                                    "departure_seconds": board["departure_seconds"],
                                    "departure_absolute": board_absolute,
                                    "arrival_time": downstream["arrival_time"],
                                    "arrival_seconds": arrival_seconds,
                                    "arrival_absolute": downstream_absolute,
                                },
                            )
                            heapq.heappush(queue, (downstream_absolute, target_raw))

                search_bound_hit = (
                    boarding_limit_hit
                    or cache_limit_hit
                    or trip_stop_limit_hits > 0
                    or invalid_trip_time_hits > 0
                )
                if not goal_finalized or search_bound_hit:
                    detail = (
                        "SCHEDULE_DEADLINE_REACHED"
                        if deadline_hit.is_set()
                        else "SEARCH_EXPANSION_LIMIT_REACHED"
                        if queue and expansions >= MAX_GTFS_SCHEDULE_EXPANSIONS
                        else "SCHEDULE_SEARCH_BOUND_REACHED"
                        if search_bound_hit
                        else "NO_OPERATING_GTFS_PATH_AT_REQUESTED_TIME"
                    )
                    gap = self._gtfs_schedule_gap(
                        provider_id, civil_day_text, departure_time,
                        detail, feed_id=feed_id,
                    )
                    gap["reason"] = "SCHEDULE_DATA_GAP"
                    gap["schedule"]["reason"] = "SCHEDULE_DATA_GAP"
                    gap["schedule"]["detail_reason"] = detail
                    gap["graph"].update(
                        {
                            "expanded_stops": expansions,
                            "departures_scanned": departures_scanned,
                            "boarding_limit_hit": boarding_limit_hit,
                            "trip_stop_limit_hits": trip_stop_limit_hits,
                            "invalid_trip_time_hits": invalid_trip_time_hits,
                            "cache_limit_hit": cache_limit_hit,
                            "search_complete": (
                                detail == "NO_OPERATING_GTFS_PATH_AT_REQUESTED_TIME"
                            ),
                        }
                    )
                    return gap

                rides_reversed: list[dict[str, Any]] = []
                cursor = goal_raw
                origin_raw = str(endpoints["origin"]["raw_stop_id"])
                while cursor != origin_raw:
                    parent = previous.get(cursor)
                    if parent is None:
                        return self._gtfs_schedule_gap(
                            provider_id, civil_day_text, departure_time,
                            "SCHEDULE_PREDECESSOR_DATA_GAP", feed_id=feed_id,
                        )
                    cursor, ride = parent
                    rides_reversed.append(ride)
                rides = list(reversed(rides_reversed))
                candidate = self._gtfs_schedule_candidate(
                    rides=rides,
                    trip_cache=trip_cache,
                    civil_day=civil_day,
                    requested_seconds=requested_seconds,
                    feed_id=feed_id,
                    provider=provider_id,
                    schedule_source_id=source_id,
                )
                return {
                    "status": "READY",
                    "reason": None,
                    "schedule": {
                        "status": "READY", "reason": None,
                        "service_date": civil_day_text,
                        "departure_time": departure_time,
                        "basis": "OFFICIAL_STATIC_GTFS_RAW_EVIDENCE",
                        "provider": provider_id,
                        "feed_id": feed_id,
                        "source_date": feed["source_date"],
                        "topology_role": "active_topology",
                        "projection_allowed": True,
                        "timezone": "Asia/Seoul",
                        "actual_operation_observed": False,
                        "limitations": [
                            "CALENDAR_TXT_REQUIRED",
                            "CALENDAR_DATES_ONLY_FEED_UNSUPPORTED",
                            "TRANSFERS_TXT_NOT_INGESTED",
                            "WALKING_TRANSFER_NOT_IN_SCHEDULE_SEARCH",
                        ],
                    },
                    "graph": {
                        "algorithm": "bounded_time_dependent_dijkstra",
                        "time_axis": "civil_local_absolute_seconds",
                        "service_days_considered": [item.isoformat() for item in service_days],
                        "exact_stop_identity_only": True,
                        "name_or_distance_join": False,
                        "expanded_stops": expansions,
                        "departures_scanned": departures_scanned,
                        "timetable_rows_skipped": timetable_rows_skipped,
                        "trip_stop_limit_hits": trip_stop_limit_hits,
                        "invalid_trip_time_hits": invalid_trip_time_hits,
                        "max_expansions": MAX_GTFS_SCHEDULE_EXPANSIONS,
                        "max_departures_per_stop": MAX_GTFS_SCHEDULE_DEPARTURES_PER_STOP,
                        "max_parallel_searches": MAX_GTFS_SCHEDULE_PARALLEL_SEARCHES,
                        "active_service_rows": active_service_rows,
                        "transfer_buffer_minutes": GTFS_TRANSFER_BUFFER_SECONDS // 60,
                        "transfer_buffer_source": "server_safety_policy",
                        "transfer_model": "EXACT_STOP_SERVER_5_MIN",
                        "search_complete": True,
                        "wall_clock_deadline_seconds": GTFS_SCHEDULE_WALL_CLOCK_SECONDS,
                    },
                    "alternatives": [candidate],
                }
        except sqlite3.OperationalError as exc:
            if deadline_hit.is_set() or "interrupted" in str(exc).lower():
                return self._gtfs_schedule_gap(
                    provider_id, civil_day_text, departure_time,
                    "SCHEDULE_DEADLINE_REACHED", feed_id=feed_id_for_gap,
                )
            raise
        finally:
            self._schedule_slots.release()

    @staticmethod
    def _gtfs_schedule_gap(
        provider: str,
        service_date: str,
        departure_time: str,
        detail_reason: str,
        *,
        feed_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "DATA_GAP",
            "reason": "SCHEDULE_DATA_GAP",
            "schedule": {
                "status": "DATA_GAP", "reason": "SCHEDULE_DATA_GAP",
                "detail_reason": detail_reason,
                "service_date": service_date,
                "departure_time": departure_time,
                "basis": "OFFICIAL_STATIC_GTFS_RAW_EVIDENCE" if feed_id else None,
                "provider": provider,
                "feed_id": feed_id,
                "timezone": "Asia/Seoul",
                "limitations": [
                    "CALENDAR_TXT_REQUIRED",
                    "CALENDAR_DATES_ONLY_FEED_UNSUPPORTED",
                    "TRANSFERS_TXT_NOT_INGESTED",
                    "WALKING_TRANSFER_NOT_IN_SCHEDULE_SEARCH",
                ],
            },
            "graph": {
                "algorithm": "bounded_time_dependent_dijkstra",
                "exact_stop_identity_only": True,
                "name_or_distance_join": False,
                "search_complete": False,
            },
            "alternatives": [],
        }

    @staticmethod
    def _gtfs_civil_timestamp(
        civil_day: date, absolute_seconds: int
    ) -> str:
        value = datetime.combine(
            civil_day, datetime.min.time(), tzinfo=SEOUL_TIMEZONE
        ) + timedelta(
            seconds=absolute_seconds
        )
        return value.isoformat(timespec="seconds")

    def _gtfs_schedule_candidate(
        self,
        *,
        rides: list[dict[str, Any]],
        trip_cache: Mapping[str, tuple[dict[str, Any], ...] | None],
        civil_day: date,
        requested_seconds: int,
        feed_id: str,
        provider: str,
        schedule_source_id: str,
    ) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        replay_candidates: list[dict[str, Any]] = []
        replay_gaps: list[dict[str, Any]] = []
        route_ids: list[str] = []
        route_labels: dict[str, str] = {}
        ride_endpoints: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for ride_index, ride in enumerate(rides):
            route_id = str(ride["route_id"])
            if not route_ids or route_ids[-1] != route_id:
                route_ids.append(route_id)
            route_labels[route_id] = str(ride["route_no"])
            rows = trip_cache[str(ride["raw_trip_id"])] or ()
            selected = [
                row for row in rows
                if int(ride["board_sequence"]) <= int(row["stop_sequence"]) <= int(ride["alight_sequence"])
            ]
            if len(selected) < 2:
                raise CatalogValidationError("selected GTFS trip segment is incomplete")
            ride_endpoints.append((selected[0], selected[-1]))
            for before, after in zip(selected, selected[1:]):
                steps.append(
                    {
                        "kind": "ride",
                        "route_id": route_id,
                        "route_no": ride["route_no"],
                        "trip_id": ride["trip_id"],
                        "gtfs_service_date": ride["gtfs_service_date"],
                        "departure_time": before["departure_time"],
                        "departure_seconds": before["departure_seconds"],
                        "arrival_time": after["arrival_time"],
                        "arrival_seconds": after["arrival_seconds"],
                        "departure_datetime": self._gtfs_civil_timestamp(
                            civil_day,
                            int(before["departure_seconds"]) + int(ride["day_offset_seconds"]),
                        ) if before["departure_seconds"] is not None else None,
                        "arrival_datetime": self._gtfs_civil_timestamp(
                            civil_day,
                            int(after["arrival_seconds"]) + int(ride["day_offset_seconds"]),
                        ) if after["arrival_seconds"] is not None else None,
                        "from": {
                            "city_code": ride["city_code"], "node_id": before["node_id"],
                            "node_name": before["stop_name"], "node_order": int(before["stop_sequence"]),
                            "latitude": before["latitude"], "longitude": before["longitude"],
                        },
                        "to": {
                            "city_code": ride["city_code"], "node_id": after["node_id"],
                            "node_name": after["stop_name"], "node_order": int(after["stop_sequence"]),
                            "latitude": after["latitude"], "longitude": after["longitude"],
                        },
                        "distance_m": 0.0,
                        "evidence": {
                            "type": "official_gtfs_stop_times",
                            "source": schedule_source_id,
                            "feed_id": feed_id,
                        },
                    }
                )
            if ride_index:
                previous_ride = rides[ride_index - 1]
                previous_stop = ride_endpoints[ride_index - 1][1]
                next_stop = selected[0]
                transfer_step = {
                    "kind": "transfer", "route_id": None,
                    "from": {
                        "city_code": previous_ride["city_code"],
                        "node_id": previous_stop["node_id"],
                        "node_name": previous_stop["stop_name"],
                        "node_order": int(previous_stop["stop_sequence"]),
                        "latitude": previous_stop["latitude"], "longitude": previous_stop["longitude"],
                    },
                    "to": {
                        "city_code": ride["city_code"], "node_id": next_stop["node_id"],
                        "node_name": next_stop["stop_name"],
                        "node_order": int(next_stop["stop_sequence"]),
                        "latitude": next_stop["latitude"], "longitude": next_stop["longitude"],
                    },
                    "distance_m": 0.0,
                    "scheduled_arrival": previous_ride["arrival_time"],
                    "next_departure": ride["departure_time"],
                    "evidence": {"type": "shared_node_id", "source": schedule_source_id, "feed_id": feed_id},
                }
                steps.insert(len(steps) - (len(selected) - 1), transfer_step)
                replay_leg = {
                    "id": f"transfer-{ride_index}",
                    "route_id": previous_ride["route_id"],
                    "node_id": previous_stop["node_id"],
                    "node_order": int(previous_stop["stop_sequence"]),
                    "scheduled_arrival": previous_ride["arrival_time"],
                    "next_departure": ride["departure_time"],
                    "minimum_transfer_minutes": GTFS_TRANSFER_BUFFER_SECONDS // 60,
                    "minimum_transfer_source": "server_safety_policy",
                    "time_evidence_source": schedule_source_id,
                    "time_evidence_verified": True,
                    "time_evidence_feed_id": feed_id,
                    "time_evidence_trip_id": previous_ride["trip_id"],
                    "next_route_id": ride["route_id"],
                    "next_node_id": next_stop["node_id"],
                    "next_node_order": int(next_stop["stop_sequence"]),
                    "next_time_evidence_trip_id": ride["trip_id"],
                    "next_time_evidence_feed_id": feed_id,
                    "gtfs_service_date": previous_ride["gtfs_service_date"],
                    "next_gtfs_service_date": ride["gtfs_service_date"],
                }
                if previous_ride["gtfs_service_date"] == ride["gtfs_service_date"]:
                    replay_candidates.append(replay_leg)
                else:
                    replay_gaps.append(
                        {**replay_leg, "reason": "CROSS_SERVICE_DAY_REPLAY_UNSUPPORTED"}
                    )

        replay_legs = [] if replay_gaps else replay_candidates
        departure_absolute = int(rides[0]["departure_absolute"])
        arrival_absolute = int(rides[-1]["arrival_absolute"])
        return {
            "criterion": "earliest_arrival",
            "status": "READY",
            "reasons": [],
            "scheduled": True,
            "schedule_status": "READY",
            "success_probability": None,
            "probability_basis": None,
            "probability_scope": None,
            "estimated_minutes": round((arrival_absolute - requested_seconds) / 60.0, 1),
            "in_vehicle_and_wait_minutes": round((arrival_absolute - departure_absolute) / 60.0, 1),
            "departure_time": rides[0]["departure_time"],
            "arrival_time": rides[-1]["arrival_time"],
            "departure_datetime": self._gtfs_civil_timestamp(civil_day, departure_absolute),
            "arrival_datetime": self._gtfs_civil_timestamp(civil_day, arrival_absolute),
            "operating_assumption": False,
            "transfers": max(0, len(rides) - 1),
            "walking_m": 0.0,
            "transfer_model": "EXACT_STOP_SERVER_5_MIN",
            "route_ids": route_ids,
            "route_labels": route_labels,
            "steps": steps,
            "replay_ready": bool(replay_legs),
            "replay_legs": replay_legs,
            "replay_data_gaps": replay_gaps,
            "gtfs_service_dates": sorted({str(ride["gtfs_service_date"]) for ride in rides}),
            "evidence": {
                "topology": "active_official_gtfs_patterns",
                "schedule": "active_official_gtfs_calendar_trips_stop_times",
                "basis": "OFFICIAL_STATIC_GTFS_RAW_EVIDENCE",
                "provider": provider,
                "source_id": schedule_source_id,
                "feed_id": feed_id,
                "actual_operation_observed": False,
                "success_rate_eligible": False,
                "service_routes": len(route_ids),
                "passage_routes": 0,
            },
            "coverage": {
                "structural": 1.0,
                "service_routes": len(route_ids),
                "schedule_routes": len(route_ids),
                "observed_service_routes": 0,
                "passage_routes": 0,
                "total_routes": len(route_ids),
                "minimum_passage_samples": 8,
            },
        }

    def create_topology_run(
        self,
        *,
        run_id: str,
        provider: str,
        target_source: str,
        request_budget: int,
        target_limit: int | None,
    ) -> None:
        run = _safe_code(run_id, "run_id")
        provider_id = _safe_code(provider, "provider")
        source = _safe_text(target_source, "target_source", required=True, maximum=32)
        budget = int(request_budget)
        if not 1 <= budget <= 100_000:
            raise CatalogLimitError("request_budget must be 1..100000")
        if target_limit is not None and not 1 <= int(target_limit) <= 500_000:
            raise CatalogLimitError("target_limit must be 1..500000")
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO topology_runs(run_id,provider,target_source,status,request_budget,target_limit,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (run, provider_id, source, "RUNNING", budget, target_limit, now, now),
            )
            connection.commit()

    def update_topology_run(self, run_id: str, **increments: int) -> None:
        run = _safe_code(run_id, "run_id")
        allowed = {"requests_used", "targets_processed", "succeeded", "unchanged", "failed", "deferred"}
        if not increments or set(increments) - allowed:
            raise CatalogValidationError("invalid topology run counter")
        values: list[Any] = []
        assignments: list[str] = []
        for field, value in sorted(increments.items()):
            amount = int(value)
            if amount < 0:
                raise CatalogValidationError("topology run counters cannot decrease")
            assignments.append(f"{field}={field}+?")
            values.append(amount)
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        values.extend([now, run])
        with self.connect() as connection:
            changed = connection.execute(
                f"UPDATE topology_runs SET {','.join(assignments)},updated_at=? WHERE run_id=? AND status='RUNNING'",
                values,
            ).rowcount
            if changed != 1:
                raise CatalogValidationError("topology run is not active")
            connection.commit()

    def finish_topology_run(self, run_id: str, status: str) -> dict[str, Any]:
        run = _safe_code(run_id, "run_id")
        final_status = _safe_text(status, "status", required=True, maximum=32)
        if final_status not in {"COMPLETE", "PARTIAL", "BUDGET_EXHAUSTED", "DATA_GAP", "FAILED"}:
            raise CatalogValidationError("invalid topology run status")
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute(
                "UPDATE topology_runs SET status=?,updated_at=?,finished_at=? WHERE run_id=? AND status='RUNNING'",
                (final_status, now, now, run),
            )
            row = connection.execute("SELECT * FROM topology_runs WHERE run_id=?", (run,)).fetchone()
            connection.commit()
        if row is None:
            raise CatalogValidationError("topology run does not exist")
        return dict(row)

    def upsert_topology_cities(self, *, provider: str, cities: Iterable[Mapping[str, Any]]) -> int:
        provider_id = _safe_code(provider, "provider")
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        rows: list[tuple[str, str, str, str]] = []
        for item in cities:
            rows.append(
                (
                    provider_id,
                    _safe_code(item.get("city_code"), "city_code"),
                    _safe_text(item.get("city_name"), "city_name", required=True, maximum=160),
                    now,
                )
            )
        with self.connect() as connection:
            connection.executemany(
                "INSERT INTO topology_discovered_cities VALUES(?,?,?,?) ON CONFLICT(provider,city_code) DO UPDATE SET city_name=excluded.city_name,discovered_at=excluded.discovered_at",
                rows,
            )
            connection.commit()
        return len(rows)

    def topology_cities(self, *, provider: str) -> list[dict[str, Any]]:
        provider_id = _safe_code(provider, "provider")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT city_code,city_name,discovered_at FROM topology_discovered_cities WHERE provider=? ORDER BY city_code",
                (provider_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def topology_discovery_progress(self, *, provider: str, scope_key: str) -> dict[str, Any]:
        provider_id = _safe_code(provider, "provider")
        scope = _safe_text(scope_key, "scope_key", required=True, maximum=120)
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO topology_discovery_progress(provider,scope_key,updated_at) VALUES(?,?,?)",
                (provider_id, scope, now),
            )
            row = connection.execute(
                "SELECT * FROM topology_discovery_progress WHERE provider=? AND scope_key=?",
                (provider_id, scope),
            ).fetchone()
            connection.commit()
        return dict(row)

    def update_topology_discovery(
        self,
        *,
        provider: str,
        scope_key: str,
        status: str,
        next_page: int,
        total_count: int | None,
        request_increment: int = 0,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        provider_id = _safe_code(provider, "provider")
        scope = _safe_text(scope_key, "scope_key", required=True, maximum=120)
        state = _safe_text(status, "status", required=True, maximum=20)
        if state not in {"PENDING", "IN_PROGRESS", "COMPLETE", "FAILED", "DEFERRED"}:
            raise CatalogValidationError("invalid discovery status")
        page = int(next_page)
        if page < 1:
            raise CatalogValidationError("next_page must be positive")
        code = _safe_text(error_code, "error_code", maximum=64) or None
        message = _safe_text(error_message, "error_message", maximum=240) or None
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO topology_discovery_progress(provider,scope_key,status,next_page,total_count,requests_used,error_code,error_message,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider,scope_key) DO UPDATE SET status=excluded.status,next_page=excluded.next_page,total_count=excluded.total_count,requests_used=topology_discovery_progress.requests_used+?,error_code=excluded.error_code,error_message=excluded.error_message,updated_at=excluded.updated_at",
                (provider_id, scope, state, page, total_count, request_increment, code, message, now, request_increment),
            )
            connection.commit()

    def upsert_topology_targets(
        self,
        *,
        provider: str,
        routes: Iterable[Mapping[str, Any]],
        discovery_source: str,
    ) -> int:
        provider_id = _safe_code(provider, "provider")
        source = _safe_text(discovery_source, "discovery_source", required=True, maximum=80)
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        rows: list[tuple[str, str, str, str, str, str]] = []
        for item in routes:
            rows.append(
                (
                    provider_id,
                    _safe_code(item.get("city_code"), "city_code"),
                    _safe_transport_identifier(item.get("route_id"), "route_id"),
                    _safe_text(item.get("route_no"), "route_no", maximum=160),
                    source,
                    now,
                )
            )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT INTO topology_targets VALUES(?,?,?,?,?,?) ON CONFLICT(provider,city_code,route_id) DO UPDATE SET route_no=excluded.route_no,discovery_source=excluded.discovery_source,discovered_at=excluded.discovered_at",
                rows,
            )
            connection.executemany(
                "INSERT OR IGNORE INTO topology_progress(provider,city_code,route_id,updated_at) VALUES(?,?,?,?)",
                [(provider_id, row[1], row[2], now) for row in rows],
            )
            connection.commit()
        return len(rows)

    def seed_topology_targets_from_catalog(
        self,
        *,
        provider: str,
        identifiers_verified_for_provider: bool = False,
    ) -> int:
        """Seed only when an operator verified the catalog's identifier namespace.

        The bundled TS route file uses a different identifier namespace from
        TAGO in some regions, so this method refuses implicit cross-provider
        hydration.
        """
        if not identifiers_verified_for_provider:
            raise CatalogValidationError(
                "catalog route identifiers are not verified for this provider"
            )
        provider_id = _safe_code(provider, "provider")
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT city_code,route_id,route_no FROM catalog_routes WHERE source_id=(SELECT value FROM catalog_meta WHERE key='active_routes_source_id') ORDER BY city_code,route_id"
            ).fetchall()
            connection.executemany(
                "INSERT OR IGNORE INTO topology_targets VALUES(?,?,?,?,?,?)",
                [(provider_id, row["city_code"], row["route_id"], row["route_no"], "VERIFIED_STATIC_CATALOG", now) for row in rows],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO topology_progress(provider,city_code,route_id,updated_at) VALUES(?,?,?,?)",
                [(provider_id, row["city_code"], row["route_id"], now) for row in rows],
            )
            connection.commit()
        return len(rows)

    def claim_topology_target(self, *, provider: str, run_id: str) -> dict[str, Any] | None:
        provider_id = _safe_code(provider, "provider")
        run = _safe_code(run_id, "run_id")
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "WITH completed_by_city AS ("
                "SELECT city_code,COUNT(*) completed_count FROM topology_progress "
                "WHERE provider=? AND status IN ('COMPLETE','UNCHANGED') GROUP BY city_code"
                ") "
                "SELECT p.*,t.route_no,t.discovery_source FROM topology_progress p "
                "JOIN topology_targets t USING(provider,city_code,route_id) "
                "LEFT JOIN completed_by_city c ON c.city_code=p.city_code "
                "WHERE p.provider=? AND p.status IN ('PENDING','FAILED','DEFERRED','IN_PROGRESS') "
                "AND (p.status<>'FAILED' OR p.attempts<3) "
                "AND (p.status<>'FAILED' OR COALESCE(p.error_code,'')<>?) "
                "AND (p.last_run_id IS NULL OR p.last_run_id<>?) "
                "ORDER BY CASE p.status WHEN 'IN_PROGRESS' THEN 0 WHEN 'DEFERRED' THEN 1 WHEN 'FAILED' THEN 2 ELSE 3 END,"
                "COALESCE(c.completed_count,0),p.city_code,p.route_id LIMIT 1",
                (
                    provider_id,
                    provider_id,
                    SINGLE_POINT_ROUTE_SPIKE_ERROR_CODE,
                    run,
                ),
            ).fetchone()
            if row is not None:
                retrying_failed = str(row["status"]) == "FAILED"
                if retrying_failed:
                    # A failed provider attempt is not a resumable checkpoint.
                    # Reusing its pages can combine two different upstream
                    # responses (for example an empty page 1 with a later page
                    # 2), producing a permanently incomplete sequence.
                    connection.execute(
                        "DELETE FROM topology_pages "
                        "WHERE provider=? AND city_code=? AND route_id=?",
                        (provider_id, row["city_code"], row["route_id"]),
                    )
                connection.execute(
                    "UPDATE topology_progress SET status='IN_PROGRESS',attempts=attempts+1,last_run_id=?,"
                    "next_page=CASE WHEN status='FAILED' THEN 1 ELSE next_page END,"
                    "total_count=CASE WHEN status='FAILED' THEN NULL ELSE total_count END,"
                    "pages_fetched=CASE WHEN status='FAILED' THEN 0 ELSE pages_fetched END,"
                    "staged_count=CASE WHEN status='FAILED' THEN 0 ELSE staged_count END,"
                    "error_code=NULL,error_message=NULL,updated_at=? "
                    "WHERE provider=? AND city_code=? AND route_id=?",
                    (run, now, provider_id, row["city_code"], row["route_id"]),
                )
            connection.commit()
        if row is None:
            return None
        claimed = dict(row)
        if str(row["status"]) == "FAILED":
            claimed.update(
                {
                    "next_page": 1,
                    "total_count": None,
                    "pages_fetched": 0,
                    "staged_count": 0,
                }
            )
        return claimed

    def claim_specific_topology_target(
        self,
        *,
        provider: str,
        run_id: str,
        city_code: str,
        route_id: str,
        refresh_complete: bool = False,
    ) -> dict[str, Any] | None:
        """Atomically claim one operator-selected topology target.

        A completed target is a successful no-op unless ``refresh_complete``
        is explicit. Missing targets, exhausted failures, invalid states, and a
        second claim by the same run fail closed instead of falling through to
        a different route.
        """
        provider_id = _safe_code(provider, "provider")
        run = _safe_code(run_id, "run_id")
        city = _safe_code(city_code, "city_code")
        route = _safe_transport_identifier(route_id, "route_id")
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_run = connection.execute(
                "SELECT 1 FROM topology_runs "
                "WHERE run_id=? AND provider=? AND status='RUNNING'",
                (run, provider_id),
            ).fetchone()
            if active_run is None:
                connection.rollback()
                raise CatalogValidationError("topology run is not active")
            row = connection.execute(
                "SELECT p.*,t.route_no,t.discovery_source "
                "FROM topology_targets t "
                "JOIN topology_progress p USING(provider,city_code,route_id) "
                "WHERE t.provider=? AND t.city_code=? AND t.route_id=?",
                (provider_id, city, route),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise CatalogValidationError(
                    f"requested topology target does not exist: {city}:{route}"
                )

            status = str(row["status"])
            attempts = int(row["attempts"])
            last_run_id = row["last_run_id"]
            if status in {"COMPLETE", "UNCHANGED"} and not refresh_complete:
                connection.commit()
                return None
            if (
                status == "FAILED"
                and attempts >= 3
                and str(row["error_code"] or "")
                != SINGLE_POINT_ROUTE_SPIKE_ERROR_CODE
            ):
                connection.rollback()
                raise CatalogValidationError(
                    f"requested topology target exhausted retry attempts: {city}:{route}"
                )
            if status not in {
                "PENDING",
                "FAILED",
                "DEFERRED",
                "IN_PROGRESS",
                "COMPLETE",
                "UNCHANGED",
            }:
                connection.rollback()
                raise CatalogValidationError(
                    f"requested topology target has invalid status: {city}:{route}"
                )
            if last_run_id == run:
                connection.rollback()
                raise CatalogValidationError(
                    f"requested topology target is already owned by this run: {city}:{route}"
                )

            refreshing = status in {"COMPLETE", "UNCHANGED"}
            restarting = refreshing or status == "FAILED"
            if restarting:
                connection.execute(
                    "DELETE FROM topology_pages "
                    "WHERE provider=? AND city_code=? AND route_id=?",
                    (provider_id, city, route),
                )
                changed = connection.execute(
                    "UPDATE topology_progress SET "
                    "status='IN_PROGRESS',attempts=attempts+1,last_run_id=?,"
                    "next_page=1,total_count=NULL,pages_fetched=0,staged_count=0,"
                    "error_code=NULL,error_message=NULL,updated_at=?,completed_at=NULL "
                    "WHERE provider=? AND city_code=? AND route_id=? "
                    "AND status=? AND attempts=? AND last_run_id IS ?",
                    (
                        run,
                        now,
                        provider_id,
                        city,
                        route,
                        status,
                        attempts,
                        last_run_id,
                    ),
                ).rowcount
            else:
                changed = connection.execute(
                    "UPDATE topology_progress SET "
                    "status='IN_PROGRESS',attempts=attempts+1,last_run_id=?,"
                    "error_code=NULL,error_message=NULL,updated_at=? "
                    "WHERE provider=? AND city_code=? AND route_id=? "
                    "AND status=? AND attempts=? AND last_run_id IS ?",
                    (
                        run,
                        now,
                        provider_id,
                        city,
                        route,
                        status,
                        attempts,
                        last_run_id,
                    ),
                ).rowcount
            if changed != 1:
                connection.rollback()
                raise CatalogValidationError("topology target claim lost ownership")
            connection.commit()

        claimed = dict(row)
        if restarting:
            claimed.update(
                {
                    "next_page": 1,
                    "total_count": None,
                    "pages_fetched": 0,
                    "staged_count": 0,
                }
            )
        return claimed

    def repair_corrupt_topology_retries(self, *, provider: str) -> int:
        """Requeue only failures proven to contain mixed retry pages.

        The old retry path could retain an empty ``page 1 / total 0`` response
        and append a later page reporting a positive total.  That combination
        cannot describe one upstream snapshot.  Genuine zero- or one-stop
        routes do not match this predicate and remain failed for audit.
        """
        provider_id = _safe_code(provider, "provider")
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT p.city_code,p.route_id FROM topology_progress p "
                "WHERE p.provider=? AND p.status='FAILED' "
                "AND p.error_code='INVALID_ROUTE_TOPOLOGY' "
                "AND p.error_message IN ("
                "  'complete route-stop sequence was not staged',"
                "  'ordered_stops must contain 2..10000 rows') "
                "AND EXISTS (SELECT 1 FROM topology_pages first_page "
                "  WHERE first_page.provider=p.provider "
                "  AND first_page.city_code=p.city_code "
                "  AND first_page.route_id=p.route_id "
                "  AND first_page.page_no=1 "
                "  AND first_page.item_count=0 "
                "  AND first_page.total_count=0) "
                "AND EXISTS (SELECT 1 FROM topology_pages later_page "
                "  WHERE later_page.provider=p.provider "
                "  AND later_page.city_code=p.city_code "
                "  AND later_page.route_id=p.route_id "
                "  AND later_page.page_no>1 "
                "  AND later_page.total_count>0) "
                "ORDER BY p.city_code,p.route_id",
                (provider_id,),
            ).fetchall()
            targets = [(row["city_code"], row["route_id"]) for row in rows]
            connection.executemany(
                "DELETE FROM topology_pages "
                "WHERE provider=? AND city_code=? AND route_id=?",
                [(provider_id, city, route) for city, route in targets],
            )
            connection.executemany(
                "UPDATE topology_progress SET "
                "status='PENDING',next_page=1,total_count=NULL,pages_fetched=0,"
                "attempts=0,staged_count=0,error_code=NULL,error_message=NULL,"
                "last_run_id=NULL,updated_at=?,completed_at=NULL "
                "WHERE provider=? AND city_code=? AND route_id=? "
                "AND status='FAILED'",
                [(now, provider_id, city, route) for city, route in targets],
            )
            connection.commit()
        return len(targets)

    def queue_topology_refresh(self, *, provider: str) -> int:
        """Explicitly queue already-complete targets for a fresh hash check."""
        provider_id = _safe_code(provider, "provider")
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT city_code,route_id FROM topology_progress WHERE provider=? AND status IN ('COMPLETE','UNCHANGED')",
                (provider_id,),
            ).fetchall()
            connection.execute(
                "DELETE FROM topology_pages WHERE provider=? AND (city_code,route_id) IN (SELECT city_code,route_id FROM topology_progress WHERE provider=? AND status IN ('COMPLETE','UNCHANGED'))",
                (provider_id, provider_id),
            )
            connection.execute(
                "UPDATE topology_progress SET status='PENDING',next_page=1,total_count=NULL,pages_fetched=0,staged_count=0,error_code=NULL,error_message=NULL,updated_at=?,completed_at=NULL WHERE provider=? AND status IN ('COMPLETE','UNCHANGED')",
                (now, provider_id),
            )
            connection.commit()
        return len(rows)

    def stage_topology_page(
        self,
        *,
        provider: str,
        city_code: str,
        route_id: str,
        page_no: int,
        items: Sequence[Mapping[str, Any]],
        total_count: int,
    ) -> dict[str, Any]:
        provider_id = _safe_code(provider, "provider")
        city = _safe_code(city_code, "city_code")
        route = _safe_transport_identifier(route_id, "route_id")
        page = int(page_no)
        total = int(total_count)
        if page < 1 or total < 0 or len(items) > MAX_TOPOLOGY_PAGE_ITEMS:
            raise CatalogLimitError("invalid topology page bounds")
        normalized = [dict(item) for item in items]
        payload = _canonical(normalized)
        if len(payload.encode("utf-8")) > MAX_TOPOLOGY_PAGE_BYTES:
            raise CatalogLimitError("topology page exceeds storage bound")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO topology_pages VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,city_code,route_id,page_no) DO UPDATE SET item_count=excluded.item_count,total_count=excluded.total_count,payload_sha256=excluded.payload_sha256,items_json=excluded.items_json,fetched_at=excluded.fetched_at",
                (provider_id, city, route, page, len(normalized), total, digest, payload, now),
            )
            summary = connection.execute(
                "SELECT COUNT(*) pages,COALESCE(SUM(item_count),0) items FROM topology_pages WHERE provider=? AND city_code=? AND route_id=?",
                (provider_id, city, route),
            ).fetchone()
            connection.execute(
                "UPDATE topology_progress SET next_page=?,total_count=?,pages_fetched=?,staged_count=?,updated_at=? WHERE provider=? AND city_code=? AND route_id=?",
                (page + 1, total, summary["pages"], summary["items"], now, provider_id, city, route),
            )
            connection.commit()
        return {"page_no": page, "item_count": len(normalized), "sha256": digest}

    def record_topology_target_request(self, *, provider: str, city_code: str, route_id: str) -> None:
        provider_id = _safe_code(provider, "provider")
        city = _safe_code(city_code, "city_code")
        route = _safe_transport_identifier(route_id, "route_id")
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute(
                "UPDATE topology_progress SET requests_used=requests_used+1,updated_at=? WHERE provider=? AND city_code=? AND route_id=?",
                (now, provider_id, city, route),
            )
            connection.commit()

    def record_topology_request_attempt(
        self,
        *,
        run_id: str,
        provider: str,
        city_code: str | None = None,
        route_id: str | None = None,
    ) -> None:
        """Atomically reserve one run request and its optional target request."""
        run = _safe_code(run_id, "run_id")
        provider_id = _safe_code(provider, "provider")
        if (city_code is None) != (route_id is None):
            raise CatalogValidationError(
                "city_code and route_id must both be supplied for a target request"
            )
        city = _safe_code(city_code, "city_code") if city_code is not None else None
        route = (
            _safe_transport_identifier(route_id, "route_id")
            if route_id is not None
            else None
        )
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE topology_runs SET requests_used=requests_used+1,updated_at=? "
                "WHERE run_id=? AND provider=? AND status='RUNNING'",
                (now, run, provider_id),
            ).rowcount
            if changed != 1:
                raise CatalogValidationError("topology run is not active")
            if city is not None and route is not None:
                changed = connection.execute(
                    "UPDATE topology_progress SET requests_used=requests_used+1,updated_at=? "
                    "WHERE provider=? AND city_code=? AND route_id=? "
                    "AND status='IN_PROGRESS' AND last_run_id=?",
                    (now, provider_id, city, route, run),
                ).rowcount
                if changed != 1:
                    raise CatalogValidationError(
                        "topology target is not owned by the active run"
                    )
            connection.commit()

    def staged_topology_route(self, *, provider: str, city_code: str, route_id: str) -> list[dict[str, Any]]:
        provider_id = _safe_code(provider, "provider")
        city = _safe_code(city_code, "city_code")
        route = _safe_transport_identifier(route_id, "route_id")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT page_no,items_json FROM topology_pages WHERE provider=? AND city_code=? AND route_id=? ORDER BY page_no",
                (provider_id, city, route),
            ).fetchall()
        items: list[dict[str, Any]] = []
        expected_page = 1
        for row in rows:
            if int(row["page_no"]) != expected_page:
                raise CatalogValidationError("staged topology pages are not contiguous")
            decoded = json.loads(row["items_json"])
            if not isinstance(decoded, list):
                raise CatalogValidationError("staged topology page is invalid")
            items.extend(item for item in decoded if isinstance(item, dict))
            expected_page += 1
        return items

    def finish_topology_target(
        self,
        *,
        provider: str,
        city_code: str,
        route_id: str,
        unchanged: bool,
        content_sha256: str,
        sequence_id: str,
    ) -> None:
        provider_id = _safe_code(provider, "provider")
        city = _safe_code(city_code, "city_code")
        route = _safe_transport_identifier(route_id, "route_id")
        digest = _safe_text(content_sha256, "content_sha256", required=True, maximum=64)
        sequence = _safe_code(sequence_id, "sequence_id")
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        state = "UNCHANGED" if unchanged else "COMPLETE"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE topology_progress SET status=?,content_sha256=?,sequence_id=?,error_code=NULL,error_message=NULL,updated_at=?,completed_at=? WHERE provider=? AND city_code=? AND route_id=?",
                (state, digest, sequence, now, now, provider_id, city, route),
            )
            connection.execute(
                "DELETE FROM topology_pages WHERE provider=? AND city_code=? AND route_id=?",
                (provider_id, city, route),
            )
            connection.commit()

    def quarantine_topology_route_spike(
        self,
        *,
        provider: str,
        city_code: str,
        route_id: str,
        expected_sequence_id: str | None,
        evidence: SinglePointRouteSpike,
    ) -> dict[str, Any]:
        """Fail one target and remove only its expected active pointer.

        Immutable sequence versions, stops, provenance, and hashes remain in
        place for audit.  A stale caller cannot remove a newly activated
        corrected sequence because the delete includes ``expected_sequence_id``.
        """

        provider_id = _safe_code(provider, "provider")
        city = _safe_code(city_code, "city_code")
        route = _safe_transport_identifier(route_id, "route_id")
        expected = (
            _safe_code(expected_sequence_id, "expected_sequence_id")
            if expected_sequence_id is not None
            else None
        )
        if not isinstance(evidence, SinglePointRouteSpike):
            raise CatalogValidationError("single-point spike evidence is required")
        message = evidence.bounded_evidence()
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        pointer_removed = False
        guard_matched = False
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT sequence_id FROM active_route_sequences "
                "WHERE city_code=? AND route_id=?",
                (city, route),
            ).fetchone()
            actual = str(active["sequence_id"]) if active is not None else None
            guard_matched = actual == expected
            if expected is not None and guard_matched:
                pointer_removed = (
                    connection.execute(
                        "DELETE FROM active_route_sequences "
                        "WHERE city_code=? AND route_id=? AND sequence_id=?",
                        (city, route, expected),
                    ).rowcount
                    == 1
                )
            changed = connection.execute(
                "UPDATE topology_progress SET status='FAILED',"
                "content_sha256=NULL,sequence_id=NULL,error_code=?,error_message=?,"
                "updated_at=?,completed_at=NULL "
                "WHERE provider=? AND city_code=? AND route_id=?",
                (
                    SINGLE_POINT_ROUTE_SPIKE_ERROR_CODE,
                    message,
                    now,
                    provider_id,
                    city,
                    route,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise CatalogValidationError("topology target does not exist")
            if pointer_removed:
                revision = self._bump_revision(connection)
            else:
                revision_row = connection.execute(
                    "SELECT value FROM catalog_meta WHERE key='revision'"
                ).fetchone()
                revision = int(revision_row[0] if revision_row else 0)
            connection.commit()
        if pointer_removed:
            self._invalidate_cache()
        return {
            "pointer_removed": pointer_removed,
            "expected_sequence_id_matched": guard_matched,
            "revision": revision,
            "error_code": SINGLE_POINT_ROUTE_SPIKE_ERROR_CODE,
            "error_message": message,
        }

    def defer_or_fail_topology_target(
        self,
        *,
        provider: str,
        city_code: str,
        route_id: str,
        deferred: bool,
        error_code: str,
        error_message: str,
    ) -> None:
        provider_id = _safe_code(provider, "provider")
        city = _safe_code(city_code, "city_code")
        route = _safe_transport_identifier(route_id, "route_id")
        code = _safe_text(error_code, "error_code", required=True, maximum=64)
        message = _safe_text(error_message, "error_message", required=True, maximum=240)
        now = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute(
                "UPDATE topology_progress SET status=?,error_code=?,error_message=?,updated_at=? WHERE provider=? AND city_code=? AND route_id=?",
                ("DEFERRED" if deferred else "FAILED", code, message, now, provider_id, city, route),
            )
            connection.commit()

    def topology_coverage(self, *, provider: str) -> dict[str, Any]:
        provider_id = _safe_code(provider, "provider")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) count FROM topology_progress WHERE provider=? GROUP BY status",
                (provider_id,),
            ).fetchall()
            hydrated = connection.execute(
                "SELECT COUNT(*) FROM active_route_sequences a JOIN topology_targets t ON t.city_code=a.city_code AND t.route_id=a.route_id WHERE t.provider=?",
                (provider_id,),
            ).fetchone()[0]
            discovery = self._topology_discovery_summary(connection, provider_id)
        statuses = {row["status"]: int(row["count"]) for row in rows}
        total = sum(statuses.values())
        complete = statuses.get("COMPLETE", 0) + statuses.get("UNCHANGED", 0)
        return {
            "provider": provider_id,
            "targets": total,
            "complete": complete,
            "hydrated_active_sequences": int(hydrated),
            "coverage_ratio": (complete / total) if total else 0.0,
            "statuses": statuses,
            "discovery": discovery,
        }

    @staticmethod
    def _topology_discovery_summary(
        connection: sqlite3.Connection, provider_id: str
    ) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT scope_key,status FROM topology_discovery_progress "
            "WHERE provider=? ORDER BY scope_key",
            (provider_id,),
        ).fetchall()
        city_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM topology_discovered_cities WHERE provider=?",
                (provider_id,),
            ).fetchone()[0]
        )
        status_counts: dict[str, int] = {}
        city_scope_complete = False
        route_scope_count = 0
        complete_route_scopes = 0
        for row in rows:
            status = str(row["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
            scope = str(row["scope_key"])
            if scope == "cities":
                city_scope_complete = status == "COMPLETE"
            elif scope.startswith("routes:"):
                route_scope_count += 1
                if status == "COMPLETE":
                    complete_route_scopes += 1
        complete = (
            city_scope_complete
            and city_count > 0
            and route_scope_count == city_count
            and complete_route_scopes == city_count
        )
        return {
            "complete": complete,
            "cities_discovered": city_count,
            "route_scopes": route_scope_count,
            "complete_route_scopes": complete_route_scopes,
            "statuses": status_counts,
        }

    def active_topology_summary(self) -> dict[str, Any]:
        """Return bounded aggregate evidence for the currently active graph."""
        with self.connect() as connection:
            aggregate = connection.execute(
                """
                SELECT COUNT(*) route_sequences,
                       COALESCE(SUM(v.stop_count),0) directed_stop_rows,
                       COUNT(DISTINCT v.city_code) city_count,
                       COUNT(DISTINCT v.source) source_count
                FROM active_route_sequences a
                JOIN route_sequence_versions v ON v.sequence_id=a.sequence_id
                """
            ).fetchone()
            unique_stops = connection.execute(
                """
                SELECT COUNT(*) FROM (
                  SELECT v.city_code,s.node_id
                  FROM active_route_sequences a
                  JOIN route_sequence_versions v ON v.sequence_id=a.sequence_id
                  JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
                  GROUP BY v.city_code,s.node_id
                )
                """
            ).fetchone()[0]
        return {
            "graph_ready": int(aggregate["route_sequences"]) > 0,
            "active_route_sequences": int(aggregate["route_sequences"]),
            "directed_stop_rows": int(aggregate["directed_stop_rows"]),
            "unique_graph_stops": int(unique_stops),
            "city_count": int(aggregate["city_count"]),
            "source_count": int(aggregate["source_count"]),
            "nationwide_complete": None,
        }

    @staticmethod
    def _bump_revision(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT value FROM catalog_meta WHERE key='revision'").fetchone()
        revision = int(row[0] if row else 0) + 1
        connection.execute(
            "INSERT INTO catalog_meta(key,value) VALUES('revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(revision),),
        )
        return revision

    def _invalidate_cache(self) -> None:
        with self._cache_lock:
            self._snapshot_cache = None
            self._planning_cache = None

    @staticmethod
    def _bounded_query(query: str, limit: int) -> tuple[str, int]:
        text = _safe_text(query, "query", maximum=MAX_QUERY_CHARS)
        try:
            bounded = int(limit)
        except (TypeError, ValueError) as exc:
            raise CatalogValidationError("limit must be an integer") from exc
        if not 1 <= bounded <= MAX_SEARCH_LIMIT:
            raise CatalogLimitError(f"limit must be 1..{MAX_SEARCH_LIMIT}")
        return text, bounded

    def search_cities(self, query: str = "", *, limit: int = 20) -> list[dict[str, Any]]:
        text, bounded = self._bounded_query(query, limit)
        pattern = f"%{_like(text)}%"
        with self.connect() as connection:
            rows = connection.execute(
                """
                WITH active AS (SELECT value source_id FROM catalog_meta WHERE key='active_stops_source_id')
                SELECT city_code,city_name,COUNT(*) stop_count
                FROM catalog_stops WHERE source_id=(SELECT source_id FROM active)
                  AND (city_code LIKE ? ESCAPE '\\' OR city_name LIKE ? ESCAPE '\\')
                GROUP BY city_code,city_name ORDER BY city_name,city_code LIMIT ?
                """,
                (pattern, pattern, bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_hydrated_stops(
        self,
        query: str = "",
        *,
        city_code: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search exact stop IDs that participate in the active directed graph."""
        text, bounded = self._bounded_query(query, limit)
        pattern = f"%{_like(text)}%"
        clauses = [
            "(s.node_name LIKE ? ESCAPE '\\' OR s.node_id LIKE ? ESCAPE '\\')"
        ]
        params: list[Any] = [pattern, pattern]
        if city_code:
            clauses.append("v.city_code=?")
            params.append(_safe_code(city_code, "city_code"))
        params.append(bounded)
        with self.connect() as connection:
            rows = connection.execute(
                """
                WITH matched AS (
                    SELECT v.city_code,v.route_id,v.source,s.node_id,s.node_name,
                           s.latitude,s.longitude
                    FROM route_sequence_stops s NOT INDEXED
                    JOIN route_sequence_versions v ON v.sequence_id=s.sequence_id
                    JOIN active_route_sequences a ON a.sequence_id=s.sequence_id
                    WHERE """
                + " AND ".join(clauses)
                + """
                ), grouped AS (
                    SELECT city_code,node_id,MIN(node_name) node_name,
                           MIN(latitude) latitude,MIN(longitude) longitude,
                           COUNT(DISTINCT route_id) route_count,MIN(source) source
                    FROM matched GROUP BY city_code,node_id
                )
                SELECT g.city_code,g.node_id,g.node_name,g.latitude,g.longitude,
                       '' mobile_short_no,
                       COALESCE(
                         (SELECT cs.city_name FROM catalog_stops cs
                          WHERE cs.source_id=(SELECT value FROM catalog_meta WHERE key='active_stops_source_id')
                            AND cs.city_code=g.city_code
                          GROUP BY cs.city_name ORDER BY COUNT(*) DESC,cs.city_name LIMIT 1),
                         (SELECT tc.city_name FROM topology_discovered_cities tc
                          WHERE tc.city_code=g.city_code ORDER BY tc.provider LIMIT 1),
                         ''
                       ) city_name,
                       COALESCE(
                         (SELECT cs.managing_city_name FROM catalog_stops cs
                          WHERE cs.source_id=(SELECT value FROM catalog_meta WHERE key='active_stops_source_id')
                            AND cs.city_code=g.city_code
                          GROUP BY cs.managing_city_name
                          ORDER BY COUNT(*) DESC,cs.managing_city_name LIMIT 1),
                         ''
                       ) managing_city_name,
                       g.route_count,g.source
                FROM grouped g ORDER BY g.node_name,g.node_id LIMIT ?
                """,
                params,
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["catalog_kind"] = "HYDRATED_TOPOLOGY"
            item["graph_ready"] = True
            results.append(item)
        return results

    def search_stops(self, query: str = "", *, city_code: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        text, bounded = self._bounded_query(query, limit)
        clauses = ["cs.source_id=(SELECT value FROM catalog_meta WHERE key='active_stops_source_id')", "(cs.node_name LIKE ? ESCAPE '\\' OR cs.node_id LIKE ? ESCAPE '\\' OR cs.mobile_short_no LIKE ? ESCAPE '\\')"]
        pattern = f"%{_like(text)}%"
        params: list[Any] = [pattern, pattern, pattern]
        if city_code:
            clauses.append("cs.city_code=?")
            params.append(_safe_code(city_code, "city_code"))
        params.append(bounded)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT cs.city_code,cs.node_id,cs.node_name,cs.latitude,cs.longitude,
                       cs.mobile_short_no,cs.city_name,cs.managing_city_name,cs.source_id,
                       (
                         SELECT COUNT(DISTINCT v.route_id) FROM active_route_sequences a
                         JOIN route_sequence_versions v ON v.sequence_id=a.sequence_id
                         JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
                         WHERE v.city_code=cs.city_code AND s.node_id=cs.node_id
                       ) route_count
                FROM catalog_stops cs NOT INDEXED WHERE """
                + " AND ".join(clauses)
                + " ORDER BY cs.city_name,cs.node_name,cs.node_id LIMIT ?",
                params,
            ).fetchall()
        static_results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["source"] = item.pop("source_id")
            item["catalog_kind"] = "OFFICIAL_STATIC_CATALOG"
            item["route_count"] = int(item["route_count"])
            item["graph_ready"] = item["route_count"] > 0
            static_results.append(item)

        hydrated_results = self.search_hydrated_stops(
            text, city_code=city_code, limit=bounded
        )
        combined: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in hydrated_results + static_results:
            key = (item["city_code"], item["node_id"])
            if key in seen:
                continue
            seen.add(key)
            combined.append(item)
        combined.sort(
            key=lambda item: (
                not bool(item.get("graph_ready")),
                0 if item.get("catalog_kind") == "HYDRATED_TOPOLOGY" else 1,
                item.get("city_name") or "",
                item.get("node_name") or "",
                item.get("node_id") or "",
            )
        )
        return combined[:bounded]

    def planning_stop_reference(
        self, *, node_id: str, city_code: str | None = None
    ) -> dict[str, Any] | None:
        """Resolve one selected stop without materializing the nationwide catalog.

        The returned coordinate may be used only as an explicit walking
        access/egress point. It never aliases a route identifier or invents a
        ride edge.
        """
        node = _safe_transport_identifier(node_id, "node_id")
        city = _safe_code(city_code, "city_code") if city_code else None
        with self.connect() as connection:
            clauses = [
                "source_id=(SELECT value FROM catalog_meta WHERE key='active_stops_source_id')",
                "node_id=?",
            ]
            params: list[Any] = [node]
            if city:
                clauses.append("city_code=?")
                params.append(city)
            row = connection.execute(
                "SELECT city_code,node_id,node_name,latitude,longitude "
                "FROM catalog_stops WHERE "
                + " AND ".join(clauses)
                + " ORDER BY city_code,node_id LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                hydrated_clauses = ["s.node_id=?"]
                hydrated_params: list[Any] = [node]
                if city:
                    hydrated_clauses.append("v.city_code=?")
                    hydrated_params.append(city)
                row = connection.execute(
                    "SELECT v.city_code,s.node_id,MIN(s.node_name) node_name,"
                    "MIN(s.latitude) latitude,MIN(s.longitude) longitude "
                    "FROM active_route_sequences a "
                    "JOIN route_sequence_versions v ON v.sequence_id=a.sequence_id "
                    "JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id "
                    "WHERE "
                    + " AND ".join(hydrated_clauses)
                    + " GROUP BY v.city_code,s.node_id ORDER BY v.city_code LIMIT 1",
                    hydrated_params,
                ).fetchone()
        if row is None:
            return None
        latitude_value = row["latitude"]
        longitude_value = row["longitude"]
        # Active route topology can legitimately identify a stop before the
        # provider publishes coordinates.  Preserve that exact identity for
        # directed routing, but expose coordinates only as a complete pair so
        # callers cannot accidentally perform a half-defined nearby lookup.
        if latitude_value is None or longitude_value is None:
            latitude = None
            longitude = None
        else:
            latitude = float(latitude_value)
            longitude = float(longitude_value)
        return {
            "city_code": str(row["city_code"]),
            "node_id": str(row["node_id"]),
            "node_name": str(row["node_name"]),
            "latitude": latitude,
            "longitude": longitude,
        }

    def search_routes(self, query: str = "", *, city_code: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        text, bounded = self._bounded_query(query, limit)
        clauses = ["source_id=(SELECT value FROM catalog_meta WHERE key='active_routes_source_id')", "(route_no LIKE ? ESCAPE '\\' OR route_id LIKE ? ESCAPE '\\' OR start_stop_name LIKE ? ESCAPE '\\' OR end_stop_name LIKE ? ESCAPE '\\')"]
        pattern = f"%{_like(text)}%"
        params: list[Any] = [pattern, pattern, pattern, pattern]
        if city_code:
            clauses.append("city_code=?")
            params.append(_safe_code(city_code, "city_code"))
        params.append(bounded)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT city_code,route_id,route_no,start_node_id,end_node_id,start_stop_name,end_stop_name,municipality_name FROM catalog_routes "
                f"WHERE {' AND '.join(clauses)} ORDER BY municipality_name,route_no,route_id LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def provenance(self, *, dataset_kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        _, bounded = self._bounded_query("", limit)
        params: list[Any] = []
        where = ""
        if dataset_kind:
            if dataset_kind not in {"stops", "routes"}:
                raise CatalogValidationError("dataset_kind must be stops or routes")
            where = " WHERE dataset_kind=?"
            params.append(dataset_kind)
        params.append(bounded)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT s.source_id,s.dataset_kind,s.source_url,s.source_date,s.sha256,s.encoding,s.row_count,s.imported_at,"
                "q.input_row_count,q.imported_row_count,q.rejected_row_count,q.rejection_reasons_json "
                "FROM catalog_sources s LEFT JOIN catalog_import_quality q ON q.source_id=s.source_id"
                + where
                + " ORDER BY s.imported_at DESC,s.source_id LIMIT ?",
                params,
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["quality"] = {
                "input_row_count": item.pop("input_row_count", None),
                "imported_row_count": item.pop("imported_row_count", item.get("row_count")),
                "rejected_row_count": item.pop("rejected_row_count", 0),
                "rejection_reasons": json.loads(item.pop("rejection_reasons_json") or "{}"),
                "coordinates_corrected": 0,
            }
            results.append(item)
        return results

    def snapshot(self) -> CatalogSnapshot:
        with self.connect() as connection:
            revision_row = connection.execute("SELECT value FROM catalog_meta WHERE key='revision'").fetchone()
            revision = int(revision_row[0] if revision_row else 0)
        with self._cache_lock:
            if self._snapshot_cache is not None and self._snapshot_cache.revision == revision:
                return self._snapshot_cache
            with self.connect() as connection:
                stop_rows = connection.execute(
                    "SELECT * FROM catalog_stops WHERE source_id=(SELECT value FROM catalog_meta WHERE key='active_stops_source_id') ORDER BY city_code,node_id"
                ).fetchall()
                route_rows = connection.execute(
                    "SELECT * FROM catalog_routes WHERE source_id=(SELECT value FROM catalog_meta WHERE key='active_routes_source_id') ORDER BY city_code,route_id"
                ).fetchall()
                sequence_rows = connection.execute(
                    """
                    SELECT v.* FROM route_sequence_versions v
                    JOIN active_route_sequences a ON a.sequence_id=v.sequence_id
                    ORDER BY v.city_code,v.route_id
                    """
                ).fetchall()
                sequences: list[RouteSequence] = []
                sequence_ids: list[str] = []
                for row in sequence_rows:
                    stop_sequence = connection.execute(
                        "SELECT * FROM route_sequence_stops WHERE sequence_id=? ORDER BY node_order",
                        (row["sequence_id"],),
                    ).fetchall()
                    records = tuple(
                        RouteStopRecord(
                            city_code=row["city_code"],
                            route_id=row["route_id"],
                            node_id=item["node_id"],
                            node_order=int(item["node_order"]),
                            node_name=item["node_name"],
                            latitude=item["latitude"],
                            longitude=item["longitude"],
                            direction=item["direction"],
                            can_board=bool(item["can_board"]),
                            can_alight=bool(item["can_alight"]),
                        )
                        for item in stop_sequence
                    )
                    sequences.append(RouteSequence(row["city_code"], row["route_id"], row["source"], row["captured_at"], row["sha256"], records))
                    sequence_ids.append(row["sequence_id"])
            stops = tuple(
                StopRecord(
                    city_code=row["city_code"],
                    node_id=row["node_id"],
                    node_name=row["node_name"],
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                    collected_date=row["collected_date"],
                    mobile_short_no=row["mobile_short_no"],
                    city_name=row["city_name"],
                    managing_city_name=row["managing_city_name"],
                    source_id=row["source_id"],
                )
                for row in stop_rows
            )
            routes = tuple(
                RouteRecord(
                    city_code=row["city_code"],
                    route_id=row["route_id"],
                    route_no=row["route_no"],
                    start_node_id=row["start_node_id"],
                    end_node_id=row["end_node_id"],
                    start_stop_name=row["start_stop_name"],
                    end_stop_name=row["end_stop_name"],
                    municipality_name=row["municipality_name"],
                    source_id=row["source_id"],
                )
                for row in route_rows
            )
            active_sources = sorted({item.source_id for item in stops} | {item.source_id for item in routes})
            version = hashlib.sha256(_canonical([active_sources, sequence_ids]).encode("utf-8")).hexdigest()
            snapshot = CatalogSnapshot(version, revision, stops, routes, tuple(sequences))
            self._snapshot_cache = snapshot
            return snapshot

    def planning_route_context(
        self, route_keys: Iterable[tuple[str, str]] = ()
    ) -> CatalogSnapshot:
        """Return planner identity and requested route labels without topology rows.

        The on-demand SQLite journey planner owns route-stop traversal.  API
        presentation still needs a catalog-wide revision and public route
        numbers, but loading every active sequence for those two values would
        recreate the nationwide in-memory graph this path is designed to
        avoid.
        """
        normalized = sorted(
            {
                (
                    _safe_code(city_code, "city_code"),
                    _safe_transport_identifier(route_id, "route_id"),
                )
                for city_code, route_id in route_keys
            }
        )
        if len(normalized) > 400:
            raise CatalogLimitError("planning route context exceeds 400 routes")
        with self.connect() as connection:
            revision_row = connection.execute(
                "SELECT value FROM catalog_meta WHERE key='revision'"
            ).fetchone()
            revision = int(revision_row[0] if revision_row else 0)
            rows: list[sqlite3.Row] = []
            if normalized:
                where = " OR ".join(
                    "(a.city_code=? AND a.route_id=?)" for _item in normalized
                )
                parameters = [value for item in normalized for value in item]
                rows = connection.execute(
                    "SELECT a.city_code,a.route_id,"
                    "COALESCE(t.route_no,a.route_id) public_route_no,v.source "
                    "FROM active_route_sequences a "
                    "JOIN route_sequence_versions v ON v.sequence_id=a.sequence_id "
                    "LEFT JOIN topology_targets t ON t.provider='TAGO' "
                    "AND t.city_code=a.city_code AND t.route_id=a.route_id "
                    f"WHERE {where} ORDER BY a.city_code,a.route_id",
                    parameters,
                ).fetchall()
        routes = tuple(
            RouteRecord(
                city_code=str(row["city_code"]),
                route_id=str(row["route_id"]),
                route_no=_public_route_label(
                    row["source"], row["public_route_no"]
                ),
                start_node_id="",
                end_node_id="",
                start_stop_name="",
                end_stop_name="",
                municipality_name="",
                source_id=str(row["source"]),
            )
            for row in rows
        )
        version = hashlib.sha256(
            _canonical(["catalog_revision", revision]).encode("utf-8")
        ).hexdigest()
        return CatalogSnapshot(version, revision, (), routes, ())

    def planning_snapshot(self) -> CatalogSnapshot:
        """Load only hydrated topology for bounded journey requests.

        Nationwide stop search stays in indexed SQLite queries; the planner
        does not need to materialize hundreds of thousands of catalog rows.
        """
        with self.connect() as connection:
            revision_row = connection.execute(
                "SELECT value FROM catalog_meta WHERE key='revision'"
            ).fetchone()
            revision = int(revision_row[0] if revision_row else 0)
        with self._cache_lock:
            if self._planning_cache is not None and self._planning_cache.revision == revision:
                return self._planning_cache
            with self.connect() as connection:
                catalog_route_count_row = connection.execute(
                    "SELECT COUNT(*) FROM catalog_routes WHERE source_id=(SELECT value FROM catalog_meta WHERE key='active_routes_source_id')"
                ).fetchone()
                catalog_route_count = int(
                    catalog_route_count_row[0] if catalog_route_count_row else 0
                )
                topology_rows = connection.execute(
                    "SELECT status,COUNT(*) count FROM topology_progress "
                    "WHERE provider='TAGO' GROUP BY status"
                ).fetchall()
                topology_statuses = {
                    str(row["status"]): int(row["count"]) for row in topology_rows
                }
                topology_target_count = sum(topology_statuses.values())
                topology_complete_count = (
                    topology_statuses.get("COMPLETE", 0)
                    + topology_statuses.get("UNCHANGED", 0)
                )
                topology_hydrated_row = connection.execute(
                    "SELECT COUNT(*) FROM active_route_sequences a "
                    "JOIN topology_targets t ON t.provider='TAGO' "
                    "AND t.city_code=a.city_code AND t.route_id=a.route_id"
                ).fetchone()
                topology_hydrated_count = int(
                    topology_hydrated_row[0] if topology_hydrated_row else 0
                )
                topology_discovery = self._topology_discovery_summary(
                    connection, "TAGO"
                )
                sequence_rows = connection.execute(
                    """
                    SELECT v.*,COALESCE(t.route_no,v.route_id) AS public_route_no
                    FROM route_sequence_versions v
                    JOIN active_route_sequences a ON a.sequence_id=v.sequence_id
                    LEFT JOIN topology_targets t
                      ON t.provider='TAGO'
                     AND t.city_code=v.city_code
                     AND t.route_id=v.route_id
                    ORDER BY v.city_code,v.route_id
                    """
                ).fetchall()
                sequences: list[RouteSequence] = []
                routes: list[RouteRecord] = []
                sequence_ids: list[str] = []
                for row in sequence_rows:
                    stop_rows = connection.execute(
                        "SELECT * FROM route_sequence_stops WHERE sequence_id=? ORDER BY node_order",
                        (row["sequence_id"],),
                    ).fetchall()
                    stops = tuple(
                        RouteStopRecord(
                            city_code=row["city_code"],
                            route_id=row["route_id"],
                            node_id=item["node_id"],
                            node_order=int(item["node_order"]),
                            node_name=item["node_name"],
                            latitude=item["latitude"],
                            longitude=item["longitude"],
                            direction=item["direction"],
                            can_board=bool(item["can_board"]),
                            can_alight=bool(item["can_alight"]),
                        )
                        for item in stop_rows
                    )
                    sequences.append(
                        RouteSequence(
                            row["city_code"], row["route_id"], row["source"],
                            row["captured_at"], row["sha256"], stops,
                        )
                    )
                    routes.append(
                        RouteRecord(
                            city_code=row["city_code"],
                            route_id=row["route_id"],
                            route_no=_public_route_label(
                                row["source"], row["public_route_no"]
                            ),
                            start_node_id=stops[0].node_id if stops else "",
                            end_node_id=stops[-1].node_id if stops else "",
                            start_stop_name=stops[0].node_name if stops else "",
                            end_stop_name=stops[-1].node_name if stops else "",
                            municipality_name="",
                            source_id=row["source"],
                        )
                    )
                    sequence_ids.append(row["sequence_id"])
            version = hashlib.sha256(_canonical(sequence_ids).encode("utf-8")).hexdigest()
            snapshot = CatalogSnapshot(
                version,
                revision,
                (),
                tuple(routes),
                tuple(sequences),
                catalog_route_count=catalog_route_count,
                topology_target_count=topology_target_count,
                topology_complete_count=topology_complete_count,
                topology_discovery_complete=bool(topology_discovery["complete"]),
                topology_hydrated_count=topology_hydrated_count,
            )
            self._planning_cache = snapshot
            return snapshot


__all__ = [
    "CatalogError",
    "CatalogLimitError",
    "CatalogSnapshot",
    "CatalogValidationError",
    "NetworkCatalog",
    "RouteRecord",
    "RouteSequence",
    "RouteStopRecord",
    "StopRecord",
]
