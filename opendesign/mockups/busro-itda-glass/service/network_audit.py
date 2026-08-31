"""Read-only nationwide route-graph audit for a Busro catalog.

The command intentionally opens the supplied SQLite file with ``mode=ro`` and
``query_only``.  It can therefore inspect the same WAL-backed catalog that an
operator ingest is updating without initializing, migrating, copying, or
otherwise changing that database.  Output contains aggregate evidence and
bounded identifier samples only; source URLs, credentials, and stored error
messages are never emitted.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterator, Mapping, Sequence


MAX_SAMPLE_LIMIT = 100
MAX_CITY_LIMIT = 100
MAX_COMPONENT_STOPS = 500_000
MAX_COMPONENT_PAIR_CHECKS = 25_000_000
DEFAULT_TRANSFER_RADIUS_METERS = 300.0

_REQUIRED_TABLES = frozenset(
    {
        "catalog_meta",
        "catalog_stops",
        "catalog_routes",
        "route_sequence_versions",
        "route_sequence_stops",
        "active_route_sequences",
    }
)
_TOPOLOGY_TABLES = frozenset(
    {
        "topology_targets",
        "topology_progress",
        "topology_runs",
        "topology_discovered_cities",
        "topology_discovery_progress",
    }
)


class NetworkAuditError(RuntimeError):
    """Raised when a supplied path is not a readable Busro catalog."""


@dataclass(frozen=True, slots=True)
class AuditOptions:
    sample_limit: int = 20
    city_limit: int = 10
    components_300m: bool = False
    max_component_stops: int = 250_000

    def validate(self) -> None:
        if not 1 <= self.sample_limit <= MAX_SAMPLE_LIMIT:
            raise NetworkAuditError(
                f"sample_limit must be 1..{MAX_SAMPLE_LIMIT}"
            )
        if not 1 <= self.city_limit <= MAX_CITY_LIMIT:
            raise NetworkAuditError(f"city_limit must be 1..{MAX_CITY_LIMIT}")
        if not 1 <= self.max_component_stops <= MAX_COMPONENT_STOPS:
            raise NetworkAuditError(
                f"max_component_stops must be 1..{MAX_COMPONENT_STOPS}"
            )


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def _dict_rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _group_counts(
    connection: sqlite3.Connection, query: str, parameters: Sequence[Any] = ()
) -> dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(query, parameters).fetchall()
    }


@contextmanager
def open_catalog_read_only(path: Path) -> Iterator[sqlite3.Connection]:
    """Open one consistent SQLite snapshot without creating or mutating files."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise NetworkAuditError("catalog database does not exist or is not a file")
    uri = f"{resolved.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise NetworkAuditError("catalog database could not be opened read-only") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN")
        yield connection
    except sqlite3.Error as exc:
        raise NetworkAuditError("catalog audit query failed") from exc
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def _schema_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _active_sources(connection: sqlite3.Connection) -> dict[str, str | None]:
    values = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT key,value FROM catalog_meta "
            "WHERE key IN ('revision','active_stops_source_id','active_routes_source_id')"
        ).fetchall()
    }
    return {
        "revision": values.get("revision"),
        "active_stops_source_id": values.get("active_stops_source_id"),
        "active_routes_source_id": values.get("active_routes_source_id"),
    }


def _catalog_and_graph_counts(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM catalog_stops
             WHERE source_id=(SELECT value FROM catalog_meta
                              WHERE key='active_stops_source_id')) AS static_stops,
          (SELECT COUNT(*) FROM catalog_routes
             WHERE source_id=(SELECT value FROM catalog_meta
                              WHERE key='active_routes_source_id')) AS static_routes,
          (SELECT COUNT(*) FROM active_route_sequences) AS graph_routes,
          (SELECT COUNT(*) FROM active_route_sequences a
             JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id) AS graph_rows,
          (SELECT COUNT(*) FROM (
             SELECT a.city_code,s.node_id
             FROM active_route_sequences a
             JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
             GROUP BY a.city_code,s.node_id
           )) AS graph_unique_stops
        """
    ).fetchone()
    assert row is not None
    return {key: int(row[key]) for key in row.keys()}


def _route_overlap(
    connection: sqlite3.Connection, sample_limit: int
) -> dict[str, Any]:
    counts = connection.execute(
        """
        WITH catalog AS (
          SELECT city_code,route_id
          FROM catalog_routes
          WHERE source_id=(SELECT value FROM catalog_meta
                           WHERE key='active_routes_source_id')
        ), graph AS (
          SELECT city_code,route_id FROM active_route_sequences
        )
        SELECT
          (SELECT COUNT(*) FROM catalog) AS catalog_routes,
          (SELECT COUNT(*) FROM graph) AS graph_routes,
          (SELECT COUNT(*) FROM catalog c JOIN graph g
             ON g.city_code=c.city_code AND g.route_id=c.route_id) AS exact_overlap,
          (SELECT COUNT(*) FROM catalog c LEFT JOIN graph g
             ON g.city_code=c.city_code AND g.route_id=c.route_id
             WHERE g.route_id IS NULL) AS catalog_missing_from_graph,
          (SELECT COUNT(*) FROM graph g LEFT JOIN catalog c
             ON c.city_code=g.city_code AND c.route_id=g.route_id
             WHERE c.route_id IS NULL) AS graph_missing_from_catalog
        """
    ).fetchone()
    assert counts is not None
    result = {key: int(counts[key]) for key in counts.keys()}
    result["catalog_exact_graph_coverage_ratio"] = _ratio(
        result["exact_overlap"], result["catalog_routes"]
    )
    result["graph_exact_catalog_coverage_ratio"] = _ratio(
        result["exact_overlap"], result["graph_routes"]
    )
    result["catalog_missing_sample"] = _dict_rows(
        connection.execute(
            """
            SELECT c.city_code,c.route_id,c.route_no
            FROM catalog_routes c
            LEFT JOIN active_route_sequences g
              ON g.city_code=c.city_code AND g.route_id=c.route_id
            WHERE c.source_id=(SELECT value FROM catalog_meta
                               WHERE key='active_routes_source_id')
              AND g.route_id IS NULL
            ORDER BY c.city_code,c.route_id
            LIMIT ?
            """,
            (sample_limit,),
        ).fetchall()
    )
    result["graph_extra_sample"] = _dict_rows(
        connection.execute(
            """
            SELECT g.city_code,g.route_id,g.sequence_id
            FROM active_route_sequences g
            LEFT JOIN catalog_routes c
              ON c.source_id=(SELECT value FROM catalog_meta
                              WHERE key='active_routes_source_id')
             AND c.city_code=g.city_code AND c.route_id=g.route_id
            WHERE c.route_id IS NULL
            ORDER BY g.city_code,g.route_id
            LIMIT ?
            """,
            (sample_limit,),
        ).fetchall()
    )
    return result


def _stop_overlap(
    connection: sqlite3.Connection, sample_limit: int
) -> dict[str, Any]:
    counts = connection.execute(
        """
        WITH graph AS (
          SELECT a.city_code,s.node_id
          FROM active_route_sequences a
          JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
          GROUP BY a.city_code,s.node_id
        ), catalog AS (
          SELECT city_code,node_id
          FROM catalog_stops
          WHERE source_id=(SELECT value FROM catalog_meta
                           WHERE key='active_stops_source_id')
        )
        SELECT
          (SELECT COUNT(*) FROM catalog) AS catalog_stops,
          (SELECT COUNT(*) FROM graph) AS graph_unique_stops,
          (SELECT COUNT(*) FROM catalog c JOIN graph g
             ON g.city_code=c.city_code AND g.node_id=c.node_id) AS exact_overlap,
          (SELECT COUNT(*) FROM catalog c LEFT JOIN graph g
             ON g.city_code=c.city_code AND g.node_id=c.node_id
             WHERE g.node_id IS NULL) AS catalog_missing_from_graph,
          (SELECT COUNT(*) FROM graph g LEFT JOIN catalog c
             ON c.city_code=g.city_code AND c.node_id=g.node_id
             WHERE c.node_id IS NULL) AS graph_missing_from_catalog
        """
    ).fetchone()
    assert counts is not None
    result = {key: int(counts[key]) for key in counts.keys()}
    result["catalog_exact_graph_inclusion_ratio"] = _ratio(
        result["exact_overlap"], result["catalog_stops"]
    )
    result["graph_exact_catalog_inclusion_ratio"] = _ratio(
        result["exact_overlap"], result["graph_unique_stops"]
    )
    result["catalog_missing_sample"] = _dict_rows(
        connection.execute(
            """
            WITH graph AS (
              SELECT a.city_code,s.node_id
              FROM active_route_sequences a
              JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
              GROUP BY a.city_code,s.node_id
            )
            SELECT c.city_code,c.node_id,c.node_name
            FROM catalog_stops c
            LEFT JOIN graph g
              ON g.city_code=c.city_code AND g.node_id=c.node_id
            WHERE c.source_id=(SELECT value FROM catalog_meta
                               WHERE key='active_stops_source_id')
              AND g.node_id IS NULL
            ORDER BY c.city_code,c.node_id
            LIMIT ?
            """,
            (sample_limit,),
        ).fetchall()
    )
    result["graph_extra_sample"] = _dict_rows(
        connection.execute(
            """
            WITH graph AS (
              SELECT a.city_code,s.node_id,MIN(s.node_name) AS node_name
              FROM active_route_sequences a
              JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
              GROUP BY a.city_code,s.node_id
            )
            SELECT g.city_code,g.node_id,g.node_name
            FROM graph g
            LEFT JOIN catalog_stops c
              ON c.source_id=(SELECT value FROM catalog_meta
                              WHERE key='active_stops_source_id')
             AND c.city_code=g.city_code AND c.node_id=g.node_id
            WHERE c.node_id IS NULL
            ORDER BY g.city_code,g.node_id
            LIMIT ?
            """,
            (sample_limit,),
        ).fetchall()
    )
    return result


_SEQUENCE_SUMMARY = """
WITH sequence_summary AS (
  SELECT
    a.city_code,
    a.route_id,
    a.sequence_id,
    v.city_code AS version_city_code,
    v.route_id AS version_route_id,
    v.stop_count AS declared_stop_count,
    COUNT(s.node_order) AS actual_stop_count,
    COUNT(DISTINCT s.node_order) AS distinct_order_count,
    MIN(s.node_order) AS min_order,
    MAX(s.node_order) AS max_order,
    SUM(CASE WHEN s.node_order IS NOT NULL AND s.node_order < 0 THEN 1 ELSE 0 END)
      AS negative_order_rows,
    SUM(CASE WHEN s.node_order IS NOT NULL
              AND s.latitude IS NULL AND s.longitude IS NULL THEN 1 ELSE 0 END)
      AS missing_coordinate_rows,
    SUM(CASE WHEN s.node_order IS NOT NULL
              AND ((s.latitude IS NULL) <> (s.longitude IS NULL)) THEN 1 ELSE 0 END)
      AS partial_coordinate_rows,
    SUM(CASE WHEN s.node_order IS NOT NULL AND (
              (s.latitude IS NOT NULL AND (s.latitude < -90 OR s.latitude > 90)) OR
              (s.longitude IS NOT NULL AND (s.longitude < -180 OR s.longitude > 180))
            ) THEN 1 ELSE 0 END) AS out_of_range_coordinate_rows
  FROM active_route_sequences a
  JOIN route_sequence_versions v ON v.sequence_id=a.sequence_id
  LEFT JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
  GROUP BY a.city_code,a.route_id,a.sequence_id,
           v.city_code,v.route_id,v.stop_count
)
"""


def _sequence_integrity(
    connection: sqlite3.Connection, sample_limit: int
) -> dict[str, Any]:
    aggregate = connection.execute(
        _SEQUENCE_SUMMARY
        + """
        SELECT
          COUNT(*) AS sequences,
          SUM(actual_stop_count) AS rows,
          SUM(CASE WHEN actual_stop_count < 2 THEN 1 ELSE 0 END)
            AS fewer_than_two_sequences,
          SUM(CASE WHEN declared_stop_count <> actual_stop_count THEN 1 ELSE 0 END)
            AS declared_count_mismatch_sequences,
          SUM(CASE WHEN distinct_order_count <> actual_stop_count THEN 1 ELSE 0 END)
            AS duplicate_order_sequences,
          SUM(negative_order_rows) AS negative_order_rows,
          SUM(CASE WHEN distinct_order_count <> actual_stop_count OR
                        negative_order_rows > 0 THEN 1 ELSE 0 END)
            AS order_issue_sequences,
          SUM(CASE WHEN city_code <> version_city_code OR route_id <> version_route_id
                   THEN 1 ELSE 0 END) AS active_key_mismatch_sequences,
          SUM(missing_coordinate_rows) AS missing_coordinate_rows,
          SUM(partial_coordinate_rows) AS partial_coordinate_rows,
          SUM(out_of_range_coordinate_rows) AS out_of_range_coordinate_rows,
          SUM(CASE WHEN missing_coordinate_rows + partial_coordinate_rows +
                        out_of_range_coordinate_rows > 0 THEN 1 ELSE 0 END)
            AS sequences_with_coordinate_issues,
          SUM(CASE WHEN actual_stop_count < 2 OR
                        declared_stop_count <> actual_stop_count OR
                        distinct_order_count <> actual_stop_count OR
                        negative_order_rows > 0 OR
                        city_code <> version_city_code OR route_id <> version_route_id OR
                        missing_coordinate_rows + partial_coordinate_rows +
                          out_of_range_coordinate_rows > 0
                   THEN 1 ELSE 0 END) AS anomalous_sequences
        FROM sequence_summary
        """
    ).fetchone()
    assert aggregate is not None
    result = {
        key: int(aggregate[key] or 0)
        for key in aggregate.keys()
    }
    samples = connection.execute(
        _SEQUENCE_SUMMARY
        + """
        SELECT city_code,route_id,sequence_id,declared_stop_count,actual_stop_count,
               min_order,max_order,missing_coordinate_rows,
               partial_coordinate_rows,out_of_range_coordinate_rows,
               CASE WHEN city_code <> version_city_code OR route_id <> version_route_id
                    THEN 1 ELSE 0 END AS active_key_mismatch
        FROM sequence_summary
        WHERE actual_stop_count < 2 OR
              declared_stop_count <> actual_stop_count OR
              distinct_order_count <> actual_stop_count OR
              negative_order_rows > 0 OR
              city_code <> version_city_code OR route_id <> version_route_id OR
              missing_coordinate_rows + partial_coordinate_rows +
                out_of_range_coordinate_rows > 0
        ORDER BY city_code,route_id
        LIMIT ?
        """,
        (sample_limit,),
    ).fetchall()
    result["anomaly_sample"] = _dict_rows(samples)
    return result


def _topology_state(
    connection: sqlite3.Connection, tables: set[str], sample_limit: int
) -> dict[str, Any]:
    missing = sorted(_TOPOLOGY_TABLES - tables)
    if missing:
        return {"available": False, "missing_tables": missing}

    target_counts = _group_counts(
        connection,
        "SELECT provider,COUNT(*) FROM topology_targets GROUP BY provider ORDER BY provider",
    )
    progress_counts = _group_counts(
        connection,
        "SELECT status,COUNT(*) FROM topology_progress GROUP BY status ORDER BY status",
    )
    discovery_counts = _group_counts(
        connection,
        "SELECT status,COUNT(*) FROM topology_discovery_progress "
        "GROUP BY status ORDER BY status",
    )
    failure_row = connection.execute(
        """
        SELECT
          COUNT(*) AS failed_targets,
          COALESCE(SUM(CASE WHEN total_count=0 THEN 1 ELSE 0 END),0)
            AS failed_zero_stop_targets,
          COALESCE(SUM(CASE WHEN total_count=1 THEN 1 ELSE 0 END),0)
            AS failed_one_stop_targets,
          COALESCE(SUM(CASE WHEN attempts<3 THEN 1 ELSE 0 END),0)
            AS retryable_failures,
          COALESCE(SUM(CASE WHEN attempts<3 AND
                                 (error_code IS NULL OR
                                  error_code<>'INVALID_ROUTE_TOPOLOGY')
                            THEN 1 ELSE 0 END),0)
            AS retryable_provider_failures,
          COALESCE(SUM(CASE WHEN attempts<3 AND
                                 error_code='INVALID_ROUTE_TOPOLOGY'
                            THEN 1 ELSE 0 END),0)
            AS retryable_invalid_topology_failures,
          COALESCE(SUM(CASE WHEN attempts>=3 THEN 1 ELSE 0 END),0)
            AS exhausted_failures,
          COALESCE(SUM(CASE WHEN attempts>=3 AND
                                 error_code='INVALID_ROUTE_TOPOLOGY' AND
                                 total_count IN (0,1)
                            THEN 1 ELSE 0 END),0)
            AS terminal_unusable_targets,
          COALESCE(SUM(CASE WHEN attempts>=3 AND (
                                 error_code IS NULL OR
                                 error_code<>'INVALID_ROUTE_TOPOLOGY' OR
                                 total_count IS NULL OR
                                 total_count NOT IN (0,1))
                            THEN 1 ELSE 0 END),0)
            AS exhausted_other_failures
        FROM topology_progress
        WHERE status='FAILED'
        """
    ).fetchone()
    assert failure_row is not None
    failures = {key: int(failure_row[key]) for key in failure_row.keys()}

    if "topology_pages" in tables:
        staging_row = connection.execute(
            """
            WITH pages AS (
              SELECT provider,city_code,route_id,
                     COUNT(*) AS page_count,
                     COUNT(DISTINCT total_count) AS distinct_total_counts,
                     MAX(CASE WHEN page_no=1 THEN total_count END) AS page1_total,
                     MAX(CASE WHEN page_no>1 AND total_count>0 THEN 1 ELSE 0 END)
                       AS later_positive_total,
                     SUM(item_count) AS staged_items,
                     MIN(total_count) AS min_page_total,
                     MAX(total_count) AS max_page_total
              FROM topology_pages
              GROUP BY provider,city_code,route_id
            ), failed AS (
              SELECT p.provider,p.city_code,p.route_id,p.total_count,p.staged_count,
                     COALESCE(g.page_count,0) AS page_count,
                     COALESCE(g.distinct_total_counts,0) AS distinct_total_counts,
                     g.page1_total,COALESCE(g.later_positive_total,0)
                       AS later_positive_total,
                     COALESCE(g.staged_items,0) AS staged_items,
                     g.min_page_total,g.max_page_total
              FROM topology_progress p
              LEFT JOIN pages g USING(provider,city_code,route_id)
              WHERE p.status='FAILED'
            )
            SELECT
              COALESCE(SUM(CASE WHEN page_count=0 THEN 1 ELSE 0 END),0)
                AS failed_targets_without_pages,
              COALESCE(SUM(CASE WHEN distinct_total_counts>1 THEN 1 ELSE 0 END),0)
                AS inconsistent_page_total_targets,
              COALESCE(SUM(CASE WHEN page1_total=0 AND later_positive_total=1
                                THEN 1 ELSE 0 END),0)
                AS page1_zero_later_positive_targets,
              COALESCE(SUM(CASE WHEN staged_items<>staged_count THEN 1 ELSE 0 END),0)
                AS staged_count_mismatch_targets,
              COALESCE(SUM(CASE WHEN page_count>0 AND total_count IS NOT NULL AND
                                     (min_page_total<>total_count OR
                                      max_page_total<>total_count)
                                THEN 1 ELSE 0 END),0)
                AS progress_page_total_mismatch_targets,
              COALESCE(SUM(CASE WHEN distinct_total_counts>1 OR
                                     (page1_total=0 AND later_positive_total=1)
                                THEN 1 ELSE 0 END),0)
                AS mixed_page_corruption_targets
            FROM failed
            """
        ).fetchone()
        assert staging_row is not None
        staging = {
            "available": True,
            **{key: int(staging_row[key]) for key in staging_row.keys()},
        }
    else:
        staging = {"available": False, "reason": "TOPOLOGY_PAGES_TABLE_MISSING"}
    row = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM topology_targets) AS targets,
          (SELECT COUNT(*) FROM topology_progress) AS progress_rows,
          (SELECT COUNT(*) FROM topology_targets t
             LEFT JOIN topology_progress p USING(provider,city_code,route_id)
             WHERE p.route_id IS NULL) AS targets_without_progress,
          (SELECT COUNT(*) FROM topology_progress p
             LEFT JOIN topology_targets t USING(provider,city_code,route_id)
             WHERE t.route_id IS NULL) AS progress_without_target,
          (SELECT COALESCE(SUM(requests_used),0) FROM topology_progress)
            AS progress_requests_used,
          (SELECT COALESCE(SUM(pages_fetched),0) FROM topology_progress)
            AS pages_fetched,
          (SELECT COUNT(*) FROM topology_discovered_cities) AS discovered_cities,
          (SELECT COUNT(*) FROM topology_discovery_progress) AS discovery_scopes
        """
    ).fetchone()
    assert row is not None
    result: dict[str, Any] = {
        "available": True,
        **{key: int(row[key]) for key in row.keys()},
        "targets_by_provider": target_counts,
        "progress_by_status": progress_counts,
        "discovery_by_status": discovery_counts,
        **failures,
        "failed_by_error_code": _group_counts(
            connection,
            "SELECT COALESCE(error_code,'UNCLASSIFIED'),COUNT(*) "
            "FROM topology_progress WHERE status='FAILED' "
            "GROUP BY COALESCE(error_code,'UNCLASSIFIED') ORDER BY 1",
        ),
        "failed_staging_integrity": staging,
    }
    result["incomplete_targets"] = (
        result["targets_without_progress"]
        + sum(
            count
            for status, count in progress_counts.items()
            if status not in {"COMPLETE", "UNCHANGED"}
        )
    )
    discovery_complete = bool(discovery_counts) and all(
        status == "COMPLETE" for status in discovery_counts
    )
    result["discovery_complete"] = discovery_complete
    result["hydrated_targets"] = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM topology_targets t
            JOIN active_route_sequences a
              ON a.city_code=t.city_code AND a.route_id=t.route_id
            """
        ).fetchone()[0]
    )
    result["unhydrated_targets"] = result["targets"] - result["hydrated_targets"]
    result["scan_remaining_targets"] = (
        result["targets_without_progress"]
        + progress_counts.get("PENDING", 0)
        + progress_counts.get("IN_PROGRESS", 0)
        + progress_counts.get("DEFERRED", 0)
        + result["retryable_failures"]
    )
    result["scan_complete"] = (
        discovery_complete
        and result["progress_without_target"] == 0
        and result["scan_remaining_targets"] == 0
    )
    result["nationwide_topology_complete"] = (
        result["scan_complete"]
        and result["failed_targets"] == 0
        and result["hydrated_targets"] == result["targets"]
    )
    result["noncomplete_target_sample"] = _dict_rows(
        connection.execute(
            """
            SELECT t.provider,t.city_code,t.route_id,t.route_no,
                   COALESCE(p.status,'NOT_STARTED') AS status,
                   p.error_code,p.attempts,p.requests_used
            FROM topology_targets t
            LEFT JOIN topology_progress p USING(provider,city_code,route_id)
            WHERE p.status IS NULL OR p.status NOT IN ('COMPLETE','UNCHANGED')
            ORDER BY status,t.provider,t.city_code,t.route_id
            LIMIT ?
            """,
            (sample_limit,),
        ).fetchall()
    )
    # Deliberately omit target source, URLs, and error messages from operator output.
    result["recent_runs"] = _dict_rows(
        connection.execute(
            """
            SELECT run_id,provider,status,request_budget,requests_used,
                   targets_processed,succeeded,unchanged,failed,deferred,
                   started_at,updated_at,finished_at
            FROM topology_runs
            ORDER BY started_at DESC,run_id DESC
            LIMIT ?
            """,
            (sample_limit,),
        ).fetchall()
    )
    return result


def _city_coverage(
    connection: sqlite3.Connection, city_limit: int
) -> dict[str, Any]:
    route_rows = connection.execute(
        """
        WITH catalog AS (
          SELECT city_code,COUNT(*) AS catalog_routes
          FROM catalog_routes
          WHERE source_id=(SELECT value FROM catalog_meta
                           WHERE key='active_routes_source_id')
          GROUP BY city_code
        ), graph AS (
          SELECT city_code,COUNT(*) AS graph_routes
          FROM active_route_sequences GROUP BY city_code
        ), overlap AS (
          SELECT c.city_code,COUNT(*) AS exact_routes
          FROM catalog_routes c JOIN active_route_sequences g
            ON g.city_code=c.city_code AND g.route_id=c.route_id
          WHERE c.source_id=(SELECT value FROM catalog_meta
                             WHERE key='active_routes_source_id')
          GROUP BY c.city_code
        ), cities AS (
          SELECT city_code FROM catalog UNION SELECT city_code FROM graph
        )
        SELECT cities.city_code,
               COALESCE(catalog.catalog_routes,0) AS catalog_routes,
               COALESCE(graph.graph_routes,0) AS graph_routes,
               COALESCE(overlap.exact_routes,0) AS exact_routes
        FROM cities
        LEFT JOIN catalog USING(city_code)
        LEFT JOIN graph USING(city_code)
        LEFT JOIN overlap USING(city_code)
        ORDER BY cities.city_code
        """
    ).fetchall()
    stop_rows = connection.execute(
        """
        WITH catalog AS (
          SELECT city_code,COUNT(*) AS catalog_stops
          FROM catalog_stops
          WHERE source_id=(SELECT value FROM catalog_meta
                           WHERE key='active_stops_source_id')
          GROUP BY city_code
        ), graph_nodes AS (
          SELECT a.city_code,s.node_id
          FROM active_route_sequences a
          JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
          GROUP BY a.city_code,s.node_id
        ), graph AS (
          SELECT city_code,COUNT(*) AS graph_stops
          FROM graph_nodes GROUP BY city_code
        ), overlap AS (
          SELECT c.city_code,COUNT(*) AS exact_stops
          FROM catalog_stops c JOIN graph_nodes g
            ON g.city_code=c.city_code AND g.node_id=c.node_id
          WHERE c.source_id=(SELECT value FROM catalog_meta
                             WHERE key='active_stops_source_id')
          GROUP BY c.city_code
        ), cities AS (
          SELECT city_code FROM catalog UNION SELECT city_code FROM graph
        )
        SELECT cities.city_code,
               COALESCE(catalog.catalog_stops,0) AS catalog_stops,
               COALESCE(graph.graph_stops,0) AS graph_stops,
               COALESCE(overlap.exact_stops,0) AS exact_stops
        FROM cities
        LEFT JOIN catalog USING(city_code)
        LEFT JOIN graph USING(city_code)
        LEFT JOIN overlap USING(city_code)
        ORDER BY cities.city_code
        """
    ).fetchall()

    cities: dict[str, dict[str, Any]] = {}
    for row in route_rows:
        city = cities.setdefault(str(row["city_code"]), {"city_code": str(row["city_code"])})
        city.update({key: int(row[key]) for key in ("catalog_routes", "graph_routes", "exact_routes")})
    for row in stop_rows:
        city = cities.setdefault(str(row["city_code"]), {"city_code": str(row["city_code"])})
        city.update({key: int(row[key]) for key in ("catalog_stops", "graph_stops", "exact_stops")})
    for city in cities.values():
        for key in (
            "catalog_routes", "graph_routes", "exact_routes",
            "catalog_stops", "graph_stops", "exact_stops",
        ):
            city.setdefault(key, 0)
        city["route_coverage_ratio"] = _ratio(
            city["exact_routes"], city["catalog_routes"]
        )
        city["stop_coverage_ratio"] = _ratio(
            city["exact_stops"], city["catalog_stops"]
        )

    route_ranked = [city for city in cities.values() if city["catalog_routes"] > 0]
    stop_ranked = [city for city in cities.values() if city["catalog_stops"] > 0]

    def ranked(items: list[dict[str, Any]], metric: str) -> dict[str, Any]:
        low = sorted(items, key=lambda item: (item[metric], item["city_code"]))
        high = sorted(items, key=lambda item: (-item[metric], item["city_code"]))
        return {
            "sort_metric": metric,
            "eligible_cities": len(items),
            "lowest": low[:city_limit],
            "highest": high[:city_limit],
        }

    return {
        "all_graph_or_catalog_cities": len(cities),
        "route_coverage": ranked(route_ranked, "route_coverage_ratio"),
        "stop_coverage": ranked(stop_ranked, "stop_coverage_ratio"),
    }


def _haversine_meters(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    radius = 6_371_008.8
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(value)))


def _components_300m(
    connection: sqlite3.Connection,
    *,
    enabled: bool,
    max_stops: int,
    known_stop_count: int,
    sample_limit: int,
) -> dict[str, Any]:
    if not enabled:
        return {"computed": False, "reason": "NOT_REQUESTED"}
    if known_stop_count > max_stops:
        return {
            "computed": False,
            "reason": "STOP_LIMIT_EXCEEDED",
            "graph_unique_stops": known_stop_count,
            "max_component_stops": max_stops,
        }

    parent: list[int] = []
    sizes: list[int] = []
    node_indexes: dict[tuple[str, str], int] = {}
    coordinates: dict[int, tuple[float, float]] = {}

    def node_index(key: tuple[str, str]) -> int:
        existing = node_indexes.get(key)
        if existing is not None:
            return existing
        index = len(parent)
        node_indexes[key] = index
        parent.append(index)
        sizes.append(1)
        return index

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> bool:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return False
        if sizes[left_root] < sizes[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        sizes[left_root] += sizes[right_root]
        return True

    previous_sequence: str | None = None
    previous_node: int | None = None
    route_edges = 0
    route_unions = 0
    for row in connection.execute(
        """
        SELECT a.city_code,a.sequence_id,s.node_order,s.node_id,
               s.latitude,s.longitude
        FROM active_route_sequences a
        JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
        ORDER BY a.city_code,a.route_id,a.sequence_id,s.node_order
        """
    ):
        index = node_index((str(row["city_code"]), str(row["node_id"])))
        if row["sequence_id"] == previous_sequence and previous_node is not None:
            route_edges += 1
            if union(previous_node, index):
                route_unions += 1
        else:
            previous_sequence = str(row["sequence_id"])
        previous_node = index
        latitude = row["latitude"]
        longitude = row["longitude"]
        if (
            index not in coordinates
            and latitude is not None
            and longitude is not None
            and -90 <= float(latitude) <= 90
            and -180 <= float(longitude) <= 180
        ):
            coordinates[index] = (float(latitude), float(longitude))

    if len(node_indexes) != known_stop_count:
        return {
            "computed": False,
            "reason": "SNAPSHOT_COUNT_MISMATCH",
            "expected_graph_unique_stops": known_stop_count,
            "observed_graph_unique_stops": len(node_indexes),
        }

    radius = DEFAULT_TRANSFER_RADIUS_METERS
    latitude_cell = radius / 110_574.0
    longitude_cell = radius / 111_320.0
    grid: dict[tuple[int, int], list[int]] = {}
    pair_checks = 0
    within_radius = 0
    transfer_unions = 0
    for index, (latitude, longitude) in coordinates.items():
        latitude_bucket = math.floor(latitude / latitude_cell)
        longitude_bucket = math.floor(longitude / longitude_cell)
        cosine = max(abs(math.cos(math.radians(latitude))), 0.01)
        longitude_span = math.ceil(1.0 / cosine) + 1
        for lat_delta in (-1, 0, 1):
            for lon_delta in range(-longitude_span, longitude_span + 1):
                for other in grid.get(
                    (latitude_bucket + lat_delta, longitude_bucket + lon_delta), ()
                ):
                    pair_checks += 1
                    if pair_checks > MAX_COMPONENT_PAIR_CHECKS:
                        return {
                            "computed": False,
                            "reason": "PAIR_CHECK_LIMIT_EXCEEDED",
                            "graph_unique_stops": len(node_indexes),
                            "coordinate_stops": len(coordinates),
                            "pair_check_limit": MAX_COMPONENT_PAIR_CHECKS,
                        }
                    other_latitude, other_longitude = coordinates[other]
                    if _haversine_meters(
                        latitude,
                        longitude,
                        other_latitude,
                        other_longitude,
                    ) <= radius:
                        within_radius += 1
                        if union(index, other):
                            transfer_unions += 1
        grid.setdefault((latitude_bucket, longitude_bucket), []).append(index)

    component_sizes: dict[int, int] = {}
    for index in range(len(parent)):
        root = find(index)
        component_sizes[root] = component_sizes.get(root, 0) + 1
    largest = sorted(component_sizes.values(), reverse=True)
    return {
        "computed": True,
        "radius_meters": radius,
        "graph_unique_stops": len(node_indexes),
        "coordinate_stops": len(coordinates),
        "route_edges_considered": route_edges,
        "route_unions": route_unions,
        "proximity_pairs_checked": pair_checks,
        "proximity_pairs_within_radius": within_radius,
        "proximity_unions": transfer_unions,
        "component_count": len(component_sizes),
        "singleton_components": sum(size == 1 for size in component_sizes.values()),
        "largest_component_stops": largest[0] if largest else 0,
        "largest_component_sizes": largest[:sample_limit],
    }


def audit_database(
    path: Path, options: AuditOptions | None = None
) -> dict[str, Any]:
    options = options or AuditOptions()
    options.validate()
    resolved = Path(path).expanduser().resolve()
    with open_catalog_read_only(resolved) as connection:
        tables = _schema_tables(connection)
        missing = sorted(_REQUIRED_TABLES - tables)
        if missing:
            raise NetworkAuditError(
                "catalog schema is missing required tables: " + ", ".join(missing)
            )
        sources = _active_sources(connection)
        counts = _catalog_and_graph_counts(connection)
        routes = _route_overlap(connection, options.sample_limit)
        stops = _stop_overlap(connection, options.sample_limit)
        sequences = _sequence_integrity(connection, options.sample_limit)
        topology = _topology_state(connection, tables, options.sample_limit)
        cities = _city_coverage(connection, options.city_limit)
        components = _components_300m(
            connection,
            enabled=options.components_300m,
            max_stops=options.max_component_stops,
            known_stop_count=counts["graph_unique_stops"],
            sample_limit=options.sample_limit,
        )

    topology_incomplete = (
        int(topology.get("incomplete_targets", 0))
        if topology.get("available")
        else 0
    )
    findings = {
        "catalog_routes_missing_graph": routes["catalog_missing_from_graph"],
        "catalog_stops_missing_graph": stops["catalog_missing_from_graph"],
        "sequence_anomalies": sequences["anomalous_sequences"],
        "topology_incomplete_targets": topology_incomplete,
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "path": str(resolved),
            "bytes": resolved.stat().st_size,
            "open_mode": "read_only_snapshot",
        },
        "active_catalog": {
            **sources,
            "static_routes": counts["static_routes"],
            "static_stops": counts["static_stops"],
        },
        "active_graph": {
            "route_sequences": counts["graph_routes"],
            "route_stop_rows": counts["graph_rows"],
            "unique_stops": counts["graph_unique_stops"],
        },
        "exact_route_overlap": routes,
        "exact_stop_overlap": stops,
        "sequence_integrity": sequences,
        "topology": topology,
        "city_coverage": cities,
        "components_300m": components,
        "findings": findings,
        "audit_status": (
            "ISSUES_FOUND" if any(findings.values()) else "PASS"
        ),
        "ok": True,
    }


def _bounded_positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit a nationwide Busro route graph without modifying its SQLite catalog"
    )
    parser.add_argument("--catalog-db", required=True, type=Path)
    parser.add_argument("--sample-limit", type=_bounded_positive, default=20)
    parser.add_argument("--city-limit", type=_bounded_positive, default=10)
    parser.add_argument(
        "--components-300m",
        action="store_true",
        help="also compute route plus 300m-transfer connected components",
    )
    parser.add_argument(
        "--max-component-stops",
        type=_bounded_positive,
        default=250_000,
        help="skip optional component work above this graph-stop count",
    )
    parser.add_argument(
        "--fail-on-anomaly",
        action="store_true",
        help="return exit code 2 when coverage or integrity findings exist",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = audit_database(
            args.catalog_db,
            AuditOptions(
                sample_limit=args.sample_limit,
                city_limit=args.city_limit,
                components_300m=args.components_300m,
                max_component_stops=args.max_component_stops,
            ),
        )
    except NetworkAuditError as exc:
        json.dump(
            {"ok": False, "error": {"code": "NETWORK_AUDIT_FAILED", "message": str(exc)}},
            sys.stderr,
            ensure_ascii=False,
            sort_keys=True,
        )
        sys.stderr.write("\n")
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 2 if args.fail_on_anomaly and result["audit_status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
