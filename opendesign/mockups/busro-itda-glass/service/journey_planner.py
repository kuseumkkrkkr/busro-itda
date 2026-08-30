"""Deterministic, evidence-aware journey alternatives over hydrated topology.

The planner creates structural candidates only. It never assumes that a route
operates at a requested time, and it never emits a success probability without
both explicit service evidence and sufficient passage history.
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
MAX_WALK_TARGET_STOPS = 24
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

        # Direction is authoritative: only consecutive ascending node_order is rideable.
        for sequence in sequences:
            indexes = sequence_indexes[(sequence.city_code, sequence.route_id)]
            for before_index, after_index in zip(indexes, indexes[1:]):
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
            catalog_route_count=len({(item.city_code, item.route_id) for item in catalog.routes}),
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
        starts = tuple(
            index for index in graph.node_id_indexes.get(origin, ())
            if not origin_city_code or graph.nodes[index].city_code == origin_city_code
        )
        goals = frozenset(
            index for index in graph.node_id_indexes.get(destination, ())
            if not destination_city_code or graph.nodes[index].city_code == destination_city_code
        )
        if not starts or not goals:
            return self._gap_result(graph, "STOP_NOT_IN_HYDRATED_SEQUENCE")

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
                    break
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

    def _transfer_edges(self, graph: GraphSnapshot, node_index: int) -> tuple[GraphEdge, ...]:
        source = graph.nodes[node_index]
        source_route = (source.city_code, source.route_id)
        group_index = graph.state_stop_groups[node_index]
        targets: dict[int, tuple[float, str]] = {}

        # Exact identifier equality is authoritative and uncapped (up to the
        # validated per-stop route-state bound).
        for target_index in graph.stop_group_states[group_index]:
            target = graph.nodes[target_index]
            if target_index != node_index and (target.city_code, target.route_id) != source_route:
                targets[target_index] = (0.0, "shared_node_id")

        coordinate = graph.stop_group_coordinates[group_index]
        if coordinate is not None:
            cell = (
                math.floor(coordinate[0] / graph.spatial_cell_degrees),
                math.floor(coordinate[1] / graph.spatial_cell_degrees),
            )
            nearby_groups: list[tuple[float, int]] = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for target_group in graph.spatial_buckets.get((cell[0] + dx, cell[1] + dy), ()):
                        if target_group == group_index:
                            continue
                        target_coordinate = graph.stop_group_coordinates[target_group]
                        if target_coordinate is None:
                            continue
                        distance = _coordinate_distance(coordinate, target_coordinate)
                        if distance <= graph.transfer_radius_m:
                            nearby_groups.append((distance, target_group))
            for distance, target_group in sorted(
                nearby_groups,
                key=lambda item: (round(item[0], 6), graph.stop_group_keys[item[1]]),
            )[:MAX_WALK_TARGET_STOPS]:
                for target_index in graph.stop_group_states[target_group]:
                    target = graph.nodes[target_index]
                    if (target.city_code, target.route_id) != source_route:
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
    ) -> dict[str, Any]:
        ride_edges = [edge for edge in path if edge.kind == "ride"]
        route_ids: list[str] = []
        for edge in ride_edges:
            if not route_ids or route_ids[-1] != edge.route_id:
                route_ids.append(edge.route_id)
        transfer_edges = [edge for edge in path if edge.kind == "transfer"]
        service_verified = [route for route in route_ids if self._service_verified(service_evidence.get(route))]
        service_observed = [
            route for route in route_ids
            if self._has_service_observation(service_evidence.get(route))
        ]
        passage_covered = [route for route in route_ids if self._passage_probability(passage_history.get(route)) is not None]
        reasons: list[str] = []
        if len(service_verified) != len(route_ids):
            reasons.append("VERIFIED_TIMETABLE_REQUIRED")
        if len(passage_covered) != len(route_ids):
            reasons.append("PASSAGE_HISTORY_REQUIRED")
        success_probability: float | None = None
        if not reasons and route_ids:
            probability = 1.0
            for route in route_ids:
                probability *= self._passage_probability(passage_history.get(route)) or 0.0
            success_probability = round(probability, 4)

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
        structural_coverage = 1.0 if ride_edges else 0.0
        return {
            "criterion": criterion,
            "status": "DATA_GAP" if reasons else "READY",
            "reasons": reasons,
            "success_probability": success_probability,
            "probability_basis": (
                "persisted_observed_passage_outcome_ratio" if success_probability is not None else None
            ),
            "probability_scope": (
                "observation_reconstruction_not_timetable_or_transfer_success"
                if success_probability is not None else None
            ),
            "estimated_minutes": None,
            "operating_assumption": False,
            "transfers": len(transfer_edges),
            "walking_m": round(sum(edge.distance_m for edge in transfer_edges), 1),
            "route_ids": route_ids,
            "steps": steps,
            "evidence": {
                "topology": "all_active_hydrated_route_sequences",
                "ride_edges": len(ride_edges),
                "transfer_edges": len(transfer_edges),
                "sources": sorted({edge.evidence_source for edge in path}),
                "service_routes": {
                    route: self._evidence_summary(service_evidence.get(route)) for route in route_ids
                },
                "passage_routes": {
                    route: self._evidence_summary(passage_history.get(route)) for route in route_ids
                },
            },
            "coverage": {
                "structural": structural_coverage,
                "service_routes": len(service_verified),
                "schedule_routes": len(service_verified),
                "observed_service_routes": len(service_observed),
                "passage_routes": len(passage_covered),
                "total_routes": len(route_ids),
                "minimum_passage_samples": self.min_passage_samples,
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

    def _passage_probability(self, value: Any) -> float | None:
        if not isinstance(value, Mapping):
            return None
        try:
            samples = int(value.get("sample_count", 0))
            probability = float(value.get("success_probability"))
        except (TypeError, ValueError):
            return None
        if samples < self.min_passage_samples or not 0.0 <= probability <= 1.0:
            return None
        return probability

    @staticmethod
    def _evidence_summary(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        allowed = {
            "verified", "basis", "source", "captured_at", "evidence_scope", "metric",
            "probability_scope", "precision",
            "observation_count", "arrival_observation_count", "position_observation_count",
            "snapshot_count", "sample_count", "passage_count", "data_gap_count",
            "regression_count", "service_date_count", "first_observed_at", "last_observed_at",
        }
        return {key: value[key] for key in allowed if key in value}

    def _graph_metadata(self, graph: GraphSnapshot) -> dict[str, Any]:
        catalog_routes: int | None = graph.catalog_route_count or None
        if catalog_routes is None:
            coverage_status = "UNVERIFIED"
            nationwide_complete: bool | None = None
            missing_routes: int | None = None
        else:
            missing_routes = max(0, catalog_routes - graph.hydrated_route_count)
            nationwide_complete = missing_routes == 0
            coverage_status = "COMPLETE" if nationwide_complete else "PARTIAL"
        return {
            "version": graph.version,
            "catalog_version": graph.catalog_version,
            "algorithm": "directed_dijkstra",
            "alternative_algorithm": "deterministic_penalized_dijkstra",
            "topology_scope": "all_active_hydrated_route_sequences",
            "directionality": "ascending_authoritative_node_order_only",
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
                "catalog_routes": catalog_routes,
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
