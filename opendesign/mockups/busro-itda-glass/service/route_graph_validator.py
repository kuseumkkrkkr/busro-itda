"""Bounded, read-only validation of the active nationwide route graph.

The validator deliberately works at two small state levels:

* one active route sequence at a time for directed-order checks; and
* one exact stop plus a route-level disjoint-set for transfer components.

It never builds the planner's route-stop state graph in Python.  The supplied
SQLite catalog is opened with ``mode=ro`` and a consistent read transaction, so
the command is safe to run while a WAL-backed topology ingest is writing.
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
from typing import Any, Iterator, Sequence

from route_topology_anomalies import single_point_route_spike


EARTH_RADIUS_METERS = 6_371_008.8
DEFAULT_TRANSFER_RADIUS_METERS = 300.0
DEFAULT_COORDINATE_CONFLICT_METERS = 100.0

MAX_SAMPLE_LIMIT = 100
MAX_ACTIVE_ROUTES = 100_000
MAX_ROUTE_STOP_ROWS = 5_000_000
MAX_UNIQUE_STOPS = 1_000_000
MAX_PAIR_CHECKS = 50_000_000
MAX_ROUTES_PER_STOP = 10_000

_REQUIRED_TABLES = frozenset(
    {
        "route_sequence_versions",
        "route_sequence_stops",
        "active_route_sequences",
    }
)


class RouteGraphValidationError(RuntimeError):
    """Raised for invalid input, schema, or an explicitly bounded limit."""


@dataclass(frozen=True, slots=True)
class ValidationOptions:
    sample_limit: int = 20
    transfer_radius_meters: float = DEFAULT_TRANSFER_RADIUS_METERS
    coordinate_conflict_meters: float = DEFAULT_COORDINATE_CONFLICT_METERS
    max_active_routes: int = MAX_ACTIVE_ROUTES
    max_route_stop_rows: int = MAX_ROUTE_STOP_ROWS
    max_unique_stops: int = MAX_UNIQUE_STOPS
    max_pair_checks: int = MAX_PAIR_CHECKS
    max_routes_per_stop: int = MAX_ROUTES_PER_STOP

    def validate(self) -> None:
        integer_bounds = (
            ("sample_limit", self.sample_limit, 1, MAX_SAMPLE_LIMIT),
            ("max_active_routes", self.max_active_routes, 1, MAX_ACTIVE_ROUTES),
            ("max_route_stop_rows", self.max_route_stop_rows, 1, MAX_ROUTE_STOP_ROWS),
            ("max_unique_stops", self.max_unique_stops, 1, MAX_UNIQUE_STOPS),
            ("max_pair_checks", self.max_pair_checks, 1, MAX_PAIR_CHECKS),
            ("max_routes_per_stop", self.max_routes_per_stop, 1, MAX_ROUTES_PER_STOP),
        )
        for name, value, minimum, maximum in integer_bounds:
            if not minimum <= value <= maximum:
                raise RouteGraphValidationError(
                    f"{name} must be {minimum}..{maximum}"
                )
        if not 1.0 <= self.transfer_radius_meters <= 2_000.0:
            raise RouteGraphValidationError(
                "transfer_radius_meters must be 1..2000"
            )
        if not 1.0 <= self.coordinate_conflict_meters <= 10_000.0:
            raise RouteGraphValidationError(
                "coordinate_conflict_meters must be 1..10000"
            )


@contextmanager
def open_catalog_read_only(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a consistent query-only snapshot without creating any file."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise RouteGraphValidationError(
            "catalog database does not exist or is not a file"
        )
    try:
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise RouteGraphValidationError(
            "catalog database could not be opened read-only"
        ) from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN")
        yield connection
    except sqlite3.Error as exc:
        raise RouteGraphValidationError("catalog validation query failed") from exc
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


class _DisjointSet:
    __slots__ = ("parent", "size", "unions")

    def __init__(self, count: int):
        self.parent = list(range(count))
        self.size = [1] * count
        self.unions = 0

    def find(self, index: int) -> int:
        parent = self.parent
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(self, left: int, right: int) -> bool:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return False
        if self.size[left_root] < self.size[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        self.size[left_root] += self.size[right_root]
        self.unions += 1
        return True

    def component_summary(self, sample_limit: int) -> dict[str, Any]:
        sizes: dict[int, int] = {}
        for index in range(len(self.parent)):
            root = self.find(index)
            sizes[root] = sizes.get(root, 0) + 1
        ordered = sorted(sizes.values(), reverse=True)
        connected_pairs = sum(size * (size - 1) // 2 for size in ordered)
        return {
            "component_count": len(ordered),
            "singleton_routes": sum(size == 1 for size in ordered),
            "largest_component_routes": ordered[0] if ordered else 0,
            "largest_component_sizes": ordered[:sample_limit],
            "route_pairs_in_same_component": connected_pairs,
            "successful_unions": self.unions,
        }


@dataclass(frozen=True, slots=True)
class _RouteRef:
    city_code: str
    route_id: str
    sequence_id: str
    declared_stop_count: int | None
    version_key_matches: bool


@dataclass(slots=True)
class _SequenceState:
    actual_count: int = 0
    first_order: int | None = None
    previous_order: int | None = None
    previous_node_id: str | None = None
    previous_coordinate: tuple[float, float] | None = None
    previous_direction: str | None = None
    prior_boardable: int = 0
    direction_prior_boardable: int = 0
    forward_od_pairs: int = 0
    direction_scoped_forward_od_pairs: int = 0
    edge_count: int = 0
    order_gap_edges: int = 0
    same_direction_order_gap_edges: int = 0
    direction_boundary_order_gap_edges: int = 0
    non_increasing_edges: int = 0
    negative_order_rows: int = 0
    repeated_node_rows: int = 0
    self_loop_edges: int = 0
    missing_coordinate_rows: int = 0
    partial_coordinate_rows: int = 0
    out_of_range_coordinate_rows: int = 0
    direction_change_edges: int = 0
    direction_boundaries_over_300m: int = 0
    direction_boundaries_over_1km: int = 0
    direction_boundaries_over_5km: int = 0
    segments_over_5km: int = 0
    segments_over_20km: int = 0
    single_point_route_spikes: int = 0
    penultimate_point: dict[str, Any] | None = None
    previous_point: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _StopPoint:
    route_anchor: int
    route_count: int
    city_code: str
    node_id: str
    latitude: float
    longitude: float
    unit_x: float
    unit_y: float
    unit_z: float


def _valid_coordinate(latitude: Any, longitude: Any) -> tuple[float, float] | None:
    if latitude is None or longitude is None:
        return None
    latitude = float(latitude)
    longitude = float(longitude)
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    return latitude, longitude


def _unit_vector(latitude: float, longitude: float) -> tuple[float, float, float]:
    latitude_radians = math.radians(latitude)
    longitude_radians = math.radians(longitude)
    cosine = math.cos(latitude_radians)
    return (
        cosine * math.cos(longitude_radians),
        cosine * math.sin(longitude_radians),
        math.sin(latitude_radians),
    )


def _haversine_meters(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    latitude_a_radians = math.radians(latitude_a)
    latitude_b_radians = math.radians(latitude_b)
    delta_latitude = latitude_b_radians - latitude_a_radians
    delta_longitude = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_latitude / 2.0) ** 2
        + math.cos(latitude_a_radians)
        * math.cos(latitude_b_radians)
        * math.sin(delta_longitude / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_METERS * math.asin(min(1.0, math.sqrt(value)))


def _append_sample(samples: list[dict[str, Any]], value: dict[str, Any], limit: int) -> None:
    if len(samples) < limit:
        samples.append(value)


def _schema_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _load_routes(
    connection: sqlite3.Connection, options: ValidationOptions
) -> tuple[list[_RouteRef], dict[tuple[str, str, str], int], int]:
    count_row = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM active_route_sequences) AS active_routes,
          (SELECT COUNT(*) FROM active_route_sequences a
             JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id)
             AS route_stop_rows
        """
    ).fetchone()
    assert count_row is not None
    route_count = int(count_row["active_routes"])
    route_stop_rows = int(count_row["route_stop_rows"])
    if route_count > options.max_active_routes:
        raise RouteGraphValidationError(
            f"active route limit exceeded: {route_count}>{options.max_active_routes}"
        )
    if route_stop_rows > options.max_route_stop_rows:
        raise RouteGraphValidationError(
            "route-stop row limit exceeded: "
            f"{route_stop_rows}>{options.max_route_stop_rows}"
        )

    routes: list[_RouteRef] = []
    indexes: dict[tuple[str, str, str], int] = {}
    for row in connection.execute(
        """
        SELECT a.city_code,a.route_id,a.sequence_id,
               v.city_code AS version_city_code,
               v.route_id AS version_route_id,
               v.stop_count AS declared_stop_count
        FROM active_route_sequences a
        LEFT JOIN route_sequence_versions v ON v.sequence_id=a.sequence_id
        ORDER BY a.city_code,a.route_id,a.sequence_id
        """
    ):
        city_code = str(row["city_code"])
        route_id = str(row["route_id"])
        sequence_id = str(row["sequence_id"])
        version_city = row["version_city_code"]
        version_route = row["version_route_id"]
        route = _RouteRef(
            city_code=city_code,
            route_id=route_id,
            sequence_id=sequence_id,
            declared_stop_count=(
                None
                if row["declared_stop_count"] is None
                else int(row["declared_stop_count"])
            ),
            version_key_matches=(
                version_city is not None
                and str(version_city) == city_code
                and str(version_route) == route_id
            ),
        )
        indexes[(city_code, route_id, sequence_id)] = len(routes)
        routes.append(route)
    if len(routes) != route_count:
        raise RouteGraphValidationError("active route count changed inside snapshot")
    return routes, indexes, route_stop_rows


def _sequence_validation(
    connection: sqlite3.Connection,
    routes: list[_RouteRef],
    route_indexes: dict[tuple[str, str, str], int],
    expected_rows: int,
    options: ValidationOptions,
) -> dict[str, Any]:
    states = [_SequenceState() for _ in routes]
    seen_nodes: set[str] = set()
    current_route_index: int | None = None
    traversed_rows = 0
    large_segment_sample: list[dict[str, Any]] = []
    direction_boundary_sample: list[dict[str, Any]] = []
    direction_boundary_over_5km_sample: list[dict[str, Any]] = []
    direction_code_rows: dict[str, int] = {}
    direction_transition_counts: dict[str, int] = {}
    single_point_spike_sample: list[dict[str, Any]] = []

    for row in connection.execute(
        """
        SELECT a.city_code,a.route_id,a.sequence_id,
               s.node_order,s.node_id,s.latitude,s.longitude,
               s.direction,s.can_board,s.can_alight
        FROM active_route_sequences a
        JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
        ORDER BY a.city_code,a.route_id,a.sequence_id,s.node_order
        """
    ):
        key = (str(row["city_code"]), str(row["route_id"]), str(row["sequence_id"]))
        route_index = route_indexes[key]
        state = states[route_index]
        if current_route_index != route_index:
            seen_nodes.clear()
            current_route_index = route_index

        traversed_rows += 1
        state.actual_count += 1
        node_order = int(row["node_order"])
        node_id = str(row["node_id"])
        direction = str(row["direction"] or "").strip()
        direction_label = direction or "<blank>"
        direction_code_rows[direction_label] = (
            direction_code_rows.get(direction_label, 0) + 1
        )
        point = {
            "node_order": node_order,
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "direction": direction,
        }
        if state.penultimate_point is not None and state.previous_point is not None:
            spike = single_point_route_spike(
                state.penultimate_point,
                state.previous_point,
                point,
            )
            if spike is not None:
                state.single_point_route_spikes += 1
                route = routes[route_index]
                _append_sample(
                    single_point_spike_sample,
                    {
                        "city_code": route.city_code,
                        "route_id": route.route_id,
                        "previous_order": spike.previous_order,
                        "middle_order": spike.middle_order,
                        "following_order": spike.following_order,
                        "previous_to_middle_meters": round(
                            spike.previous_to_middle_meters, 1
                        ),
                        "middle_to_following_meters": round(
                            spike.middle_to_following_meters, 1
                        ),
                        "previous_to_following_meters": round(
                            spike.previous_to_following_meters, 1
                        ),
                    },
                    options.sample_limit,
                )
        state.penultimate_point = state.previous_point
        state.previous_point = point
        if state.first_order is None:
            state.first_order = node_order
        if node_order < 0:
            state.negative_order_rows += 1
        if state.previous_order is not None:
            state.edge_count += 1
            direction_changed = bool(
                direction
                and state.previous_direction
                and direction != state.previous_direction
            )
            if node_order <= state.previous_order:
                state.non_increasing_edges += 1
            elif node_order != state.previous_order + 1:
                state.order_gap_edges += 1
                if direction_changed:
                    state.direction_boundary_order_gap_edges += 1
                else:
                    state.same_direction_order_gap_edges += 1
            if node_id == state.previous_node_id:
                state.self_loop_edges += 1
            if direction_changed:
                state.direction_change_edges += 1
                state.direction_prior_boardable = 0
                transition_label = f"{state.previous_direction}->{direction}"
                direction_transition_counts[transition_label] = (
                    direction_transition_counts.get(transition_label, 0) + 1
                )
        else:
            direction_changed = False

        if node_id in seen_nodes:
            state.repeated_node_rows += 1
        else:
            seen_nodes.add(node_id)

        latitude = row["latitude"]
        longitude = row["longitude"]
        if latitude is None and longitude is None:
            state.missing_coordinate_rows += 1
            coordinate = None
        elif latitude is None or longitude is None:
            state.partial_coordinate_rows += 1
            coordinate = None
        else:
            coordinate = _valid_coordinate(latitude, longitude)
            if coordinate is None:
                state.out_of_range_coordinate_rows += 1

        if coordinate is not None and state.previous_coordinate is not None:
            distance = _haversine_meters(
                state.previous_coordinate[0],
                state.previous_coordinate[1],
                coordinate[0],
                coordinate[1],
            )
            if distance > 5_000.0:
                state.segments_over_5km += 1
            if distance > 20_000.0:
                state.segments_over_20km += 1
                route = routes[route_index]
                _append_sample(
                    large_segment_sample,
                    {
                        "city_code": route.city_code,
                        "route_id": route.route_id,
                        "from_order": state.previous_order,
                        "to_order": node_order,
                        "distance_meters": round(distance, 1),
                    },
                    options.sample_limit,
                )
            if direction_changed:
                if distance > 300.0:
                    state.direction_boundaries_over_300m += 1
                if distance > 1_000.0:
                    state.direction_boundaries_over_1km += 1
                if distance > 5_000.0:
                    state.direction_boundaries_over_5km += 1
                route = routes[route_index]
                _append_sample(
                    direction_boundary_sample,
                    {
                        "city_code": route.city_code,
                        "route_id": route.route_id,
                        "from_order": state.previous_order,
                        "to_order": node_order,
                        "from_direction": state.previous_direction,
                        "to_direction": direction,
                        "distance_meters": round(distance, 1),
                    },
                    options.sample_limit,
                )
                if distance > 5_000.0:
                    _append_sample(
                        direction_boundary_over_5km_sample,
                        {
                            "city_code": route.city_code,
                            "route_id": route.route_id,
                            "from_order": state.previous_order,
                            "to_order": node_order,
                            "from_node_id": state.previous_node_id,
                            "to_node_id": node_id,
                            "from_direction": state.previous_direction,
                            "to_direction": direction,
                            "distance_meters": round(distance, 1),
                        },
                        options.sample_limit,
                    )

        if bool(row["can_alight"]):
            state.forward_od_pairs += state.prior_boardable
            state.direction_scoped_forward_od_pairs += (
                state.direction_prior_boardable
            )
        if bool(row["can_board"]):
            state.prior_boardable += 1
            state.direction_prior_boardable += 1

        state.previous_order = node_order
        state.previous_node_id = node_id
        state.previous_coordinate = coordinate
        state.previous_direction = direction or None

    if traversed_rows != expected_rows:
        raise RouteGraphValidationError("route-stop row count changed inside snapshot")

    counters = {
        "active_routes": len(routes),
        "route_stop_rows_traversed": traversed_rows,
        "forward_order_edges": 0,
        "forward_reachable_od_pairs": 0,
        "direction_scoped_forward_od_pairs": 0,
        "cross_direction_od_pairs_in_linear_chain": 0,
        "routes_with_forward_od_pair": 0,
        "routes_without_forward_od_pair": 0,
        "routes_with_direction_scoped_forward_od_pair": 0,
        "routes_without_direction_scoped_forward_od_pair": 0,
        "routes_with_only_cross_direction_od_pairs": 0,
        "routes_with_fewer_than_two_stops": 0,
        "missing_version_routes": 0,
        "active_version_key_mismatch_routes": 0,
        "declared_count_mismatch_routes": 0,
        "routes_with_non_increasing_order": 0,
        "routes_with_order_gaps": 0,
        "routes_with_same_direction_order_gaps": 0,
        "routes_with_direction_boundary_order_gaps": 0,
        "negative_order_rows": 0,
        "routes_with_repeated_nodes": 0,
        "repeated_node_rows": 0,
        "self_loop_edges": 0,
        "missing_coordinate_rows": 0,
        "partial_coordinate_rows": 0,
        "out_of_range_coordinate_rows": 0,
        "routes_with_coordinate_issues": 0,
        "direction_change_edges": 0,
        "routes_with_direction_changes": 0,
        "direction_boundaries_over_300m": 0,
        "direction_boundaries_over_1km": 0,
        "direction_boundaries_over_5km": 0,
        "routes_with_direction_boundary_over_300m": 0,
        "routes_with_cross_direction_od_pairs": 0,
        "segments_over_5km": 0,
        "segments_over_20km": 0,
        "single_point_route_spikes": 0,
        "routes_with_single_point_route_spikes": 0,
    }
    anomaly_sample: list[dict[str, Any]] = []
    repeated_node_sample: list[dict[str, Any]] = []
    direction_change_sample: list[dict[str, Any]] = []

    for route, state in zip(routes, states):
        counters["forward_order_edges"] += state.edge_count
        counters["forward_reachable_od_pairs"] += state.forward_od_pairs
        counters["direction_scoped_forward_od_pairs"] += (
            state.direction_scoped_forward_od_pairs
        )
        cross_direction_pairs = (
            state.forward_od_pairs - state.direction_scoped_forward_od_pairs
        )
        counters["cross_direction_od_pairs_in_linear_chain"] += (
            cross_direction_pairs
        )
        counters["negative_order_rows"] += state.negative_order_rows
        counters["repeated_node_rows"] += state.repeated_node_rows
        counters["self_loop_edges"] += state.self_loop_edges
        counters["missing_coordinate_rows"] += state.missing_coordinate_rows
        counters["partial_coordinate_rows"] += state.partial_coordinate_rows
        counters["out_of_range_coordinate_rows"] += state.out_of_range_coordinate_rows
        counters["direction_change_edges"] += state.direction_change_edges
        counters["direction_boundaries_over_300m"] += (
            state.direction_boundaries_over_300m
        )
        counters["direction_boundaries_over_1km"] += (
            state.direction_boundaries_over_1km
        )
        counters["direction_boundaries_over_5km"] += (
            state.direction_boundaries_over_5km
        )
        counters["segments_over_5km"] += state.segments_over_5km
        counters["segments_over_20km"] += state.segments_over_20km
        counters["single_point_route_spikes"] += state.single_point_route_spikes

        if state.forward_od_pairs:
            counters["routes_with_forward_od_pair"] += 1
        else:
            counters["routes_without_forward_od_pair"] += 1
        if state.direction_scoped_forward_od_pairs:
            counters["routes_with_direction_scoped_forward_od_pair"] += 1
        else:
            counters["routes_without_direction_scoped_forward_od_pair"] += 1
            if state.forward_od_pairs:
                counters["routes_with_only_cross_direction_od_pairs"] += 1
        if state.actual_count < 2:
            counters["routes_with_fewer_than_two_stops"] += 1
        if route.declared_stop_count is None:
            counters["missing_version_routes"] += 1
        if not route.version_key_matches:
            counters["active_version_key_mismatch_routes"] += 1
        if (
            route.declared_stop_count is None
            or route.declared_stop_count != state.actual_count
        ):
            counters["declared_count_mismatch_routes"] += 1
        if state.non_increasing_edges:
            counters["routes_with_non_increasing_order"] += 1
        if state.order_gap_edges:
            counters["routes_with_order_gaps"] += 1
        if state.same_direction_order_gap_edges:
            counters["routes_with_same_direction_order_gaps"] += 1
        if state.direction_boundary_order_gap_edges:
            counters["routes_with_direction_boundary_order_gaps"] += 1
        if state.repeated_node_rows:
            counters["routes_with_repeated_nodes"] += 1
            _append_sample(
                repeated_node_sample,
                {
                    "city_code": route.city_code,
                    "route_id": route.route_id,
                    "repeated_node_rows": state.repeated_node_rows,
                    "self_loop_edges": state.self_loop_edges,
                },
                options.sample_limit,
            )
        coordinate_issues = (
            state.missing_coordinate_rows
            + state.partial_coordinate_rows
            + state.out_of_range_coordinate_rows
        )
        if coordinate_issues:
            counters["routes_with_coordinate_issues"] += 1
        if state.direction_change_edges:
            counters["routes_with_direction_changes"] += 1
            _append_sample(
                direction_change_sample,
                {
                    "city_code": route.city_code,
                    "route_id": route.route_id,
                    "direction_change_edges": state.direction_change_edges,
                },
                options.sample_limit,
            )
        if state.direction_boundaries_over_300m:
            counters["routes_with_direction_boundary_over_300m"] += 1
        if cross_direction_pairs:
            counters["routes_with_cross_direction_od_pairs"] += 1
        if state.single_point_route_spikes:
            counters["routes_with_single_point_route_spikes"] += 1

        hard_anomaly = (
            state.actual_count < 2
            or route.declared_stop_count is None
            or route.declared_stop_count != state.actual_count
            or not route.version_key_matches
            or bool(state.non_increasing_edges)
            or bool(state.same_direction_order_gap_edges)
            or bool(state.negative_order_rows)
            or coordinate_issues > 0
            or state.forward_od_pairs == 0
            or bool(state.single_point_route_spikes)
        )
        if hard_anomaly:
            _append_sample(
                anomaly_sample,
                {
                    "city_code": route.city_code,
                    "route_id": route.route_id,
                    "declared_stops": route.declared_stop_count,
                    "actual_stops": state.actual_count,
                    "forward_od_pairs": state.forward_od_pairs,
                    "order_gap_edges": state.order_gap_edges,
                    "same_direction_order_gap_edges": (
                        state.same_direction_order_gap_edges
                    ),
                    "non_increasing_edges": state.non_increasing_edges,
                    "coordinate_issue_rows": coordinate_issues,
                    "active_version_key_matches": route.version_key_matches,
                    "single_point_route_spikes": state.single_point_route_spikes,
                },
                options.sample_limit,
            )

    counters["anomaly_sample"] = anomaly_sample
    counters["repeated_node_sample"] = repeated_node_sample
    counters["direction_change_sample"] = direction_change_sample
    counters["direction_boundary_sample"] = direction_boundary_sample
    counters["direction_boundary_over_5km_sample"] = (
        direction_boundary_over_5km_sample
    )
    counters["direction_code_rows"] = dict(sorted(direction_code_rows.items()))
    counters["direction_transition_counts"] = dict(
        sorted(direction_transition_counts.items())
    )
    counters["large_segment_sample"] = large_segment_sample
    counters["single_point_spike_sample"] = single_point_spike_sample
    return counters


def _finalize_stop_group(
    *,
    city_code: str,
    node_id: str,
    route_indexes: set[int],
    coordinate_sum: list[float],
    coordinate_count: int,
    coordinate_anchor: tuple[float, float] | None,
    max_anchor_distance: float,
    exact_dsu: _DisjointSet,
    proximity_dsu: _DisjointSet,
    grid: dict[tuple[int, int, int], list[_StopPoint]],
    chord_cell_size: float,
    options: ValidationOptions,
    metrics: dict[str, Any],
) -> None:
    metrics["unique_exact_stops"] += 1
    if metrics["unique_exact_stops"] > options.max_unique_stops:
        raise RouteGraphValidationError(
            "unique stop limit exceeded: "
            f"{metrics['unique_exact_stops']}>{options.max_unique_stops}"
        )
    if len(route_indexes) > options.max_routes_per_stop:
        raise RouteGraphValidationError(
            "routes-per-stop limit exceeded at one exact stop: "
            f"{len(route_indexes)}>{options.max_routes_per_stop}"
        )
    if not route_indexes:
        return

    ordered_routes = sorted(route_indexes)
    route_anchor = ordered_routes[0]
    if len(ordered_routes) > 1:
        metrics["exact_shared_stop_groups"] += 1
        metrics["exact_route_pair_incidences"] += (
            len(ordered_routes) * (len(ordered_routes) - 1) // 2
        )
        for route_index in ordered_routes[1:]:
            exact_dsu.union(route_anchor, route_index)
            proximity_dsu.union(route_anchor, route_index)

    if coordinate_count == 0:
        metrics["exact_stops_without_valid_coordinate"] += 1
        return
    metrics["exact_stops_with_valid_coordinate"] += 1
    if max_anchor_distance > options.coordinate_conflict_meters:
        metrics["coordinate_conflict_stop_groups"] += 1
        _append_sample(
            metrics["coordinate_conflict_sample"],
            {
                "city_code": city_code,
                "node_id": node_id,
                "max_distance_from_first_meters": round(max_anchor_distance, 1),
                "coordinate_rows": coordinate_count,
            },
            options.sample_limit,
        )

    unit_length = math.sqrt(sum(value * value for value in coordinate_sum))
    if unit_length < 1e-12:
        assert coordinate_anchor is not None
        latitude, longitude = coordinate_anchor
        unit_x, unit_y, unit_z = _unit_vector(latitude, longitude)
    else:
        unit_x, unit_y, unit_z = (
            coordinate_sum[0] / unit_length,
            coordinate_sum[1] / unit_length,
            coordinate_sum[2] / unit_length,
        )
        latitude = math.degrees(math.asin(max(-1.0, min(1.0, unit_z))))
        longitude = math.degrees(math.atan2(unit_y, unit_x))

    point = _StopPoint(
        route_anchor=route_anchor,
        route_count=len(ordered_routes),
        city_code=city_code,
        node_id=node_id,
        latitude=latitude,
        longitude=longitude,
        unit_x=unit_x,
        unit_y=unit_y,
        unit_z=unit_z,
    )
    bucket = (
        math.floor(unit_x / chord_cell_size),
        math.floor(unit_y / chord_cell_size),
        math.floor(unit_z / chord_cell_size),
    )
    for x_delta in (-1, 0, 1):
        for y_delta in (-1, 0, 1):
            for z_delta in (-1, 0, 1):
                nearby = grid.get(
                    (
                        bucket[0] + x_delta,
                        bucket[1] + y_delta,
                        bucket[2] + z_delta,
                    ),
                    (),
                )
                for other in nearby:
                    metrics["proximity_pair_checks"] += 1
                    if metrics["proximity_pair_checks"] > options.max_pair_checks:
                        raise RouteGraphValidationError(
                            "proximity pair-check limit exceeded: "
                            f"{metrics['proximity_pair_checks']}>{options.max_pair_checks}"
                        )
                    if _haversine_meters(
                        latitude,
                        longitude,
                        other.latitude,
                        other.longitude,
                    ) > options.transfer_radius_meters:
                        continue
                    metrics["proximity_stop_pairs_within_radius"] += 1
                    if city_code != other.city_code:
                        metrics["cross_city_proximity_stop_pairs"] += 1
                    if proximity_dsu.union(route_anchor, other.route_anchor):
                        metrics["proximity_route_component_unions"] += 1
    grid.setdefault(bucket, []).append(point)


def _transfer_validation(
    connection: sqlite3.Connection,
    routes: list[_RouteRef],
    route_indexes: dict[tuple[str, str, str], int],
    expected_rows: int,
    options: ValidationOptions,
) -> dict[str, Any]:
    exact_dsu = _DisjointSet(len(routes))
    proximity_dsu = _DisjointSet(len(routes))
    # A chord on the unit sphere is globally safe for a 3-D 27-cell neighbor
    # search: any two points within the radius differ by no more than one cell
    # in each Cartesian component.
    chord_cell_size = 2.0 * math.sin(
        options.transfer_radius_meters / (2.0 * EARTH_RADIUS_METERS)
    )
    grid: dict[tuple[int, int, int], list[_StopPoint]] = {}
    metrics: dict[str, Any] = {
        "route_stop_rows_traversed": 0,
        "unique_exact_stops": 0,
        "exact_shared_stop_groups": 0,
        "exact_route_pair_incidences": 0,
        "exact_stops_with_valid_coordinate": 0,
        "exact_stops_without_valid_coordinate": 0,
        "coordinate_conflict_stop_groups": 0,
        "coordinate_conflict_sample": [],
        "proximity_pair_checks": 0,
        "proximity_stop_pairs_within_radius": 0,
        "cross_city_proximity_stop_pairs": 0,
        "proximity_route_component_unions": 0,
    }

    current_key: tuple[str, str] | None = None
    current_routes: set[int] = set()
    coordinate_sum = [0.0, 0.0, 0.0]
    coordinate_count = 0
    coordinate_anchor: tuple[float, float] | None = None
    max_anchor_distance = 0.0

    def flush() -> None:
        nonlocal current_routes, coordinate_sum, coordinate_count
        nonlocal coordinate_anchor, max_anchor_distance
        if current_key is None:
            return
        _finalize_stop_group(
            city_code=current_key[0],
            node_id=current_key[1],
            route_indexes=current_routes,
            coordinate_sum=coordinate_sum,
            coordinate_count=coordinate_count,
            coordinate_anchor=coordinate_anchor,
            max_anchor_distance=max_anchor_distance,
            exact_dsu=exact_dsu,
            proximity_dsu=proximity_dsu,
            grid=grid,
            chord_cell_size=chord_cell_size,
            options=options,
            metrics=metrics,
        )
        current_routes = set()
        coordinate_sum = [0.0, 0.0, 0.0]
        coordinate_count = 0
        coordinate_anchor = None
        max_anchor_distance = 0.0

    for row in connection.execute(
        """
        SELECT a.city_code,a.route_id,a.sequence_id,
               s.node_id,s.latitude,s.longitude
        FROM active_route_sequences a
        JOIN route_sequence_stops s ON s.sequence_id=a.sequence_id
        ORDER BY a.city_code,s.node_id,a.route_id,a.sequence_id,s.node_order
        """
    ):
        key = (str(row["city_code"]), str(row["node_id"]))
        if current_key != key:
            flush()
            current_key = key
        route_key = (
            str(row["city_code"]),
            str(row["route_id"]),
            str(row["sequence_id"]),
        )
        current_routes.add(route_indexes[route_key])
        metrics["route_stop_rows_traversed"] += 1
        coordinate = _valid_coordinate(row["latitude"], row["longitude"])
        if coordinate is None:
            continue
        if coordinate_anchor is None:
            coordinate_anchor = coordinate
        else:
            max_anchor_distance = max(
                max_anchor_distance,
                _haversine_meters(
                    coordinate_anchor[0],
                    coordinate_anchor[1],
                    coordinate[0],
                    coordinate[1],
                ),
            )
        vector = _unit_vector(coordinate[0], coordinate[1])
        coordinate_sum[0] += vector[0]
        coordinate_sum[1] += vector[1]
        coordinate_sum[2] += vector[2]
        coordinate_count += 1
    flush()

    if metrics["route_stop_rows_traversed"] != expected_rows:
        raise RouteGraphValidationError("route-stop row count changed inside snapshot")

    exact_components = exact_dsu.component_summary(options.sample_limit)
    proximity_components = proximity_dsu.component_summary(options.sample_limit)
    metrics["component_semantics"] = (
        "undirected physical route connectivity; boarding/alighting reachability "
        "is audited separately"
    )
    metrics["transfer_radius_meters"] = options.transfer_radius_meters
    metrics["exact_components"] = exact_components
    metrics["components_with_proximity"] = proximity_components
    metrics["route_pairs_connected_only_by_proximity"] = (
        proximity_components["route_pairs_in_same_component"]
        - exact_components["route_pairs_in_same_component"]
    )
    return metrics


def validate_database(
    path: Path, options: ValidationOptions | None = None
) -> dict[str, Any]:
    options = options or ValidationOptions()
    options.validate()
    resolved = Path(path).expanduser().resolve()
    with open_catalog_read_only(resolved) as connection:
        missing_tables = sorted(_REQUIRED_TABLES - _schema_tables(connection))
        if missing_tables:
            raise RouteGraphValidationError(
                "catalog schema is missing required tables: "
                + ", ".join(missing_tables)
            )
        routes, route_indexes, row_count = _load_routes(connection, options)
        sequences = _sequence_validation(
            connection,
            routes,
            route_indexes,
            row_count,
            options,
        )
        transfers = _transfer_validation(
            connection,
            routes,
            route_indexes,
            row_count,
            options,
        )

    hard_findings = {
        "routes_with_fewer_than_two_stops": sequences[
            "routes_with_fewer_than_two_stops"
        ],
        "missing_version_routes": sequences["missing_version_routes"],
        "active_version_key_mismatch_routes": sequences[
            "active_version_key_mismatch_routes"
        ],
        "declared_count_mismatch_routes": sequences[
            "declared_count_mismatch_routes"
        ],
        "routes_with_non_increasing_order": sequences[
            "routes_with_non_increasing_order"
        ],
        "negative_order_rows": sequences["negative_order_rows"],
        "routes_without_forward_od_pair": sequences[
            "routes_without_forward_od_pair"
        ],
        "routes_with_coordinate_issues": sequences[
            "routes_with_coordinate_issues"
        ],
        "coordinate_conflict_stop_groups": transfers[
            "coordinate_conflict_stop_groups"
        ],
        "routes_with_single_point_route_spikes": sequences[
            "routes_with_single_point_route_spikes"
        ],
    }
    warnings = {
        # Several official providers publish sparse order labels (for example
        # 1, 6, 8) while preserving a complete, strictly increasing row
        # sequence. The planner uses row positions, so gaps are diagnostic and
        # only non-increasing order remains a hard integrity failure.
        "routes_with_same_direction_order_gaps": sequences[
            "routes_with_same_direction_order_gaps"
        ],
        "routes_with_repeated_nodes": sequences["routes_with_repeated_nodes"],
        "self_loop_edges": sequences["self_loop_edges"],
        "routes_with_direction_changes": sequences[
            "routes_with_direction_changes"
        ],
        "routes_with_direction_boundary_order_gaps": sequences[
            "routes_with_direction_boundary_order_gaps"
        ],
        "routes_with_direction_boundary_over_300m": sequences[
            "routes_with_direction_boundary_over_300m"
        ],
        "cross_direction_od_pairs_in_linear_chain": sequences[
            "cross_direction_od_pairs_in_linear_chain"
        ],
        "routes_with_only_cross_direction_od_pairs": sequences[
            "routes_with_only_cross_direction_od_pairs"
        ],
        "segments_over_20km": sequences["segments_over_20km"],
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "path": str(resolved),
            "bytes": resolved.stat().st_size,
            "open_mode": "read_only_consistent_snapshot",
        },
        "validation_scope": {
            "all_active_routes_traversed": True,
            "route_stop_rows_per_pass": row_count,
            "route_stop_streaming_passes": 2,
            "python_state_model": "route-level DSU plus unique exact stop spatial index",
            "planner_route_stop_state_graph_materialized": False,
        },
        "sequence_validation": sequences,
        "transfer_connectivity": transfers,
        "hard_findings": hard_findings,
        "warnings": warnings,
        "audit_status": "ISSUES_FOUND" if any(hard_findings.values()) else "PASS",
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


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Traverse every active hydrated route and validate order, coordinates, "
            "and exact/nearby transfer components without mutating SQLite"
        )
    )
    parser.add_argument("--catalog-db", required=True, type=Path)
    parser.add_argument("--sample-limit", type=_bounded_positive, default=20)
    parser.add_argument(
        "--transfer-radius-meters",
        type=_positive_float,
        default=DEFAULT_TRANSFER_RADIUS_METERS,
    )
    parser.add_argument(
        "--coordinate-conflict-meters",
        type=_positive_float,
        default=DEFAULT_COORDINATE_CONFLICT_METERS,
    )
    parser.add_argument(
        "--max-active-routes", type=_bounded_positive, default=MAX_ACTIVE_ROUTES
    )
    parser.add_argument(
        "--max-route-stop-rows",
        type=_bounded_positive,
        default=MAX_ROUTE_STOP_ROWS,
    )
    parser.add_argument(
        "--max-unique-stops", type=_bounded_positive, default=MAX_UNIQUE_STOPS
    )
    parser.add_argument(
        "--max-pair-checks", type=_bounded_positive, default=MAX_PAIR_CHECKS
    )
    parser.add_argument(
        "--max-routes-per-stop",
        type=_bounded_positive,
        default=MAX_ROUTES_PER_STOP,
    )
    parser.add_argument("--fail-on-anomaly", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = validate_database(
            args.catalog_db,
            ValidationOptions(
                sample_limit=args.sample_limit,
                transfer_radius_meters=args.transfer_radius_meters,
                coordinate_conflict_meters=args.coordinate_conflict_meters,
                max_active_routes=args.max_active_routes,
                max_route_stop_rows=args.max_route_stop_rows,
                max_unique_stops=args.max_unique_stops,
                max_pair_checks=args.max_pair_checks,
                max_routes_per_stop=args.max_routes_per_stop,
            ),
        )
    except RouteGraphValidationError as exc:
        json.dump(
            {
                "ok": False,
                "error": {
                    "code": "ROUTE_GRAPH_VALIDATION_FAILED",
                    "message": str(exc),
                },
            },
            sys.stderr,
            ensure_ascii=False,
            sort_keys=True,
        )
        sys.stderr.write("\n")
        return 1
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 2 if args.fail_on_anomaly and report["audit_status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
