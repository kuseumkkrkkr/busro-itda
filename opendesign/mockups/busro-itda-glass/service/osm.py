"""OSM route geometry resolver with an explicit accuracy fallback ladder.

The primary result is an existing OSM ``route=bus`` relation.  If a matching
relation is not mapped, ordered official stop coordinates can be routed over
OSM roads through OSRM.  That second result is deliberately labelled as an
estimate: it is not evidence of the operator's exact driven path.
"""

from __future__ import annotations

import json
import math
import re
import socket
import threading
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSRM_URL = "https://router.project-osrm.org"
ROUTE_REF_RE = re.compile(r"^[0-9A-Za-z가-힣._ -]{1,24}$")
MAX_STOPS = 160
MAX_UPSTREAM_BYTES = 4_000_000
OSRM_CHUNK_SIZE = 20
MAX_OSRM_CHUNKS = 9
MAX_CONCURRENT_GEOMETRY_REQUESTS = 3
GEOMETRY_ADMISSION_WAIT_SECONDS = 0.25
MAX_RESOLVE_TIMEOUT_SECONDS = 20.0
MAX_RELATION_LOOKUP_SECONDS = 8.0
MAX_RELATION_MEMBER_GAP_M = 500.0
MAX_RELATION_ENDPOINT_SNAP_M = 1_200.0
MAX_RELATION_STOP_SNAP_M = 1_500.0
MAX_RELATION_ENDPOINT_CANDIDATES = 8
MAX_RELATION_PAIR_CANDIDATES = 8
KOREA_LAT_RANGE = (32.0, 39.8)
KOREA_LON_RANGE = (123.0, 132.5)
_GEOMETRY_ADMISSION = threading.BoundedSemaphore(MAX_CONCURRENT_GEOMETRY_REQUESTS)
_UPSTREAM_HTTP_ADMISSION = threading.BoundedSemaphore(MAX_CONCURRENT_GEOMETRY_REQUESTS)


class OSMError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _resolve_deadline(timeout_seconds: float) -> float:
    try:
        requested = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise OSMError("INVALID_OSM_TIMEOUT", "OSM geometry timeout must be a positive number", status=400) from exc
    if not math.isfinite(requested) or requested <= 0:
        raise OSMError("INVALID_OSM_TIMEOUT", "OSM geometry timeout must be a positive number", status=400)
    return time.monotonic() + min(requested, MAX_RESOLVE_TIMEOUT_SECONDS)


def _deadline_timeout(deadline: float | None, requested: float) -> float:
    if deadline is None:
        return min(max(float(requested), 2.0), 15.0)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OSMError(
            "OSM_DEADLINE_EXCEEDED",
            "OSM geometry resolution exceeded its total time limit",
            status=504,
        )
    return min(remaining, 15.0)


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None:
        _deadline_timeout(deadline, 0.0)


def _validate_endpoint(value: str, expected_host: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != expected_host or parsed.username or parsed.password:
        raise OSMError("OSM_ENDPOINT_INVALID", "Configured OSM endpoint is not allowed", status=503)
    return value.rstrip("/")


def _coordinates(stops: Iterable[dict[str, Any]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for stop in stops:
        if len(result) >= MAX_STOPS:
            raise OSMError("TOO_MANY_STOPS", f"At most {MAX_STOPS} ordered stops are accepted", status=400)
        try:
            lat = float(stop.get("latitude", stop.get("gpslati")))
            lon = float(stop.get("longitude", stop.get("gpslong")))
        except (AttributeError, TypeError, ValueError) as exc:
            raise OSMError("INVALID_STOP_COORDINATE", "Every stop needs numeric latitude and longitude", status=400) from exc
        if not (KOREA_LAT_RANGE[0] <= lat <= KOREA_LAT_RANGE[1] and KOREA_LON_RANGE[0] <= lon <= KOREA_LON_RANGE[1]):
            raise OSMError("STOP_OUTSIDE_KOREA", "Stop coordinate is outside the supported Korean service area", status=400)
        point = (lat, lon)
        if not result or point != result[-1]:
            result.append(point)
    if len(result) < 2:
        raise OSMError("ROUTE_STOPS_REQUIRED", "At least two distinct ordered stop coordinates are required", status=422)
    return result


def _read_json_blocking(request: Request, *, timeout_seconds: float) -> bytes:
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read(MAX_UPSTREAM_BYTES + 1)


def _read_json(
    request: Request, *, timeout_seconds: float, _deadline: float | None = None,
) -> dict[str, Any]:
    """Read one upstream response without letting slow-drip I/O own the caller.

    ``urllib`` applies its timeout to socket inactivity, not to the whole body.
    Run the blocking read in a daemon worker and keep the admission permit until
    that worker really exits.  The caller observes a hard wall-clock timeout,
    while abandoned slow readers and their response buffers stay process-wide
    bounded by ``MAX_CONCURRENT_GEOMETRY_REQUESTS``.  Python cannot safely kill
    a stuck thread, so a permanently stalled reader intentionally keeps one of
    those permits for process lifetime; saturation fails fast instead of
    creating more reader threads.
    """
    started_at = time.monotonic()
    call_deadline = started_at + max(0.0, float(timeout_seconds))
    deadline = min(call_deadline, _deadline) if _deadline is not None else call_deadline

    def wall_timeout() -> OSMError:
        if _deadline is not None and _deadline <= call_deadline:
            return OSMError(
                "OSM_DEADLINE_EXCEEDED",
                "OSM geometry resolution exceeded its total time limit",
                status=504,
            )
        return OSMError("OSM_TIMEOUT", "OSM geometry service timed out", status=504)

    remaining = deadline - time.monotonic()
    admission = _UPSTREAM_HTTP_ADMISSION
    if remaining <= 0:
        raise wall_timeout()
    admission_wait = min(GEOMETRY_ADMISSION_WAIT_SECONDS, remaining)
    if not admission.acquire(timeout=admission_wait):
        if deadline - time.monotonic() <= 0:
            raise wall_timeout()
        raise OSMError(
            "OSM_BUSY",
            "OSM geometry capacity is busy; retry shortly",
            status=429,
        )

    finished = threading.Event()
    outcome: dict[str, Any] = {}

    def read_in_worker() -> None:
        try:
            socket_timeout = deadline - time.monotonic()
            if socket_timeout <= 0:
                raise wall_timeout()
            outcome["payload"] = _read_json_blocking(request, timeout_seconds=socket_timeout)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            admission.release()
            finished.set()

    try:
        worker = threading.Thread(target=read_in_worker, name="osm-upstream-read", daemon=True)
        worker.start()
    except Exception:
        admission.release()
        raise

    remaining = deadline - time.monotonic()
    if remaining <= 0 or not finished.wait(timeout=remaining):
        raise wall_timeout()

    try:
        if "error" in outcome:
            raise outcome["error"]
        payload = outcome["payload"]
    except HTTPError as exc:
        raise OSMError("OSM_HTTP_ERROR", f"OSM service returned HTTP {exc.code}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise OSMError("OSM_TIMEOUT", "OSM geometry service timed out", status=504) from exc
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise OSMError("OSM_TIMEOUT", "OSM geometry service timed out", status=504) from exc
        raise OSMError("OSM_UNAVAILABLE", "OSM geometry service could not be reached") from exc
    if len(payload) > MAX_UPSTREAM_BYTES:
        raise OSMError("OSM_RESPONSE_TOO_LARGE", "OSM geometry response exceeded 4 MB")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSMError("OSM_INVALID_JSON", "OSM geometry service returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise OSMError("OSM_INVALID_RESPONSE", "OSM geometry response must be an object")
    return value


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 12_742_000 * math.asin(min(1.0, math.sqrt(h)))


def _relation_lines(element: dict[str, Any]) -> list[list[list[float]]]:
    lines: list[list[list[float]]] = []
    for member in element.get("members", [])[:5000]:
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        geometry = member.get("geometry")
        if not isinstance(geometry, list):
            continue
        line: list[list[float]] = []
        for point in geometry[:5000]:
            if not isinstance(point, dict):
                continue
            try:
                lat, lon = float(point["lat"]), float(point["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if KOREA_LAT_RANGE[0] <= lat <= KOREA_LAT_RANGE[1] and KOREA_LON_RANGE[0] <= lon <= KOREA_LON_RANGE[1]:
                line.append([lon, lat])
        if len(line) >= 2:
            lines.append(line)
    return lines


def _relation_score(lines: list[list[list[float]]], ordered: list[tuple[float, float]]) -> float:
    sampled: list[tuple[float, float]] = []
    for line in lines:
        stride = max(1, len(line) // 100)
        sampled.extend((point[1], point[0]) for point in line[::stride])
        if line and (line[-1][1], line[-1][0]) != sampled[-1]:
            sampled.append((line[-1][1], line[-1][0]))
    if not sampled:
        return math.inf
    endpoint_distance = min(_haversine_m(ordered[0], point) for point in sampled)
    endpoint_distance += min(_haversine_m(ordered[-1], point) for point in sampled)
    # Prefer a relation that passes several official stops, not merely one with
    # the same route number in the search bounding box.
    probes = ordered[:: max(1, len(ordered) // 8)]
    stop_distance = sum(min(_haversine_m(stop, point) for point in sampled) for stop in probes)
    return endpoint_distance * 2 + stop_distance


def _endpoint_gap_m(left: list[list[float]], right: list[list[float]]) -> float:
    return min(
        _haversine_m((left_point[1], left_point[0]), (right_point[1], right_point[0]))
        for left_point in (left[0], left[-1])
        for right_point in (right[0], right[-1])
    )


def _orient_relation_group(group: list[list[list[float]]]) -> list[list[list[float]]]:
    """Orient consecutive relation ways without merging their geometries."""
    if len(group) <= 1:
        return [[point[:] for point in line] for line in group]
    costs: list[dict[int, tuple[float, int | None]]] = [
        {0: (0.0, None), 1: (0.0, None)}
    ]
    for index in range(1, len(group)):
        choices: dict[int, tuple[float, int | None]] = {}
        for orientation in (0, 1):
            current = group[index] if orientation == 0 else list(reversed(group[index]))
            best: tuple[float, int | None] | None = None
            for previous_orientation in (0, 1):
                previous = (
                    group[index - 1]
                    if previous_orientation == 0
                    else list(reversed(group[index - 1]))
                )
                cost = costs[index - 1][previous_orientation][0] + _haversine_m(
                    (previous[-1][1], previous[-1][0]),
                    (current[0][1], current[0][0]),
                )
                candidate = (cost, previous_orientation)
                if best is None or candidate < best:
                    best = candidate
            assert best is not None
            choices[orientation] = best
        costs.append(choices)
    orientation = min((0, 1), key=lambda value: costs[-1][value][0])
    orientations = [orientation]
    for index in range(len(group) - 1, 0, -1):
        previous = costs[index][orientations[-1]][1]
        assert previous is not None
        orientations.append(previous)
    orientations.reverse()
    return [
        [point[:] for point in (line if orientation == 0 else reversed(line))]
        for line, orientation in zip(group, orientations)
    ]


def _relation_chains(lines: list[list[list[float]]]) -> list[list[list[list[float]]]]:
    """Split a relation at spatially disconnected consecutive way members."""
    chains: list[list[list[list[float]]]] = []
    group: list[list[list[float]]] = []

    def append_group() -> None:
        if not group:
            return
        oriented = _orient_relation_group(group)
        connected: list[list[list[float]]] = []
        for line in oriented:
            if connected and _haversine_m(
                (connected[-1][-1][1], connected[-1][-1][0]),
                (line[0][1], line[0][0]),
            ) > MAX_RELATION_MEMBER_GAP_M:
                chains.append(connected)
                connected = []
            connected.append(line)
        if connected:
            chains.append(connected)

    for line in lines:
        if group and _endpoint_gap_m(group[-1], line) > MAX_RELATION_MEMBER_GAP_M:
            append_group()
            group = []
        group.append(line)
    append_group()
    return chains


def _project_to_segment(
    stop: tuple[float, float], start: list[float], end: list[float],
) -> tuple[float, list[float], float]:
    """Return segment fraction, projected ``[lon, lat]``, and distance."""
    stop_lat, stop_lon = stop
    mean_lat = math.radians((stop_lat + start[1] + end[1]) / 3)
    lon_scale = max(0.01, math.cos(mean_lat))
    vector_x = (end[0] - start[0]) * lon_scale
    vector_y = end[1] - start[1]
    point_x = (stop_lon - start[0]) * lon_scale
    point_y = stop_lat - start[1]
    denominator = vector_x * vector_x + vector_y * vector_y
    fraction = 0.0 if denominator <= 1e-18 else (point_x * vector_x + point_y * vector_y) / denominator
    fraction = min(1.0, max(0.0, fraction))
    projected = [
        start[0] + (end[0] - start[0]) * fraction,
        start[1] + (end[1] - start[1]) * fraction,
    ]
    return fraction, projected, _haversine_m(stop, (projected[1], projected[0]))


def _projection_candidates(
    lines: list[list[list[float]]], stop: tuple[float, float], *, limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    offset_m = 0.0
    for line_index, line in enumerate(lines):
        for segment_index in range(len(line) - 1):
            start, end = line[segment_index], line[segment_index + 1]
            segment_m = _haversine_m((start[1], start[0]), (end[1], end[0]))
            if segment_m <= 0:
                continue
            fraction, point, distance_m = _project_to_segment(stop, start, end)
            candidate = {
                "distance_m": distance_m,
                "offset_m": offset_m + segment_m * fraction,
                "line_index": line_index,
                "segment_index": segment_index,
                "point": point,
            }
            candidates.append(candidate)
            candidates.sort(
                key=lambda item: (
                    item["distance_m"], item["offset_m"], item["line_index"], item["segment_index"]
                )
            )
            if len(candidates) > limit:
                candidates.pop()
            offset_m += segment_m
    return candidates


def _dedupe_line(points: list[list[float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for point in points:
        if not result or point != result[-1]:
            result.append(point)
    return result


def _clip_oriented_lines(
    lines: list[list[list[float]]], start: dict[str, Any], end: dict[str, Any],
) -> list[list[list[float]]]:
    clipped: list[list[list[float]]] = []
    start_line, end_line = int(start["line_index"]), int(end["line_index"])
    for line_index in range(start_line, end_line + 1):
        line = lines[line_index]
        if start_line == end_line:
            points = [start["point"]]
            points.extend(line[int(start["segment_index"]) + 1 : int(end["segment_index"]) + 1])
            points.append(end["point"])
        elif line_index == start_line:
            points = [start["point"], *line[int(start["segment_index"]) + 1 :]]
        elif line_index == end_line:
            points = [*line[: int(end["segment_index"]) + 1], end["point"]]
        else:
            points = list(line)
        points = _dedupe_line(points)
        if len(points) >= 2:
            clipped.append(points)
    return clipped


def _probe_stops(ordered: list[tuple[float, float]], limit: int = 9) -> list[tuple[float, float]]:
    if len(ordered) <= limit:
        return ordered
    indexes = {
        round(index * (len(ordered) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [ordered[index] for index in sorted(indexes)]


def _clip_relation_lines(
    lines: list[list[list[float]]], ordered: list[tuple[float, float]],
) -> tuple[list[list[list[float]]], float, str] | None:
    """Clip a relation to the boarding/alighting corridor.

    OSM relation ways remain separate MultiLineString members, so an unmapped
    gap is never rendered as a fabricated straight connection.  Both traversal
    directions are considered because relation member order may be opposite to
    the official stop sequence supplied by the caller.
    """
    expected_m = sum(_haversine_m(left, right) for left, right in zip(ordered, ordered[1:]))
    finalists: list[tuple[float, list[list[list[float]]], str]] = []
    for chain in _relation_chains(lines):
        directions = (
            (chain, "relation_order"),
            ([list(reversed(line)) for line in reversed(chain)], "reverse_relation_order"),
        )
        for oriented, direction in directions:
            starts = _projection_candidates(
                oriented, ordered[0], limit=MAX_RELATION_ENDPOINT_CANDIDATES
            )
            ends = _projection_candidates(
                oriented, ordered[-1], limit=MAX_RELATION_ENDPOINT_CANDIDATES
            )
            pairs: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
            for start in starts:
                if start["distance_m"] > MAX_RELATION_ENDPOINT_SNAP_M:
                    continue
                for end in ends:
                    interval_m = end["offset_m"] - start["offset_m"]
                    if end["distance_m"] > MAX_RELATION_ENDPOINT_SNAP_M or interval_m <= 1.0:
                        continue
                    length_penalty = abs(
                        math.log((interval_m + 50.0) / (expected_m + 50.0))
                    ) * 500.0
                    pairs.append(
                        (
                            (start["distance_m"] + end["distance_m"]) * 4.0 + length_penalty,
                            start,
                            end,
                        )
                    )
            pairs.sort(key=lambda item: (item[0], item[1]["offset_m"], item[2]["offset_m"]))
            for pair_score, start, end in pairs[:MAX_RELATION_PAIR_CANDIDATES]:
                clipped = _clip_oriented_lines(oriented, start, end)
                if not clipped:
                    continue
                probe_projections = [
                    _projection_candidates(clipped, stop, limit=1)
                    for stop in _probe_stops(ordered)
                ]
                if any(not projections for projections in probe_projections):
                    continue
                distances = [projections[0]["distance_m"] for projections in probe_projections]
                if max(distances) > MAX_RELATION_STOP_SNAP_M:
                    continue
                offsets = [projections[0]["offset_m"] for projections in probe_projections]
                backtrack_m = sum(
                    max(0.0, previous - current - 100.0)
                    for previous, current in zip(offsets, offsets[1:])
                )
                score = _relation_score(clipped, ordered) + pair_score + backtrack_m * 5.0
                finalists.append((score, clipped, direction))
    if not finalists:
        return None
    score, clipped, direction = min(finalists, key=lambda item: item[0])
    return clipped, score, direction


def fetch_bus_relation(
    *, route_ref: str, stops: Iterable[dict[str, Any]], timeout_seconds: float = 12.0,
    overpass_url: str = OVERPASS_URL, _deadline: float | None = None,
) -> dict[str, Any] | None:
    route_ref = str(route_ref or "").strip()
    if not ROUTE_REF_RE.fullmatch(route_ref):
        raise OSMError("INVALID_ROUTE_REF", "Route number has an invalid format", status=400)
    ordered = _coordinates(stops)
    latitudes, longitudes = [item[0] for item in ordered], [item[1] for item in ordered]
    south, north = max(KOREA_LAT_RANGE[0], min(latitudes) - 0.035), min(KOREA_LAT_RANGE[1], max(latitudes) + 0.035)
    west, east = max(KOREA_LON_RANGE[0], min(longitudes) - 0.035), min(KOREA_LON_RANGE[1], max(longitudes) + 0.035)
    # route_ref is constrained above; JSON escaping still protects quotes if
    # the accepted character set is expanded later.
    escaped_ref = json.dumps(route_ref, ensure_ascii=False)
    query = (
        "[out:json][timeout:12];"
        f"relation[\"type\"=\"route\"][\"route\"=\"bus\"][\"ref\"={escaped_ref}]"
        f"({south:.6f},{west:.6f},{north:.6f},{east:.6f});out body geom;"
    )
    endpoint = _validate_endpoint(overpass_url, "overpass-api.de")
    request = Request(
        endpoint,
        data=urlencode({"data": query}).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "busro-itda/0.3 (local-development)",
        },
        method="POST",
    )
    payload = _read_json(
        request,
        timeout_seconds=_deadline_timeout(_deadline, timeout_seconds),
        _deadline=_deadline,
    )
    _check_deadline(_deadline)
    candidates: list[tuple[float, dict[str, Any], list[list[list[float]]], str]] = []
    elements = payload.get("elements") if isinstance(payload.get("elements"), list) else []
    for element in elements[:50]:
        if not isinstance(element, dict) or element.get("type") != "relation":
            continue
        lines = _relation_lines(element)
        clipped = _clip_relation_lines(lines, ordered) if lines else None
        if clipped:
            clipped_lines, score, direction = clipped
            candidates.append((score, element, clipped_lines, direction))
    _check_deadline(_deadline)
    if not candidates:
        return None
    score, chosen, lines, direction = min(
        candidates, key=lambda item: (item[0], int(item[1].get("id") or 0))
    )
    tags = chosen.get("tags") if isinstance(chosen.get("tags"), dict) else {}
    return {
        "ok": True,
        "geometry": {"type": "MultiLineString", "coordinates": lines},
        "geometry_source": "osm_bus_relation",
        "precision": "community_mapped_route",
        "verified_operator_shape": False,
        "relation": {
            "id": str(chosen.get("id") or ""),
            "name": str(tags.get("name") or "")[:160],
            "ref": str(tags.get("ref") or "")[:24],
            "network": str(tags.get("network") or "")[:120],
            "operator": str(tags.get("operator") or "")[:120],
            "match_score_m": round(score, 1),
            "segment_clipped": True,
            "segment_direction": direction,
        },
        "attribution": "© OpenStreetMap contributors · ODbL",
        "data_gap": None,
    }


def _chunks(points: list[tuple[float, float]], size: int = OSRM_CHUNK_SIZE):
    start = 0
    count = 0
    while start < len(points) - 1:
        count += 1
        if count > MAX_OSRM_CHUNKS:
            raise OSMError("TOO_MANY_OSRM_CHUNKS", "Ordered stops require too many road-router chunks", status=400)
        end = min(len(points), start + size)
        yield points[start:end]
        start = end - 1


def fetch_road_estimate(
    *, stops: Iterable[dict[str, Any]], timeout_seconds: float = 8.0, osrm_url: str = OSRM_URL,
    _deadline: float | None = None,
) -> dict[str, Any]:
    ordered = _coordinates(stops)
    endpoint = _validate_endpoint(osrm_url, "router.project-osrm.org")
    combined: list[list[float]] = []
    distance = 0.0
    duration = 0.0
    for chunk in _chunks(ordered):
        _check_deadline(_deadline)
        encoded = ";".join(f"{quote(str(lon), safe='.-')},{quote(str(lat), safe='.-')}" for lat, lon in chunk)
        query = urlencode({"overview": "full", "geometries": "geojson", "steps": "false", "continue_straight": "true"})
        request = Request(
            f"{endpoint}/route/v1/driving/{encoded}?{query}",
            headers={"Accept": "application/json", "User-Agent": "busro-itda/0.3 (local-development)"},
        )
        payload = _read_json(
            request,
            timeout_seconds=_deadline_timeout(_deadline, timeout_seconds),
            _deadline=_deadline,
        )
        _check_deadline(_deadline)
        routes = payload.get("routes") if isinstance(payload.get("routes"), list) else []
        if payload.get("code") != "Ok" or not routes:
            raise OSMError("OSRM_NO_ROUTE", "OSM road router could not connect the ordered stops", status=422)
        route = routes[0] if isinstance(routes[0], dict) else {}
        geometry = route.get("geometry") if isinstance(route.get("geometry"), dict) else {}
        coordinates = geometry.get("coordinates") if isinstance(geometry.get("coordinates"), list) else []
        valid = [point for point in coordinates if isinstance(point, list) and len(point) >= 2]
        if len(valid) < 2:
            raise OSMError("OSRM_INVALID_GEOMETRY", "OSM road router returned no usable geometry")
        if combined and valid[0] == combined[-1]:
            valid = valid[1:]
        combined.extend(valid)
        distance += float(route.get("distance") or 0)
        duration += float(route.get("duration") or 0)
        _check_deadline(_deadline)
    return {
        "ok": True,
        "geometry": {"type": "LineString", "coordinates": combined},
        "geometry_source": "osm_road_route_estimate",
        "precision": "ordered_stops_road_estimate",
        "verified_operator_shape": False,
        "distance_m": round(distance, 1),
        "duration_seconds_car_profile": round(duration, 1),
        "attribution": "© OpenStreetMap contributors · routed with OSRM",
        "data_gap": "No matching OSM bus relation was found; road geometry is an estimate between official stops.",
    }


def resolve_route_geometry(
    *, route_ref: str, stops: Iterable[dict[str, Any]], timeout_seconds: float = 12.0,
    allow_road_estimate: bool = True,
) -> dict[str, Any]:
    deadline = _resolve_deadline(timeout_seconds)
    relation_timeout_seconds = min(float(timeout_seconds), MAX_RELATION_LOOKUP_SECONDS)
    admission_wait = min(GEOMETRY_ADMISSION_WAIT_SECONDS, max(0.0, deadline - time.monotonic()))
    if not _GEOMETRY_ADMISSION.acquire(timeout=admission_wait):
        raise OSMError(
            "OSM_BUSY",
            "OSM geometry capacity is busy; retry shortly",
            status=429,
        )
    try:
        _check_deadline(deadline)
        materialized = list(stops)
        relation_error: OSMError | None = None
        try:
            relation = fetch_bus_relation(
                route_ref=route_ref,
                stops=materialized,
                timeout_seconds=relation_timeout_seconds,
                _deadline=deadline,
            )
            if relation is not None:
                return relation
        except OSMError as exc:
            _check_deadline(deadline)
            if exc.code == "OSM_DEADLINE_EXCEEDED":
                raise
            relation_error = exc
        if allow_road_estimate:
            try:
                result = fetch_road_estimate(
                    stops=materialized,
                    timeout_seconds=timeout_seconds,
                    _deadline=deadline,
                )
                if relation_error:
                    result["relation_lookup_error"] = relation_error.code
                return result
            except OSMError as exc:
                _check_deadline(deadline)
                if exc.code == "OSM_DEADLINE_EXCEEDED":
                    raise
                if relation_error:
                    raise relation_error
                raise
        if relation_error:
            raise relation_error
        raise OSMError("OSM_ROUTE_NOT_MAPPED", "No matching OSM bus route relation was found", status=404)
    finally:
        _GEOMETRY_ADMISSION.release()
