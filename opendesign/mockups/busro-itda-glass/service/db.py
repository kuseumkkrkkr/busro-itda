"""SQLite snapshot, observation, idempotency, and response-cache storage."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class IdempotencyConflict(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        # SQLite WAL permits concurrent readers but still has one writer. Keep
        # this process's short write transactions ordered so a burst of 200
        # distinct collections cannot exhaust busy_timeout and become HTTP 500.
        self._write_lock = threading.RLock()
        self._write_state_lock = threading.Lock()
        self._pending_writes = 0
        self._writer_pins = 0
        self._writer_connection: sqlite3.Connection | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _open_connection(
        self, *, check_same_thread: bool = True
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            check_same_thread=check_same_thread,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        # Keep SQLite's power-loss durability guarantee explicit. Throughput
        # comes from reusing the serialized WAL writer during a burst, not from
        # weakening synchronous commit semantics.
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write_connect(self) -> Iterator[sqlite3.Connection]:
        # Count waiters before entering the writer lock.  During a burst this
        # keeps one WAL writer connection open across the queued transactions,
        # avoiding a checkpoint/open/close cycle for every request.  The last
        # writer closes it, so tests and short-lived services retain the old
        # deterministic file-lifetime behavior on Windows.
        with self._write_state_lock:
            self._pending_writes += 1
        with self._write_lock:
            connection: sqlite3.Connection | None = None
            try:
                with self._write_state_lock:
                    if self._writer_connection is None:
                        self._writer_connection = self._open_connection(
                            check_same_thread=False
                        )
                    connection = self._writer_connection
                yield connection
            except BaseException:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                close_connection: sqlite3.Connection | None = None
                with self._write_state_lock:
                    self._pending_writes -= 1
                    if self._pending_writes == 0 and self._writer_pins == 0:
                        close_connection = self._writer_connection
                        self._writer_connection = None
                if close_connection is not None:
                    close_connection.close()

    def pin_writer(self) -> None:
        """Keep the serialized WAL writer open for one server lifetime."""
        with self._write_lock:
            with self._write_state_lock:
                if self._writer_connection is None:
                    self._writer_connection = self._open_connection(
                        check_same_thread=False
                    )
                self._writer_pins += 1

    def unpin_writer(self) -> None:
        """Release one server pin and close the idle writer deterministically."""
        close_connection: sqlite3.Connection | None = None
        with self._write_lock:
            with self._write_state_lock:
                if self._writer_pins <= 0:
                    return
                self._writer_pins -= 1
                if self._writer_pins == 0 and self._pending_writes == 0:
                    close_connection = self._writer_connection
                    self._writer_connection = None
            if close_connection is not None:
                if close_connection.in_transaction:
                    close_connection.rollback()
                close_connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cache_entries (
                    cache_key TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS upstream_daily_attempts (
                    service_date TEXT PRIMARY KEY,
                    attempted_calls INTEGER NOT NULL CHECK(attempted_calls >= 0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    service_date TEXT NOT NULL,
                    upstream_json TEXT NOT NULL,
                    arrivals_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS arrival_observations (
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    route_id TEXT NOT NULL,
                    route_no TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    arrival_seconds INTEGER,
                    remaining_stops INTEGER,
                    PRIMARY KEY (snapshot_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_captured
                    ON snapshots(captured_at DESC);
                CREATE INDEX IF NOT EXISTS idx_observations_route_time
                    ON arrival_observations(route_id, observed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_observations_node_time
                    ON arrival_observations(node_id, observed_at DESC);
                CREATE TABLE IF NOT EXISTS position_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    service_date TEXT NOT NULL,
                    upstream_json TEXT NOT NULL,
                    positions_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS position_observations (
                    snapshot_id TEXT NOT NULL REFERENCES position_snapshots(snapshot_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    vehicle_no TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    node_order INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    latitude REAL,
                    longitude REAL,
                    PRIMARY KEY (snapshot_id, ordinal),
                    UNIQUE (snapshot_id, route_id, vehicle_no)
                );
                CREATE TABLE IF NOT EXISTS passages (
                    passage_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES position_snapshots(snapshot_id) ON DELETE CASCADE,
                    previous_snapshot_id TEXT NOT NULL REFERENCES position_snapshots(snapshot_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    precision TEXT NOT NULL,
                    gap_reason TEXT,
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    vehicle_no TEXT NOT NULL,
                    from_node_id TEXT NOT NULL,
                    from_node_name TEXT NOT NULL,
                    from_node_order INTEGER NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    node_order INTEGER NOT NULL,
                    node_order_delta INTEGER NOT NULL,
                    observed_from TEXT NOT NULL,
                    observed_to TEXT NOT NULL,
                    service_date TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_position_snapshots_route_time
                    ON position_snapshots(route_id, captured_at DESC);
                CREATE INDEX IF NOT EXISTS idx_position_observations_vehicle_time
                    ON position_observations(route_id, vehicle_no, observed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_passages_route_date_node
                    ON passages(route_id, service_date, node_id, node_order);
                CREATE INDEX IF NOT EXISTS idx_passages_vehicle_time
                    ON passages(route_id, vehicle_no, observed_to DESC);
                CREATE TABLE IF NOT EXISTS catalog_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    resource_type TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    upstream_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    query_json TEXT NOT NULL,
                    records_json TEXT NOT NULL,
                    UNIQUE(resource_type, request_hash, upstream_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_resource_time
                    ON catalog_snapshots(resource_type, captured_at DESC);
                CREATE TABLE IF NOT EXISTS mapping_validations (
                    validation_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    upstream_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    valid INTEGER NOT NULL CHECK(valid IN (0,1)),
                    node_order INTEGER,
                    match_json TEXT,
                    UNIQUE(request_hash, upstream_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_mapping_route_node
                    ON mapping_validations(city_code, route_id, node_id, captured_at DESC);
                """
            )
            connection.commit()

    def get_cache(self, cache_key: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM cache_entries WHERE cache_key=? AND expires_at>?",
                (cache_key, now),
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["payload_json"])

    def put_cache(self, cache_key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        with self.write_connect() as connection:
            connection.execute("DELETE FROM cache_entries WHERE expires_at<=?", (int(time.time()),))
            connection.execute(
                """INSERT INTO cache_entries(cache_key, expires_at, payload_json)
                   VALUES(?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     expires_at=excluded.expires_at, payload_json=excluded.payload_json""",
                (cache_key, int(time.time()) + ttl_seconds, _json(payload)),
            )
            connection.commit()

    def reserve_tago_attempt(
        self,
        *,
        service_date: str,
        attempted_at: str,
        daily_limit: int,
    ) -> tuple[bool, int]:
        """Atomically reserve one real TAGO attempt for a KST service date."""
        with self.write_connect() as connection:
            row = connection.execute(
                """INSERT INTO upstream_daily_attempts(
                       service_date, attempted_calls, updated_at
                   ) VALUES(?, 1, ?)
                   ON CONFLICT(service_date) DO UPDATE SET
                     attempted_calls=upstream_daily_attempts.attempted_calls + 1,
                     updated_at=excluded.updated_at
                   WHERE upstream_daily_attempts.attempted_calls < ?
                   RETURNING attempted_calls""",
                (service_date, attempted_at, daily_limit),
            ).fetchone()
            if row is None:
                current = connection.execute(
                    "SELECT attempted_calls FROM upstream_daily_attempts WHERE service_date=?",
                    (service_date,),
                ).fetchone()
                attempted_calls = int(current["attempted_calls"]) if current else 0
                allowed = False
            else:
                attempted_calls = int(row["attempted_calls"])
                allowed = True
            connection.commit()
        return allowed, attempted_calls

    def tago_attempt_count(self, service_date: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempted_calls FROM upstream_daily_attempts WHERE service_date=?",
                (service_date,),
            ).fetchone()
            return int(row["attempted_calls"]) if row else 0

    def get_snapshot_by_idempotency(self, key: str) -> dict[str, Any] | None:
        # Collection immediately follows this indexed lookup with a serialized
        # snapshot insert. Reuse that same guarded connection so a 200-request
        # burst does not open 200 short-lived SQLite handles on Windows.
        with self.write_connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE idempotency_key=?", (key,)
            ).fetchone()
            return self._snapshot_row(row) if row else None

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """Return one arrival snapshot by its public identifier."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            return self._snapshot_row(row) if row else None

    def create_snapshot(
        self,
        *,
        snapshot_id: str,
        idempotency_key: str,
        request_hash: str,
        payload_hash: str,
        source: str,
        city_code: str,
        node_id: str,
        captured_at: str,
        upstream: dict[str, Any],
        arrivals: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        service_date = captured_at[:10]
        with self.write_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM snapshots WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    connection.rollback()
                    raise IdempotencyConflict("idempotency key was already used for another request")
                connection.commit()
                return self._snapshot_row(existing), False

            connection.execute(
                """INSERT INTO snapshots(
                     snapshot_id,idempotency_key,request_hash,payload_hash,source,
                     city_code,node_id,captured_at,service_date,upstream_json,arrivals_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    idempotency_key,
                    request_hash,
                    payload_hash,
                    source,
                    city_code,
                    node_id,
                    captured_at,
                    service_date,
                    _json(upstream),
                    _json(arrivals),
                ),
            )
            for ordinal, arrival in enumerate(arrivals):
                connection.execute(
                    """INSERT INTO arrival_observations(
                         snapshot_id,ordinal,route_id,route_no,city_code,node_id,
                         observed_at,arrival_seconds,remaining_stops
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        snapshot_id,
                        ordinal,
                        arrival.get("route_id", ""),
                        arrival.get("route_no", ""),
                        city_code,
                        arrival.get("node_id") or node_id,
                        captured_at,
                        arrival.get("arrival_seconds"),
                        arrival.get("remaining_stops"),
                    ),
                )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            return self._snapshot_row(row), True

    def history(
        self,
        *,
        route_id: str | None,
        city_code: str | None,
        node_id: str | None,
        from_value: str | None,
        to_value: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        joins = ""
        clauses: list[str] = []
        params: list[Any] = []
        if route_id:
            joins = " JOIN arrival_observations o ON o.snapshot_id=s.snapshot_id "
            clauses.append("o.route_id=?")
            params.append(route_id)
        if city_code:
            clauses.append("s.city_code=?")
            params.append(city_code)
        if node_id:
            clauses.append("s.node_id=?")
            params.append(node_id)
        if from_value:
            clauses.append("s.captured_at>=?")
            params.append(from_value)
        if to_value:
            clauses.append("s.captured_at<=?")
            params.append(to_value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        query = (
            "SELECT DISTINCT s.* FROM snapshots s"
            + joins
            + where
            + " ORDER BY s.captured_at DESC LIMIT ?"
        )
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
            result = [self._snapshot_row(row) for row in rows]
        if route_id:
            for snapshot in result:
                snapshot["arrivals"] = [
                    item for item in snapshot["arrivals"] if item.get("route_id") == route_id
                ]
        return result

    def delay_observations(self, *, route_id: str, node_id: str | None, limit: int = 5000) -> list[dict[str, Any]]:
        clauses = ["route_id=?", "arrival_seconds IS NOT NULL"]
        params: list[Any] = [route_id]
        if node_id:
            clauses.append("node_id=?")
            params.append(node_id)
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT observed_at,arrival_seconds,node_id FROM arrival_observations "
                f"WHERE {' AND '.join(clauses)} ORDER BY observed_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_position_snapshot_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with self.write_connect() as connection:
            row = connection.execute(
                "SELECT * FROM position_snapshots WHERE idempotency_key=?", (key,)
            ).fetchone()
            return self._position_snapshot_row(row) if row else None

    def get_position_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """Return one vehicle-position snapshot by its public identifier."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM position_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            return self._position_snapshot_row(row) if row else None

    def create_position_snapshot(
        self,
        *,
        snapshot_id: str,
        idempotency_key: str,
        request_hash: str,
        payload_hash: str,
        source: str,
        city_code: str,
        route_id: str,
        captured_at: str,
        service_date: str,
        upstream: dict[str, Any],
        positions: list[dict[str, Any]],
        maximum_gap_seconds: int,
    ) -> tuple[dict[str, Any], bool, list[dict[str, Any]]]:
        """Atomically persist one location poll and derive transition evidence."""
        with self.write_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM position_snapshots WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    connection.rollback()
                    raise IdempotencyConflict("idempotency key was already used for another request")
                events = connection.execute(
                    "SELECT * FROM passages WHERE snapshot_id=? ORDER BY observed_to,passage_id",
                    (existing["snapshot_id"],),
                ).fetchall()
                connection.commit()
                return self._position_snapshot_row(existing), False, [dict(row) for row in events]

            connection.execute(
                """INSERT INTO position_snapshots(
                     snapshot_id,idempotency_key,request_hash,payload_hash,source,
                     city_code,route_id,captured_at,service_date,upstream_json,positions_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    idempotency_key,
                    request_hash,
                    payload_hash,
                    source,
                    city_code,
                    route_id,
                    captured_at,
                    service_date,
                    _json(upstream),
                    _json(positions),
                ),
            )
            created_events: list[dict[str, Any]] = []
            for ordinal, position in enumerate(positions):
                previous = connection.execute(
                    """SELECT o.*,s.service_date
                       FROM position_observations o
                       JOIN position_snapshots s ON s.snapshot_id=o.snapshot_id
                       WHERE o.route_id=? AND o.vehicle_no=?
                       ORDER BY o.observed_at DESC,o.snapshot_id DESC LIMIT 1""",
                    (route_id, position["vehicle_no"]),
                ).fetchone()
                connection.execute(
                    """INSERT INTO position_observations(
                         snapshot_id,ordinal,city_code,route_id,vehicle_no,node_id,node_name,
                         node_order,observed_at,latitude,longitude
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        snapshot_id,
                        ordinal,
                        city_code,
                        route_id,
                        position["vehicle_no"],
                        position["node_id"],
                        position.get("node_name", ""),
                        position["node_order"],
                        captured_at,
                        position.get("latitude"),
                        position.get("longitude"),
                    ),
                )
                if previous is None:
                    continue
                delta = int(position["node_order"]) - int(previous["node_order"])
                status: str | None = None
                gap_reason: str | None = None
                try:
                    before = datetime.fromisoformat(str(previous["observed_at"]).replace("Z", "+00:00"))
                    after = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
                    elapsed = (after - before).total_seconds()
                except (TypeError, ValueError):
                    elapsed = maximum_gap_seconds + 1
                if delta < 0:
                    status, gap_reason = "REGRESSION", "NODE_ORDER_REGRESSION"
                elif elapsed <= 0 and delta != 0:
                    status, gap_reason = "DATA_GAP", "NON_MONOTONIC_CAPTURE_TIME"
                elif delta == 1 and elapsed <= maximum_gap_seconds:
                    status = "PASSAGE"
                elif delta > 1:
                    status, gap_reason = "DATA_GAP", "NODE_ORDER_JUMP"
                elif position["node_id"] != previous["node_id"]:
                    status, gap_reason = "DATA_GAP", "NODE_ORDER_COLLISION"
                elif elapsed > maximum_gap_seconds:
                    status, gap_reason = "DATA_GAP", "OBSERVATION_TIME_GAP"
                if status is None:
                    continue

                identity = _json(
                    {
                        "snapshot": snapshot_id,
                        "previous": previous["snapshot_id"],
                        "route": route_id,
                        "vehicle": position["vehicle_no"],
                        "status": status,
                    }
                ).encode("utf-8")
                passage_id = "pass_" + hashlib.sha256(identity).hexdigest()[:24]
                event = {
                    "passage_id": passage_id,
                    "snapshot_id": snapshot_id,
                    "previous_snapshot_id": previous["snapshot_id"],
                    "status": status,
                    "precision": "polling_window",
                    "gap_reason": gap_reason,
                    "city_code": city_code,
                    "route_id": route_id,
                    "vehicle_no": position["vehicle_no"],
                    "from_node_id": previous["node_id"],
                    "from_node_name": previous["node_name"],
                    "from_node_order": int(previous["node_order"]),
                    "node_id": position["node_id"],
                    "node_name": position.get("node_name", ""),
                    "node_order": int(position["node_order"]),
                    "node_order_delta": delta,
                    "observed_from": previous["observed_at"],
                    "observed_to": captured_at,
                    "service_date": service_date,
                }
                connection.execute(
                    """INSERT INTO passages(
                         passage_id,snapshot_id,previous_snapshot_id,status,precision,gap_reason,
                         city_code,route_id,vehicle_no,from_node_id,from_node_name,from_node_order,
                         node_id,node_name,node_order,node_order_delta,observed_from,observed_to,service_date
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(event[key] for key in (
                        "passage_id", "snapshot_id", "previous_snapshot_id", "status", "precision",
                        "gap_reason", "city_code", "route_id", "vehicle_no", "from_node_id",
                        "from_node_name", "from_node_order", "node_id", "node_name", "node_order",
                        "node_order_delta", "observed_from", "observed_to", "service_date"
                    )),
                )
                created_events.append(event)
            connection.commit()
            row = connection.execute(
                "SELECT * FROM position_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            return self._position_snapshot_row(row), True, created_events

    def passages(
        self,
        *,
        route_id: str | None,
        vehicle_no: str | None,
        node_id: str | None,
        status: str | None,
        from_date: str | None,
        to_date: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("route_id", route_id),
            ("vehicle_no", vehicle_no),
            ("node_id", node_id),
            ("status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if from_date:
            clauses.append("service_date>=?")
            params.append(from_date)
        if to_date:
            clauses.append("service_date<=?")
            params.append(to_date)
        params.append(limit)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM passages" + where + " ORDER BY observed_to DESC LIMIT ?", params
            ).fetchall()
            return [dict(row) for row in rows]

    def journey_evidence(
        self, route_ids: list[str] | tuple[str, ...]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Aggregate only persisted live TAGO observations for candidate routes.

        Observations prove that a vehicle was seen, not that a timetable or a
        transfer window is valid. Empty upstream snapshots and fixture sources
        are deliberately excluded. The passage ratio describes reconstructed
        transition outcomes; it is not timetable or transfer-success evidence.
        """
        requested = tuple(sorted({str(value) for value in route_ids if value}))
        service: dict[str, dict[str, Any]] = {}
        passage: dict[str, dict[str, Any]] = {}
        if not requested:
            return service, passage

        # Each route id appears twice in the UNION query.  Keep well below
        # SQLite's common 999-variable limit.
        for offset in range(0, len(requested), 400):
            batch = requested[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            with self.connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT route_id,
                           COUNT(*) AS observation_count,
                           SUM(kind='arrival') AS arrival_observation_count,
                           SUM(kind='position') AS position_observation_count,
                           COUNT(DISTINCT snapshot_id) AS snapshot_count,
                           MIN(observed_at) AS first_observed_at,
                           MAX(observed_at) AS last_observed_at
                    FROM (
                        SELECT o.route_id,o.snapshot_id,o.observed_at,'arrival' AS kind
                        FROM arrival_observations o
                        JOIN snapshots s ON s.snapshot_id=o.snapshot_id
                        WHERE s.source='TAGO' AND o.route_id IN ({placeholders})
                        UNION ALL
                        SELECT o.route_id,o.snapshot_id,o.observed_at,'position' AS kind
                        FROM position_observations o
                        JOIN position_snapshots s ON s.snapshot_id=o.snapshot_id
                        WHERE s.source='TAGO_POSITION' AND o.route_id IN ({placeholders})
                    )
                    GROUP BY route_id
                    """,
                    (*batch, *batch),
                ).fetchall()
                for row in rows:
                    route_id = str(row["route_id"])
                    service[route_id] = {
                        "verified": False,
                        "basis": "persisted_live_tago_observations",
                        "evidence_scope": "vehicle_observation_only_not_verified_timetable",
                        "observation_count": int(row["observation_count"]),
                        "arrival_observation_count": int(row["arrival_observation_count"] or 0),
                        "position_observation_count": int(row["position_observation_count"] or 0),
                        "snapshot_count": int(row["snapshot_count"]),
                        "first_observed_at": row["first_observed_at"],
                        "last_observed_at": row["last_observed_at"],
                    }

                rows = connection.execute(
                    f"""
                    SELECT p.route_id,
                           COUNT(*) AS sample_count,
                           SUM(p.status='PASSAGE') AS passage_count,
                           SUM(p.status='DATA_GAP') AS data_gap_count,
                           SUM(p.status='REGRESSION') AS regression_count,
                           COUNT(DISTINCT p.service_date) AS service_date_count,
                           MIN(p.observed_from) AS first_observed_at,
                           MAX(p.observed_to) AS last_observed_at
                    FROM passages p
                    JOIN position_snapshots s ON s.snapshot_id=p.snapshot_id
                    WHERE s.source='TAGO_POSITION'
                      AND p.status IN ('PASSAGE','DATA_GAP','REGRESSION')
                      AND p.route_id IN ({placeholders})
                    GROUP BY p.route_id
                    """,
                    batch,
                ).fetchall()
                for row in rows:
                    route_id = str(row["route_id"])
                    sample_count = int(row["sample_count"])
                    passage_count = int(row["passage_count"] or 0)
                    passage[route_id] = {
                        "sample_count": sample_count,
                        "observed_passage_ratio": passage_count / sample_count,
                        "passage_count": passage_count,
                        "data_gap_count": int(row["data_gap_count"] or 0),
                        "regression_count": int(row["regression_count"] or 0),
                        "service_date_count": int(row["service_date_count"] or 0),
                        "basis": "persisted_live_tago_passage_reconstruction",
                        "metric": "observed_consecutive_passage_ratio",
                        "probability_scope": "observation_reconstruction_not_timetable_or_transfer_success",
                        "precision": "polling_window",
                        "first_observed_at": row["first_observed_at"],
                        "last_observed_at": row["last_observed_at"],
                    }
        return service, passage

    def position_snapshot_events(self, snapshot_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM passages WHERE snapshot_id=? ORDER BY observed_to,passage_id",
                (snapshot_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def replay_events(
        self,
        *,
        route_id: str,
        service_date: str,
        vehicle_no: str | None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["route_id=?", "service_date=?"]
        params: list[Any] = [route_id, service_date]
        if vehicle_no:
            clauses.append("vehicle_no=?")
            params.append(vehicle_no)
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM passages WHERE " + " AND ".join(clauses)
                + " ORDER BY observed_to LIMIT ?",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            snapshots = connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            observations = connection.execute("SELECT COUNT(*) FROM arrival_observations").fetchone()[0]
            position_snapshots = connection.execute("SELECT COUNT(*) FROM position_snapshots").fetchone()[0]
            passages = connection.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
            catalog_snapshots = connection.execute("SELECT COUNT(*) FROM catalog_snapshots").fetchone()[0]
            mapping_validations = connection.execute("SELECT COUNT(*) FROM mapping_validations").fetchone()[0]
        return {
            "snapshots": snapshots,
            "observations": observations,
            "position_snapshots": position_snapshots,
            "passages": passages,
            "catalog_snapshots": catalog_snapshots,
            "mapping_validations": mapping_validations,
        }

    def create_catalog_snapshot(
        self,
        *,
        snapshot_id: str,
        resource_type: str,
        request_hash: str,
        upstream_hash: str,
        source: str,
        provenance: str,
        captured_at: str,
        query: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        with self.write_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT * FROM catalog_snapshots
                   WHERE resource_type=? AND request_hash=? AND upstream_hash=?""",
                (resource_type, request_hash, upstream_hash),
            ).fetchone()
            if existing:
                connection.commit()
                return self._catalog_snapshot_row(existing), False
            connection.execute(
                """INSERT INTO catalog_snapshots(
                     snapshot_id,resource_type,request_hash,upstream_hash,source,provenance,
                     captured_at,query_json,records_json
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    resource_type,
                    request_hash,
                    upstream_hash,
                    source,
                    provenance,
                    captured_at,
                    _json(query),
                    _json(records),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM catalog_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            return self._catalog_snapshot_row(row), True

    def create_mapping_validation(
        self,
        *,
        validation_id: str,
        request_hash: str,
        upstream_hash: str,
        source: str,
        provenance: str,
        captured_at: str,
        city_code: str,
        route_id: str,
        node_id: str,
        valid: bool,
        match: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        with self.write_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM mapping_validations WHERE request_hash=? AND upstream_hash=?",
                (request_hash, upstream_hash),
            ).fetchone()
            if existing:
                connection.commit()
                return self._mapping_validation_row(existing), False
            connection.execute(
                """INSERT INTO mapping_validations(
                     validation_id,request_hash,upstream_hash,source,provenance,captured_at,
                     city_code,route_id,node_id,valid,node_order,match_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    validation_id,
                    request_hash,
                    upstream_hash,
                    source,
                    provenance,
                    captured_at,
                    city_code,
                    route_id,
                    node_id,
                    1 if valid else 0,
                    match.get("node_order") if match else None,
                    _json(match) if match else None,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM mapping_validations WHERE validation_id=?", (validation_id,)
            ).fetchone()
            return self._mapping_validation_row(row), True

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "snapshot_id": row["snapshot_id"],
            "source": row["source"],
            "city_code": row["city_code"],
            "node_id": row["node_id"],
            "captured_at": row["captured_at"],
            "service_date": row["service_date"],
            "payload_hash": row["payload_hash"],
            "arrivals": json.loads(row["arrivals_json"]),
            "request_hash": row["request_hash"],
        }

    @staticmethod
    def _position_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "snapshot_id": row["snapshot_id"],
            "source": row["source"],
            "city_code": row["city_code"],
            "route_id": row["route_id"],
            "captured_at": row["captured_at"],
            "service_date": row["service_date"],
            "payload_hash": row["payload_hash"],
            "positions": json.loads(row["positions_json"]),
            "request_hash": row["request_hash"],
        }

    @staticmethod
    def _catalog_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "snapshot_id": row["snapshot_id"],
            "resource_type": row["resource_type"],
            "request_hash": row["request_hash"],
            "upstream_hash": row["upstream_hash"],
            "source": row["source"],
            "provenance": row["provenance"],
            "captured_at": row["captured_at"],
            "query": json.loads(row["query_json"]),
            "records": json.loads(row["records_json"]),
        }

    @staticmethod
    def _mapping_validation_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "validation_id": row["validation_id"],
            "request_hash": row["request_hash"],
            "upstream_hash": row["upstream_hash"],
            "source": row["source"],
            "provenance": row["provenance"],
            "captured_at": row["captured_at"],
            "city_code": row["city_code"],
            "route_id": row["route_id"],
            "node_id": row["node_id"],
            "valid": bool(row["valid"]),
            "node_order": row["node_order"],
            "match": json.loads(row["match_json"]) if row["match_json"] else None,
        }
