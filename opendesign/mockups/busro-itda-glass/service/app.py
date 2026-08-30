"""Application layer for the bus data service."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import hashlib
import json
import math
import random
import re
import threading
import time
from typing import Any

from config import Settings
from db import IdempotencyConflict, Store
from journey_planner import JourneyPlanner, PlannerError
from network_catalog import CatalogError, NetworkCatalog
from osm import OSMError, resolve_route_geometry
from source_registry import RegistryError, load_default_registry
from tago import (
    TagoError,
    fetch_arrivals,
    fetch_catalog,
    fetch_positions,
    normalize_arrivals,
    normalize_catalog,
    normalize_positions,
)


CITY_CODE_RE = re.compile(r"^[0-9]{1,9}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{2,64}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
VEHICLE_NO_RE = re.compile(r"^[0-9A-Za-z가-힣-]{1,32}$")
PASSAGE_STATUSES = {"PASSAGE", "DATA_GAP", "REGRESSION"}
SEOUL_TZ = timezone(timedelta(hours=9), name="Asia/Seoul")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class AppError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"ok": False, "error": error}


class _KeyedSingleFlight:
    """Share one in-flight result or failure across callers of the same key."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._recent_errors: dict[str, tuple[float, BaseException]] = {}

    def do(self, key: str, operation):
        with self._guard:
            now = time.monotonic()
            recent_error = self._recent_errors.get(key)
            if recent_error and recent_error[0] > now:
                raise recent_error[1]
            if recent_error:
                del self._recent_errors[key]
            if len(self._recent_errors) > 256:
                self._recent_errors = {
                    item_key: item
                    for item_key, item in self._recent_errors.items()
                    if item[0] > now
                }
            flight = self._entries.get(key)
            leader = flight is None
            if leader:
                flight = {"event": threading.Event(), "value": None, "error": None}
                self._entries[key] = flight

        if leader:
            try:
                flight["value"] = operation()
            except BaseException as exc:
                flight["error"] = exc
                with self._guard:
                    # Brief negative caching prevents a burst from becoming a
                    # serialized upstream retry storm after the leader fails.
                    self._recent_errors[key] = (time.monotonic() + 1.0, exc)
            finally:
                flight["event"].set()
                with self._guard:
                    if self._entries.get(key) is flight:
                        del self._entries[key]
            if flight["error"] is not None:
                raise flight["error"]
            return flight["value"], False

        flight["event"].wait()
        if flight["error"] is not None:
            raise flight["error"]
        return flight["value"], True


class BusroService:
    def __init__(self, settings: Settings, *, clock=utc_now):
        self.settings = settings
        self.clock = clock
        self.store = Store(settings.db_path)
        self.network_catalog = NetworkCatalog(settings.network_catalog_path, clock=clock)
        self.journey_planner = JourneyPlanner()
        self._planner_graph_lock = threading.Lock()
        self.source_registry = load_default_registry()
        self._fixture_delays: dict[str, Any] | None = None
        self._singleflight = _KeyedSingleFlight()

    def status(self) -> dict[str, Any]:
        tago_state = "fixture" if self.settings.fixture_mode else ("ready" if self.settings.tago_service_key else "missing_key")
        network_sources = self.network_catalog.provenance(limit=10)
        topology = self.network_catalog.active_topology_summary()
        return {
            "ok": True,
            "service": "busro-itda-data-service",
            "version": "0.2.0",
            "mode": "fixture" if self.settings.fixture_mode else "live",
            "tago": {
                "configured": bool(self.settings.tago_service_key),
                "state": tago_state,
                "key_exposed": False,
            },
            "storage": self.store.counts(),
            "network_catalog": {
                "ready": bool(network_sources),
                "sources": network_sources,
                "topology": topology,
                "path_exposed": False,
            },
            "capabilities": {
                "live_arrivals": True,
                "snapshot_collection": True,
                "live_positions": True,
                "position_snapshot_collection": True,
                "passage_reconstruction": "polling_window",
                "historical_archive": "locally_collected_snapshots",
                "daily_route_simulation": True,
                "passage_replay": True,
                "nationwide_city_catalog": True,
                "route_and_stop_catalog": True,
                "route_stop_mapping_validation": True,
                "nationwide_static_stop_search": "/api/network/stops",
                "verified_route_hydration": "/api/network/hydrate",
                "diverse_journey_generation": "/api/journeys/generate",
                "osm_route_geometry": "/api/osm/geometry",
                "municipal_source_registry": "/api/sources",
                "simulation_basis": "fixture_model" if self.settings.fixture_mode else "passage_history_required",
            },
        }

    def cities(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, set(), "query")
        return self._catalog_response("cities", {}, {}, "cities")

    def routes(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, {"city_code", "route_no", "page", "limit"}, "query")
        city_code = self._city_code(query.get("city_code"), "city_code")
        page, limit = self._pagination(query)
        normalized_query: dict[str, Any] = {"city_code": city_code, "page": page, "limit": limit}
        parameters = {"cityCode": city_code, "pageNo": str(page), "numOfRows": str(limit)}
        if query.get("route_no") not in (None, ""):
            route_no = self._catalog_text(query.get("route_no"), "route_no", 40)
            normalized_query["route_no"] = route_no
            parameters["routeNo"] = route_no
        return self._catalog_response("routes", normalized_query, parameters, "routes")

    def route_info(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, {"city_code", "route_id"}, "query")
        city_code = self._city_code(query.get("city_code"), "city_code")
        route_id = self._identifier(query.get("route_id"), "route_id")
        result = self._catalog_response(
            "route_info",
            {"city_code": city_code, "route_id": route_id},
            {"cityCode": city_code, "routeId": route_id},
            "routes",
        )
        public = dict(result)
        route_records = public.pop("routes", [])
        public["route"] = route_records[0] if route_records else None
        return public

    def route_stops(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, {"city_code", "route_id", "page", "limit"}, "query")
        city_code = self._city_code(query.get("city_code"), "city_code")
        route_id = self._identifier(query.get("route_id"), "route_id")
        page, limit = self._pagination(query)
        return self._catalog_response(
            "route_stops",
            {"city_code": city_code, "route_id": route_id, "page": page, "limit": limit},
            {"cityCode": city_code, "routeId": route_id, "pageNo": str(page), "numOfRows": str(limit)},
            "stops",
        )

    def stops(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(
            query, {"city_code", "node_name", "node_no", "page", "limit"}, "query"
        )
        city_code = self._city_code(query.get("city_code"), "city_code")
        page, limit = self._pagination(query)
        normalized_query: dict[str, Any] = {"city_code": city_code, "page": page, "limit": limit}
        parameters = {"cityCode": city_code, "pageNo": str(page), "numOfRows": str(limit)}
        if query.get("node_name") not in (None, ""):
            node_name = self._catalog_text(query.get("node_name"), "node_name", 60)
            normalized_query["node_name"] = node_name
            parameters["nodeNm"] = node_name
        if query.get("node_no") not in (None, ""):
            node_no = self._catalog_text(query.get("node_no"), "node_no", 40)
            normalized_query["node_no"] = node_no
            parameters["nodeNo"] = node_no
        return self._catalog_response("stops", normalized_query, parameters, "stops")

    def nearby_stops(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, {"latitude", "longitude", "page", "limit"}, "query")
        latitude = self._coordinate(query.get("latitude"), "latitude", -90.0, 90.0)
        longitude = self._coordinate(query.get("longitude"), "longitude", -180.0, 180.0)
        page, limit = self._pagination(query)
        normalized_query = {
            "latitude": latitude,
            "longitude": longitude,
            "radius_meters": 500,
            "page": page,
            "limit": limit,
        }
        return self._catalog_response(
            "nearby_stops",
            normalized_query,
            {
                "gpsLati": str(latitude),
                "gpsLong": str(longitude),
                "pageNo": str(page),
                "numOfRows": str(limit),
            },
            "stops",
        )

    def stop_routes(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, {"city_code", "node_id", "page", "limit"}, "query")
        city_code = self._city_code(query.get("city_code"), "city_code")
        node_id = self._node_id(query.get("node_id"), "node_id")
        page, limit = self._pagination(query)
        return self._catalog_response(
            "stop_routes",
            {"city_code": city_code, "node_id": node_id, "page": page, "limit": limit},
            {
                "cityCode": city_code,
                "nodeid": node_id,
                "pageNo": str(page),
                "numOfRows": str(limit),
            },
            "routes",
        )

    def validate_mapping(self, body: dict[str, Any]) -> dict[str, Any]:
        self._only_fields(body, {"city_code", "route_id", "node_id"}, "body")
        city_code = self._city_code(body.get("city_code"), "city_code")
        route_id = self._identifier(body.get("route_id"), "route_id")
        node_id = self._node_id(body.get("node_id"), "node_id")
        request = {"city_code": city_code, "route_id": route_id, "node_id": node_id}

        match: dict[str, Any] | None = None
        page = 1
        total_count = 1
        page_hashes: list[str] = []
        catalog_source = ""
        catalog_mode = ""
        while (page - 1) * 100 < total_count and page <= 10:
            catalog = self.route_stops(
                {"city_code": city_code, "route_id": route_id, "page": str(page), "limit": "100"}
            )
            catalog_source = catalog["source"]
            catalog_mode = catalog["mode"]
            page_hashes.append(catalog["provenance"]["upstream_hash"])
            match = next(
                (
                    stop
                    for stop in catalog["stops"]
                    if stop.get("route_id") == route_id and stop.get("node_id") == node_id
                ),
                None,
            )
            if match:
                break
            try:
                total_count = max(0, int(catalog["upstream"].get("total_count", 0)))
            except (TypeError, ValueError):
                total_count = 0
            page += 1
        if not match and total_count > 1000:
            raise AppError(
                "ROUTE_STOPS_TOO_LARGE",
                "Route stop list exceeded the 1,000-record validation bound",
                status=502,
            )

        captured_at = iso_utc(self.clock())
        upstream_hash = canonical_hash(page_hashes)
        request_hash = canonical_hash({"mapping": request, "mode": catalog_mode})
        validation_id = "map_" + canonical_hash(
            {"request_hash": request_hash, "upstream_hash": upstream_hash}
        )[:24]
        provenance = (
            "TAGO_SCHEMA_FIXTURE_NOT_LIVE"
            if self.settings.fixture_mode
            else "TAGO_ROUTE_STOPS_LIVE"
        )
        stored, created = self.store.create_mapping_validation(
            validation_id=validation_id,
            request_hash=request_hash,
            upstream_hash=upstream_hash,
            source=catalog_source,
            provenance=provenance,
            captured_at=captured_at,
            city_code=city_code,
            route_id=route_id,
            node_id=node_id,
            valid=match is not None,
            match=match,
        )
        return {
            "ok": True,
            "mode": catalog_mode,
            "source": catalog_source,
            "valid": match is not None,
            "reason": "ROUTE_CONTAINS_NODE" if match else "NODE_NOT_ON_ROUTE",
            "mapping": request,
            "match": match,
            "validation": {
                "validation_id": stored["validation_id"],
                "persisted": True,
                "created": created,
                "captured_at": stored["captured_at"],
                "provenance": stored["provenance"],
                "upstream_hash": stored["upstream_hash"],
            },
        }

    def network_status(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, set(), "query")
        try:
            sources = self.network_catalog.provenance(limit=100)
            topology_coverage = self.network_catalog.topology_coverage(provider="TAGO")
            active_topology = self.network_catalog.active_topology_summary()
        except CatalogError as exc:
            raise AppError("NETWORK_CATALOG_INVALID", str(exc), status=500) from exc
        topology_targets = int(topology_coverage.get("targets") or 0)
        topology_complete = int(topology_coverage.get("complete") or 0)
        hydrated_sequences = int(topology_coverage.get("hydrated_active_sequences") or 0)
        nationwide_graph_complete = (
            topology_targets > 0
            and topology_complete == topology_targets
            and hydrated_sequences >= topology_complete
        )
        return {
            "ok": True,
            "ready": bool(sources),
            "static_catalog_ready": bool(sources),
            "graph_ready": bool(active_topology["graph_ready"]),
            "graph_scope": "COMPLETE" if nationwide_graph_complete else ("PARTIAL" if active_topology["graph_ready"] else "DATA_GAP"),
            "nationwide_graph_complete": nationwide_graph_complete,
            "sources": sources,
            "active_topology": active_topology,
            "topology_coverage": topology_coverage,
            "path_algorithm": "directed_dijkstra",
            "topology_policy": "all_active_verified_route_sequences",
            "id_join_policy": "exact_identifiers_only",
            "success_probability_policy": "verified_timetable_and_persisted_live_passage_outcomes_required",
        }

    def sources(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, {"q", "limit", "offset", "status", "priority_tier"}, "query")
        limit = self._bounded_int(query.get("limit", 25), "limit", 1, 100)
        try:
            if query.get("q"):
                records = self.source_registry.search(str(query["q"]), limit=limit)
            else:
                priority = query.get("priority_tier")
                records = self.source_registry.list_sources(
                    limit=limit,
                    offset=self._bounded_int(query.get("offset", 0), "offset", 0, 10_000),
                    status=query.get("status") or None,
                    priority_tier=int(priority) if priority not in (None, "") else None,
                )
        except (RegistryError, TypeError, ValueError) as exc:
            raise AppError("INVALID_SOURCE_QUERY", str(exc)) from exc
        return {
            "ok": True,
            "count": len(records),
            "priority_order": self.source_registry.priority_order,
            "sources": records,
            "collection_policy": "TAGO first; permission required where registry says so",
        }

    def network_cities(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, {"q", "limit"}, "query")
        try:
            cities = self.network_catalog.search_cities(
                query.get("q", ""), limit=self._bounded_int(query.get("limit", 20), "limit", 1, 100)
            )
        except CatalogError as exc:
            raise AppError("INVALID_NETWORK_QUERY", str(exc)) from exc
        return {"ok": True, "source": "OFFICIAL_STATIC_CATALOG", "count": len(cities), "cities": cities}

    def network_stops(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, {"q", "city_code", "limit"}, "query")
        try:
            stops = self.network_catalog.search_stops(
                query.get("q", ""),
                city_code=query.get("city_code") or None,
                limit=self._bounded_int(query.get("limit", 20), "limit", 1, 100),
            )
        except CatalogError as exc:
            raise AppError("INVALID_NETWORK_QUERY", str(exc)) from exc
        kinds = sorted({str(stop.get("catalog_kind") or "") for stop in stops if stop.get("catalog_kind")})
        return {
            "ok": True,
            "source": "OFFICIAL_STATIC_AND_HYDRATED_TOPOLOGY",
            "sources": kinds,
            "count": len(stops),
            "graph_ready_count": sum(1 for stop in stops if stop.get("graph_ready")),
            "stops": stops,
        }

    def network_routes(self, query: dict[str, str]) -> dict[str, Any]:
        self._only_fields(query, {"q", "city_code", "limit"}, "query")
        try:
            routes = self.network_catalog.search_routes(
                query.get("q", ""),
                city_code=query.get("city_code") or None,
                limit=self._bounded_int(query.get("limit", 20), "limit", 1, 100),
            )
        except CatalogError as exc:
            raise AppError("INVALID_NETWORK_QUERY", str(exc)) from exc
        return {"ok": True, "source": "OFFICIAL_STATIC_CATALOG", "count": len(routes), "routes": routes}

    def hydrate_network_route(self, body: dict[str, Any]) -> dict[str, Any]:
        self._only_fields(body, {"city_code", "route_id"}, "body")
        city_code = self._city_code(body.get("city_code"), "city_code")
        route_id = self._identifier(body.get("route_id"), "route_id")
        pages: list[dict[str, Any]] = []
        stops: list[dict[str, Any]] = []
        total_count = 1
        page = 1
        while (page - 1) * 100 < total_count and page <= 10:
            payload = self.route_stops(
                {"city_code": city_code, "route_id": route_id, "page": str(page), "limit": "100"}
            )
            pages.append(payload)
            stops.extend(payload.get("stops", []))
            try:
                total_count = max(0, int(payload.get("upstream", {}).get("total_count", len(stops))))
            except (TypeError, ValueError):
                total_count = len(stops)
            page += 1
        if total_count > 1000:
            raise AppError("ROUTE_STOPS_TOO_LARGE", "Route stop list exceeded 1,000 rows", status=413)
        if total_count > len(stops):
            raise AppError("ROUTE_STOPS_INCOMPLETE", "Complete ordered route stops could not be loaded", status=502)
        ordered = sorted(stops, key=lambda item: (item.get("node_order") is None, item.get("node_order") or 0))
        if len(ordered) < 2:
            raise AppError("ROUTE_STOPS_REQUIRED", "At least two ordered route stops are required", status=422)
        captured_at = max(str(item.get("retrieved_at") or "") for item in pages) or iso_utc(self.clock())
        provenance = "TAGO_SCHEMA_FIXTURE_NOT_LIVE" if self.settings.fixture_mode else "TAGO_ROUTE_STOPS_LIVE"
        sequence_rows = [
            {
                "node_id": item.get("node_id"),
                "node_name": item.get("node_name"),
                "node_order": item.get("node_order"),
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
                "direction": item.get("up_down_code") or "",
            }
            for item in ordered
        ]
        try:
            sequence = self.network_catalog.hydrate_route_sequence(
                city_code=city_code,
                route_id=route_id,
                ordered_stops=sequence_rows,
                source=provenance,
                captured_at=captured_at,
            )
        except CatalogError as exc:
            raise AppError("INVALID_ROUTE_SEQUENCE", str(exc), status=422) from exc
        return {
            "ok": True,
            "mode": "fixture" if self.settings.fixture_mode else "live",
            "sequence": sequence,
            "stop_count": len(sequence_rows),
            "source": provenance,
            "fixture_notice": "SCHEMA_ONLY_NOT_LIVE" if self.settings.fixture_mode else None,
        }

    def generate_journeys(self, body: dict[str, Any]) -> dict[str, Any]:
        self._only_fields(
            body,
            {
                "from_stop_id", "to_stop_id", "from_city_code", "to_city_code",
                "preference", "max_alternatives", "transfer_radius_m",
            },
            "body",
        )
        preference = str(body.get("preference") or "diverse")
        if preference not in {"diverse", "low_transfer", "reliable", "challenge"}:
            raise AppError("INVALID_PREFERENCE", "preference is not supported")
        alternatives = self._bounded_int(body.get("max_alternatives", 3), "max_alternatives", 1, 5)
        transfer_radius = self._bounded_int(
            body.get("transfer_radius_m", 500 if preference == "challenge" else 300),
            "transfer_radius_m",
            50,
            800,
        )
        try:
            snapshot = self.network_catalog.planning_snapshot()
            # One bounded graph build per catalog revision/radius; path searches
            # can then run concurrently over the immutable cached graph.
            with self._planner_graph_lock:
                self.journey_planner.build_graph(snapshot, transfer_radius_m=transfer_radius)
            planned = self.journey_planner.plan(
                snapshot,
                origin_node_id=body.get("from_stop_id"),
                destination_node_id=body.get("to_stop_id"),
                origin_city_code=body.get("from_city_code") or None,
                destination_city_code=body.get("to_city_code") or None,
                transfer_radius_m=transfer_radius,
                alternatives=alternatives,
                preference=preference,
                evidence_loader=lambda route_ids: self.store.journey_evidence(route_ids),
            )
            topology_coverage = self.network_catalog.topology_coverage(provider="TAGO")
        except (CatalogError, PlannerError) as exc:
            raise AppError("JOURNEY_PLANNER_INPUT_INVALID", str(exc), status=422) from exc

        graph_metadata = planned.get("graph")
        if isinstance(graph_metadata, dict):
            graph_coverage = graph_metadata.get("coverage")
            if not isinstance(graph_coverage, dict):
                graph_coverage = {}
                graph_metadata["coverage"] = graph_coverage
            topology_targets = int(topology_coverage.get("targets") or 0)
            topology_complete = int(topology_coverage.get("complete") or 0)
            hydrated_sequences = int(topology_coverage.get("hydrated_active_sequences") or 0)
            graph_coverage.update(
                {
                    "ingest_targets": topology_targets,
                    "ingest_complete": topology_complete,
                    "ingest_hydrated_active_sequences": hydrated_sequences,
                    "ingest_coverage_ratio": float(topology_coverage.get("coverage_ratio") or 0.0),
                }
            )
            if topology_targets > 0:
                complete = (
                    topology_complete == topology_targets
                    and hydrated_sequences >= topology_complete
                )
                graph_coverage.update(
                    {
                        "status": "COMPLETE" if complete else "PARTIAL",
                        "nationwide_topology_complete": complete,
                        "catalog_routes": topology_targets,
                        "missing_routes": max(0, topology_targets - hydrated_sequences),
                    }
                )

        route_lookup = {(item.city_code, item.route_id): item for item in snapshot.routes}
        labels = {
            "minimum_transfers": "최소 환승",
            "generalized_cost": "균형 경로",
            "explorer": "탐험 경로",
        }
        candidates: list[dict[str, Any]] = []
        for index, candidate in enumerate(planned.get("alternatives", [])):
            seen: set[tuple[str, str]] = set()
            routes: list[dict[str, Any]] = []
            for step in candidate.get("steps", []):
                if step.get("kind") != "ride" or not step.get("route_id"):
                    continue
                city = str(step.get("from", {}).get("city_code") or "")
                route_id = str(step["route_id"])
                key = (city, route_id)
                if key in seen:
                    continue
                seen.add(key)
                catalog_route = route_lookup.get(key)
                routes.append(
                    {
                        "city_code": city,
                        "route_id": route_id,
                        "route_no": catalog_route.route_no if catalog_route else route_id,
                    }
                )
            coverage = candidate.get("coverage", {})
            total_routes = max(1, int(coverage.get("total_routes") or len(routes) or 1))
            evidence_coverage = min(
                float(coverage.get("structural") or 0),
                int(coverage.get("service_routes", coverage.get("schedule_routes")) or 0) / total_routes,
                int(coverage.get("passage_routes") or 0) / total_routes,
            )
            criterion = str(candidate.get("criterion") or "explorer")
            candidates.append(
                {
                    "id": "journey_" + canonical_hash([snapshot.version, index, candidate.get("steps")])[:20],
                    "criterion": criterion,
                    "kind": criterion,
                    "kind_label": labels.get(criterion, "대안 경로"),
                    "title": f"{len(routes)}개 시내버스로 잇기",
                    "status": candidate.get("status", "DATA_GAP"),
                    "reasons": candidate.get("reasons", []),
                    "success_probability": candidate.get("success_probability"),
                    "probability_basis": candidate.get("probability_basis"),
                    "probability_scope": candidate.get("probability_scope"),
                    "estimated_minutes": candidate.get("estimated_minutes"),
                    "transfer_count": candidate.get("transfers", 0),
                    "transfers": candidate.get("transfers", 0),
                    "walking_m": candidate.get("walking_m", 0),
                    "route_count": len(routes),
                    "route_ids": [item["route_id"] for item in routes],
                    "routes": routes,
                    "steps": candidate.get("steps", []),
                    "evidence": {**candidate.get("evidence", {}), "coverage": round(evidence_coverage, 4)},
                    "coverage": coverage,
                }
            )
        if preference == "challenge":
            candidates.sort(key=lambda item: (-item["route_count"], -item["transfer_count"], item["id"]))
        elif preference == "reliable":
            candidates.sort(
                key=lambda item: (
                    item["success_probability"] is None,
                    -(item["success_probability"] or 0),
                    -item["evidence"]["coverage"],
                    item["id"],
                )
            )
        elif preference == "low_transfer":
            candidates.sort(key=lambda item: (item["transfer_count"], item["walking_m"], item["id"]))
        return {
            "ok": True,
            "status": planned.get("status", "DATA_GAP"),
            "reason": planned.get("reason"),
            "preference": preference,
            "graph": planned.get("graph", {}),
            "count": len(candidates),
            "candidates": candidates,
            "alternatives": candidates,
            "evidence_policy": (
                "Only persisted live TAGO observations and at least 8 reconstructed outcomes per route; "
                "the ratio measures observation reconstruction, not timetable or transfer reliability"
            ),
        }

    def route_geometry(self, body: dict[str, Any]) -> dict[str, Any]:
        self._only_fields(body, {"route_ref", "stops", "allow_road_estimate"}, "body")
        route_ref = self._short_text(body.get("route_ref"), "route_ref", 24)
        raw_stops = body.get("stops")
        if not isinstance(raw_stops, list) or not 2 <= len(raw_stops) <= 160:
            raise AppError("INVALID_ROUTE_STOPS", "stops must contain 2-160 ordered coordinates")
        sanitized: list[dict[str, float]] = []
        for index, item in enumerate(raw_stops):
            if not isinstance(item, dict):
                raise AppError("INVALID_ROUTE_STOP", f"stops[{index}] must be an object")
            sanitized.append(
                {
                    "latitude": self._coordinate(item.get("latitude", item.get("gpslati")), f"stops[{index}].latitude", 32.0, 39.8),
                    "longitude": self._coordinate(item.get("longitude", item.get("gpslong")), f"stops[{index}].longitude", 123.0, 132.5),
                }
            )
        allow_estimate = body.get("allow_road_estimate", True)
        if not isinstance(allow_estimate, bool):
            raise AppError("INVALID_ALLOW_ESTIMATE", "allow_road_estimate must be boolean")
        cache_key = "osm:v2:" + canonical_hash([route_ref, sanitized, allow_estimate])
        cached = self.store.get_cache(cache_key)
        if cached:
            return {**cached, "cached": True}

        def load_once():
            existing = self.store.get_cache(cache_key)
            if existing:
                return {**existing, "cached": True}
            try:
                result = resolve_route_geometry(
                    route_ref=route_ref,
                    stops=sanitized,
                    timeout_seconds=min(15.0, max(2.0, self.settings.tago_timeout_seconds * 2)),
                    allow_road_estimate=allow_estimate,
                )
            except OSMError as exc:
                raise AppError(exc.code, exc.message, status=exc.status) from exc
            payload = {**result, "cached": False, "retrieved_at": iso_utc(self.clock())}
            self.store.put_cache(cache_key, payload, self.settings.catalog_cache_ttl_seconds)
            return payload

        result, shared = self._singleflight.do(cache_key, load_once)
        return {**result, "cached": bool(result.get("cached") or shared)}

    def _catalog_response(
        self,
        operation: str,
        normalized_query: dict[str, Any],
        parameters: dict[str, str],
        output_key: str,
    ) -> dict[str, Any]:
        mode = "fixture" if self.settings.fixture_mode else "live"
        request_hash = canonical_hash(
            {"operation": operation, "query": normalized_query, "mode": mode}
        )
        cache_key = f"catalog:{operation}:{request_hash}:{mode}"
        cached = self.store.get_cache(cache_key)
        if cached:
            cached["cached"] = True
            return cached

        def load_once():
            existing = self.store.get_cache(cache_key)
            if existing:
                existing["cached"] = True
                return existing
            try:
                upstream = fetch_catalog(
                    operation=operation,
                    parameters=parameters,
                    service_key=self.settings.tago_service_key,
                    timeout_seconds=self.settings.tago_timeout_seconds,
                    fixture_mode=self.settings.fixture_mode,
                    fixture_path=self.settings.catalog_fixture_path,
                )
                records, metadata = normalize_catalog(
                    upstream,
                    operation=operation,
                    fallback_city_code=str(normalized_query.get("city_code") or ""),
                    fallback_route_id=str(normalized_query.get("route_id") or ""),
                )
            except (TagoError, OSError, json.JSONDecodeError) as exc:
                if isinstance(exc, TagoError):
                    raise AppError(exc.code, exc.message, status=exc.status) from exc
                raise AppError("CATALOG_FIXTURE_INVALID", "Catalog fixture could not be loaded", status=500) from exc

            if self.settings.fixture_mode:
                records = self._filter_fixture_catalog(operation, normalized_query, records)
                metadata = dict(metadata)
                metadata["normalized_count"] = len(records)
            captured_at = iso_utc(self.clock())
            upstream_hash = canonical_hash(upstream)
            source = "TAGO_SCHEMA_FIXTURE" if self.settings.fixture_mode else "TAGO"
            provenance = (
                "TAGO_SCHEMA_FIXTURE_NOT_LIVE"
                if self.settings.fixture_mode
                else f"TAGO:{operation}"
            )
            snapshot_id = "cat_" + canonical_hash(
                {"operation": operation, "request_hash": request_hash, "upstream_hash": upstream_hash}
            )[:24]
            snapshot, created = self.store.create_catalog_snapshot(
                snapshot_id=snapshot_id,
                resource_type=operation,
                request_hash=request_hash,
                upstream_hash=upstream_hash,
                source=source,
                provenance=provenance,
                captured_at=captured_at,
                query=normalized_query,
                records=records,
            )
            result = {
                "ok": True,
                "mode": mode,
                "source": source,
                "retrieved_at": captured_at,
                "cached": False,
                "query": normalized_query,
                output_key: records,
                "count": len(records),
                "upstream": metadata,
                "provenance": {
                    "provider": "국토교통부 TAGO",
                    "operation": operation,
                    "fixture": self.settings.fixture_mode,
                    "fixture_notice": "SCHEMA_ONLY_NOT_LIVE" if self.settings.fixture_mode else None,
                    "snapshot_id": snapshot["snapshot_id"],
                    "snapshot_created": created,
                    "upstream_hash": upstream_hash,
                    "captured_at": snapshot["captured_at"],
                },
            }
            self.store.put_cache(cache_key, result, self.settings.catalog_cache_ttl_seconds)
            return result

        result, shared = self._singleflight.do(cache_key, load_once)
        if shared:
            result = dict(result)
            result["cached"] = True
        return result

    def arrivals(self, query: dict[str, str], *, bypass_cache: bool = False) -> dict[str, Any]:
        city_code, node_id = self._arrival_query(query)
        cache_key = f"arrivals:{city_code}:{node_id}:{'fixture' if self.settings.fixture_mode else 'live'}"
        if not bypass_cache:
            cached = self.store.get_cache(cache_key)
            if cached:
                cached["cached"] = True
                return cached

        def load_once():
            # Recheck after becoming the per-key leader. This also lets a
            # simultaneous collect reuse the just-fetched response instead of
            # multiplying upstream calls.
            if not bypass_cache:
                cached = self.store.get_cache(cache_key)
                if cached:
                    cached["cached"] = True
                    return cached
            try:
                upstream = fetch_arrivals(
                    city_code=city_code,
                    node_id=node_id,
                    service_key=self.settings.tago_service_key,
                    timeout_seconds=self.settings.tago_timeout_seconds,
                    fixture_mode=self.settings.fixture_mode,
                    fixture_path=self.settings.fixture_path,
                )
                arrivals, metadata = normalize_arrivals(upstream)
            except TagoError as exc:
                raise AppError(exc.code, exc.message, status=exc.status) from exc

            result = {
                "ok": True,
                "mode": "fixture" if self.settings.fixture_mode else "live",
                "source": "TAGO_FIXTURE" if self.settings.fixture_mode else "TAGO",
                "retrieved_at": iso_utc(self.clock()),
                "cached": False,
                "query": {"city_code": city_code, "node_id": node_id},
                "arrivals": arrivals,
                "upstream": metadata,
            }
            self.store.put_cache(cache_key, result, self.settings.cache_ttl_seconds)
            return result

        flight_key = ("fresh:" if bypass_cache else "cached:") + cache_key
        result, shared = self._singleflight.do(flight_key, load_once)
        if shared:
            result = dict(result)
            result["cached"] = not bypass_cache
        return result

    def positions(self, query: dict[str, Any], *, bypass_cache: bool = False) -> dict[str, Any]:
        city_code, route_id = self._position_query(query)
        mode = "fixture" if self.settings.fixture_mode else "live"
        cache_key = f"positions:{city_code}:{route_id}:{mode}"
        if not bypass_cache:
            cached = self.store.get_cache(cache_key)
            if cached:
                cached["cached"] = True
                return cached

        def load_once():
            if not bypass_cache:
                cached = self.store.get_cache(cache_key)
                if cached:
                    cached["cached"] = True
                    return cached
            try:
                upstream = fetch_positions(
                    city_code=city_code,
                    route_id=route_id,
                    service_key=self.settings.tago_service_key,
                    timeout_seconds=self.settings.tago_timeout_seconds,
                    fixture_mode=self.settings.fixture_mode,
                    fixture_path=self.settings.position_fixture_path,
                )
                normalized, metadata = normalize_positions(upstream, route_id=route_id)
            except (TagoError, OSError, json.JSONDecodeError) as exc:
                if isinstance(exc, TagoError):
                    raise AppError(exc.code, exc.message, status=exc.status) from exc
                raise AppError("POSITION_FIXTURE_INVALID", "Position fixture could not be loaded", status=500) from exc

            result = {
                "ok": True,
                "mode": mode,
                "source": "TAGO_POSITION_FIXTURE" if self.settings.fixture_mode else "TAGO_POSITION",
                "retrieved_at": iso_utc(self.clock()),
                "timezone": "Asia/Seoul",
                "cached": False,
                "query": {"city_code": city_code, "route_id": route_id},
                "positions": normalized,
                "upstream": metadata,
            }
            if not bypass_cache:
                self.store.put_cache(cache_key, result, self.settings.cache_ttl_seconds)
            return result

        flight_key = ("fresh:" if bypass_cache else "cached:") + cache_key
        result, shared = self._singleflight.do(flight_key, load_once)
        if shared:
            result = dict(result)
            result["cached"] = not bypass_cache
        return result

    def collect_positions(
        self,
        body: dict[str, Any],
        *,
        header_idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        city_code, route_id = self._position_query(body)
        request_hash = canonical_hash({"city_code": city_code, "route_id": route_id})
        supplied_key = header_idempotency_key or body.get("idempotency_key")
        if supplied_key is not None:
            supplied_key = str(supplied_key)
            if not IDEMPOTENCY_RE.fullmatch(supplied_key):
                raise AppError(
                    "INVALID_IDEMPOTENCY_KEY",
                    "Idempotency-Key must be 8-128 safe ASCII characters",
                )
            idempotency_key = supplied_key
        else:
            bucket = int(self.clock().timestamp()) // 30
            idempotency_key = "position:auto:" + canonical_hash(
                {"request": request_hash, "bucket": bucket}
            )

        result, shared = self._singleflight.do(
            f"position-collect:{idempotency_key}",
            lambda: self._collect_positions_once(
                city_code=city_code,
                route_id=route_id,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
            ),
        )
        payload, status = result
        snapshot = payload["snapshot"]
        if snapshot["city_code"] != city_code or snapshot["route_id"] != route_id:
            raise AppError(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was already used for another request",
                status=409,
            )
        if shared:
            payload = dict(payload)
            payload["created"] = False
            return payload, 200
        return payload, status

    def _collect_positions_once(
        self,
        *,
        city_code: str,
        route_id: str,
        request_hash: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], int]:
        existing = self.store.get_position_snapshot_by_idempotency(idempotency_key)
        if existing:
            if existing["request_hash"] != request_hash:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was already used for another request",
                    status=409,
                )
            events = self.store.position_snapshot_events(existing["snapshot_id"])
            return {
                "ok": True,
                "created": False,
                "snapshot": self._public_position_snapshot(existing),
                "passages": events,
            }, 200

        position_result = self.positions(
            {"city_code": city_code, "route_id": route_id}, bypass_cache=True
        )
        captured_at = position_result["retrieved_at"]
        try:
            captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AppError("INVALID_CAPTURE_TIME", "Server capture time is invalid", status=500) from exc
        service_date = captured.astimezone(SEOUL_TZ).date().isoformat()
        payload_hash = canonical_hash(position_result["positions"])
        snapshot_id = "pos_" + canonical_hash(
            {"key": idempotency_key, "request": request_hash, "payload": payload_hash}
        )[:24]
        try:
            snapshot, created, events = self.store.create_position_snapshot(
                snapshot_id=snapshot_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                payload_hash=payload_hash,
                source=position_result["source"],
                city_code=city_code,
                route_id=route_id,
                captured_at=captured_at,
                service_date=service_date,
                upstream=position_result["upstream"],
                positions=position_result["positions"],
                maximum_gap_seconds=self.settings.position_gap_seconds,
            )
        except IdempotencyConflict as exc:
            raise AppError("IDEMPOTENCY_CONFLICT", str(exc), status=409) from exc
        counts = Counter(event["status"] for event in events)
        payload = {
            "ok": True,
            "created": created,
            "snapshot": self._public_position_snapshot(snapshot),
            "passages": events,
            "reconstruction": {
                "precision": "polling_window",
                "passage_count": counts["PASSAGE"],
                "data_gap_count": counts["DATA_GAP"],
                "regression_count": counts["REGRESSION"],
            },
        }
        return payload, (201 if created else 200)

    def collect(self, body: dict[str, Any], *, header_idempotency_key: str | None = None) -> tuple[dict[str, Any], int]:
        city_code, node_id = self._arrival_query(body)
        request_hash = canonical_hash({"city_code": city_code, "node_id": node_id})
        supplied_key = header_idempotency_key or body.get("idempotency_key")
        if supplied_key is not None:
            supplied_key = str(supplied_key)
            if not IDEMPOTENCY_RE.fullmatch(supplied_key):
                raise AppError(
                    "INVALID_IDEMPOTENCY_KEY",
                    "Idempotency-Key must be 8-128 safe ASCII characters",
                )
            idempotency_key = supplied_key
        else:
            bucket = int(self.clock().timestamp()) // 300
            idempotency_key = "auto:" + canonical_hash({"request": request_hash, "bucket": bucket})

        result, shared = self._singleflight.do(
            f"collect:{idempotency_key}",
            lambda: self._collect_once(
                city_code=city_code,
                node_id=node_id,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
            ),
        )
        payload, status = result
        snapshot = payload["snapshot"]
        if snapshot["city_code"] != city_code or snapshot["node_id"] != node_id:
            raise AppError(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was already used for another request",
                status=409,
            )
        if shared:
            payload = dict(payload)
            payload["created"] = False
            return payload, 200
        return payload, status

    def _collect_once(
        self,
        *,
        city_code: str,
        node_id: str,
        request_hash: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], int]:
        existing = self.store.get_snapshot_by_idempotency(idempotency_key)
        if existing:
            if existing["request_hash"] != request_hash:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was already used for another request",
                    status=409,
                )
            return {"ok": True, "created": False, "snapshot": self._public_snapshot(existing)}, 200

        arrival_result = self.arrivals(
            {"city_code": city_code, "node_id": node_id}, bypass_cache=True
        )
        captured_at = arrival_result["retrieved_at"]
        payload_hash = canonical_hash(arrival_result["arrivals"])
        snapshot_id = "snap_" + canonical_hash(
            {"key": idempotency_key, "request": request_hash, "payload": payload_hash}
        )[:24]
        try:
            snapshot, created = self.store.create_snapshot(
                snapshot_id=snapshot_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                payload_hash=payload_hash,
                source=arrival_result["source"],
                city_code=city_code,
                node_id=node_id,
                captured_at=captured_at,
                upstream=arrival_result["upstream"],
                arrivals=arrival_result["arrivals"],
            )
        except IdempotencyConflict as exc:
            raise AppError("IDEMPOTENCY_CONFLICT", str(exc), status=409) from exc
        return {"ok": True, "created": created, "snapshot": self._public_snapshot(snapshot)}, (201 if created else 200)

    def history(self, query: dict[str, str]) -> dict[str, Any]:
        route_id = self._optional_identifier(query.get("route_id"), "route_id")
        city_code = query.get("city_code") or None
        if city_code and not CITY_CODE_RE.fullmatch(city_code):
            raise AppError("INVALID_CITY_CODE", "city_code must contain 1-9 digits")
        node_id = query.get("node_id") or None
        if node_id and not NODE_ID_RE.fullmatch(node_id):
            raise AppError("INVALID_NODE_ID", "node_id has an invalid format")

        from_value = self._history_boundary(query.get("from"), end=False)
        to_value = self._history_boundary(query.get("to"), end=True)
        if from_value and to_value and from_value > to_value:
            raise AppError("INVALID_DATE_RANGE", "from must be earlier than or equal to to")
        limit = self._bounded_int(query.get("limit", "100"), "limit", 1, 500)
        records = self.store.history(
            route_id=route_id,
            city_code=city_code,
            node_id=node_id,
            from_value=from_value,
            to_value=to_value,
            limit=limit,
        )
        return {
            "ok": True,
            "filters": {
                "route_id": route_id,
                "city_code": city_code,
                "node_id": node_id,
                "from": from_value,
                "to": to_value,
            },
            "count": len(records),
            "snapshots": [self._public_snapshot(record) for record in records],
        }

    def passage_history(self, query: dict[str, str]) -> dict[str, Any]:
        route_id = self._optional_identifier(query.get("route_id"), "route_id")
        node_id = query.get("node_id") or None
        if node_id and not NODE_ID_RE.fullmatch(node_id):
            raise AppError("INVALID_NODE_ID", "node_id has an invalid format")
        vehicle_no = query.get("vehicle_no") or None
        if vehicle_no:
            vehicle_no = self._vehicle_no(vehicle_no, "vehicle_no")
        status = (query.get("status") or "").upper() or None
        if status and status not in PASSAGE_STATUSES:
            raise AppError(
                "INVALID_PASSAGE_STATUS",
                "status must be PASSAGE, DATA_GAP, or REGRESSION",
            )
        from_date = self._kst_date_filter(query.get("from"), "from")
        to_date = self._kst_date_filter(query.get("to"), "to")
        if from_date and to_date and from_date > to_date:
            raise AppError("INVALID_DATE_RANGE", "from must be earlier than or equal to to")
        limit = self._bounded_int(query.get("limit", "100"), "limit", 1, 500)
        rows = self.store.passages(
            route_id=route_id,
            vehicle_no=vehicle_no,
            node_id=node_id,
            status=status,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
        return {
            "ok": True,
            "timezone": "Asia/Seoul",
            "filters": {
                "route_id": route_id,
                "node_id": node_id,
                "vehicle_no": vehicle_no,
                "status": status,
                "from": from_date,
                "to": to_date,
            },
            "count": len(rows),
            "passages": rows,
        }

    def replay(self, body: dict[str, Any]) -> dict[str, Any]:
        route = body.get("route")
        if isinstance(route, str):
            route_info = {"id": self._identifier(route, "route")}
        elif isinstance(route, dict):
            route_info = {"id": self._identifier(route.get("id"), "route.id")}
            if route.get("name") is not None:
                route_info["name"] = self._short_text(route["name"], "route.name", 100)
        else:
            raise AppError("INVALID_ROUTE", "route must be an id string or object with id")
        legs_input = body.get("legs")
        if not isinstance(legs_input, list) or not 1 <= len(legs_input) <= 12:
            raise AppError("INVALID_LEGS", "legs must contain 1-12 route-mapped checkpoints")
        legs = [self._replay_leg(item, index) for index, item in enumerate(legs_input)]
        dates = self._simulation_dates(body.get("dates"))
        workload = len(legs) * len(dates)
        if workload > 300:
            raise AppError(
                "REPLAY_TOO_LARGE",
                "dates × legs must not exceed 300",
                status=413,
                details={"workload": workload, "maximum": 300},
            )
        match_window = self._bounded_int(
            body.get("match_window_minutes", 180), "match_window_minutes", 15, 360
        )

        cache: dict[tuple[str, str, str | None], list[dict[str, Any]]] = {}
        processed_events = 0
        daily: list[dict[str, Any]] = []
        reason_counts: Counter[str] = Counter()
        for service_date in dates:
            leg_results: list[dict[str, Any]] = []
            for leg in legs:
                cache_key = (leg["route_id"], service_date.isoformat(), leg["vehicle_no"])
                if cache_key not in cache:
                    events = self.store.replay_events(
                        route_id=leg["route_id"],
                        service_date=service_date.isoformat(),
                        vehicle_no=leg["vehicle_no"],
                        limit=500,
                    )
                    processed_events += len(events)
                    if processed_events > 100_000:
                        raise AppError(
                            "REPLAY_TOO_LARGE",
                            "Replay event scan exceeded 100000 records",
                            status=413,
                        )
                    cache[cache_key] = events
                leg_results.append(
                    self._replay_leg_result(
                        leg,
                        service_date,
                        cache[cache_key],
                        match_window_minutes=match_window,
                    )
                )

            if any(item["status"] == "data_gap" for item in leg_results):
                day_status = "data_gap"
                reason = next(
                    item["reason"] for item in leg_results if item["status"] == "data_gap"
                )
            elif any(item["status"] == "failure" for item in leg_results):
                day_status = "failure"
                reason = next(
                    item["reason"] for item in leg_results if item["status"] == "failure"
                )
            else:
                day_status, reason = "success", "ALL_CONNECTIONS_CONFIRMED"
            reason_counts[reason] += 1
            daily.append(
                {
                    "date": service_date.isoformat(),
                    "status": day_status,
                    "reason": reason,
                    "legs": leg_results,
                }
            )

        success_days = sum(item["status"] == "success" for item in daily)
        failure_days = sum(item["status"] == "failure" for item in daily)
        gap_days = sum(item["status"] == "data_gap" for item in daily)
        eligible_days = success_days + failure_days
        return {
            "ok": True,
            "route": route_info,
            "timezone": "Asia/Seoul",
            "basis": {
                "mode": "fixture" if self.settings.fixture_mode else "live",
                "evidence": "route-mapped_vehicle_position_transitions",
                "precision": "polling_window",
                "match_window_minutes": match_window,
                "data_gap_excluded_from_denominator": True,
                "events_scanned": processed_events,
            },
            "daily": daily,
            "summary": {
                "days": len(daily),
                "eligible_days": eligible_days,
                "success_days": success_days,
                "failure_days": failure_days,
                "gap_days": gap_days,
                "success_rate": (round(success_days / eligible_days, 4) if eligible_days else None),
                "failure_rate": (round(failure_days / eligible_days, 4) if eligible_days else None),
                "reason_counts": dict(reason_counts),
            },
        }

    def simulate(self, body: dict[str, Any]) -> dict[str, Any]:
        route = body.get("route")
        if isinstance(route, str):
            route_info = {"id": self._identifier(route, "route")}
        elif isinstance(route, dict):
            route_info = {"id": self._identifier(route.get("id"), "route.id")}
            if route.get("name") is not None:
                route_info["name"] = self._short_text(route["name"], "route.name", 100)
        else:
            raise AppError("INVALID_ROUTE", "route must be an id string or object with id")

        legs_input = body.get("legs")
        if not isinstance(legs_input, list) or not 1 <= len(legs_input) <= 12:
            raise AppError("INVALID_LEGS", "legs must contain 1-12 transfer legs")
        legs = [self._simulation_leg(item, index) for index, item in enumerate(legs_input)]
        dates = self._simulation_dates(body.get("dates"))
        trials = self._bounded_int(body.get("trials", 1000), "trials", 100, 5000)
        workload = len(dates) * trials * len(legs)
        if workload > 500_000:
            raise AppError(
                "SIMULATION_TOO_LARGE",
                "dates × trials × legs must not exceed 500000",
                status=413,
                details={"workload": workload, "maximum": 500_000},
            )
        if not self.settings.fixture_mode:
            raise AppError(
                "PASSAGE_HISTORY_REQUIRED",
                "Live simulation requires reconstructed vehicle passage history; arrival ETA snapshots are not passage events",
                status=422,
                details={
                    "required": "route-mapped vehicle location passage events",
                    "next_step": "enable TAGO bus location data and route mapping, then reconstruct stop passages",
                },
            )
        seed = self._bounded_int(body.get("seed", 20260831), "seed", 0, 2_147_483_647)
        threshold_value = body.get("success_threshold", 0.8)
        try:
            threshold = float(threshold_value)
        except (TypeError, ValueError) as exc:
            raise AppError("INVALID_SUCCESS_THRESHOLD", "success_threshold must be a number") from exc
        if not 0.5 <= threshold <= 0.99:
            raise AppError("INVALID_SUCCESS_THRESHOLD", "success_threshold must be between 0.5 and 0.99")

        sample_sets: dict[str, list[float]] = {}
        sample_basis: dict[str, str] = {}
        sample_counts: dict[str, int] = {}
        for leg in legs:
            samples = leg["fallback_delay_minutes"]
            basis = "request_fallback_assumption"
            if len(samples) < 8:
                samples = self._fixture_delay_samples(leg["route_id"], weekend=False)
                basis = "fixture_model"
            if len(samples) < 8:
                # Fixture files are controlled test assets. Refuse a malformed
                # fixture rather than silently running an underspecified model.
                raise AppError(
                    "INSUFFICIENT_FIXTURE_SAMPLES",
                    "Fixture simulation requires at least 8 delay samples per leg",
                    status=422,
                    details={"leg": leg["id"]},
                )
            sample_sets[leg["id"]] = samples
            sample_basis[leg["id"]] = basis
            sample_counts[leg["id"]] = len(samples)

        daily: list[dict[str, Any]] = []
        summary_reasons: Counter[str] = Counter()
        for service_date in dates:
            rng = random.Random(f"{seed}:{route_info['id']}:{service_date.isoformat()}")
            successes = 0
            failure_reasons: Counter[str] = Counter()
            for _ in range(trials):
                failed_leg: str | None = None
                for leg in legs:
                    samples = sample_sets[leg["id"]]
                    if sample_basis[leg["id"]] == "fixture_model":
                        samples = self._fixture_delay_samples(
                            leg["route_id"], weekend=service_date.weekday() >= 5
                        )
                    delay = rng.choice(samples)
                    if delay > leg["delay_budget_minutes"]:
                        failed_leg = leg["id"]
                        break
                if failed_leg is None:
                    successes += 1
                else:
                    failure_reasons[f"MISSED_TRANSFER:{failed_leg}"] += 1

            probability = successes / trials
            status = "success" if probability >= threshold else "failure"
            reason = "SUCCESS_PROBABILITY_AT_OR_ABOVE_THRESHOLD"
            critical_leg_id = None
            if failure_reasons:
                top_reason, _ = failure_reasons.most_common(1)[0]
                critical_leg_id = top_reason.split(":", 1)[1]
                if status == "failure":
                    reason = top_reason
            summary_reasons[reason] += 1
            daily.append(
                {
                    "date": service_date.isoformat(),
                    "status": status,
                    "reason": reason,
                    "success_probability": round(probability, 4),
                    "failure_probability": round(1.0 - probability, 4),
                    "critical_leg_id": critical_leg_id,
                    "trials": trials,
                }
            )

        success_days = sum(1 for item in daily if item["status"] == "success")
        average_probability = sum(item["success_probability"] for item in daily) / len(daily)
        return {
            "ok": True,
            "route": route_info,
            "basis": {
                "mode": "fixture" if self.settings.fixture_mode else "live",
                "trials_per_day": trials,
                "seed": seed,
                "success_threshold": threshold,
                "legs": [
                    {
                        "leg_id": leg["id"],
                        "source": sample_basis[leg["id"]],
                        "sample_count": sample_counts[leg["id"]],
                        "delay_budget_minutes": leg["delay_budget_minutes"],
                    }
                    for leg in legs
                ],
            },
            "daily": daily,
            "summary": {
                "days": len(daily),
                "success_days": success_days,
                "failure_days": len(daily) - success_days,
                "average_success_probability": round(average_probability, 4),
                "reason_counts": dict(summary_reasons),
            },
        }

    def _arrival_query(self, data: dict[str, Any]) -> tuple[str, str]:
        city_code = str(data.get("city_code") or "")
        node_id = str(data.get("node_id") or "")
        if not CITY_CODE_RE.fullmatch(city_code):
            raise AppError("INVALID_CITY_CODE", "city_code must contain 1-9 digits")
        if not NODE_ID_RE.fullmatch(node_id):
            raise AppError("INVALID_NODE_ID", "node_id must be a 2-64 character safe identifier")
        return city_code, node_id

    def _position_query(self, data: dict[str, Any]) -> tuple[str, str]:
        city_code = str(data.get("city_code") or "")
        if not CITY_CODE_RE.fullmatch(city_code):
            raise AppError("INVALID_CITY_CODE", "city_code must contain 1-9 digits")
        route_id = self._identifier(data.get("route_id"), "route_id")
        return city_code, route_id

    @staticmethod
    def _public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        public = {key: value for key, value in snapshot.items() if key != "request_hash"}
        public["observation_kind"] = "arrival_eta"
        public["passage_evidence"] = False
        return public

    @staticmethod
    def _public_position_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        public = {key: value for key, value in snapshot.items() if key != "request_hash"}
        public["observation_kind"] = "vehicle_position"
        public["passage_evidence"] = "derived_from_consecutive_node_order"
        public["precision"] = "polling_window"
        return public

    def _replay_leg(self, value: Any, index: int) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise AppError("INVALID_LEG", f"legs[{index}] must be an object")
        leg_id = self._identifier(value.get("id") or f"leg-{index + 1}", f"legs[{index}].id")
        route_id = self._identifier(value.get("route_id"), f"legs[{index}].route_id")
        node_id = str(value.get("node_id") or "")
        if not NODE_ID_RE.fullmatch(node_id):
            raise AppError("INVALID_NODE_ID", f"legs[{index}].node_id has an invalid format")
        node_order = self._bounded_int(
            value.get("node_order"), f"legs[{index}].node_order", 1, 9999
        )
        scheduled = self._clock_minutes(
            value.get("scheduled_arrival"), f"legs[{index}].scheduled_arrival"
        )
        next_departure = self._clock_minutes(
            value.get("next_departure"), f"legs[{index}].next_departure"
        )
        if next_departure < scheduled:
            next_departure += 24 * 60
        minimum_transfer = self._bounded_int(
            value.get("minimum_transfer_minutes", 5),
            f"legs[{index}].minimum_transfer_minutes",
            0,
            60,
        )
        vehicle_no = value.get("vehicle_no")
        if vehicle_no is not None:
            vehicle_no = self._vehicle_no(vehicle_no, f"legs[{index}].vehicle_no")
        return {
            "id": leg_id,
            "route_id": route_id,
            "node_id": node_id,
            "node_order": node_order,
            "vehicle_no": vehicle_no,
            "scheduled_minutes": scheduled,
            "next_departure_minutes": next_departure,
            "minimum_transfer_minutes": minimum_transfer,
        }

    def _replay_leg_result(
        self,
        leg: dict[str, Any],
        service_date: date,
        events: list[dict[str, Any]],
        *,
        match_window_minutes: int,
    ) -> dict[str, Any]:
        scheduled = datetime.combine(service_date, datetime_time.min, tzinfo=SEOUL_TZ) + timedelta(
            minutes=leg["scheduled_minutes"]
        )
        deadline = datetime.combine(service_date, datetime_time.min, tzinfo=SEOUL_TZ) + timedelta(
            minutes=leg["next_departure_minutes"] - leg["minimum_transfer_minutes"]
        )
        matching: list[tuple[float, dict[str, Any], datetime, datetime]] = []
        anomalies: list[dict[str, Any]] = []
        for event in events:
            try:
                observed_from = datetime.fromisoformat(event["observed_from"].replace("Z", "+00:00"))
                observed_to = datetime.fromisoformat(event["observed_to"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            target_crossed = (
                int(event["from_node_order"]) < leg["node_order"] <= int(event["node_order"])
                if int(event["node_order_delta"]) > 0
                else int(event["node_order"]) == leg["node_order"]
            )
            exact_target = (
                event["node_id"] == leg["node_id"]
                and int(event["node_order"]) == leg["node_order"]
            )
            if event["status"] == "PASSAGE" and exact_target:
                midpoint = observed_from + (observed_to - observed_from) / 2
                distance = abs((midpoint.astimezone(SEOUL_TZ) - scheduled).total_seconds()) / 60
                if distance <= match_window_minutes:
                    matching.append((distance, event, observed_from, observed_to))
            elif event["status"] in {"DATA_GAP", "REGRESSION"} and target_crossed:
                anomalies.append(event)

        base = {
            "leg_id": leg["id"],
            "route_id": leg["route_id"],
            "node_id": leg["node_id"],
            "node_order": leg["node_order"],
            "vehicle_no": leg["vehicle_no"],
        }
        if not matching:
            reason = "NO_PASSAGE_EVIDENCE"
            if anomalies:
                reason = anomalies[0].get("gap_reason") or "AMBIGUOUS_POSITION_TRANSITION"
            return {
                **base,
                "status": "data_gap",
                "reason": reason,
                "precision": "polling_window",
                "passage": None,
            }

        _distance, event, observed_from, observed_to = min(matching, key=lambda item: item[0])
        observed_from_kst = observed_from.astimezone(SEOUL_TZ)
        observed_to_kst = observed_to.astimezone(SEOUL_TZ)
        passage = {
            "passage_id": event["passage_id"],
            "city_code": event["city_code"],
            "route_id": event["route_id"],
            "node_id": event["node_id"],
            "vehicle_no": event["vehicle_no"],
            "service_date": event["service_date"],
            "observed_from": event["observed_from"],
            "observed_to": event["observed_to"],
            "observed_from_kst": observed_from_kst.isoformat(),
            "observed_to_kst": observed_to_kst.isoformat(),
            "precision": event["precision"],
            "status": event["status"],
        }
        cutoff_utc = deadline.astimezone(timezone.utc)
        if observed_to <= cutoff_utc:
            confirmed_margin = (cutoff_utc - observed_to).total_seconds() / 60
            return {
                **base,
                "status": "success",
                "reason": "CONNECTION_CONFIRMED_WITH_POLLING_WINDOW",
                "confirmed_margin_minutes": round(confirmed_margin, 2),
                "precision": "polling_window",
                "passage": passage,
            }
        if observed_from > cutoff_utc:
            missed_by = (observed_from - cutoff_utc).total_seconds() / 60
            return {
                **base,
                "status": "failure",
                "reason": "CONNECTION_MISSED_AFTER_POLLING_WINDOW",
                "missed_by_minutes": round(missed_by, 2),
                "precision": "polling_window",
                "passage": passage,
            }
        return {
            **base,
            "status": "data_gap",
            "reason": "PASSAGE_WINDOW_CROSSES_DEADLINE",
            "precision": "polling_window",
            "passage": passage,
        }

    def _simulation_leg(self, value: Any, index: int) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise AppError("INVALID_LEG", f"legs[{index}] must be an object")
        leg_id = self._identifier(value.get("id") or f"leg-{index + 1}", f"legs[{index}].id")
        route_id = self._identifier(value.get("route_id"), f"legs[{index}].route_id")
        node_id = value.get("node_id")
        if node_id is not None and not NODE_ID_RE.fullmatch(str(node_id)):
            raise AppError("INVALID_NODE_ID", f"legs[{index}].node_id has an invalid format")
        scheduled = self._clock_minutes(value.get("scheduled_arrival"), f"legs[{index}].scheduled_arrival")
        next_departure = self._clock_minutes(value.get("next_departure"), f"legs[{index}].next_departure")
        if next_departure < scheduled:
            next_departure += 24 * 60
        minimum_transfer = self._bounded_int(
            value.get("minimum_transfer_minutes", 5),
            f"legs[{index}].minimum_transfer_minutes",
            0,
            60,
        )
        fallback = value.get("fallback_delay_minutes", [])
        if not isinstance(fallback, list) or len(fallback) > 500:
            raise AppError("INVALID_DELAY_SAMPLES", f"legs[{index}].fallback_delay_minutes must be a list of at most 500 numbers")
        parsed_fallback: list[float] = []
        for sample in fallback:
            try:
                sample_value = float(sample)
            except (TypeError, ValueError) as exc:
                raise AppError("INVALID_DELAY_SAMPLES", f"legs[{index}] contains a non-numeric delay sample") from exc
            if not -30 <= sample_value <= 240:
                raise AppError("INVALID_DELAY_SAMPLES", f"legs[{index}] delay samples must be between -30 and 240 minutes")
            parsed_fallback.append(sample_value)
        return {
            "id": leg_id,
            "route_id": route_id,
            "node_id": str(node_id) if node_id is not None else None,
            "scheduled_arrival": str(value.get("scheduled_arrival")),
            "scheduled_minutes": scheduled,
            "delay_budget_minutes": next_departure - scheduled - minimum_transfer,
            "fallback_delay_minutes": parsed_fallback,
        }

    def _historical_delay_samples(self, leg: dict[str, Any]) -> list[float]:
        """Diagnostic ETA projection offsets only; never passage evidence.

        This helper remains for QA of collected ETA data. `simulate` does not
        consume it because repeated arrival predictions are not actual vehicle
        stop-passage observations.
        """
        observations = self.store.delay_observations(
            route_id=leg["route_id"], node_id=leg["node_id"]
        )
        samples: list[float] = []
        scheduled_clock = datetime_time(
            hour=leg["scheduled_minutes"] // 60,
            minute=leg["scheduled_minutes"] % 60,
            tzinfo=SEOUL_TZ,
        )
        for observation in observations:
            try:
                observed = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                projected = observed.astimezone(SEOUL_TZ) + timedelta(
                    seconds=int(observation["arrival_seconds"])
                )
            except (ValueError, TypeError, OverflowError):
                continue
            candidates = [
                datetime.combine(projected.date() + timedelta(days=offset), scheduled_clock)
                for offset in (-1, 0, 1)
            ]
            scheduled_datetime = min(candidates, key=lambda item: abs((projected - item).total_seconds()))
            delay = (projected - scheduled_datetime).total_seconds() / 60.0
            if -30 <= delay <= 180:
                samples.append(round(delay, 2))
        return samples

    def _fixture_delay_samples(self, route_id: str, *, weekend: bool) -> list[float]:
        if self._fixture_delays is None:
            self._fixture_delays = json.loads(self.settings.fixture_delays_path.read_text(encoding="utf-8"))
        route_samples = self._fixture_delays.get(route_id) or self._fixture_delays.get("*") or {}
        key = "weekend" if weekend else "weekday"
        return [float(value) for value in route_samples.get(key, [])]

    def _simulation_dates(self, value: Any) -> list[date]:
        if isinstance(value, list):
            if not 1 <= len(value) <= 31:
                raise AppError("INVALID_DATES", "dates must contain 1-31 ISO dates")
            parsed = [self._date(item, "dates") for item in value]
            return sorted(set(parsed))
        if isinstance(value, dict):
            start = self._date(value.get("from"), "dates.from")
            end = self._date(value.get("to"), "dates.to")
            days = (end - start).days
            if days < 0 or days > 30:
                raise AppError("INVALID_DATES", "date range must be forward and at most 31 days")
            return [start + timedelta(days=offset) for offset in range(days + 1)]
        raise AppError("INVALID_DATES", "dates must be an ISO date list or {from,to}")

    @staticmethod
    def _date(value: Any, field: str) -> date:
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise AppError("INVALID_DATE", f"{field} must be YYYY-MM-DD") from exc

    @staticmethod
    def _clock_minutes(value: Any, field: str) -> int:
        try:
            parsed = datetime.strptime(str(value), "%H:%M")
        except ValueError as exc:
            raise AppError("INVALID_TIME", f"{field} must be HH:MM") from exc
        return parsed.hour * 60 + parsed.minute

    @staticmethod
    def _history_boundary(value: str | None, *, end: bool) -> str | None:
        if value is None or value == "":
            return None
        try:
            if len(value) == 10:
                parsed = datetime.combine(date.fromisoformat(value), datetime_time.max if end else datetime_time.min, tzinfo=timezone.utc)
            else:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError as exc:
            raise AppError("INVALID_DATETIME", "from/to must be ISO-8601 date or datetime") from exc

    @staticmethod
    def _kst_date_filter(value: str | None, field: str) -> str | None:
        if value is None or value == "":
            return None
        try:
            if len(value) == 10:
                return date.fromisoformat(value).isoformat()
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=SEOUL_TZ)
            return parsed.astimezone(SEOUL_TZ).date().isoformat()
        except ValueError as exc:
            raise AppError("INVALID_DATETIME", f"{field} must be an ISO-8601 date or datetime") from exc

    @staticmethod
    def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise AppError("INVALID_INTEGER", f"{field} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise AppError("INVALID_INTEGER", f"{field} must be an integer") from exc
        if str(parsed) != str(value) and not isinstance(value, int):
            raise AppError("INVALID_INTEGER", f"{field} must be an integer")
        if not minimum <= parsed <= maximum:
            raise AppError("OUT_OF_RANGE", f"{field} must be between {minimum} and {maximum}")
        return parsed

    @staticmethod
    def _only_fields(data: dict[str, Any], allowed: set[str], location: str) -> None:
        unexpected = sorted(set(data) - allowed)
        if unexpected:
            raise AppError(
                "UNEXPECTED_FIELD",
                f"Unexpected {location} fields",
                details=unexpected,
            )

    @staticmethod
    def _city_code(value: Any, field: str) -> str:
        parsed = str(value or "").strip()
        if not CITY_CODE_RE.fullmatch(parsed):
            raise AppError("INVALID_CITY_CODE", f"{field} must contain 1-9 digits")
        return parsed

    @staticmethod
    def _node_id(value: Any, field: str) -> str:
        parsed = str(value or "").strip()
        if not NODE_ID_RE.fullmatch(parsed):
            raise AppError("INVALID_NODE_ID", f"{field} must be a 2-64 character safe identifier")
        return parsed

    def _pagination(self, data: dict[str, Any]) -> tuple[int, int]:
        page = self._bounded_int(data.get("page", 1), "page", 1, 100_000)
        limit = self._bounded_int(data.get("limit", 100), "limit", 1, 100)
        return page, limit

    @staticmethod
    def _coordinate(value: Any, field: str, minimum: float, maximum: float) -> float:
        if isinstance(value, bool):
            raise AppError("INVALID_COORDINATE", f"{field} must be numeric")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise AppError("INVALID_COORDINATE", f"{field} must be numeric") from exc
        if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
            raise AppError(
                "INVALID_COORDINATE", f"{field} must be between {minimum} and {maximum}"
            )
        return parsed

    @staticmethod
    def _catalog_text(value: Any, field: str, limit: int) -> str:
        parsed = str(value or "").strip()
        if not parsed or len(parsed) > limit or any(ord(character) < 32 for character in parsed):
            raise AppError("INVALID_TEXT", f"{field} must be 1-{limit} printable characters")
        return parsed

    @staticmethod
    def _filter_fixture_catalog(
        operation: str,
        query: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Make schema fixtures deterministic without presenting them as coverage."""
        filtered = records
        city_code = query.get("city_code")
        route_id = query.get("route_id")
        if city_code and operation != "cities":
            filtered = [record for record in filtered if record.get("city_code") == city_code]
        if route_id and operation in {"route_info", "route_stops"}:
            filtered = [record for record in filtered if record.get("route_id") == route_id]
        route_no = query.get("route_no")
        if route_no and operation == "routes":
            filtered = [record for record in filtered if route_no in str(record.get("route_no") or "")]
        node_name = query.get("node_name")
        if node_name and operation == "stops":
            filtered = [record for record in filtered if node_name in str(record.get("node_name") or "")]
        node_no = query.get("node_no")
        if node_no and operation == "stops":
            filtered = [record for record in filtered if record.get("node_no") == node_no]
        page = int(query.get("page") or 1)
        limit = int(query.get("limit") or len(filtered) or 1)
        start = (page - 1) * limit
        return filtered[start : start + limit]

    def _identifier(self, value: Any, field: str) -> str:
        parsed = str(value or "")
        if not IDENTIFIER_RE.fullmatch(parsed):
            raise AppError("INVALID_IDENTIFIER", f"{field} must be 1-64 safe identifier characters")
        return parsed

    def _optional_identifier(self, value: Any, field: str) -> str | None:
        return None if value is None or value == "" else self._identifier(value, field)

    @staticmethod
    def _vehicle_no(value: Any, field: str) -> str:
        parsed = str(value or "").strip()
        if not VEHICLE_NO_RE.fullmatch(parsed):
            raise AppError(
                "INVALID_VEHICLE_NO",
                f"{field} must be 1-32 Korean letters, ASCII letters, digits, or hyphens",
            )
        return parsed

    @staticmethod
    def _short_text(value: Any, field: str, limit: int) -> str:
        parsed = str(value).strip()
        if not parsed or len(parsed) > limit:
            raise AppError("INVALID_TEXT", f"{field} must be 1-{limit} characters")
        return parsed
