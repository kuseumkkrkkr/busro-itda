"""Deterministic, evidence-aware journey alternatives over hydrated topology.

The planner creates structural candidates only. It never assumes that a route
operates at a requested time. Reconstructed passages remain coverage evidence;
success probability stays empty until a separately validated current-timetable,
actual-outcome, and historical-prior model exists.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
import hashlib
import heapq
import json
import math
import re
import threading
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from network_catalog import CatalogSnapshot, RouteSequence, RouteStopRecord


MIN_TRANSFER_RADIUS_M = 50
MAX_TRANSFER_RADIUS_M = 800
DEFAULT_MAX_GRAPH_NODES = 500_000
DEFAULT_MAX_GRAPH_EDGES = 750_000
DEFAULT_MAX_EXPANSIONS = 600_000
DEFAULT_MAX_PARALLEL_SEARCHES = 8
MAX_ALTERNATIVES = 5
MAX_ALTERNATIVE_ATTEMPTS = 2
PURE_TRANSFER_REUSE_PENALTY = 16
MAX_WALK_TARGET_STOPS = 128
ENDPOINT_ACCESS_RADIUS_M = 300
MAX_ROUTE_STATES_PER_STOP = 256
MAX_SPATIAL_BUCKET = 4_096
MIN_PASSAGE_SAMPLES = 8
VERIFIED_TIMETABLE_BASES = frozenset({"verified_official_timetable"})
_CODE = re.compile(r"^[0-9A-Za-z_.:-]{1,96}$")


class PlannerError(ValueError):
    """Base planner error."""


class PlannerLimitError(PlannerError):
    """Raised before bounded graph or CPU limits can be exceeded."""


class PlannerValidationError(PlannerError):
    """Raised for invalid planner inputs."""


@dataclass(frozen=True, slots=True)
class GraphNode:
    index: int
    city_code: str
    route_id: str
    node_id: str
    node_name: str
    node_order: int
    latitude: float | None
    longitude: float | None
    source: str
    captured_at: str
    can_board: bool
    can_alight: bool


@dataclass(frozen=True, slots=True)
class GraphEdge:
    index: int
    edge_id: str
    source_index: int
    target_index: int
    kind: str
    city_code: str
    route_id: str
    distance_m: float
    evidence_type: str
    evidence_source: str
    captured_at: str


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    version: str
    catalog_version: str
    transfer_radius_m: int
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    adjacency: tuple[tuple[int, ...], ...]
    stop_indexes: Mapping[tuple[str, str], tuple[int, ...]]
    node_id_indexes: Mapping[str, tuple[int, ...]]
    state_stop_groups: tuple[int, ...]
    stop_group_keys: tuple[tuple[str, str], ...]
    stop_group_states: tuple[tuple[int, ...], ...]
    stop_group_coordinates: tuple[tuple[float, float] | None, ...]
    spatial_buckets: Mapping[tuple[int, int], tuple[int, ...]]
    spatial_cell_degrees: float
    hydrated_route_count: int
    catalog_route_count: int
    topology_target_count: int
    topology_complete_count: int
    topology_discovery_complete: bool | None
    topology_hydrated_count: int
    city_count: int


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _CODE.fullmatch(text):
        raise PlannerValidationError(f"{field} has an invalid identifier")
    return text


def _haversine(a: GraphNode | RouteStopRecord, b: GraphNode | RouteStopRecord) -> float | None:
    if a.latitude is None or a.longitude is None or b.latitude is None or b.longitude is None:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (a.latitude, a.longitude, b.latitude, b.longitude))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_000.0 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _coordinate_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_000.0 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _is_direction_boundary(
    before: RouteStopRecord, after: RouteStopRecord
) -> bool:
    """Return true only for an explicit change between two direction codes.

    TAGO documents ``updowncd`` as the up/down direction code.  A passenger
    through-ride across different non-empty values is therefore not asserted
    without trip/block evidence.  Empty values mean the provider supplied no
    direction evidence and preserve the existing ordered sequence.  Unknown
    non-empty values are still distinct codes; distance alone never splits a
    route because rural/express adjacent stops can legitimately be far apart.
    """
    left = str(before.direction or "").strip()
    right = str(after.direction or "").strip()
    return bool(left and right and left != right)


class JourneyPlanner:
    def __init__(
        self,
        *,
        max_graph_nodes: int = DEFAULT_MAX_GRAPH_NODES,
        max_graph_edges: int = DEFAULT_MAX_GRAPH_EDGES,
        max_expansions: int = DEFAULT_MAX_EXPANSIONS,
        max_parallel_searches: int = DEFAULT_MAX_PARALLEL_SEARCHES,
        cache_entries: int = 4,
        min_passage_samples: int = MIN_PASSAGE_SAMPLES,
    ):
        self.max_graph_nodes = max(2, min(int(max_graph_nodes), DEFAULT_MAX_GRAPH_NODES))
        self.max_graph_edges = max(2, min(int(max_graph_edges), DEFAULT_MAX_GRAPH_EDGES))
        self.max_expansions = max(1, min(int(max_expansions), DEFAULT_MAX_EXPANSIONS))
        self.max_parallel_searches = max(
            1, min(int(max_parallel_searches), DEFAULT_MAX_PARALLEL_SEARCHES)
        )
        self.cache_entries = max(1, min(int(cache_entries), 16))
        self.min_passage_samples = max(1, min(int(min_passage_samples), 10_000))
        self._cache: OrderedDict[tuple[str, int], GraphSnapshot] = OrderedDict()
        self._cache_lock = threading.RLock()
        # A graph is immutable and shared by every request.  Only bounded
        # Dijkstra work is admitted concurrently so 200 HTTP clients cannot
        # multiply the per-search heap/distance-map memory without limit.
        self._search_slots = threading.BoundedSemaphore(self.max_parallel_searches)

    def build_graph(self, catalog: CatalogSnapshot, *, transfer_radius_m: int = 300) -> GraphSnapshot:
        try:
            radius = int(transfer_radius_m)
        except (TypeError, ValueError) as exc:
            raise PlannerValidationError("transfer_radius_m must be an integer") from exc
        if not MIN_TRANSFER_RADIUS_M <= radius <= MAX_TRANSFER_RADIUS_M:
            raise PlannerValidationError(
                f"transfer_radius_m must be {MIN_TRANSFER_RADIUS_M}..{MAX_TRANSFER_RADIUS_M}"
            )
        cache_key = (catalog.version, radius)
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return cached

        sequences = tuple(sorted(catalog.route_sequences, key=lambda item: (item.city_code, item.route_id)))
        node_count = sum(len(sequence.stops) for sequence in sequences)
        if node_count > self.max_graph_nodes:
            raise PlannerLimitError(f"hydrated graph exceeds {self.max_graph_nodes} nodes")

        nodes: list[GraphNode] = []
        sequence_indexes: dict[tuple[str, str], list[int]] = {}
        stop_indexes: dict[tuple[str, str], list[int]] = defaultdict(list)
        node_id_indexes: dict[str, list[int]] = defaultdict(list)
        for sequence in sequences:
            indexes: list[int] = []
            for stop in sequence.stops:
                index = len(nodes)
                node = GraphNode(
                    index=index,
                    city_code=sequence.city_code,
                    route_id=sequence.route_id,
                    node_id=stop.node_id,
                    node_name=stop.node_name,
                    node_order=stop.node_order,
                    latitude=stop.latitude,
                    longitude=stop.longitude,
                    source=sequence.source,
                    captured_at=sequence.captured_at,
                    can_board=stop.can_board,
                    can_alight=stop.can_alight,
                )
                nodes.append(node)
                indexes.append(index)
                stop_indexes[(node.city_code, node.node_id)].append(index)
                node_id_indexes[node.node_id].append(index)
            sequence_indexes[(sequence.city_code, sequence.route_id)] = indexes

        edges: list[GraphEdge] = []
        adjacency_lists: list[list[int]] = [[] for _ in nodes]
        def add_edge(source_index: int, target_index: int, kind: str, distance_m: float, evidence_type: str) -> None:
            if len(edges) >= self.max_graph_edges:
                raise PlannerLimitError(f"hydrated graph exceeds {self.max_graph_edges} edges")
            source_node = nodes[source_index]
            target_node = nodes[target_index]
            route_id = source_node.route_id if kind == "ride" else ""
            identity = [kind, source_node.city_code, source_node.route_id, source_node.node_order, target_node.city_code, target_node.route_id, target_node.node_order]
            edge_id = hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()[:24]
            edge = GraphEdge(
                index=len(edges),
                edge_id=edge_id,
                source_index=source_index,
                target_index=target_index,
                kind=kind,
                city_code=source_node.city_code,
                route_id=route_id,
                distance_m=round(max(0.0, distance_m), 3),
                evidence_type=evidence_type,
                evidence_source=source_node.source if kind == "ride" else evidence_type,
                captured_at=source_node.captured_at if kind == "ride" else max(source_node.captured_at, target_node.captured_at),
            )
            edges.append(edge)
            adjacency_lists[source_index].append(edge.index)

        # Ascending order is authoritative only inside one explicit direction
        # segment.  Do not invent passenger through-service across an
        # up/down-code boundary; blank direction evidence remains contiguous.
        for sequence in sequences:
            indexes = sequence_indexes[(sequence.city_code, sequence.route_id)]
            for before_stop, after_stop, before_index, after_index in zip(
                sequence.stops,
                sequence.stops[1:],
                indexes,
                indexes[1:],
            ):
                if _is_direction_boundary(before_stop, after_stop):
                    continue
                distance = _haversine(nodes[before_index], nodes[after_index])
                add_edge(before_index, after_index, "ride", distance or 0.0, "hydrated_route_sequence")

        # Transfers are indexed by physical stop and generated lazily during
        # Dijkstra.  Materialising route-state-to-route-state transfer edges is
        # quadratic at large interchanges and makes a nationwide graph much
        # larger than its source topology.
        stop_group_keys = tuple(sorted(stop_indexes))
        stop_group_states: list[tuple[int, ...]] = []
        stop_group_coordinates: list[tuple[float, float] | None] = []
        state_stop_groups = [-1] * len(nodes)
        for group_index, key in enumerate(stop_group_keys):
            indexes = tuple(sorted(stop_indexes[key]))
            if len(indexes) > MAX_ROUTE_STATES_PER_STOP:
                raise PlannerLimitError(
                    f"one stop exceeds {MAX_ROUTE_STATES_PER_STOP} route states"
                )
            stop_group_states.append(indexes)
            for node_index in indexes:
                state_stop_groups[node_index] = group_index
            located = next(
                (
                    (nodes[index].latitude, nodes[index].longitude)
                    for index in indexes
                    if nodes[index].latitude is not None and nodes[index].longitude is not None
                ),
                None,
            )
            stop_group_coordinates.append(located)

        # Coordinate proximity is partitioned by a stop-level spatial grid.
        # A dense stop served by many routes occupies one bucket entry, not one
        # entry per route state.
        cell_degrees = radius / 111_000.0
        buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        for group_index, coordinate in enumerate(stop_group_coordinates):
            if coordinate is None:
                continue
            latitude, longitude = coordinate
            key = (math.floor(latitude / cell_degrees), math.floor(longitude / cell_degrees))
            buckets[key].append(group_index)
            if len(buckets[key]) > MAX_SPATIAL_BUCKET:
                raise PlannerLimitError("spatial transfer bucket exceeds its CPU bound")

        adjacency = tuple(tuple(sorted(indexes, key=lambda index: edges[index].edge_id)) for indexes in adjacency_lists)
        immutable_stops = MappingProxyType({key: tuple(value) for key, value in sorted(stop_indexes.items())})
        immutable_node_ids = MappingProxyType({key: tuple(value) for key, value in sorted(node_id_indexes.items())})
        immutable_buckets = MappingProxyType(
            {key: tuple(sorted(value)) for key, value in sorted(buckets.items())}
        )
        graph_version = hashlib.sha256(_canonical([catalog.version, radius, [edge.edge_id for edge in edges]]).encode("utf-8")).hexdigest()
        graph = GraphSnapshot(
            version=graph_version,
            catalog_version=catalog.version,
            transfer_radius_m=radius,
            nodes=tuple(nodes),
            edges=tuple(edges),
            adjacency=adjacency,
            stop_indexes=immutable_stops,
            node_id_indexes=immutable_node_ids,
            state_stop_groups=tuple(state_stop_groups),
            stop_group_keys=stop_group_keys,
            stop_group_states=tuple(stop_group_states),
            stop_group_coordinates=tuple(stop_group_coordinates),
            spatial_buckets=immutable_buckets,
            spatial_cell_degrees=cell_degrees,
            hydrated_route_count=len(sequences),
            catalog_route_count=(
                catalog.catalog_route_count
                if catalog.catalog_route_count is not None
                else len({(item.city_code, item.route_id) for item in catalog.routes})
            ),
            topology_target_count=int(catalog.topology_target_count or 0),
            topology_complete_count=int(catalog.topology_complete_count or 0),
            topology_discovery_complete=catalog.topology_discovery_complete,
            topology_hydrated_count=(
                int(catalog.topology_hydrated_count)
                if catalog.topology_hydrated_count is not None
                else len(sequences)
            ),
            city_count=len({item.city_code for item in sequences}),
        )
        with self._cache_lock:
            self._cache[cache_key] = graph
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self.cache_entries:
                self._cache.popitem(last=False)
        return graph

    def plan(
        self,
        catalog: CatalogSnapshot,
        *,
        origin_node_id: str,
        destination_node_id: str,
        origin_city_code: str | None = None,
        destination_city_code: str | None = None,
        transfer_radius_m: int = 300,
        alternatives: int = 3,
        preference: str = "diverse",
        service_evidence: Mapping[str, Any] | None = None,
        passage_history: Mapping[str, Any] | None = None,
        evidence_loader: Callable[
            [tuple[str, ...]],
            tuple[Mapping[str, Any], Mapping[str, Any]],
        ] | None = None,
        origin_access: Mapping[str, Any] | None = None,
        destination_access: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        origin = _identifier(origin_node_id, "origin_node_id")
        destination = _identifier(destination_node_id, "destination_node_id")
        if origin == destination and (not origin_city_code or origin_city_code == destination_city_code):
            raise PlannerValidationError("origin and destination must differ")
        if origin_city_code:
            origin_city_code = _identifier(origin_city_code, "origin_city_code")
        if destination_city_code:
            destination_city_code = _identifier(destination_city_code, "destination_city_code")
        try:
            requested_alternatives = int(alternatives)
        except (TypeError, ValueError) as exc:
            raise PlannerValidationError("alternatives must be an integer") from exc
        if not 1 <= requested_alternatives <= MAX_ALTERNATIVES:
            raise PlannerLimitError(f"alternatives must be 1..{MAX_ALTERNATIVES}")
        if preference not in {"diverse", "low_transfer", "reliable", "challenge"}:
            raise PlannerValidationError("preference is not supported")

        graph = self.build_graph(catalog, transfer_radius_m=transfer_radius_m)
        normalized_origin_access = self._normalize_access_point(
            origin_access, expected_node_id=origin, expected_city_code=origin_city_code
        )
        normalized_destination_access = self._normalize_access_point(
            destination_access,
            expected_node_id=destination,
            expected_city_code=destination_city_code,
        )
        origin_indexes = tuple(
            index for index in graph.node_id_indexes.get(origin, ())
            if not origin_city_code or graph.nodes[index].city_code == origin_city_code
        )
        destination_indexes = tuple(
            index for index in graph.node_id_indexes.get(destination, ())
            if (
                not destination_city_code
                or graph.nodes[index].city_code == destination_city_code
            )
        )
        origin_snapped = False
        destination_snapped = False
        if not origin_indexes and normalized_origin_access is not None:
            origin_indexes = self._endpoint_indexes(
                graph, normalized_origin_access, require_board=True
            )
            origin_snapped = bool(origin_indexes)
        if not destination_indexes and normalized_destination_access is not None:
            destination_indexes = self._endpoint_indexes(
                graph, normalized_destination_access, require_board=False
            )
            destination_snapped = bool(destination_indexes)
        if not origin_indexes or not destination_indexes:
            reason = (
                "STOP_NOT_ROUTABLE_NEARBY"
                if (
                    (not origin_indexes and normalized_origin_access is not None)
                    or (
                        not destination_indexes
                        and normalized_destination_access is not None
                    )
                )
                else "STOP_NOT_IN_HYDRATED_SEQUENCE"
            )
            return self._gap_result(graph, reason)
        starts = tuple(index for index in origin_indexes if graph.nodes[index].can_board)
        goals = frozenset(
            index for index in destination_indexes if graph.nodes[index].can_alight
        )
        if not starts or not goals:
            return self._gap_result(graph, "STOP_ACCESS_RESTRICTED")

        criteria_by_preference = {
            "diverse": ("minimum_transfers", "generalized_cost", "explorer"),
            "low_transfer": ("minimum_transfers",),
            # Reliability remains evidence-based downstream.  Without verified
            # history this only changes structural search ordering.
            "reliable": ("generalized_cost", "minimum_transfers", "explorer"),
            "challenge": ("explorer",),
        }
        criteria = criteria_by_preference[preference]
        penalties: Counter[str] = Counter()
        signatures: set[tuple[str, ...]] = set()
        candidate_paths: list[tuple[Sequence[GraphEdge], str]] = []
        work_budget = [self.max_expansions]
        with self._search_slots:
            for slot in range(requested_alternatives):
                criterion = criteria[slot % len(criteria)]
                found: tuple[GraphEdge, ...] | None = None
                for attempt in range(MAX_ALTERNATIVE_ATTEMPTS):
                    path = self._shortest_path(graph, starts, goals, criterion, penalties, work_budget, attempt)
                    if path is None:
                        break
                    if not any(edge.kind == "ride" for edge in path):
                        for edge in path:
                            penalties[edge.edge_id] += PURE_TRANSFER_REUSE_PENALTY
                        continue
                    signature = tuple(edge.edge_id for edge in path)
                    if signature not in signatures:
                        found = path
                        signatures.add(signature)
                        break
                    for edge in path:
                        penalties[edge.edge_id] += 1
                if found is None:
                    # One search criterion can exhaust its distinct options
                    # while a later criterion still has a meaningfully
                    # different (often higher-transfer) route.  Keep trying
                    # the remaining bounded slots instead of truncating the
                    # whole alternative search.
                    continue
                candidate_paths.append((found, criterion))
                for edge in found:
                    penalties[edge.edge_id] += 1

        if not candidate_paths:
            return self._gap_result(graph, "NO_DIRECTED_PATH_IN_HYDRATED_GRAPH")
        loaded_service = service_evidence or {}
        loaded_passages = passage_history or {}
        if evidence_loader is not None:
            route_ids = tuple(sorted({
                edge.route_id
                for path, _criterion in candidate_paths
                for edge in path
                if edge.kind == "ride" and edge.route_id
            }))
            loaded_service, loaded_passages = evidence_loader(route_ids)
        candidates = [
            self._candidate(
                graph,
                path,
                criterion=criterion,
                service_evidence=loaded_service,
                passage_history=loaded_passages,
                origin_access=(normalized_origin_access if origin_snapped else None),
                destination_access=(
                    normalized_destination_access if destination_snapped else None
                ),
            )
            for path, criterion in candidate_paths
        ]
        return {
            "status": "DATA_GAP" if any(item["status"] == "DATA_GAP" for item in candidates) else "READY",
            "reason": "EVIDENCE_INCOMPLETE" if any(item["status"] == "DATA_GAP" for item in candidates) else None,
            "graph": self._graph_metadata(graph),
            "alternatives": candidates,
        }

    def _shortest_path(
        self,
        graph: GraphSnapshot,
        starts: Sequence[int],
        goals: frozenset[int],
        criterion: str,
        penalties: Mapping[str, int],
        work_budget: list[int],
        attempt: int,
    ) -> tuple[GraphEdge, ...] | None:
        queue: list[tuple[float, float, int, int]] = []
        best: dict[int, tuple[float, float, int]] = {}
        previous: dict[int, tuple[int, GraphEdge]] = {}
        for start in sorted(starts):
            best[start] = (0.0, 0, 0)
            heapq.heappush(queue, (0.0, 0, 0, start))
        goal: int | None = None
        while queue:
            if work_budget[0] <= 0:
                raise PlannerLimitError(f"path search exceeds {self.max_expansions} node expansions")
            work_budget[0] -= 1
            primary, secondary, hops, node_index = heapq.heappop(queue)
            if best.get(node_index) != (primary, secondary, hops):
                continue
            if node_index in goals:
                goal = node_index
                break
            for edge in self._outgoing_edges(graph, node_index):
                state = self._advance_state(
                    (primary, secondary, hops),
                    edge,
                    criterion,
                    penalties.get(edge.edge_id, 0),
                    attempt,
                )
                existing = best.get(edge.target_index)
                if existing is None or state < existing:
                    best[edge.target_index] = state
                    previous[edge.target_index] = (node_index, edge)
                    heapq.heappush(queue, (*state, edge.target_index))
        if goal is None:
            return None
        reversed_edges: list[GraphEdge] = []
        cursor = goal
        while cursor not in starts:
            parent = previous.get(cursor)
            if parent is None:
                return None
            cursor, edge = parent
            reversed_edges.append(edge)
        return tuple(reversed(reversed_edges))

    def _outgoing_edges(self, graph: GraphSnapshot, node_index: int) -> tuple[GraphEdge, ...]:
        ride_edges = [graph.edges[index] for index in graph.adjacency[node_index]]
        return tuple(sorted((*ride_edges, *self._transfer_edges(graph, node_index)), key=lambda edge: edge.edge_id))

    @staticmethod
    def _normalize_access_point(
        value: Mapping[str, Any] | None,
        *,
        expected_node_id: str,
        expected_city_code: str | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise PlannerValidationError("endpoint access must be an object")
        node_id = _identifier(value.get("node_id"), "endpoint_access.node_id")
        city_code = _identifier(
            value.get("city_code"), "endpoint_access.city_code"
        )
        if node_id != expected_node_id:
            raise PlannerValidationError("endpoint access node does not match request")
        if expected_city_code and city_code != expected_city_code:
            raise PlannerValidationError("endpoint access city does not match request")
        try:
            latitude = float(value.get("latitude"))
            longitude = float(value.get("longitude"))
        except (TypeError, ValueError) as exc:
            raise PlannerValidationError(
                "endpoint access coordinates are invalid"
            ) from exc
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise PlannerValidationError("endpoint access coordinates are invalid")
        return {
            "city_code": city_code,
            "node_id": node_id,
            "node_name": str(value.get("node_name") or node_id)[:160],
            "latitude": latitude,
            "longitude": longitude,
        }

    def _endpoint_indexes(
        self,
        graph: GraphSnapshot,
        access: Mapping[str, Any],
        *,
        require_board: bool,
    ) -> tuple[int, ...]:
        coordinate = (float(access["latitude"]), float(access["longitude"]))
        nearby = self._nearby_stop_groups(
            graph,
            coordinate,
            radius_m=ENDPOINT_ACCESS_RADIUS_M,
            excluded_group=None,
        )
        indexes: list[int] = []
        for _distance, group_index in nearby:
            for node_index in graph.stop_group_states[group_index]:
                node = graph.nodes[node_index]
                accessible = node.can_board if require_board else node.can_alight
                if (
                    accessible
                    and node.latitude is not None
                    and node.longitude is not None
                ):
                    indexes.append(node_index)
        return tuple(sorted(set(indexes)))

    @staticmethod
    def _nearby_stop_groups(
        graph: GraphSnapshot,
        coordinate: tuple[float, float],
        *,
        radius_m: int,
        excluded_group: int | None,
    ) -> tuple[tuple[float, int], ...]:
        cell = (
            math.floor(coordinate[0] / graph.spatial_cell_degrees),
            math.floor(coordinate[1] / graph.spatial_cell_degrees),
        )
        cell_radius_ratio = radius_m / graph.transfer_radius_m
        latitude_cell_span = max(1, math.ceil(cell_radius_ratio))
        latitude_margin_degrees = radius_m / 111_000.0
        bounding_latitude = min(
            89.999999,
            abs(coordinate[0]) + latitude_margin_degrees,
        )
        longitude_scale = max(math.cos(math.radians(bounding_latitude)), 1e-9)
        longitude_cell_span = max(
            1, math.ceil(cell_radius_ratio / longitude_scale)
        )
        nearby_groups: list[tuple[float, int]] = []
        for latitude_offset in range(
            -latitude_cell_span, latitude_cell_span + 1
        ):
            for longitude_offset in range(
                -longitude_cell_span, longitude_cell_span + 1
            ):
                for target_group in graph.spatial_buckets.get(
                    (
                        cell[0] + latitude_offset,
                        cell[1] + longitude_offset,
                    ),
                    (),
                ):
                    if target_group == excluded_group:
                        continue
                    target_coordinate = graph.stop_group_coordinates[target_group]
                    if target_coordinate is None:
                        continue
                    distance = _coordinate_distance(coordinate, target_coordinate)
                    if distance <= radius_m:
                        nearby_groups.append((distance, target_group))
        if len(nearby_groups) > MAX_WALK_TARGET_STOPS:
            raise PlannerLimitError(
                "walk-transfer targets exceed "
                f"the {MAX_WALK_TARGET_STOPS}-stop CPU bound"
            )
        return tuple(
            sorted(
                nearby_groups,
                key=lambda item: (
                    round(item[0], 6),
                    graph.stop_group_keys[item[1]],
                ),
            )
        )

    def _transfer_edges(self, graph: GraphSnapshot, node_index: int) -> tuple[GraphEdge, ...]:
        source = graph.nodes[node_index]
        if not source.can_alight:
            return ()
        source_route = (source.city_code, source.route_id)
        group_index = graph.state_stop_groups[node_index]
        targets: dict[int, tuple[float, str]] = {}

        # Exact identifier equality is authoritative and uncapped (up to the
        # validated per-stop route-state bound).
        for target_index in graph.stop_group_states[group_index]:
            target = graph.nodes[target_index]
            if (
                target.can_board
                and target_index != node_index
                and (target.city_code, target.route_id) != source_route
            ):
                targets[target_index] = (0.0, "shared_node_id")

        coordinate = graph.stop_group_coordinates[group_index]
        if coordinate is not None:
            for distance, target_group in self._nearby_stop_groups(
                graph,
                coordinate,
                radius_m=graph.transfer_radius_m,
                excluded_group=group_index,
            ):
                for target_index in graph.stop_group_states[target_group]:
                    target = graph.nodes[target_index]
                    if (
                        target.can_board
                        and (target.city_code, target.route_id) != source_route
                    ):
                        targets.setdefault(target_index, (distance, "geodesic_proximity"))

        return tuple(
            self._transfer_edge(graph, node_index, target_index, distance, evidence_type)
            for target_index, (distance, evidence_type) in sorted(
                targets.items(),
                key=lambda item: (
                    0 if item[1][1] == "shared_node_id" else 1,
                    round(item[1][0], 6),
                    graph.nodes[item[0]].city_code,
                    graph.nodes[item[0]].route_id,
                    graph.nodes[item[0]].node_order,
                ),
            )
        )

    @staticmethod
    def _transfer_edge(
        graph: GraphSnapshot,
        source_index: int,
        target_index: int,
        distance_m: float,
        evidence_type: str,
    ) -> GraphEdge:
        source = graph.nodes[source_index]
        target = graph.nodes[target_index]
        return GraphEdge(
            index=-1,
            edge_id=f"transfer:{source_index}:{target_index}:{evidence_type}",
            source_index=source_index,
            target_index=target_index,
            kind="transfer",
            city_code=source.city_code,
            route_id="",
            distance_m=round(max(0.0, distance_m), 3),
            evidence_type=evidence_type,
            evidence_source=evidence_type,
            captured_at=max(source.captured_at, target.captured_at),
        )

    @staticmethod
    def _advance_state(
        state: tuple[float, float, int],
        edge: GraphEdge,
        criterion: str,
        reuse_count: int,
        attempt: int,
    ) -> tuple[float, float, int]:
        primary, secondary, hops = state
        is_transfer = edge.kind == "transfer"
        reuse_penalty = reuse_count * (1.0 + attempt * 0.25)
        if criterion == "minimum_transfers":
            # Lexicographic Dijkstra: transfers are the primary cost, so no
            # arbitrary scalar can let a long direct ride lose to a transfer.
            return (
                primary + (1.0 if is_transfer else 0.0),
                round(secondary + 1.0 + edge.distance_m / 100.0 + reuse_penalty * 2.0, 9),
                hops + 1,
            )
        elif criterion == "generalized_cost":
            weight = 1.0 if not is_transfer else 8.0 + edge.distance_m / 100.0
            return (
                round(primary + weight + reuse_penalty * 6.0, 9),
                secondary + (1.0 if is_transfer else 0.0),
                hops + 1,
            )
        weight = 1.0 if not is_transfer else 5.0 + edge.distance_m / 120.0
        return (
            round(primary + weight + reuse_penalty * 12.0, 9),
            secondary + (1.0 if is_transfer else 0.0),
            hops + 1,
        )

    def _candidate(
        self,
        graph: GraphSnapshot,
        path: Sequence[GraphEdge],
        *,
        criterion: str,
        service_evidence: Mapping[str, Any],
        passage_history: Mapping[str, Any],
        origin_access: Mapping[str, Any] | None = None,
        destination_access: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        ride_edges = [edge for edge in path if edge.kind == "ride"]
        route_ids: list[str] = []
        for edge in ride_edges:
            if not route_ids or route_ids[-1] != edge.route_id:
                route_ids.append(edge.route_id)
        transfer_edges = [edge for edge in path if edge.kind == "transfer"]
        steps = []
        for edge in path:
            source = graph.nodes[edge.source_index]
            target = graph.nodes[edge.target_index]
            steps.append(
                {
                    "kind": edge.kind,
                    "route_id": edge.route_id or None,
                    "from": {
                        "city_code": source.city_code,
                        "node_id": source.node_id,
                        "node_name": source.node_name,
                        "node_order": source.node_order,
                        "latitude": source.latitude,
                        "longitude": source.longitude,
                    },
                    "to": {
                        "city_code": target.city_code,
                        "node_id": target.node_id,
                        "node_name": target.node_name,
                        "node_order": target.node_order,
                        "latitude": target.latitude,
                        "longitude": target.longitude,
                    },
                    "distance_m": edge.distance_m,
                    "evidence": {"type": edge.evidence_type, "source": edge.evidence_source, "captured_at": edge.captured_at},
                }
            )
        access_walking_m = 0.0
        endpoint_access: list[dict[str, Any]] = []
        if path and origin_access is not None:
            target = graph.nodes[path[0].source_index]
            distance = _coordinate_distance(
                (float(origin_access["latitude"]), float(origin_access["longitude"])),
                (float(target.latitude), float(target.longitude)),
            )
            access_walking_m += distance
            step = self._endpoint_walk_step(
                access=origin_access,
                graph_node=target,
                distance_m=distance,
                access_kind="access",
            )
            steps.insert(0, step)
            endpoint_access.append(step)
        if path and destination_access is not None:
            source = graph.nodes[path[-1].target_index]
            distance = _coordinate_distance(
                (float(source.latitude), float(source.longitude)),
                (
                    float(destination_access["latitude"]),
                    float(destination_access["longitude"]),
                ),
            )
            access_walking_m += distance
            step = self._endpoint_walk_step(
                access=destination_access,
                graph_node=source,
                distance_m=distance,
                access_kind="egress",
            )
            steps.append(step)
            endpoint_access.append(step)
        return self.decorate_structural_candidate(
            {
            "criterion": criterion,
            "transfers": len(transfer_edges),
            "walking_m": round(
                sum(edge.distance_m for edge in transfer_edges) + access_walking_m,
                1,
            ),
            "endpoint_access": endpoint_access,
            "route_ids": route_ids,
            "steps": steps,
            "evidence": {
                "topology": "all_active_hydrated_route_sequences",
                "ride_edges": len(ride_edges),
                "transfer_edges": len(transfer_edges),
                "sources": sorted({edge.evidence_source for edge in path}),
            },
            },
            criterion=criterion,
            service_evidence=service_evidence,
            passage_history=passage_history,
        )

    def decorate_structural_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        criterion: str,
        service_evidence: Mapping[str, Any],
        passage_history: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply the shared evidence contract to any structural path shape.

        Both the legacy in-memory graph and the on-demand SQLite planner call
        this method.  Therefore changing the topology materialization cannot
        silently turn observed passages into a success probability or project
        a historical GTFS prior as a current timetable.
        """
        route_ids = [str(value) for value in candidate.get("route_ids", ())]
        service_verified = [
            route
            for route in route_ids
            if self._service_verified(service_evidence.get(route))
        ]
        service_observed = [
            route
            for route in route_ids
            if self._has_service_observation(service_evidence.get(route))
        ]
        passage_covered = [
            route
            for route in route_ids
            if self._observed_passage_ratio(passage_history.get(route)) is not None
        ]
        reasons: list[str] = []
        if len(service_verified) != len(route_ids):
            reasons.append("VERIFIED_TIMETABLE_REQUIRED")
        if len(passage_covered) != len(route_ids):
            reasons.append("PASSAGE_HISTORY_REQUIRED")
        # Consecutive vehicle passages measure observation coverage only.
        # They are not a whole-journey success model.
        reasons.append("VALIDATED_JOURNEY_SUCCESS_MODEL_REQUIRED")

        steps = list(candidate.get("steps", ()))
        base_evidence = candidate.get("evidence")
        evidence = dict(base_evidence) if isinstance(base_evidence, Mapping) else {}
        if "ride_edges" not in evidence:
            evidence["ride_edges"] = sum(
                1 for step in steps if step.get("kind") == "ride"
            )
        if "transfer_edges" not in evidence:
            evidence["transfer_edges"] = sum(
                1 for step in steps if step.get("kind") == "transfer"
            )
        if "sources" not in evidence:
            evidence["sources"] = sorted(
                {
                    str((step.get("evidence") or {}).get("source") or (step.get("evidence") or {}).get("type"))
                    for step in steps
                    if (step.get("evidence") or {}).get("source")
                    or (step.get("evidence") or {}).get("type")
                }
            )
        evidence.update(
            {
                "topology": "all_active_hydrated_route_sequences",
                "service_routes": {
                    route: self._evidence_summary(service_evidence.get(route))
                    for route in route_ids
                },
                "passage_routes": {
                    route: self._evidence_summary(passage_history.get(route))
                    for route in route_ids
                },
            }
        )
        result = dict(candidate)
        result.update(
            {
                "criterion": criterion,
                "status": "DATA_GAP" if reasons else "READY",
                "reasons": reasons,
                "success_probability": None,
                "probability_basis": None,
                "probability_scope": None,
                "reliability": {
                    "status": "DATA_GAP",
                    "success_probability": None,
                    "historical_gtfs_prior": {
                        "role": "model_weight_only",
                        "matched_to_current_route": False,
                        "value": None,
                        "projection_allowed": False,
                    },
                    "trust_assumption": {
                        "code": "USUALLY_ON_TIME",
                        "empirical_probability": False,
                    },
                    "requirements": [
                        "CURRENT_OFFICIAL_TIMETABLE",
                        "MATCHED_ACTUAL_EARLY_LATE_OUTCOMES",
                        "MATCHED_HISTORICAL_GTFS_PRIOR",
                    ],
                },
                "estimated_minutes": None,
                "operating_assumption": False,
                "route_ids": route_ids,
                "steps": steps,
                "evidence": evidence,
                "coverage": {
                    "structural": 1.0 if route_ids else 0.0,
                    "service_routes": len(service_verified),
                    "schedule_routes": len(service_verified),
                    "observed_service_routes": len(service_observed),
                    "passage_routes": len(passage_covered),
                    "total_routes": len(route_ids),
                    "minimum_passage_samples": self.min_passage_samples,
                },
            }
        )
        return result

    @staticmethod
    def _endpoint_walk_step(
        *,
        access: Mapping[str, Any],
        graph_node: GraphNode,
        distance_m: float,
        access_kind: str,
    ) -> dict[str, Any]:
        access_point = {
            "city_code": access["city_code"],
            "node_id": access["node_id"],
            "node_name": access["node_name"],
            "node_order": None,
            "latitude": access["latitude"],
            "longitude": access["longitude"],
        }
        graph_point = {
            "city_code": graph_node.city_code,
            "node_id": graph_node.node_id,
            "node_name": graph_node.node_name,
            "node_order": graph_node.node_order,
            "latitude": graph_node.latitude,
            "longitude": graph_node.longitude,
        }
        return {
            "kind": "walk",
            "route_id": None,
            "from": access_point if access_kind == "access" else graph_point,
            "to": graph_point if access_kind == "access" else access_point,
            "distance_m": round(max(0.0, distance_m), 3),
            "access_kind": access_kind,
            "evidence": {
                "type": "catalog_coordinate_access",
                "source": "official_static_catalog_to_hydrated_topology",
            },
        }

    @staticmethod
    def _service_verified(value: Any) -> bool:
        if not isinstance(value, Mapping) or value.get("verified") is not True:
            return False
        return (
            value.get("basis") in VERIFIED_TIMETABLE_BASES
            and isinstance(value.get("source"), str)
            and bool(value["source"].strip())
            and isinstance(value.get("captured_at"), str)
            and bool(value["captured_at"].strip())
        )

    @staticmethod
    def _has_service_observation(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        try:
            return int(value.get("observation_count") or 0) > 0
        except (TypeError, ValueError):
            return False

    def _observed_passage_ratio(self, value: Any) -> float | None:
        if not isinstance(value, Mapping):
            return None
        try:
            samples = int(value.get("sample_count", 0))
            ratio = float(value.get("observed_passage_ratio"))
        except (TypeError, ValueError):
            return None
        if samples < self.min_passage_samples or not 0.0 <= ratio <= 1.0:
            return None
        return ratio

    @staticmethod
    def _evidence_summary(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        allowed = {
            "verified", "basis", "source", "captured_at", "evidence_scope", "metric",
            "probability_scope", "precision",
            "observed_passage_ratio",
            "observation_count", "arrival_observation_count", "position_observation_count",
            "snapshot_count", "sample_count", "passage_count", "data_gap_count",
            "regression_count", "service_date_count", "first_observed_at", "last_observed_at",
        }
        return {key: value[key] for key in allowed if key in value}

    def _graph_metadata(self, graph: GraphSnapshot) -> dict[str, Any]:
        catalog_routes: int | None = graph.catalog_route_count or None
        topology_targets: int | None = graph.topology_target_count or None
        if topology_targets is not None:
            coverage_denominator = topology_targets
            coverage_basis = "TAGO_DISCOVERED_TARGETS"
            missing_routes = max(0, topology_targets - graph.topology_hydrated_count)
            nationwide_complete = bool(
                graph.topology_discovery_complete
                and graph.topology_complete_count == topology_targets
                and graph.topology_hydrated_count >= topology_targets
            )
            coverage_status = "COMPLETE" if nationwide_complete else "PARTIAL"
        elif catalog_routes is None:
            coverage_denominator = None
            coverage_basis = "NONE"
            coverage_status = "UNVERIFIED"
            nationwide_complete: bool | None = None
            missing_routes: int | None = None
        else:
            coverage_denominator = catalog_routes
            coverage_basis = "STATIC_CATALOG_COUNT_UNVERIFIED_NAMESPACE"
            missing_routes = max(0, catalog_routes - graph.hydrated_route_count)
            nationwide_complete = False if missing_routes else None
            coverage_status = "PARTIAL" if missing_routes else "UNVERIFIED"
        return {
            "version": graph.version,
            "catalog_version": graph.catalog_version,
            "algorithm": "directed_dijkstra",
            "alternative_algorithm": "deterministic_penalized_dijkstra",
            "topology_scope": "all_active_hydrated_route_sequences",
            "directionality": (
                "ascending_node_order_with_nonempty_direction_boundaries"
            ),
            "nodes": len(graph.nodes),
            # Only authoritative ride edges are materialised. Transfer edges
            # are derived lazily from exact-ID and spatial stop indexes.
            "edges": len(graph.edges),
            "ride_edges": len(graph.edges),
            "transfer_edges": "LAZY_STOP_INDEX",
            "transfer_radius_m": graph.transfer_radius_m,
            "immutable": True,
            "coverage": {
                "status": coverage_status,
                "nationwide_topology_complete": nationwide_complete,
                "hydrated_routes": graph.hydrated_route_count,
                "hydrated_discovered_targets": graph.topology_hydrated_count,
                "catalog_routes": catalog_routes,
                "discovered_targets": topology_targets,
                "completed_targets": graph.topology_complete_count,
                "discovery_complete": graph.topology_discovery_complete,
                "denominator_routes": coverage_denominator,
                "basis": coverage_basis,
                "missing_routes": missing_routes,
                "cities": graph.city_count,
                "route_states": len(graph.nodes),
                "all_active_sequences_loaded": True,
            },
            "scaling": {
                "graph_cache": "IMMUTABLE_SHARED_BY_CATALOG_VERSION_AND_RADIUS",
                "transfer_index": "LAZY_STOP_LEVEL_SPATIAL_GRID",
                "max_route_states": self.max_graph_nodes,
                "max_ride_edges": self.max_graph_edges,
                "max_expansions_per_request": self.max_expansions,
                "max_parallel_searches": self.max_parallel_searches,
            },
        }

    def _gap_result(self, graph: GraphSnapshot, reason: str) -> dict[str, Any]:
        return {
            "status": "DATA_GAP",
            "reason": reason,
            "graph": self._graph_metadata(graph),
            "alternatives": [],
        }


__all__ = [
    "GraphEdge",
    "GraphNode",
    "GraphSnapshot",
    "JourneyPlanner",
    "PlannerError",
    "PlannerLimitError",
    "PlannerValidationError",
]
