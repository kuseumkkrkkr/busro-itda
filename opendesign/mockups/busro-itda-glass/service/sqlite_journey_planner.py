"""On-demand SQLite route-level Dijkstra for nationwide directed topology.

This prototype deliberately does *not* construct ``GraphNode`` objects for
every active route stop.  A search state is one active, directional route at a
current authoritative ``node_order``.  Expanding a state scans only the
remaining stops of that route, then discovers boardable routes at an exact
``(city_code, node_id)`` match or within the requested geodesic radius.

The trade-off is intentional: repeated indexed SQLite lookups replace a large
process-wide graph allocation.  Per-request LRUs bound retained route and
transfer data.  Query, row, route-length, and expansion limits fail loudly;
they never truncate candidates and return a potentially wrong route.

This remains a structural planner.  It does not model service calendars,
departure times, minimum transfer time, fares, or live vehicle positions.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from contextlib import closing
import copy
from dataclasses import dataclass, field
import heapq
import itertools
import math
from pathlib import Path
import sqlite3
import threading
import time
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from journey_planner import PlannerError, PlannerLimitError, PlannerValidationError


EARTH_RADIUS_M = 6_371_000.0
DEFAULT_TRANSFER_RADIUS_M = 300
ENDPOINT_ACCESS_RADIUS_M = 300
MIN_TRANSFER_RADIUS_M = 50
MAX_TRANSFER_RADIUS_M = 800
DEFAULT_MAX_QUERIES = 50_000
DEFAULT_MAX_EXPANSIONS = 100_000
DEFAULT_MAX_ROWS_PER_LOOKUP = 50_000
DEFAULT_MAX_STOPS_PER_ROUTE = 20_000
DEFAULT_ROUTE_CACHE_ENTRIES = 128
DEFAULT_TRANSFER_CACHE_ENTRIES = 4_096
DEFAULT_MAX_PARALLEL_SEARCHES = 8
DEFAULT_ADMISSION_TIMEOUT_SECONDS = 0.25
DEFAULT_RESULT_CACHE_ENTRIES = 128
# A verified primary route remains useful even when the bounded search did not
# finish enumerating every alternative. Catalog revision is part of the key,
# so a five-minute hot-route TTL stays fresh while making popular POST queries
# genuinely immediate across normal user return intervals.
DEFAULT_SHORT_RESULT_TTL_SECONDS = 300.0
# Nationwide multi-transfer searches can legitimately cross the former five
# second boundary on the live catalog. Keep the request bounded, but do not
# turn a valid route into a false DATA_GAP response merely because it needs a
# few more indexed SQLite scans.
DEFAULT_SEARCH_WALL_SECONDS = 15.0
# Once a valid nearby same-name endpoint route is known, spend only a small
# slice of the remaining request budget trying to prove an exact-endpoint
# route. This keeps exact endpoints preferred without making a usable
# counterpart route wait for the full nationwide wall-clock budget.
DEFAULT_ENDPOINT_EXACT_GRACE_SECONDS = 2.0
DEFAULT_ADDITIONAL_ALTERNATIVE_EXPANSIONS = 256
DEFAULT_MAX_TRANSFER_LAYERS = 32
PRIMARY_FAST_PATH_EXPANSIONS = 100
PRIMARY_FAST_PATH_QUERIES = 100
PRIMARY_FAST_PATH_SECONDS = 0.25
MAX_ALTERNATIVES = 5
MAX_LABELS_PER_STATE = MAX_ALTERNATIVES
TERMINAL_POOL_MULTIPLIER = 2
SQL_PROGRESS_HANDLER_STEPS = 1_000
MAX_SEGMENT_STOPS = 160


class PlannerBusyError(PlannerError):
    """Raised when bounded SQLite search capacity is temporarily full."""


@dataclass(slots=True)
class _ResultFlight:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: BaseException | None = None
    waiters: int = 0


@dataclass(frozen=True, slots=True)
class _CachedResult:
    result: dict[str, Any]
    short_ttl_expires_at: float | None = None


def required_index_ddl() -> tuple[str, ...]:
    """Return (but never execute) indexes required by the current schema.

    ``route_sequence_stops`` already has a primary key on
    ``(sequence_id, node_order)``, which supports ascending route scans.  The
    statements below cover reverse lookup from a physical stop, the
    active-sequence join, and latitude-first bounding-box scans.  Core SQLite
    has no automatic geodesic index; the coordinate index narrows latitude and
    longitude before Python applies the exact haversine predicate.

    ``NetworkCatalog`` applies these statements when it initializes a writable
    catalog copy. Constructing or running this read-only planner never changes
    the database.
    """

    return (
        "CREATE INDEX IF NOT EXISTS idx_active_route_sequences_sequence "
        "ON active_route_sequences(sequence_id, city_code, route_id)",
        "CREATE INDEX IF NOT EXISTS idx_route_sequence_stops_node_lookup "
        "ON route_sequence_stops(node_id, sequence_id, node_order)",
        "CREATE INDEX IF NOT EXISTS idx_route_sequence_stops_coordinate_lookup "
        "ON route_sequence_stops(latitude, longitude, sequence_id, node_order) "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL",
    )


@dataclass(frozen=True, slots=True, order=True)
class RouteState:
    """One directional active route at its current authoritative order."""

    city_code: str
    route_id: str
    sequence_id: str
    node_order: int


@dataclass(frozen=True, slots=True)
class RouteStop:
    state: RouteState
    node_id: str
    node_name: str
    latitude: float | None
    longitude: float | None
    direction: str
    can_board: bool
    can_alight: bool


@dataclass(frozen=True, slots=True)
class LocatedStop:
    stop: RouteStop
    access_distance_m: float
    snapped: bool
    access_evidence: str | None = None


@dataclass(frozen=True, slots=True)
class RouteStopList:
    stops: tuple[RouteStop, ...]
    orders: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Transition:
    """Ride forward on ``source`` and optionally transfer to ``target``."""

    source: RouteStop
    ride_to: RouteStop
    target: RouteStop
    transfer_distance_m: float
    transfer_evidence: str | None
    destination_access_distance_m: float = 0.0
    destination_snapped: bool = False
    destination_access_evidence: str | None = None


@dataclass(frozen=True, slots=True)
class _SearchLabel:
    label_id: int
    state: RouteState
    stop: RouteStop
    metrics: tuple[int, int, float]
    priority: tuple[float, float, float]
    route_signature: tuple[tuple[str, str], ...]
    root: LocatedStop
    previous_id: int | None = None
    transition: Transition | None = None


@dataclass(frozen=True, slots=True)
class _TerminalPath:
    label_id: int
    transition: Transition
    metrics: tuple[int, int, float]
    priority: tuple[float, float, float]
    route_signature: tuple[tuple[str, str], ...]
    endpoint_fallbacks: int = 0


@dataclass(frozen=True, slots=True)
class _LayerSearchOutcome:
    labels: dict[int, _SearchLabel]
    terminals: tuple[_TerminalPath, ...]
    layer_sizes: tuple[int, ...]
    limit_reason: str | None
    elapsed_seconds: float
    endpoint_exact_search_complete: bool = True


@dataclass(slots=True)
class _SearchContext:
    connection: sqlite3.Connection
    max_queries: int
    max_expansions: int
    max_rows_per_lookup: int
    max_stops_per_route: int
    route_cache_entries: int
    transfer_cache_entries: int
    max_parallel_searches: int
    query_count: int = 0
    expansion_count: int = 0
    discovered_state_count: int = 0
    ride_candidates_scanned: int = 0
    transfer_targets_discovered: int = 0
    route_cache: OrderedDict[str, RouteStopList] = field(default_factory=OrderedDict)
    transfer_cache: OrderedDict[tuple[Any, ...], tuple[RouteStop, ...]] = field(
        default_factory=OrderedDict
    )
    route_transfer_cache: OrderedDict[
        tuple[str, int],
        dict[RouteState, tuple[tuple[RouteStop, float, str], ...]],
    ] = field(default_factory=OrderedDict)
    sql_deadline_monotonic: float | None = None

    def arm_sql_deadline(self, deadline_monotonic: float) -> None:
        """Interrupt SQLite VM work once the request wall budget expires."""

        self.sql_deadline_monotonic = deadline_monotonic
        self.connection.set_progress_handler(
            self._sql_deadline_reached,
            SQL_PROGRESS_HANDLER_STEPS,
        )

    def disarm_sql_deadline(self) -> None:
        """Allow bounded result serialization after the graph-search wall."""

        self.sql_deadline_monotonic = None
        self.connection.set_progress_handler(None, 0)

    def _sql_deadline_reached(self) -> int:
        deadline = self.sql_deadline_monotonic
        return int(deadline is not None and time.monotonic() >= deadline)

    def translate_operational_error(self, exc: sqlite3.OperationalError) -> None:
        deadline = self.sql_deadline_monotonic
        if (
            deadline is not None
            and time.monotonic() >= deadline
            and "interrupt" in str(exc).lower()
        ):
            raise PlannerLimitError(
                "SQLite journey search exceeds wall-clock SQL deadline"
            ) from exc
        raise exc

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        if self.query_count >= self.max_queries:
            raise PlannerLimitError(
                f"SQLite journey search exceeds {self.max_queries} queries"
        )
        self.query_count += 1
        try:
            return self.connection.execute(sql, tuple(parameters))
        except sqlite3.OperationalError as exc:
            self.translate_operational_error(exc)
            raise AssertionError("unreachable")

    def expand(self) -> None:
        if self.expansion_count >= self.max_expansions:
            raise PlannerLimitError(
                f"SQLite journey search exceeds {self.max_expansions} route-state expansions"
            )
        self.expansion_count += 1


def _positive_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PlannerValidationError(f"{field_name} must be an integer") from exc
    if parsed < 1:
        raise PlannerValidationError(f"{field_name} must be positive")
    return parsed


def _identifier(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 96:
        raise PlannerValidationError(f"{field_name} is invalid")
    if any(character.isspace() or ord(character) < 32 for character in text):
        raise PlannerValidationError(f"{field_name} is invalid")
    return text


def _coordinate(latitude: Any, longitude: Any, field_name: str) -> tuple[float, float]:
    try:
        parsed = (float(latitude), float(longitude))
    except (TypeError, ValueError) as exc:
        raise PlannerValidationError(f"{field_name} coordinates are invalid") from exc
    if (
        not all(math.isfinite(value) for value in parsed)
        or not -90 <= parsed[0] <= 90
        or not -180 <= parsed[1] <= 180
    ):
        raise PlannerValidationError(f"{field_name} coordinates are invalid")
    return parsed


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return EARTH_RADIUS_M * 2 * math.asin(min(1.0, math.sqrt(value)))


def _normalized_stop_name(value: Any) -> str:
    """Return a deliberately conservative stop-name comparison key.

    Counterpart access is an endpoint-only escape hatch, not a fuzzy stop
    merge. Unicode compatibility variants and whitespace may differ between
    catalog sources, but punctuation and directional qualifiers remain
    significant so nearby, unrelated stops are not silently conflated.
    """

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if not character.isspace())


def _stop_from_row(row: sqlite3.Row) -> RouteStop:
    return RouteStop(
        state=RouteState(
            city_code=str(row["city_code"]),
            route_id=str(row["route_id"]),
            sequence_id=str(row["sequence_id"]),
            node_order=int(row["node_order"]),
        ),
        node_id=str(row["node_id"]),
        node_name=str(row["node_name"]),
        latitude=(None if row["latitude"] is None else float(row["latitude"])),
        longitude=(None if row["longitude"] is None else float(row["longitude"])),
        direction=str(row["direction"]),
        can_board=bool(row["can_board"]),
        can_alight=bool(row["can_alight"]),
    )


class SQLiteJourneyPlanner:
    """Find directed structural routes without a nationwide in-memory graph.

    Route/transfer caches are intentionally per request. A small process LRU
    retains only final JSON-compatible results for hot endpoint pairs; its key
    includes the catalog revision and callers always receive deep copies.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        max_queries: int = DEFAULT_MAX_QUERIES,
        max_expansions: int = DEFAULT_MAX_EXPANSIONS,
        max_rows_per_lookup: int = DEFAULT_MAX_ROWS_PER_LOOKUP,
        max_stops_per_route: int = DEFAULT_MAX_STOPS_PER_ROUTE,
        route_cache_entries: int = DEFAULT_ROUTE_CACHE_ENTRIES,
        transfer_cache_entries: int = DEFAULT_TRANSFER_CACHE_ENTRIES,
        max_parallel_searches: int = DEFAULT_MAX_PARALLEL_SEARCHES,
        admission_timeout_seconds: float = DEFAULT_ADMISSION_TIMEOUT_SECONDS,
        result_cache_entries: int = DEFAULT_RESULT_CACHE_ENTRIES,
        search_wall_seconds: float = DEFAULT_SEARCH_WALL_SECONDS,
        additional_alternative_expansions: int = (
            DEFAULT_ADDITIONAL_ALTERNATIVE_EXPANSIONS
        ),
        max_transfer_layers: int = DEFAULT_MAX_TRANSFER_LAYERS,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        self.max_queries = _positive_int(max_queries, "max_queries")
        self.max_expansions = _positive_int(max_expansions, "max_expansions")
        self.max_rows_per_lookup = _positive_int(
            max_rows_per_lookup, "max_rows_per_lookup"
        )
        self.max_stops_per_route = _positive_int(
            max_stops_per_route, "max_stops_per_route"
        )
        self.route_cache_entries = _positive_int(
            route_cache_entries, "route_cache_entries"
        )
        self.transfer_cache_entries = _positive_int(
            transfer_cache_entries, "transfer_cache_entries"
        )
        self.max_parallel_searches = min(
            _positive_int(max_parallel_searches, "max_parallel_searches"),
            DEFAULT_MAX_PARALLEL_SEARCHES,
        )
        self.result_cache_entries = _positive_int(
            result_cache_entries, "result_cache_entries"
        )
        if self.result_cache_entries > 256:
            raise PlannerValidationError("result_cache_entries must be at most 256")
        self.additional_alternative_expansions = _positive_int(
            additional_alternative_expansions,
            "additional_alternative_expansions",
        )
        self.max_transfer_layers = _positive_int(
            max_transfer_layers, "max_transfer_layers"
        )
        if self.max_transfer_layers > 50:
            raise PlannerValidationError("max_transfer_layers must be at most 50")
        self._search_slots = threading.BoundedSemaphore(self.max_parallel_searches)
        try:
            timeout = float(admission_timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise PlannerValidationError(
                "admission_timeout_seconds must be numeric"
            ) from exc
        if not math.isfinite(timeout) or not 0.01 <= timeout <= 2.0:
            raise PlannerValidationError(
                "admission_timeout_seconds must be 0.01..2.0"
            )
        self.admission_timeout_seconds = timeout
        try:
            wall_seconds = float(search_wall_seconds)
        except (TypeError, ValueError) as exc:
            raise PlannerValidationError(
                "search_wall_seconds must be numeric"
            ) from exc
        if not math.isfinite(wall_seconds) or not 0.1 <= wall_seconds <= 30.0:
            raise PlannerValidationError(
                "search_wall_seconds must be 0.1..30.0"
            )
        self.search_wall_seconds = wall_seconds
        self.endpoint_exact_grace_seconds = min(
            DEFAULT_ENDPOINT_EXACT_GRACE_SECONDS,
            self.search_wall_seconds,
        )
        self._result_cache: OrderedDict[tuple[Any, ...], _CachedResult] = (
            OrderedDict()
        )
        self._result_cache_identity: tuple[int, tuple[Any, ...]] | None = None
        self._result_cache_lock = threading.RLock()
        self._result_cache_hits = 0
        self._result_cache_misses = 0
        self.short_result_ttl_seconds = DEFAULT_SHORT_RESULT_TTL_SECONDS
        self._monotonic_clock = time.monotonic
        self._result_flights: dict[
            tuple[tuple[int, tuple[Any, ...]], tuple[Any, ...]], _ResultFlight
        ] = {}
        self._result_flights_lock = threading.Lock()
        self.max_result_flights = min(
            256, max(self.result_cache_entries, self.max_parallel_searches)
        )
        self._singleflight_wait_seconds = min(
            32.0,
            self.search_wall_seconds + self.admission_timeout_seconds + 1.0,
        )

    def plan(
        self,
        *,
        origin_node_id: str,
        destination_node_id: str,
        origin_city_code: str | None = None,
        destination_city_code: str | None = None,
        transfer_radius_m: int = DEFAULT_TRANSFER_RADIUS_M,
        alternatives: int = 1,
        preference: str = "diverse",
        origin_access: Mapping[str, Any] | None = None,
        destination_access: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        origin = _identifier(origin_node_id, "origin_node_id")
        destination = _identifier(destination_node_id, "destination_node_id")
        origin_city = (
            _identifier(origin_city_code, "origin_city_code")
            if origin_city_code is not None
            else None
        )
        destination_city = (
            _identifier(destination_city_code, "destination_city_code")
            if destination_city_code is not None
            else None
        )
        if origin == destination and origin_city == destination_city:
            raise PlannerValidationError("origin and destination must differ")
        radius = _positive_int(transfer_radius_m, "transfer_radius_m")
        if not MIN_TRANSFER_RADIUS_M <= radius <= MAX_TRANSFER_RADIUS_M:
            raise PlannerValidationError(
                "transfer_radius_m must be "
                f"{MIN_TRANSFER_RADIUS_M}..{MAX_TRANSFER_RADIUS_M}"
            )
        try:
            requested_alternatives = int(alternatives)
        except (TypeError, ValueError) as exc:
            raise PlannerValidationError("alternatives must be an integer") from exc
        if not 1 <= requested_alternatives <= MAX_ALTERNATIVES:
            raise PlannerLimitError(
                f"alternatives must be 1..{MAX_ALTERNATIVES}"
            )
        if preference not in {"diverse", "low_transfer", "reliable", "challenge"}:
            raise PlannerValidationError("preference is not supported")
        normalized_origin_access = self._normalize_access(
            origin_access,
            expected_node_id=origin,
            expected_city_code=origin_city,
            field_name="origin_access",
        )
        normalized_destination_access = self._normalize_access(
            destination_access,
            expected_node_id=destination,
            expected_city_code=destination_city,
            field_name="destination_access",
        )
        cache_key = self._result_cache_key(
            origin=origin,
            destination=destination,
            origin_city=origin_city,
            destination_city=destination_city,
            transfer_radius_m=radius,
            alternatives=requested_alternatives,
            preference=preference,
            origin_access=normalized_origin_access,
            destination_access=normalized_destination_access,
        )

        # Hot endpoint pairs bypass the expensive-search semaphore. This
        # prevents eight unrelated cold searches from turning an already
        # cached request into a transient 503. A miss is rechecked after
        # admission so a concurrent search can fill the cache once.
        catalog_identity = self._read_catalog_identity()
        cached = self._cached_result(
            identity=catalog_identity,
            key=cache_key,
            lookup_queries=1,
        )
        if cached is not None:
            return cached

        flight_key = (catalog_identity, cache_key)
        flight, leader = self._claim_result_flight(flight_key)
        if not leader:
            return self._wait_result_flight(flight)
        try:
            result = self._compute_cache_miss(
                cache_key=cache_key,
                origin=origin,
                destination=destination,
                origin_city=origin_city,
                destination_city=destination_city,
                radius=radius,
                requested_alternatives=requested_alternatives,
                preference=preference,
                normalized_origin_access=normalized_origin_access,
                normalized_destination_access=normalized_destination_access,
            )
            self._finish_result_flight(flight_key, flight, result=result)
            return result
        except BaseException as exc:
            self._finish_result_flight(flight_key, flight, error=exc)
            raise

    def _compute_cache_miss(
        self,
        *,
        cache_key: tuple[Any, ...],
        origin: str,
        destination: str,
        origin_city: str | None,
        destination_city: str | None,
        radius: int,
        requested_alternatives: int,
        preference: str,
        normalized_origin_access: Mapping[str, Any] | None,
        normalized_destination_access: Mapping[str, Any] | None,
    ) -> dict[str, Any]:

        # ``sqlite3.Connection`` commits/rolls back but does not close itself
        # when used directly as a context manager.  Explicit closing also
        # prevents read handles from pinning a WAL file on Windows.
        admitted = self._search_slots.acquire(
            timeout=self.admission_timeout_seconds
        )
        if not admitted:
            raise PlannerBusyError("SQLite journey search capacity is busy")
        try:
            with closing(self._connect()) as connection:
                context = _SearchContext(
                    connection=connection,
                    max_queries=self.max_queries,
                    max_expansions=self.max_expansions,
                    max_rows_per_lookup=self.max_rows_per_lookup,
                    max_stops_per_route=self.max_stops_per_route,
                    route_cache_entries=self.route_cache_entries,
                    transfer_cache_entries=self.transfer_cache_entries,
                    max_parallel_searches=self.max_parallel_searches,
                )
                catalog_identity = self._catalog_identity(context)
                cached = self._cached_result(
                    identity=catalog_identity,
                    key=cache_key,
                    lookup_queries=context.query_count,
                    record_miss=False,
                )
                if cached is not None:
                    return cached
                starts = self._endpoint_states(
                    context,
                    node_id=origin,
                    city_code=origin_city,
                    access=normalized_origin_access,
                    require="board",
                )
                destinations = self._endpoint_states(
                    context,
                    node_id=destination,
                    city_code=destination_city,
                    access=normalized_destination_access,
                    require="alight",
                )
                if not starts or not destinations:
                    reason = (
                        "STOP_NOT_ROUTABLE_NEARBY"
                        if (
                            (
                                not starts
                                and self._has_access_coordinates(
                                    normalized_origin_access
                                )
                            )
                            or (
                                not destinations
                                and self._has_access_coordinates(
                                    normalized_destination_access
                                )
                            )
                        )
                        else "STOP_NOT_IN_ACTIVE_SEQUENCE"
                    )
                    result = self._gap(context, reason, radius)
                else:
                    result = self._search(
                        context,
                        starts=starts,
                        destinations=destinations,
                        transfer_radius_m=radius,
                        alternatives=requested_alternatives,
                        preference=preference,
                        origin_access=normalized_origin_access,
                        destination_access=normalized_destination_access,
                    )
                return self._store_result(
                    identity=catalog_identity,
                    key=cache_key,
                    result=result,
                )
        finally:
            self._search_slots.release()

    def _claim_result_flight(
        self,
        key: tuple[tuple[int, tuple[Any, ...]], tuple[Any, ...]],
    ) -> tuple[_ResultFlight, bool]:
        with self._result_flights_lock:
            existing = self._result_flights.get(key)
            if existing is not None:
                existing.waiters += 1
                return existing, False
            if len(self._result_flights) >= self.max_result_flights:
                raise PlannerBusyError("SQLite journey result coordination is busy")
            flight = _ResultFlight()
            self._result_flights[key] = flight
            return flight, True

    def _wait_result_flight(self, flight: _ResultFlight) -> dict[str, Any]:
        if not flight.event.wait(timeout=self._singleflight_wait_seconds):
            raise PlannerBusyError("SQLite journey result is still being computed")
        if flight.error is not None:
            raise flight.error
        if flight.result is None:
            raise PlannerError("SQLite journey result coordination failed")
        return copy.deepcopy(flight.result)

    def _finish_result_flight(
        self,
        key: tuple[tuple[int, tuple[Any, ...]], tuple[Any, ...]],
        flight: _ResultFlight,
        *,
        result: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        published = copy.deepcopy(dict(result)) if result is not None else None
        with self._result_flights_lock:
            flight.result = published
            flight.error = error
            current = self._result_flights.get(key)
            if current is flight:
                self._result_flights.pop(key, None)
            flight.event.set()

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise PlannerValidationError("network catalog database does not exist")
        uri = f"{self.database_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        # One short read transaction gives the route search a consistent
        # catalog revision while ingestion atomically switches active rows.
        connection.execute("BEGIN")
        return connection

    def _catalog_identity(
        self, context: _SearchContext
    ) -> tuple[int, tuple[Any, ...]]:
        row = context.execute(
            "SELECT value FROM catalog_meta WHERE key='revision'"
        ).fetchone()
        try:
            revision = int(row[0] if row is not None else 0)
        except (TypeError, ValueError) as exc:
            raise PlannerValidationError("catalog revision is invalid") from exc
        # File identity distinguishes a replaced catalog at the same path.
        # Modification times and WAL size are deliberately excluded: ingest
        # can write audit/progress rows without changing active topology. The
        # monotonic catalog revision is the topology invalidation authority.
        stat = self.database_path.stat()
        return revision, (stat.st_dev, stat.st_ino)

    def _read_catalog_identity(self) -> tuple[int, tuple[Any, ...]]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM catalog_meta WHERE key='revision'"
            ).fetchone()
            try:
                revision = int(row[0] if row is not None else 0)
            except (TypeError, ValueError) as exc:
                raise PlannerValidationError("catalog revision is invalid") from exc
        stat = self.database_path.stat()
        return revision, (stat.st_dev, stat.st_ino)

    @staticmethod
    def _access_cache_key(value: Mapping[str, Any] | None) -> tuple[Any, ...] | None:
        if value is None:
            return None
        latitude = value["latitude"]
        longitude = value["longitude"]
        return (
            value["city_code"],
            value["node_id"],
            value["node_name"],
            None if latitude is None else float(latitude),
            None if longitude is None else float(longitude),
        )

    @staticmethod
    def _has_access_coordinates(value: Mapping[str, Any] | None) -> bool:
        return bool(
            value is not None
            and value.get("latitude") is not None
            and value.get("longitude") is not None
        )

    def _result_cache_key(
        self,
        *,
        origin: str,
        destination: str,
        origin_city: str | None,
        destination_city: str | None,
        transfer_radius_m: int,
        alternatives: int,
        preference: str,
        origin_access: Mapping[str, Any] | None,
        destination_access: Mapping[str, Any] | None,
    ) -> tuple[Any, ...]:
        return (
            origin,
            destination,
            origin_city,
            destination_city,
            transfer_radius_m,
            alternatives,
            preference,
            self._access_cache_key(origin_access),
            self._access_cache_key(destination_access),
        )

    def _cached_result(
        self,
        *,
        identity: tuple[int, tuple[Any, ...]],
        key: tuple[Any, ...],
        lookup_queries: int,
        record_miss: bool = True,
    ) -> dict[str, Any] | None:
        with self._result_cache_lock:
            if self._result_cache_identity != identity:
                self._result_cache.clear()
                self._result_cache_identity = identity
            entry = self._result_cache.get(key)
            if (
                entry is not None
                and entry.short_ttl_expires_at is not None
                and self._monotonic_clock() >= entry.short_ttl_expires_at
            ):
                self._result_cache.pop(key, None)
                entry = None
            if entry is None:
                if record_miss:
                    self._result_cache_misses += 1
                return None
            self._result_cache.move_to_end(key)
            self._result_cache_hits += 1
            result = copy.deepcopy(entry.result)
            graph = result.setdefault("graph", {})
            graph["result_cache"] = {
                "status": (
                    "hit_short_ttl"
                    if entry.short_ttl_expires_at is not None
                    else "hit"
                ),
                "entries": len(self._result_cache),
                "max_entries": self.result_cache_entries,
                "catalog_revision": identity[0],
                "lookup_queries": lookup_queries,
                "hits": self._result_cache_hits,
                "misses": self._result_cache_misses,
            }
            return result

    def _store_result(
        self,
        *,
        identity: tuple[int, tuple[Any, ...]],
        key: tuple[Any, ...],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        stored = copy.deepcopy(dict(result))
        with self._result_cache_lock:
            # A newer request may already have observed a later revision.
            # Never reintroduce a result from the older read transaction.
            if self._result_cache_identity != identity:
                returned = copy.deepcopy(stored)
                returned.setdefault("graph", {})["result_cache"] = {
                    "status": "miss_not_stored_revision_changed",
                    "entries": len(self._result_cache),
                    "max_entries": self.result_cache_entries,
                    "catalog_revision": identity[0],
                    "hits": self._result_cache_hits,
                    "misses": self._result_cache_misses,
                }
                return returned
            cache_policy = self._result_cache_policy(stored)
            if cache_policy == "none":
                stored.setdefault("graph", {})["result_cache"] = {
                    "status": "not_stored_non_deterministic",
                    "entries": len(self._result_cache),
                    "max_entries": self.result_cache_entries,
                    "catalog_revision": identity[0],
                    "hits": self._result_cache_hits,
                    "misses": self._result_cache_misses,
                }
                return copy.deepcopy(stored)
            short_ttl = cache_policy == "short_ttl"
            stored.setdefault("graph", {})["result_cache"] = {
                "status": "miss_short_ttl" if short_ttl else "miss",
                "entries": min(len(self._result_cache) + 1, self.result_cache_entries),
                "max_entries": self.result_cache_entries,
                "catalog_revision": identity[0],
                "hits": self._result_cache_hits,
                "misses": self._result_cache_misses,
            }
            expires_at = (
                self._monotonic_clock() + self.short_result_ttl_seconds
                if short_ttl
                else None
            )
            entry = _CachedResult(copy.deepcopy(stored), expires_at)
            self._result_cache[key] = entry
            self._result_cache.move_to_end(key)
            while len(self._result_cache) > self.result_cache_entries:
                self._result_cache.popitem(last=False)
            stored["graph"]["result_cache"]["entries"] = len(self._result_cache)
            self._result_cache[key] = _CachedResult(
                copy.deepcopy(stored), expires_at
            )
            return copy.deepcopy(stored)

    @staticmethod
    def _result_cache_policy(result: Mapping[str, Any]) -> str:
        reason = result.get("reason")
        if reason == "SEARCH_BUDGET_REACHED":
            return "none"
        graph = result.get("graph")
        if not isinstance(graph, Mapping):
            return "none"
        alternatives = result.get("alternatives")
        if not isinstance(alternatives, (list, tuple)):
            return "none"
        if len(alternatives) == 0:
            # Only cache negative answers whose completeness follows from the
            # current catalog revision. Search-budget negatives must remain
            # retryable because a later request can legitimately find a path.
            limit_reason = graph.get("limit_reason")
            if limit_reason not in (None, ""):
                return "none"
            if reason in {
                "STOP_NOT_IN_ACTIVE_SEQUENCE",
                "STOP_NOT_ROUTABLE_NEARBY",
            }:
                return "short_ttl"
            if (
                reason == "NO_DIRECTED_PATH_IN_SQLITE_GRAPH"
                and graph.get("alternative_search_complete") is True
            ):
                return "short_ttl"
            return "none"
        # A positive counterpart route stopped by the intentional exact-search
        # grace is safe to reuse briefly for the same catalog revision and
        # endpoint/access key. Wall/query/layer failures remain retryable and
        # are never admitted through this exception.
        if graph.get("endpoint_exact_search_complete") is False:
            if (
                result.get("status") == "READY"
                and reason is None
                and graph.get("limit_reason")
                == "ENDPOINT_EXACT_GRACE_BUDGET"
            ):
                return "short_ttl"
            return "none"
        limit_reason = graph.get("limit_reason")
        if limit_reason == "WALL_CLOCK_BUDGET":
            return "none"
        if (
            limit_reason not in (None, "")
            or graph.get("alternative_search_complete") is False
        ):
            return "short_ttl"
        return "long"

    @staticmethod
    def _normalize_access(
        value: Mapping[str, Any] | None,
        *,
        expected_node_id: str,
        expected_city_code: str | None,
        field_name: str,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise PlannerValidationError(f"{field_name} must be an object")
        node_id = _identifier(value.get("node_id"), f"{field_name}.node_id")
        city_code = _identifier(value.get("city_code"), f"{field_name}.city_code")
        if node_id != expected_node_id:
            raise PlannerValidationError(f"{field_name} node does not match request")
        if expected_city_code is not None and city_code != expected_city_code:
            raise PlannerValidationError(f"{field_name} city does not match request")
        latitude_value = value.get("latitude")
        longitude_value = value.get("longitude")
        if latitude_value is None and longitude_value is None:
            latitude = None
            longitude = None
        elif latitude_value is None or longitude_value is None:
            raise PlannerValidationError(
                f"{field_name} coordinates must be supplied together"
            )
        else:
            latitude, longitude = _coordinate(
                latitude_value, longitude_value, field_name
            )
        return {
            "city_code": city_code,
            "node_id": node_id,
            "node_name": str(value.get("node_name") or node_id)[:160],
            "latitude": latitude,
            "longitude": longitude,
        }

    def _endpoint_states(
        self,
        context: _SearchContext,
        *,
        node_id: str,
        city_code: str | None,
        access: Mapping[str, Any] | None,
        require: str,
    ) -> tuple[LocatedStop, ...]:
        exact = self._exact_states(
            context,
            node_id=node_id,
            city_code=city_code,
            require=require,
        )
        # Keep exact graph states authoritative. When the selected stop also
        # carries catalog coordinates, add only a tightly constrained
        # opposite-side counterpart: same normalized name, same city, active
        # sequence, and at most 300 m away. This lets one bounded multi-source
        # search cross a platform/road when the selected node has no directed
        # path, without treating every nearby route as the endpoint.
        if exact:
            located: dict[RouteState, LocatedStop] = {
                stop.state: LocatedStop(stop, 0.0, False) for stop in exact
            }
            if self._has_access_coordinates(access):
                coordinate = (
                    float(access["latitude"]),
                    float(access["longitude"]),
                )
                counterpart_city = city_code or str(access["city_code"])
                endpoint_names = {
                    _normalized_stop_name(access.get("node_name")),
                    *(_normalized_stop_name(stop.node_name) for stop in exact),
                }
                endpoint_names.discard("")
                if counterpart_city and endpoint_names:
                    for stop, distance in self._nearby_states(
                        context,
                        coordinate=coordinate,
                        radius_m=ENDPOINT_ACCESS_RADIUS_M,
                        require=require,
                    ):
                        if (
                            stop.node_id == node_id
                            or stop.state.city_code != counterpart_city
                            or _normalized_stop_name(stop.node_name)
                            not in endpoint_names
                        ):
                            continue
                        candidate = LocatedStop(
                            stop,
                            distance,
                            True,
                            "same_name_nearby_stop_access",
                        )
                        current = located.get(stop.state)
                        if (
                            current is None
                            or self._located_sort_key(candidate)
                            < self._located_sort_key(current)
                        ):
                            located[stop.state] = candidate
            return tuple(sorted(located.values(), key=self._located_sort_key))
        if not self._has_access_coordinates(access):
            return ()
        coordinate = (float(access["latitude"]), float(access["longitude"]))
        nearby = self._nearby_states(
            context,
            coordinate=coordinate,
            radius_m=ENDPOINT_ACCESS_RADIUS_M,
            require=require,
        )
        return tuple(
            sorted(
                (
                    LocatedStop(
                        stop,
                        distance,
                        True,
                        "catalog_coordinate_access",
                    )
                    for stop, distance in nearby
                ),
                key=self._located_sort_key,
            )
        )

    def _search(
        self,
        context: _SearchContext,
        **kwargs: Any,
    ) -> dict[str, Any]:
        alternatives = int(kwargs["alternatives"])
        preference = str(kwargs["preference"])
        baseline_expansions = context.expansion_count
        baseline_queries = context.query_count
        if preference in {"low_transfer", "diverse", "reliable"}:
            primary = self._search_low_transfer_primary(
                context,
                starts=kwargs["starts"],
                destinations=kwargs["destinations"],
                transfer_radius_m=int(kwargs["transfer_radius_m"]),
            )
            if not primary.terminals:
                return self._gap(
                    context,
                    (
                        "SEARCH_BUDGET_REACHED"
                        if primary.limit_reason is not None
                        else "NO_DIRECTED_PATH_IN_SQLITE_GRAPH"
                    ),
                    int(kwargs["transfer_radius_m"]),
                    search={
                        "alternative_algorithm": "transfer_layer_primary",
                        "preference": preference,
                        "alternatives_requested": alternatives,
                        "alternatives_returned": 0,
                        "alternatives_truncated": alternatives > 0,
                        "alternative_search_complete": (
                            primary.limit_reason is None
                        ),
                        "limit_reason": primary.limit_reason,
                        "wall_clock_budget_seconds": self.search_wall_seconds,
                        "elapsed_seconds": round(primary.elapsed_seconds, 6),
                        "transfer_layer_sizes": list(primary.layer_sizes),
                        "max_transfer_layers": self.max_transfer_layers,
                        "endpoint_exact_search_complete": (
                            primary.endpoint_exact_search_complete
                        ),
                    },
                )
            primary_expansions = context.expansion_count - baseline_expansions
            primary_queries = context.query_count - baseline_queries
            use_fast_path = (
                (preference == "low_transfer" and alternatives == 1)
                or (
                    alternatives > 1
                    and (
                        primary_expansions >= PRIMARY_FAST_PATH_EXPANSIONS
                        or primary_queries >= PRIMARY_FAST_PATH_QUERIES
                        or primary.elapsed_seconds >= PRIMARY_FAST_PATH_SECONDS
                    )
                )
            )
            if use_fast_path:
                selected = self._select_terminal_paths(
                    primary.terminals,
                    preference=preference,
                    alternatives=alternatives,
                )
                truncated = len(selected) < alternatives
                return self._ready_paths(
                    context,
                    labels=primary.labels,
                    terminals=selected,
                    preference=preference,
                    alternatives_requested=alternatives,
                    alternative_search_complete=(
                        primary.limit_reason is None and not truncated
                    ),
                    limit_reason=(
                        primary.limit_reason
                        or ("PRIMARY_ROUTE_FAST_PATH" if truncated else None)
                    ),
                    elapsed_seconds=primary.elapsed_seconds,
                    state_label_limit=1,
                    transfer_radius_m=int(kwargs["transfer_radius_m"]),
                    origin_access=kwargs.get("origin_access"),
                    destination_access=kwargs.get("destination_access"),
                    alternative_algorithm="transfer_layer_primary",
                    transfer_layer_sizes=primary.layer_sizes,
                    endpoint_exact_search_complete=(
                        primary.endpoint_exact_search_complete
                    ),
                )
        return self._search_alternatives(context, **kwargs)

    def _search_alternatives(
        self,
        context: _SearchContext,
        *,
        starts: Sequence[LocatedStop],
        destinations: Sequence[LocatedStop],
        transfer_radius_m: int,
        alternatives: int,
        preference: str,
        origin_access: Mapping[str, Any] | None,
        destination_access: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        # Alternative search retains a bounded number of histories at a
        # converging route state. The minimum-transfer primary is computed
        # separately with one label per RouteState, so this K-label search may
        # truncate variety but cannot erase the first valid nationwide route.
        state_label_limit = MAX_LABELS_PER_STATE
        terminal_pool_limit = max(
            alternatives,
            min(MAX_ALTERNATIVES * TERMINAL_POOL_MULTIPLIER, alternatives * 2),
        )
        search_started = time.monotonic()
        deadline = search_started + self.search_wall_seconds
        context.arm_sql_deadline(deadline)
        limit_reason: str | None = None
        state_labels_pruned = False
        primary_terminal_expansion: int | None = None
        labels: dict[int, _SearchLabel] = {}
        best: dict[
            RouteState,
            dict[
                tuple[tuple[str, str], ...],
                tuple[tuple[float, float, float], tuple[int, int, float], int],
            ],
        ] = {}
        queue: list[tuple[float, float, float, int, int]] = []
        serial = itertools.count()
        label_ids = itertools.count()
        discovered_states: set[RouteState] = set()

        def admit(
            *,
            stop: RouteStop,
            metrics: tuple[int, int, float],
            route_signature: tuple[tuple[str, str], ...],
            root: LocatedStop,
            previous_id: int | None = None,
            transition: Transition | None = None,
        ) -> None:
            nonlocal state_labels_pruned
            priority = self._search_priority(metrics, preference)
            state_labels = best.setdefault(stop.state, {})
            existing = state_labels.get(route_signature)
            candidate_key = (priority, metrics)
            if existing is not None:
                existing_root_fallback = labels[existing[2]].root.snapped
                if not existing_root_fallback and root.snapped:
                    return
                if (
                    existing_root_fallback == root.snapped
                    and (existing[0], existing[1]) <= candidate_key
                ):
                    return
            if existing is None and len(state_labels) >= state_label_limit:
                worst_signature, worst = max(
                    state_labels.items(),
                    key=lambda item: (
                        int(labels[item[1][2]].root.snapped),
                        item[1][0],
                        item[1][1],
                        item[0],
                    ),
                )
                state_labels_pruned = True
                if (
                    int(labels[worst[2]].root.snapped),
                    worst[0],
                    worst[1],
                    worst_signature,
                ) <= (
                    int(root.snapped),
                    priority,
                    metrics,
                    route_signature,
                ):
                    return
                del state_labels[worst_signature]
            if len(labels) >= max(16, context.max_expansions):
                raise PlannerLimitError(
                    "SQLite journey search exceeds explicit "
                    f"{max(16, context.max_expansions)}-label limit"
                )
            label_id = next(label_ids)
            label = _SearchLabel(
                label_id=label_id,
                state=stop.state,
                stop=stop,
                metrics=metrics,
                priority=priority,
                route_signature=route_signature,
                root=root,
                previous_id=previous_id,
                transition=transition,
            )
            labels[label_id] = label
            state_labels[route_signature] = (priority, metrics, label_id)
            discovered_states.add(stop.state)
            context.discovered_state_count = len(discovered_states)
            heapq.heappush(queue, (*priority, next(serial), label_id))

        for located in sorted(starts, key=self._located_sort_key):
            route_key = (
                located.stop.state.city_code,
                located.stop.state.route_id,
            )
            admit(
                stop=located.stop,
                metrics=(0, 0, round(located.access_distance_m, 6)),
                route_signature=(route_key,),
                root=located,
            )

        destinations_by_sequence: dict[str, list[LocatedStop]] = {}
        for located in destinations:
            destinations_by_sequence.setdefault(
                located.stop.state.sequence_id, []
            ).append(located)
        for values in destinations_by_sequence.values():
            values.sort(key=self._located_sort_key)

        requires_exact_terminal = (
            any(not start.snapped for start in starts)
            and any(not destination.snapped for destination in destinations)
        )
        fallback_grace_deadline: float | None = None

        def time_limit_reason() -> str | None:
            now = time.monotonic()
            if (
                fallback_grace_deadline is not None
                and now >= fallback_grace_deadline
            ):
                return "ENDPOINT_EXACT_GRACE_BUDGET"
            if now >= deadline:
                return "WALL_CLOCK_BUDGET"
            return None

        terminals: dict[tuple[tuple[str, str], ...], _TerminalPath] = {}
        while queue:
            active_limit_reason = time_limit_reason()
            if active_limit_reason is not None:
                limit_reason = active_limit_reason
                break
            if (
                alternatives > 1
                and primary_terminal_expansion is not None
                and context.expansion_count - primary_terminal_expansion
                >= self.additional_alternative_expansions
            ):
                limit_reason = "ADDITIONAL_ALTERNATIVE_EXPANSION_BUDGET"
                break
            priority_0, priority_1, priority_2, _serial, label_id = heapq.heappop(
                queue
            )
            label = labels[label_id]
            current = best.get(label.state, {}).get(label.route_signature)
            if current is None or current[2] != label_id:
                continue
            priority = (priority_0, priority_1, priority_2)
            ranked_terminals = tuple(
                terminal
                for terminal in terminals.values()
                if (
                    not requires_exact_terminal
                    or terminal.endpoint_fallbacks == 0
                )
            )
            if len(ranked_terminals) >= terminal_pool_limit:
                worst_terminal_priority = max(
                    terminal.priority for terminal in ranked_terminals
                )
                if priority > worst_terminal_priority:
                    break
            context.expand()
            source = label.stop
            try:
                route_stops = self._route_stops(context, source)
            except PlannerLimitError:
                active_limit_reason = time_limit_reason()
                if (
                    active_limit_reason == "ENDPOINT_EXACT_GRACE_BUDGET"
                    and terminals
                ):
                    limit_reason = active_limit_reason
                    break
                raise
            reachable_stops = self._reachable_ride_stops(route_stops, label.state)
            context.ride_candidates_scanned += len(reachable_stops)
            reachable_orders = {
                stop.state.node_order for stop in reachable_stops
            }
            try:
                batched_transfers = self._batched_transfer_targets(
                    context,
                    route_stops=route_stops,
                    sources=reachable_stops,
                    transfer_radius_m=transfer_radius_m,
                )
            except PlannerLimitError:
                active_limit_reason = time_limit_reason()
                if (
                    active_limit_reason == "ENDPOINT_EXACT_GRACE_BUDGET"
                    and terminals
                ):
                    limit_reason = active_limit_reason
                    break
                raise

            # A destination is reachable on this direction only when its
            # authoritative order is later and no known direction boundary is
            # crossed.
            for destination in destinations_by_sequence.get(
                label.state.sequence_id, ()
            ):
                if destination.stop.state.node_order not in reachable_orders:
                    continue
                goal_metrics = (
                    label.metrics[0],
                    label.metrics[1]
                    + self._ride_row_delta(
                        route_stops,
                        label.state,
                        destination.stop.state,
                    ),
                    round(
                        label.metrics[2] + destination.access_distance_m, 6
                    ),
                )
                transition = Transition(
                    source=source,
                    ride_to=destination.stop,
                    target=destination.stop,
                    transfer_distance_m=0.0,
                    transfer_evidence=None,
                    destination_access_distance_m=destination.access_distance_m,
                    destination_snapped=destination.snapped,
                    destination_access_evidence=(
                        destination.access_evidence
                    ),
                )
                candidate = _TerminalPath(
                    label_id=label_id,
                    transition=transition,
                    metrics=goal_metrics,
                    priority=self._search_priority(goal_metrics, preference),
                    route_signature=label.route_signature,
                    endpoint_fallbacks=(
                        int(label.root.snapped) + int(destination.snapped)
                    ),
                )
                existing = terminals.get(label.route_signature)
                if existing is None or self._terminal_sort_key(candidate) < self._terminal_sort_key(existing):
                    terminals[label.route_signature] = candidate
                    if requires_exact_terminal:
                        if candidate.endpoint_fallbacks == 0:
                            fallback_grace_deadline = None
                            context.arm_sql_deadline(deadline)
                        elif fallback_grace_deadline is None:
                            grace_deadline = (
                                time.monotonic()
                                + self.endpoint_exact_grace_seconds
                            )
                            if grace_deadline < deadline:
                                fallback_grace_deadline = grace_deadline
                                context.arm_sql_deadline(
                                    fallback_grace_deadline
                                )
                    if (
                        primary_terminal_expansion is None
                        and (
                            not requires_exact_terminal
                            or candidate.endpoint_fallbacks == 0
                        )
                    ):
                        primary_terminal_expansion = context.expansion_count

            outgoing_candidates: dict[
                RouteState,
                tuple[
                    tuple[int, int, float],
                    tuple[tuple[str, str], ...],
                    RouteStop,
                    Transition,
                ],
            ] = {}
            for alight in reachable_stops:
                if not alight.can_alight:
                    continue
                ridden_orders = self._ride_row_delta(
                    route_stops,
                    label.state,
                    alight.state,
                )
                transfer_targets = batched_transfers.get(alight.state, ())
                context.transfer_targets_discovered += len(transfer_targets)
                for target, distance, evidence in transfer_targets:
                    target_route = (
                        target.state.city_code,
                        target.state.route_id,
                    )
                    candidate_metrics = (
                        label.metrics[0] + 1,
                        label.metrics[1] + ridden_orders,
                        round(label.metrics[2] + distance, 6),
                    )
                    candidate_signature = label.route_signature + (
                        target_route,
                    )
                    transition = Transition(
                        source=source,
                        ride_to=alight,
                        target=target,
                        transfer_distance_m=distance,
                        transfer_evidence=evidence,
                    )
                    existing = outgoing_candidates.get(target.state)
                    candidate_key = (
                        self._search_priority(candidate_metrics, preference),
                        candidate_metrics,
                        self._transition_sort_key(transition),
                    )
                    if existing is None:
                        outgoing_candidates[target.state] = (
                            candidate_metrics,
                            candidate_signature,
                            target,
                            transition,
                        )
                    else:
                        existing_key = (
                            self._search_priority(existing[0], preference),
                            existing[0],
                            self._transition_sort_key(existing[3]),
                        )
                        if candidate_key < existing_key:
                            outgoing_candidates[target.state] = (
                                candidate_metrics,
                                candidate_signature,
                                target,
                                transition,
                            )
            for metrics, signature, target, transition in sorted(
                outgoing_candidates.values(),
                key=lambda item: (
                    self._search_priority(item[0], preference),
                    item[2].state,
                    self._transition_sort_key(item[3]),
                ),
            ):
                admit(
                    stop=target,
                    metrics=metrics,
                    route_signature=signature,
                    root=label.root,
                    previous_id=label_id,
                    transition=transition,
                )

        if state_labels_pruned and limit_reason is None:
            limit_reason = "STATE_LABEL_BUDGET"
        if not terminals:
            return self._gap(
                context,
                (
                    "SEARCH_BUDGET_REACHED"
                    if limit_reason is not None
                    else "NO_DIRECTED_PATH_IN_SQLITE_GRAPH"
                ),
                transfer_radius_m,
                search={
                    "alternatives_requested": alternatives,
                    "alternatives_returned": 0,
                    "alternative_search_complete": limit_reason is None,
                    "limit_reason": limit_reason,
                    "wall_clock_budget_seconds": self.search_wall_seconds,
                    "elapsed_seconds": round(time.monotonic() - search_started, 6),
                    "max_labels_per_route_state": state_label_limit,
                    "additional_alternative_expansions": (
                        self.additional_alternative_expansions
                    ),
                },
            )
        exact_terminals = tuple(
            terminal
            for terminal in terminals.values()
            if terminal.endpoint_fallbacks == 0
        )
        eligible_terminals = (
            exact_terminals
            if requires_exact_terminal and exact_terminals
            else tuple(terminals.values())
        )
        endpoint_exact_search_complete = (
            not requires_exact_terminal
            or bool(exact_terminals)
            or limit_reason is None
        )
        selected = self._select_terminal_paths(
            eligible_terminals,
            preference=preference,
            alternatives=alternatives,
        )
        return self._ready_paths(
            context,
            labels=labels,
            terminals=selected,
            preference=preference,
            alternatives_requested=alternatives,
            alternative_search_complete=(
                limit_reason is None and len(selected) >= alternatives
            ),
            limit_reason=limit_reason,
            elapsed_seconds=time.monotonic() - search_started,
            state_label_limit=state_label_limit,
            transfer_radius_m=transfer_radius_m,
            origin_access=origin_access,
            destination_access=destination_access,
            endpoint_exact_search_complete=endpoint_exact_search_complete,
        )

    def _search_low_transfer_primary(
        self,
        context: _SearchContext,
        *,
        starts: Sequence[LocatedStop],
        destinations: Sequence[LocatedStop],
        transfer_radius_m: int,
    ) -> _LayerSearchOutcome:
        """Find the minimum-transfer layer before expanding the next layer.

        The prior generalized-cost Dijkstra expanded most two-transfer states
        before proving the 991 -> B1 -> 607 result. A complete transfer-layer
        search proves minimum transfers as soon as an authoritative exact
        endpoint is present. A same-name nearby counterpart is retained as a
        bounded fallback while exact search continues for a short grace.
        """

        started = time.monotonic()
        deadline = started + self.search_wall_seconds
        context.arm_sql_deadline(deadline)
        label_ids = itertools.count()
        labels: dict[int, _SearchLabel] = {}
        discovered_states: set[RouteState] = set()
        layer: dict[RouteState, _SearchLabel] = {}
        for located in sorted(starts, key=self._located_sort_key):
            route_signature = ((
                located.stop.state.city_code,
                located.stop.state.route_id,
            ),)
            metrics = (0, 0, round(located.access_distance_m, 6))
            label = _SearchLabel(
                label_id=next(label_ids),
                state=located.stop.state,
                stop=located.stop,
                metrics=metrics,
                priority=self._search_priority(metrics, "low_transfer"),
                route_signature=route_signature,
                root=located,
            )
            current = layer.get(label.state)
            if current is None or self._layer_label_key(label) < self._layer_label_key(
                current
            ):
                layer[label.state] = label
        for label in layer.values():
            labels[label.label_id] = label
            discovered_states.add(label.state)

        destinations_by_sequence: dict[str, list[LocatedStop]] = {}
        for destination in destinations:
            destinations_by_sequence.setdefault(
                destination.stop.state.sequence_id, []
            ).append(destination)
        for values in destinations_by_sequence.values():
            values.sort(key=self._located_sort_key)

        requires_exact_terminal = (
            any(not start.snapped for start in starts)
            and any(not destination.snapped for destination in destinations)
        )
        fallback_terminals: dict[
            tuple[tuple[str, str], ...], _TerminalPath
        ] = {}
        fallback_grace_deadline: float | None = None

        def time_limit_reason() -> str | None:
            now = time.monotonic()
            if (
                fallback_grace_deadline is not None
                and now >= fallback_grace_deadline
            ):
                return "ENDPOINT_EXACT_GRACE_BUDGET"
            if now >= deadline:
                return "WALL_CLOCK_BUDGET"
            return None

        def outcome(
            *,
            terminals: Iterable[_TerminalPath] = (),
            limit_reason: str | None,
            exact_complete: bool,
        ) -> _LayerSearchOutcome:
            return _LayerSearchOutcome(
                labels,
                tuple(sorted(terminals, key=self._terminal_sort_key)),
                tuple(layer_sizes),
                limit_reason,
                time.monotonic() - started,
                exact_complete,
            )

        segment_by_state: dict[RouteState, int] = {}
        position_by_state: dict[RouteState, int] = {}
        layer_sizes: list[int] = []
        for transfer_count in range(self.max_transfer_layers + 1):
            active_limit_reason = time_limit_reason()
            if active_limit_reason is not None:
                return outcome(
                    terminals=fallback_terminals.values(),
                    limit_reason=active_limit_reason,
                    exact_complete=not requires_exact_terminal,
                )
            if not layer:
                return outcome(
                    terminals=fallback_terminals.values(),
                    limit_reason=None,
                    exact_complete=True,
                )
            if len(layer) > context.max_expansions:
                raise PlannerLimitError(
                    "SQLite journey transfer layer exceeds explicit "
                    f"{context.max_expansions}-state limit"
                )
            layer_sizes.append(len(layer))

            reachable_by_label: dict[
                int, tuple[RouteStopList, tuple[RouteStop, ...]]
            ] = {}
            terminals: dict[
                tuple[tuple[str, str], ...], _TerminalPath
            ] = {}
            for label in sorted(layer.values(), key=self._layer_label_key):
                active_limit_reason = time_limit_reason()
                if active_limit_reason is not None:
                    return outcome(
                        terminals=fallback_terminals.values(),
                        limit_reason=active_limit_reason,
                        exact_complete=not requires_exact_terminal,
                    )
                try:
                    route = self._route_stops(context, label.stop)
                except PlannerLimitError:
                    active_limit_reason = time_limit_reason()
                    if (
                        active_limit_reason == "ENDPOINT_EXACT_GRACE_BUDGET"
                        and fallback_terminals
                    ):
                        return outcome(
                            terminals=fallback_terminals.values(),
                            limit_reason=active_limit_reason,
                            exact_complete=False,
                        )
                    raise
                self._record_direction_segments(segment_by_state, route)
                self._record_route_positions(position_by_state, route)
                reachable = self._reachable_ride_stops(route, label.stop.state)
                reachable_by_label[label.label_id] = (route, reachable)
                context.ride_candidates_scanned += len(reachable)
                reachable_states = {stop.state for stop in reachable}
                for destination in destinations_by_sequence.get(
                    label.stop.state.sequence_id, ()
                ):
                    if destination.stop.state not in reachable_states:
                        continue
                    goal_metrics = (
                        transfer_count,
                        label.metrics[1]
                        + self._ride_row_delta(
                            route,
                            label.stop.state,
                            destination.stop.state,
                        ),
                        round(
                            label.metrics[2] + destination.access_distance_m,
                            6,
                        ),
                    )
                    transition = Transition(
                        source=label.stop,
                        ride_to=destination.stop,
                        target=destination.stop,
                        transfer_distance_m=0.0,
                        transfer_evidence=None,
                        destination_access_distance_m=(
                            destination.access_distance_m
                        ),
                        destination_snapped=destination.snapped,
                        destination_access_evidence=(
                            destination.access_evidence
                        ),
                    )
                    terminal = _TerminalPath(
                        label_id=label.label_id,
                        transition=transition,
                        metrics=goal_metrics,
                        priority=self._search_priority(
                            goal_metrics, "low_transfer"
                        ),
                        route_signature=label.route_signature,
                        endpoint_fallbacks=(
                            int(label.root.snapped)
                            + int(destination.snapped)
                        ),
                    )
                    existing = terminals.get(label.route_signature)
                    if (
                        existing is None
                        or self._terminal_sort_key(terminal)
                        < self._terminal_sort_key(existing)
                    ):
                        terminals[label.route_signature] = terminal
            if terminals:
                exact_terminals = tuple(
                    terminal
                    for terminal in terminals.values()
                    if terminal.endpoint_fallbacks == 0
                )
                if exact_terminals or not requires_exact_terminal:
                    return outcome(
                        terminals=(exact_terminals or tuple(terminals.values())),
                        limit_reason=None,
                        exact_complete=True,
                    )
                for signature, terminal in terminals.items():
                    current = fallback_terminals.get(signature)
                    if (
                        current is None
                        or self._terminal_sort_key(terminal)
                        < self._terminal_sort_key(current)
                    ):
                        fallback_terminals[signature] = terminal
                if fallback_grace_deadline is None:
                    grace_deadline = (
                        time.monotonic() + self.endpoint_exact_grace_seconds
                    )
                    if grace_deadline < deadline:
                        fallback_grace_deadline = grace_deadline
                        context.arm_sql_deadline(fallback_grace_deadline)
            if transfer_count >= self.max_transfer_layers:
                return outcome(
                    terminals=fallback_terminals.values(),
                    limit_reason="MAX_TRANSFER_LAYERS",
                    exact_complete=not requires_exact_terminal,
                )

            exact_candidates: dict[RouteState, _SearchLabel] = {}
            for label in sorted(layer.values(), key=self._layer_label_key):
                active_limit_reason = time_limit_reason()
                if active_limit_reason is not None:
                    return outcome(
                        terminals=fallback_terminals.values(),
                        limit_reason=active_limit_reason,
                        exact_complete=not requires_exact_terminal,
                    )
                context.expand()
                route, reachable = reachable_by_label[label.label_id]
                try:
                    transfers = self._batched_transfer_targets(
                        context,
                        route_stops=route,
                        sources=reachable,
                        transfer_radius_m=transfer_radius_m,
                    )
                except PlannerLimitError:
                    active_limit_reason = time_limit_reason()
                    if (
                        active_limit_reason == "ENDPOINT_EXACT_GRACE_BUDGET"
                        and fallback_terminals
                    ):
                        return outcome(
                            terminals=fallback_terminals.values(),
                            limit_reason=active_limit_reason,
                            exact_complete=False,
                        )
                    raise
                for alight in reachable:
                    if not alight.can_alight:
                        continue
                    targets = transfers.get(alight.state, ())
                    context.transfer_targets_discovered += len(targets)
                    ridden_orders = label.metrics[1] + self._ride_row_delta(
                        route,
                        label.stop.state,
                        alight.state,
                    )
                    for target, distance, evidence in targets:
                        route_key = (
                            target.state.city_code,
                            target.state.route_id,
                        )
                        metrics = (
                            transfer_count + 1,
                            ridden_orders,
                            round(label.metrics[2] + distance, 6),
                        )
                        transition = Transition(
                            source=label.stop,
                            ride_to=alight,
                            target=target,
                            transfer_distance_m=distance,
                            transfer_evidence=evidence,
                        )
                        candidate = _SearchLabel(
                            label_id=next(label_ids),
                            state=target.state,
                            stop=target,
                            metrics=metrics,
                            priority=self._search_priority(
                                metrics, "low_transfer"
                            ),
                            route_signature=label.route_signature + (route_key,),
                            root=label.root,
                            previous_id=label.label_id,
                            transition=transition,
                        )
                        existing = exact_candidates.get(target.state)
                        if (
                            existing is None
                            or self._layer_label_key(candidate)
                            < self._layer_label_key(existing)
                        ):
                            if existing is None and len(exact_candidates) >= context.max_expansions:
                                raise PlannerLimitError(
                                    "SQLite journey transfer layer exceeds explicit "
                                    f"{context.max_expansions}-state limit"
                                )
                            exact_candidates[target.state] = candidate

            loaded_sequences: set[str] = set()
            for candidate in exact_candidates.values():
                sequence_id = candidate.state.sequence_id
                if sequence_id in loaded_sequences:
                    continue
                loaded_sequences.add(sequence_id)
                target_route = self._route_stops(context, candidate.stop)
                self._record_direction_segments(segment_by_state, target_route)
                self._record_route_positions(position_by_state, target_route)

            grouped: dict[tuple[str, int], list[_SearchLabel]] = {}
            for candidate in exact_candidates.values():
                group = (
                    candidate.state.sequence_id,
                    segment_by_state[candidate.state],
                )
                grouped.setdefault(group, []).append(candidate)
            retained: dict[RouteState, _SearchLabel] = {}
            for values in grouped.values():
                frontier: list[_SearchLabel] = []
                for candidate in sorted(
                    values,
                    key=lambda item: self._layer_frontier_key(
                        item, position_by_state
                    ),
                ):
                    if any(
                        self._layer_dominates(
                            current,
                            candidate,
                            position_by_state,
                        )
                        for current in frontier
                    ):
                        continue
                    frontier = [
                        current
                        for current in frontier
                        if not self._layer_dominates(
                            candidate,
                            current,
                            position_by_state,
                        )
                    ]
                    frontier.append(candidate)
                for candidate in frontier:
                    retained[candidate.state] = candidate
                    labels[candidate.label_id] = candidate
                    discovered_states.add(candidate.state)
            layer = retained
            context.discovered_state_count = len(discovered_states)

        raise PlannerValidationError("transfer-layer search ended unexpectedly")

    @staticmethod
    def _record_direction_segments(
        target: dict[RouteState, int], route: RouteStopList
    ) -> None:
        segment = 0
        for index, stop in enumerate(route.stops):
            if index:
                previous = route.stops[index - 1].direction.strip()
                current = stop.direction.strip()
                if previous and current and previous != current:
                    segment += 1
            target[stop.state] = segment

    @staticmethod
    def _record_route_positions(
        target: dict[RouteState, int], route: RouteStopList
    ) -> None:
        for position, stop in enumerate(route.stops):
            target[stop.state] = position

    @staticmethod
    def _layer_label_key(label: _SearchLabel) -> tuple[Any, ...]:
        return (
            int(label.root.snapped),
            label.metrics[1],
            label.metrics[2],
            label.route_signature,
            label.state,
            label.label_id,
        )

    @staticmethod
    def _layer_frontier_key(
        label: _SearchLabel,
        position_by_state: Mapping[RouteState, int],
    ) -> tuple[Any, ...]:
        position = position_by_state[label.state]
        return (
            int(label.root.snapped),
            position,
            label.metrics[1] - position,
            label.metrics[2],
            label.route_signature,
        )

    @staticmethod
    def _layer_dominates(
        left: _SearchLabel,
        right: _SearchLabel,
        position_by_state: Mapping[RouteState, int],
    ) -> bool:
        left_position = position_by_state[left.state]
        right_position = position_by_state[right.state]
        if left.root.snapped and not right.root.snapped:
            return False
        return (
            left_position <= right_position
            and (
                left.metrics[1] - left_position,
                left.metrics[2],
            )
            <= (
                right.metrics[1] - right_position,
                right.metrics[2],
            )
        )

    @staticmethod
    def _ride_row_delta(
        route_stops: RouteStopList,
        source: RouteState,
        destination: RouteState,
    ) -> int:
        """Count ride edges by active-sequence rows, not sparse order values."""

        if source.sequence_id != destination.sequence_id:
            raise PlannerValidationError(
                "ride transition endpoints must share one active sequence"
            )
        source_position = bisect_left(route_stops.orders, source.node_order)
        destination_position = bisect_left(
            route_stops.orders, destination.node_order
        )
        if (
            source_position >= len(route_stops.stops)
            or destination_position >= len(route_stops.stops)
            or route_stops.stops[source_position].state != source
            or route_stops.stops[destination_position].state != destination
            or source_position >= destination_position
        ):
            raise PlannerValidationError(
                "ride transition endpoints are invalid for the active sequence"
            )
        return destination_position - source_position

    @staticmethod
    def _reachable_ride_stops(
        route_stops: RouteStopList, state: RouteState
    ) -> tuple[RouteStop, ...]:
        source_index = bisect_right(route_stops.orders, state.node_order) - 1
        if source_index < 0 or route_stops.orders[source_index] != state.node_order:
            raise PlannerValidationError(
                "route state is missing from its active sequence"
            )
        reachable: list[RouteStop] = []
        for index in range(source_index + 1, len(route_stops.stops)):
            previous_direction = route_stops.stops[index - 1].direction.strip()
            current_direction = route_stops.stops[index].direction.strip()
            if (
                previous_direction
                and current_direction
                and previous_direction != current_direction
            ):
                break
            reachable.append(route_stops.stops[index])
        return tuple(reachable)

    @staticmethod
    def _search_priority(
        metrics: tuple[int, int, float], preference: str
    ) -> tuple[float, float, float]:
        transfers, ride_orders, walking_m = metrics
        if preference == "low_transfer":
            return float(transfers), float(ride_orders), walking_m
        if preference == "challenge":
            return (
                round(ride_orders + transfers * 2.0 + walking_m / 150.0, 9),
                float(transfers),
                walking_m,
            )
        transfer_weight = 8.0 if preference == "reliable" else 6.0
        return (
            round(ride_orders + transfers * transfer_weight + walking_m / 120.0, 9),
            float(transfers),
            walking_m,
        )

    @classmethod
    def _terminal_sort_key(cls, terminal: _TerminalPath) -> tuple[Any, ...]:
        return (
            terminal.endpoint_fallbacks,
            terminal.priority,
            terminal.metrics,
            terminal.route_signature,
        )

    @staticmethod
    def _generalized_terminal_key(
        terminal: _TerminalPath,
    ) -> tuple[Any, ...]:
        transfers, ride_orders, walking_m = terminal.metrics
        return (
            terminal.endpoint_fallbacks,
            round(ride_orders + transfers * 6.0 + walking_m / 120.0, 9),
            transfers,
            walking_m,
            terminal.route_signature,
        )

    def _select_terminal_paths(
        self,
        terminals: Sequence[_TerminalPath],
        *,
        preference: str,
        alternatives: int,
    ) -> tuple[_TerminalPath, ...]:
        if preference == "low_transfer":
            ordered = sorted(
                terminals,
                key=lambda item: (
                    item.endpoint_fallbacks,
                    item.metrics,
                    item.route_signature,
                ),
            )
            return tuple(ordered[:alternatives])
        if preference == "challenge":
            ordered = sorted(
                terminals,
                key=lambda item: (
                    item.endpoint_fallbacks,
                    -len(item.route_signature),
                    self._generalized_terminal_key(item),
                ),
            )
            return tuple(ordered[:alternatives])
        ordered = sorted(terminals, key=self._generalized_terminal_key)
        if preference == "reliable" or len(ordered) <= 1:
            return tuple(ordered[:alternatives])

        # Greedy max-min route-set distance keeps "diverse" alternatives
        # structurally different while the generalized cost remains the
        # deterministic tie-breaker. No synthetic timetable/reliability data
        # enters this ranking.
        selected = [ordered.pop(0)]
        while ordered and len(selected) < alternatives:
            def diversity_key(candidate: _TerminalPath) -> tuple[Any, ...]:
                candidate_routes = set(candidate.route_signature)
                minimum_distance = min(
                    1.0
                    - len(candidate_routes & set(current.route_signature))
                    / max(1, len(candidate_routes | set(current.route_signature)))
                    for current in selected
                )
                return (
                    -round(minimum_distance, 9),
                    self._generalized_terminal_key(candidate),
                )

            best_candidate = min(ordered, key=diversity_key)
            ordered.remove(best_candidate)
            selected.append(best_candidate)
        return tuple(selected)

    def _route_stops(
        self, context: _SearchContext, source: RouteStop
    ) -> RouteStopList:
        sequence_id = source.state.sequence_id
        cached = context.route_cache.get(sequence_id)
        if cached is not None:
            context.route_cache.move_to_end(sequence_id)
            return cached
        cursor = context.execute(
            """
            SELECT a.city_code,a.route_id,a.sequence_id,s.node_order,s.node_id,
                   s.node_name,s.latitude,s.longitude,s.direction,
                   s.can_board,s.can_alight
              FROM active_route_sequences a
              JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
             WHERE a.sequence_id=?
             ORDER BY s.node_order
            """,
            (sequence_id,),
        )
        rows = self._bounded_rows(
            context,
            cursor,
            limit=context.max_stops_per_route,
            label="one active route sequence",
        )
        stops = tuple(_stop_from_row(row) for row in rows)
        if not stops:
            raise PlannerValidationError("active route sequence has no stops")
        result = RouteStopList(stops, tuple(stop.state.node_order for stop in stops))
        self._cache_put(
            context.route_cache,
            sequence_id,
            result,
            context.route_cache_entries,
        )
        return result

    def _batched_transfer_targets(
        self,
        context: _SearchContext,
        *,
        route_stops: RouteStopList,
        sources: Sequence[RouteStop],
        transfer_radius_m: int,
    ) -> dict[RouteState, tuple[tuple[RouteStop, float, str], ...]]:
        """Resolve every downstream interchange with two indexed queries.

        The previous per-stop exact and geodesic lookups issued two SQL
        statements for every alightable stop. A long route therefore made
        thousands of queries before its first transfer. Joining the active
        source sequence to candidate stops keeps the same exact haversine
        predicate and candidate set while reducing one route expansion to one
        exact-ID query plus one coordinate-bounds query.
        """
        if not sources:
            return {}
        requested_states = {source.state for source in sources if source.can_alight}
        if not requested_states:
            return {}
        sequence_id = sources[0].state.sequence_id
        cache_key = (sequence_id, transfer_radius_m)
        cached = context.route_transfer_cache.get(cache_key)
        if cached is not None:
            context.route_transfer_cache.move_to_end(cache_key)
            return {
                state: cached.get(state, ())
                for state in requested_states
            }

        # Materialize interchanges once for the whole active sequence. The
        # search often reaches the same route at many boarding orders; slicing
        # this bounded per-sequence map avoids repeating both joins for every
        # route-state label.
        alightable = tuple(
            source for source in route_stops.stops if source.can_alight
        )
        if not alightable:
            return {}
        sequence_ids = {source.state.sequence_id for source in alightable}
        if len(sequence_ids) != 1:
            raise PlannerValidationError(
                "batched transfer sources must share one active sequence"
            )
        sequence_id = next(iter(sequence_ids))
        minimum_order = min(source.state.node_order for source in alightable)
        maximum_order = max(source.state.node_order for source in alightable)
        source_by_order = {
            source.state.node_order: source for source in alightable
        }
        targets: dict[
            RouteState, dict[RouteState, tuple[RouteStop, float, str]]
        ] = {source.state: {} for source in alightable}

        exact_rows = self._bounded_rows(
            context,
            context.execute(
                """
                SELECT source.node_order AS source_order,
                       a.city_code,a.route_id,a.sequence_id,
                       target.node_order,target.node_id,target.node_name,
                       target.latitude,target.longitude,target.direction,
                       target.can_board,target.can_alight
                  FROM route_sequence_stops source
                  JOIN route_sequence_stops target
                    ON target.node_id=source.node_id
                  JOIN active_route_sequences a
                    ON a.sequence_id=target.sequence_id
                 WHERE source.sequence_id=?
                   AND source.node_order BETWEEN ? AND ?
                   AND source.can_alight=1
                   AND target.can_board=1
                   AND a.city_code=?
                 ORDER BY source.node_order,a.city_code,a.route_id,target.node_order
                """,
                (
                    sequence_id,
                    minimum_order,
                    maximum_order,
                    alightable[0].state.city_code,
                ),
            ),
            limit=context.max_rows_per_lookup,
            label="batched exact stop transfer lookup",
        )
        for row in exact_rows:
            source = source_by_order.get(int(row["source_order"]))
            if source is None:
                continue
            target = _stop_from_row(row)
            if (
                target.state.city_code,
                target.state.route_id,
            ) == (source.state.city_code, source.state.route_id):
                continue
            targets[source.state][target.state] = (
                target,
                0.0,
                "shared_node_id",
            )

        coordinate_sources = tuple(
            source
            for source in alightable
            if source.latitude is not None and source.longitude is not None
        )
        if coordinate_sources:
            latitude_delta = transfer_radius_m / 111_000.0
            maximum_latitude = min(
                89.999999,
                max(abs(float(source.latitude)) for source in coordinate_sources)
                + latitude_delta,
            )
            longitude_scale = max(
                math.cos(math.radians(maximum_latitude)), 1e-9
            )
            longitude_delta = min(
                180.0,
                transfer_radius_m / (111_000.0 * longitude_scale),
            )
            nearby_rows = self._bounded_rows(
                context,
                context.execute(
                    """
                    SELECT source.node_order AS source_order,
                           a.city_code,a.route_id,a.sequence_id,
                           target.node_order,target.node_id,target.node_name,
                           target.latitude,target.longitude,target.direction,
                           target.can_board,target.can_alight
                      FROM route_sequence_stops source
                      JOIN route_sequence_stops target
                        ON target.latitude BETWEEN source.latitude-? AND source.latitude+?
                       AND MIN(
                             ABS(target.longitude-source.longitude),
                             360.0-ABS(target.longitude-source.longitude)
                           )<=?
                      JOIN active_route_sequences a
                        ON a.sequence_id=target.sequence_id
                     WHERE source.sequence_id=?
                       AND source.node_order BETWEEN ? AND ?
                       AND source.can_alight=1
                       AND source.latitude IS NOT NULL
                       AND source.longitude IS NOT NULL
                       AND target.can_board=1
                       AND target.latitude IS NOT NULL
                       AND target.longitude IS NOT NULL
                     ORDER BY source.node_order,a.city_code,a.route_id,target.node_order
                    """,
                    (
                        latitude_delta,
                        latitude_delta,
                        longitude_delta,
                        sequence_id,
                        minimum_order,
                        maximum_order,
                    ),
                ),
                limit=context.max_rows_per_lookup,
                label="batched geodesic transfer lookup",
            )
            for row in nearby_rows:
                source = source_by_order.get(int(row["source_order"]))
                if source is None:
                    continue
                target = _stop_from_row(row)
                if (
                    target.state.city_code,
                    target.state.route_id,
                ) == (source.state.city_code, source.state.route_id):
                    continue
                distance = self._stop_distance(
                    (float(source.latitude), float(source.longitude)), target
                )
                if distance > transfer_radius_m:
                    continue
                existing = targets[source.state].get(target.state)
                if existing is None or distance < existing[1]:
                    targets[source.state][target.state] = (
                        target,
                        distance,
                        "geodesic_proximity",
                    )

        result = {
            source_state: tuple(
                sorted(
                    values.values(),
                    key=lambda item: (
                        0 if item[2] == "shared_node_id" else 1,
                        round(item[1], 6),
                        item[0].state,
                    ),
                )
            )
            for source_state, values in targets.items()
        }
        self._cache_put(
            context.route_transfer_cache,
            cache_key,
            result,
            context.route_cache_entries,
        )
        return {
            state: result.get(state, ())
            for state in requested_states
        }

    def _transfer_targets(
        self,
        context: _SearchContext,
        *,
        source: RouteStop,
        transfer_radius_m: int,
    ) -> tuple[tuple[RouteStop, float, str], ...]:
        source_route = (source.state.city_code, source.state.route_id)
        targets: dict[RouteState, tuple[RouteStop, float, str]] = {}
        for target in self._exact_states(
            context,
            node_id=source.node_id,
            city_code=source.state.city_code,
            require="board",
        ):
            if (target.state.city_code, target.state.route_id) == source_route:
                continue
            targets[target.state] = (target, 0.0, "shared_node_id")
        if source.latitude is not None and source.longitude is not None:
            for target, distance in self._nearby_states(
                context,
                coordinate=(source.latitude, source.longitude),
                radius_m=transfer_radius_m,
                require="board",
            ):
                if (target.state.city_code, target.state.route_id) == source_route:
                    continue
                existing = targets.get(target.state)
                candidate = (target, distance, "geodesic_proximity")
                if existing is None or distance < existing[1]:
                    targets[target.state] = candidate
        return tuple(
            sorted(
                targets.values(),
                key=lambda item: (
                    0 if item[2] == "shared_node_id" else 1,
                    round(item[1], 6),
                    item[0].state,
                ),
            )
        )

    def _exact_states(
        self,
        context: _SearchContext,
        *,
        node_id: str,
        city_code: str | None,
        require: str,
    ) -> tuple[RouteStop, ...]:
        cache_key = ("exact", city_code, node_id, require)
        cached = context.transfer_cache.get(cache_key)
        if cached is not None:
            context.transfer_cache.move_to_end(cache_key)
            return cached
        access_column = "s.can_board" if require == "board" else "s.can_alight"
        city_clause = " AND a.city_code=?" if city_code is not None else ""
        parameters: tuple[Any, ...] = (
            (node_id, city_code) if city_code is not None else (node_id,)
        )
        cursor = context.execute(
            f"""
            SELECT a.city_code,a.route_id,a.sequence_id,s.node_order,s.node_id,
                   s.node_name,s.latitude,s.longitude,s.direction,
                   s.can_board,s.can_alight
              FROM route_sequence_stops s
              JOIN active_route_sequences a ON a.sequence_id=s.sequence_id
             WHERE s.node_id=? AND {access_column}=1{city_clause}
             ORDER BY a.city_code,a.route_id,s.node_order
            """,
            parameters,
        )
        rows = self._bounded_rows(
            context,
            cursor,
            limit=context.max_rows_per_lookup,
            label="exact stop transfer lookup",
        )
        result = tuple(_stop_from_row(row) for row in rows)
        self._cache_put(
            context.transfer_cache,
            cache_key,
            result,
            context.transfer_cache_entries,
        )
        return result

    def _nearby_states(
        self,
        context: _SearchContext,
        *,
        coordinate: tuple[float, float],
        radius_m: int,
        require: str,
    ) -> tuple[tuple[RouteStop, float], ...]:
        cache_key = ("nearby", coordinate[0], coordinate[1], radius_m, require)
        cached = context.transfer_cache.get(cache_key)
        if cached is not None:
            context.transfer_cache.move_to_end(cache_key)
            return tuple(
                (stop, self._stop_distance(coordinate, stop)) for stop in cached
            )
        latitude, longitude = coordinate
        latitude_delta = radius_m / 111_000.0
        longitude_scale = max(math.cos(math.radians(abs(latitude) + latitude_delta)), 1e-9)
        longitude_delta = min(180.0, radius_m / (111_000.0 * longitude_scale))
        minimum_longitude = longitude - longitude_delta
        maximum_longitude = longitude + longitude_delta
        longitude_sql: str
        longitude_parameters: tuple[float, ...]
        if minimum_longitude < -180:
            longitude_sql = "(s.longitude>=? OR s.longitude<=?)"
            longitude_parameters = (minimum_longitude + 360, maximum_longitude)
        elif maximum_longitude > 180:
            longitude_sql = "(s.longitude>=? OR s.longitude<=?)"
            longitude_parameters = (minimum_longitude, maximum_longitude - 360)
        else:
            longitude_sql = "s.longitude BETWEEN ? AND ?"
            longitude_parameters = (minimum_longitude, maximum_longitude)
        access_column = "s.can_board" if require == "board" else "s.can_alight"
        cursor = context.execute(
            f"""
            SELECT a.city_code,a.route_id,a.sequence_id,s.node_order,s.node_id,
                   s.node_name,s.latitude,s.longitude,s.direction,
                   s.can_board,s.can_alight
              FROM route_sequence_stops s
              JOIN active_route_sequences a ON a.sequence_id=s.sequence_id
             WHERE s.latitude BETWEEN ? AND ?
               AND {longitude_sql}
               AND {access_column}=1
             ORDER BY a.city_code,a.route_id,s.node_order
            """,
            (
                max(-90.0, latitude - latitude_delta),
                min(90.0, latitude + latitude_delta),
                *longitude_parameters,
            ),
        )
        rows = self._bounded_rows(
            context,
            cursor,
            limit=context.max_rows_per_lookup,
            label="geodesic transfer lookup",
        )
        located: list[tuple[RouteStop, float]] = []
        for row in rows:
            stop = _stop_from_row(row)
            distance = self._stop_distance(coordinate, stop)
            if distance <= radius_m:
                located.append((stop, distance))
        located.sort(key=lambda item: (round(item[1], 6), item[0].state))
        # Cache every exact haversine match.  There is deliberately no nearest-N
        # slicing: an explicit row/query limit raises instead of losing routes.
        self._cache_put(
            context.transfer_cache,
            cache_key,
            tuple(item[0] for item in located),
            context.transfer_cache_entries,
        )
        return tuple(located)

    @staticmethod
    def _stop_distance(coordinate: tuple[float, float], stop: RouteStop) -> float:
        if stop.latitude is None or stop.longitude is None:
            return math.inf
        return _distance_m(coordinate, (stop.latitude, stop.longitude))

    @staticmethod
    def _bounded_rows(
        context: _SearchContext,
        cursor: sqlite3.Cursor,
        *,
        limit: int,
        label: str,
    ) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        try:
            for row in cursor:
                rows.append(row)
                if len(rows) > limit:
                    raise PlannerLimitError(
                        f"{label} exceeds explicit {limit}-row limit"
                    )
        except sqlite3.OperationalError as exc:
            context.translate_operational_error(exc)
            raise AssertionError("unreachable")
        return rows

    @staticmethod
    def _cache_put(
        cache: OrderedDict[Any, Any], key: Any, value: Any, maximum: int
    ) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > maximum:
            cache.popitem(last=False)

    @staticmethod
    def _located_sort_key(value: LocatedStop) -> tuple[Any, ...]:
        return (
            round(value.access_distance_m, 6),
            value.stop.state,
            value.stop.node_id,
        )

    @staticmethod
    def _transition_sort_key(value: Transition) -> tuple[Any, ...]:
        return (
            value.source.state,
            value.ride_to.state,
            value.target.state,
            value.transfer_evidence or "",
        )

    def _ready_paths(
        self,
        context: _SearchContext,
        *,
        labels: Mapping[int, _SearchLabel],
        terminals: Sequence[_TerminalPath],
        preference: str,
        alternatives_requested: int,
        alternative_search_complete: bool,
        limit_reason: str | None,
        elapsed_seconds: float,
        state_label_limit: int,
        transfer_radius_m: int,
        origin_access: Mapping[str, Any] | None,
        destination_access: Mapping[str, Any] | None,
        alternative_algorithm: str = "budgeted_route_signature_dijkstra",
        transfer_layer_sizes: Sequence[int] = (),
        endpoint_exact_search_complete: bool = True,
    ) -> dict[str, Any]:
        # Search deadlines guard graph exploration. A positive terminal may be
        # retained exactly at that boundary; serializing its already-bounded
        # route slices must not inherit an expired SQLite interrupt handler.
        context.disarm_sql_deadline()
        candidates: list[dict[str, Any]] = []
        criteria = {
            "low_transfer": ("minimum_transfers",),
            "reliable": ("generalized_cost",),
            "challenge": ("explorer",),
            "diverse": (
                "minimum_transfers",
                "generalized_cost",
                "explorer",
            ),
        }[preference]
        for index, terminal in enumerate(terminals):
            transitions = [terminal.transition]
            cursor = labels[terminal.label_id]
            while cursor.transition is not None:
                transitions.append(cursor.transition)
                if cursor.previous_id is None:
                    raise PlannerValidationError(
                        "journey label chain is incomplete"
                    )
                cursor = labels[cursor.previous_id]
            transitions.reverse()
            candidates.append(
                self._structural_candidate(
                    context=context,
                    transitions=transitions,
                    terminal_cost=terminal.metrics,
                    root=labels[terminal.label_id].root,
                    criterion=criteria[index % len(criteria)],
                    origin_access=origin_access,
                    destination_access=destination_access,
                )
            )
        metadata = self._metadata(context, transfer_radius_m)
        metadata.update(
            {
                "alternative_algorithm": alternative_algorithm,
                "preference": preference,
                "alternatives_requested": alternatives_requested,
                "alternatives_returned": len(candidates),
                "alternatives_truncated": len(candidates) < alternatives_requested,
                "alternative_search_complete": alternative_search_complete,
                "limit_reason": limit_reason,
                "wall_clock_budget_seconds": self.search_wall_seconds,
                "elapsed_seconds": round(elapsed_seconds, 6),
                "max_labels_per_route_state": state_label_limit,
                "additional_alternative_expansions": (
                    self.additional_alternative_expansions
                ),
                "max_transfer_layers": self.max_transfer_layers,
                "endpoint_exact_search_complete": (
                    endpoint_exact_search_complete
                ),
                "endpoint_exact_grace_seconds": (
                    self.endpoint_exact_grace_seconds
                ),
            }
        )
        if transfer_layer_sizes:
            metadata["transfer_layer_sizes"] = list(transfer_layer_sizes)
        return {
            "status": "READY",
            "reason": None,
            "graph": metadata,
            "alternatives": candidates,
        }

    def _structural_candidate(
        self,
        *,
        context: _SearchContext,
        transitions: Sequence[Transition],
        terminal_cost: tuple[int, int, float],
        root: LocatedStop,
        criterion: str,
        origin_access: Mapping[str, Any] | None,
        destination_access: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        endpoint_access: list[dict[str, Any]] = []
        if root.snapped and origin_access is not None:
            access_step = self._walk_step(
                access=origin_access,
                graph_stop=root.stop,
                distance_m=root.access_distance_m,
                access_kind="access",
                evidence_type=(
                    root.access_evidence or "catalog_coordinate_access"
                ),
            )
            steps.append(access_step)
            endpoint_access.append(access_step)
        route_ids: list[str] = []
        for transition in transitions:
            route_id = transition.source.state.route_id
            if not route_ids or route_ids[-1] != route_id:
                route_ids.append(route_id)
            segment_stops = self._ride_segment_stops(context, transition)
            stop_order_delta = len(segment_stops) - 1
            sampled_segment = self._sample_segment_stops(segment_stops)
            steps.append(
                {
                    "kind": "ride",
                    "route_id": route_id,
                    "from": self._stop_payload(transition.source),
                    "to": self._stop_payload(transition.ride_to),
                    "stop_order_delta": stop_order_delta,
                    "stop_count": len(segment_stops),
                    "segment_stops": [
                        self._stop_payload(stop) for stop in sampled_segment
                    ],
                    "segment_stops_truncated": (
                        len(sampled_segment) < len(segment_stops)
                    ),
                    "direction": "ascending_node_order",
                    "evidence": {"type": "active_route_sequence"},
                }
            )
            if transition.transfer_evidence is not None:
                steps.append(
                    {
                        "kind": "transfer",
                        "route_id": None,
                        "from": self._stop_payload(transition.ride_to),
                        "to": self._stop_payload(transition.target),
                        "distance_m": round(transition.transfer_distance_m, 3),
                        "evidence": {"type": transition.transfer_evidence},
                    }
                )
        if (
            transitions
            and transitions[-1].destination_snapped
            and destination_access is not None
        ):
            egress_step = self._walk_step(
                access=destination_access,
                graph_stop=transitions[-1].ride_to,
                distance_m=transitions[-1].destination_access_distance_m,
                access_kind="egress",
                evidence_type=(
                    transitions[-1].destination_access_evidence
                    or "catalog_coordinate_access"
                ),
            )
            steps.append(egress_step)
            endpoint_access.append(egress_step)
        return {
            "status": "STRUCTURAL_ROUTE",
            "criterion": criterion,
            "route_ids": route_ids,
            "transfers": terminal_cost[0],
            "walking_m": round(terminal_cost[2], 1),
            "ride_order_delta": terminal_cost[1],
            "steps": steps,
            "endpoint_access": endpoint_access,
            "evidence": {
                "topology": "all_active_hydrated_route_sequences",
                "ride_edges": sum(
                    1 for step in steps if step.get("kind") == "ride"
                ),
                "transfer_edges": sum(
                    1 for step in steps if step.get("kind") == "transfer"
                ),
                "sources": sorted(
                    {
                        str((step.get("evidence") or {}).get("type"))
                        for step in steps
                        if (step.get("evidence") or {}).get("type")
                    }
                ),
            },
            "directionality": (
                "ascending_node_order_with_nonempty_direction_boundaries"
            ),
        }

    def _ride_segment_stops(
        self,
        context: _SearchContext,
        transition: Transition,
    ) -> tuple[RouteStop, ...]:
        """Return the cached active-sequence slice for one ride transition."""

        source = transition.source
        destination = transition.ride_to
        if (
            source.state.sequence_id != destination.state.sequence_id
            or source.state.node_order >= destination.state.node_order
        ):
            raise PlannerValidationError("ride transition has invalid sequence bounds")
        route = self._route_stops(context, source)
        start = bisect_left(route.orders, source.state.node_order)
        end = bisect_right(route.orders, destination.state.node_order)
        segment = route.stops[start:end]
        if (
            not segment
            or segment[0].state != source.state
            or segment[-1].state != destination.state
        ):
            raise PlannerValidationError(
                "ride transition endpoints are missing from the active sequence"
            )
        return segment

    @staticmethod
    def _sample_segment_stops(
        segment: Sequence[RouteStop],
    ) -> tuple[RouteStop, ...]:
        """Uniformly bound map/detail payloads while preserving both endpoints."""

        if len(segment) <= MAX_SEGMENT_STOPS:
            return tuple(segment)
        last = len(segment) - 1
        denominator = MAX_SEGMENT_STOPS - 1
        return tuple(
            segment[(sample_index * last) // denominator]
            for sample_index in range(MAX_SEGMENT_STOPS)
        )

    def _gap(
        self,
        context: _SearchContext,
        reason: str,
        transfer_radius_m: int,
        *,
        search: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = self._metadata(context, transfer_radius_m)
        if search is not None:
            metadata.update(dict(search))
        return {
            "status": "DATA_GAP",
            "reason": reason,
            "graph": metadata,
            "alternatives": [],
        }

    @staticmethod
    def _stop_payload(stop: RouteStop) -> dict[str, Any]:
        return {
            "city_code": stop.state.city_code,
            "route_id": stop.state.route_id,
            "node_id": stop.node_id,
            "node_name": stop.node_name,
            "node_order": stop.state.node_order,
            "latitude": stop.latitude,
            "longitude": stop.longitude,
        }

    @staticmethod
    def _walk_step(
        *,
        access: Mapping[str, Any],
        graph_stop: RouteStop,
        distance_m: float,
        access_kind: str,
        evidence_type: str,
    ) -> dict[str, Any]:
        access_payload = {
            "city_code": access["city_code"],
            "route_id": None,
            "node_id": access["node_id"],
            "node_name": access["node_name"],
            "node_order": None,
            "latitude": access["latitude"],
            "longitude": access["longitude"],
        }
        graph_payload = SQLiteJourneyPlanner._stop_payload(graph_stop)
        return {
            "kind": "walk",
            "route_id": None,
            "from": access_payload if access_kind == "access" else graph_payload,
            "to": graph_payload if access_kind == "access" else access_payload,
            "distance_m": round(distance_m, 3),
            "access_kind": access_kind,
            "evidence": {"type": evidence_type},
        }

    @staticmethod
    def _metadata(
        context: _SearchContext, transfer_radius_m: int
    ) -> dict[str, Any]:
        return {
            "algorithm": "sqlite_route_level_dijkstra",
            "state": "active_direction_route_plus_current_node_order",
            "directionality": (
                "ascending_node_order_with_nonempty_direction_boundaries"
            ),
            "transfer_radius_m": transfer_radius_m,
            "topology_materialization": "on_demand_indexed_sqlite",
            # Query-scoped numeric aliases keep the public graph telemetry
            # shape stable without claiming the full nationwide graph was
            # materialized in this process.
            "nodes": context.discovered_state_count,
            "edges": (
                context.ride_candidates_scanned
                + context.transfer_targets_discovered
            ),
            "ride_edges": context.ride_candidates_scanned,
            "transfer_edges": context.transfer_targets_discovered,
            "queries": context.query_count,
            "route_state_expansions": context.expansion_count,
            "route_cache_entries": len(context.route_cache),
            "transfer_cache_entries": len(context.transfer_cache),
            "route_transfer_cache_entries": len(context.route_transfer_cache),
            "max_parallel_searches": context.max_parallel_searches,
            "limits_are_explicit_errors": True,
        }


__all__ = [
    "DEFAULT_TRANSFER_RADIUS_M",
    "ENDPOINT_ACCESS_RADIUS_M",
    "PlannerBusyError",
    "RouteState",
    "SQLiteJourneyPlanner",
    "required_index_ddl",
]
