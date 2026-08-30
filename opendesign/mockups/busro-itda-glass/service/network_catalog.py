"""Bounded, provenance-preserving nationwide bus catalog storage.

CSV catalogs and authoritative route-stop sequences are deliberately separate.
Imported IDs are never joined by resemblance, name, or geographic proximity.
Only ``hydrate_route_sequence`` can create ordered route topology.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlparse


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
_CODE = re.compile(r"^[0-9A-Za-z_.:-]{1,96}$")
_TRANSPORT_IDENTIFIER = re.compile(r"^[0-9A-Za-z가-힣_.:-]{1,96}$")


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
                    PRIMARY KEY(sequence_id,node_order),
                    UNIQUE(sequence_id,node_id,node_order)
                );
                CREATE TABLE IF NOT EXISTS active_route_sequences (
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    sequence_id TEXT NOT NULL REFERENCES route_sequence_versions(sequence_id),
                    PRIMARY KEY(city_code,route_id)
                );
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
            ]
        )
        return result["sequences"][0]

    def hydrate_route_sequences_batch(
        self,
        sequences: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Validate and activate multiple authoritative routes atomically.

        Municipal file imports use this path so a bad or conflicting route can
        never leave only the first part of a file active.  All input is fully
        normalized before SQLite is opened for writes, and the catalog revision
        changes at most once for the whole batch.
        """
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
            canonical_stops = [asdict(stop) for stop in stops]
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
                        "INSERT INTO route_sequence_stops VALUES(?,?,?,?,?,?,?)",
                        [
                            (
                                sequence_id,
                                stop.node_order,
                                stop.node_id,
                                stop.node_name,
                                stop.latitude,
                                stop.longitude,
                                stop.direction,
                            )
                            for stop in stops
                        ],
                    )
                active = connection.execute(
                    "SELECT sequence_id FROM active_route_sequences WHERE city_code=? AND route_id=?",
                    (sequence["city_code"], sequence["route_id"]),
                ).fetchone()
                activated = active is None or active["sequence_id"] != sequence_id
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
            }
            for sequence in normalized
        ]
        return {
            "route_count": len(results),
            "created": sum(1 for result in results if result["created"]),
            "activated": sum(1 for result in results if result["activated"]),
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
            _canonical([asdict(item) for item in stops]).encode("utf-8")
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
                "AND (p.last_run_id IS NULL OR p.last_run_id<>?) "
                "ORDER BY CASE p.status WHEN 'IN_PROGRESS' THEN 0 WHEN 'DEFERRED' THEN 1 WHEN 'FAILED' THEN 2 ELSE 3 END,"
                "COALESCE(c.completed_count,0),p.city_code,p.route_id LIMIT 1",
                (provider_id, provider_id, run),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE topology_progress SET status='IN_PROGRESS',attempts=attempts+1,last_run_id=?,error_code=NULL,error_message=NULL,updated_at=? WHERE provider=? AND city_code=? AND route_id=?",
                    (run, now, provider_id, row["city_code"], row["route_id"]),
                )
            connection.commit()
        return dict(row) if row else None

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
                    FROM active_route_sequences a
                    JOIN route_sequence_versions v ON v.sequence_id=a.sequence_id
                    JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
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
                FROM catalog_stops cs WHERE """
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
                        )
                        for item in stop_rows
                    )
                    sequences.append(
                        RouteSequence(
                            row["city_code"], row["route_id"], row["source"],
                            row["captured_at"], row["sha256"], stops,
                        )
                    )
                    sequence_ids.append(row["sequence_id"])
            version = hashlib.sha256(_canonical(sequence_ids).encode("utf-8")).hexdigest()
            snapshot = CatalogSnapshot(version, revision, (), (), tuple(sequences))
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
