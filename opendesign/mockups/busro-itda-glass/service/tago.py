"""Small TAGO client. The service key never leaves this server process."""

from __future__ import annotations

import json
from pathlib import Path
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ARRIVALS_URL = (
    "https://apis.data.go.kr/1613000/ArvlInfoInqireService/"
    "getSttnAcctoArvlPrearngeInfoList"
)
POSITIONS_URL = (
    "https://apis.data.go.kr/1613000/BusLcInfoInqireService/"
    "getRouteAcctoBusLcList"
)
ROUTE_CATALOG_BASE = "https://apis.data.go.kr/1613000/BusRouteInfoInqireService/"
STOP_CATALOG_BASE = "https://apis.data.go.kr/1613000/BusSttnInfoInqireService/"
CATALOG_OPERATIONS = {
    "cities": ROUTE_CATALOG_BASE + "getCtyCodeList",
    "routes": ROUTE_CATALOG_BASE + "getRouteNoList",
    "route_info": ROUTE_CATALOG_BASE + "getRouteInfoIem",
    "route_stops": ROUTE_CATALOG_BASE + "getRouteAcctoThrghSttnList",
    "stops": STOP_CATALOG_BASE + "getSttnNoList",
    "nearby_stops": STOP_CATALOG_BASE + "getCrdntPrxmtSttnList",
    "stop_routes": STOP_CATALOG_BASE + "getSttnThrghRouteList",
}
CATALOG_PARAMETER_NAMES = {
    "cities": frozenset(),
    "routes": frozenset({"cityCode", "routeNo", "pageNo", "numOfRows"}),
    "route_info": frozenset({"cityCode", "routeId"}),
    "route_stops": frozenset({"cityCode", "routeId", "pageNo", "numOfRows"}),
    "stops": frozenset({"cityCode", "nodeNm", "nodeNo", "pageNo", "numOfRows"}),
    "nearby_stops": frozenset({"gpsLati", "gpsLong", "pageNo", "numOfRows"}),
    "stop_routes": frozenset({"cityCode", "nodeid", "pageNo", "numOfRows"}),
}


class TagoError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _error_from_xml(payload: bytes) -> TagoError:
    try:
        root = ElementTree.fromstring(payload)
        code = root.findtext(".//returnReasonCode") or root.findtext(".//resultCode") or "UPSTREAM_XML"
        message = root.findtext(".//returnAuthMsg") or root.findtext(".//resultMsg") or "TAGO returned XML instead of JSON"
    except ElementTree.ParseError:
        code, message = "UPSTREAM_INVALID_RESPONSE", "TAGO returned an unreadable response"
    return TagoError(str(code), str(message))


def _error_from_http(exc: HTTPError) -> TagoError:
    """Read only structured public error fields; never echo request URLs/keys."""
    try:
        payload = exc.read(65_537)
    except OSError:
        payload = b""
    if payload.lstrip().startswith(b"<"):
        error = _error_from_xml(payload[:65_536])
        return TagoError(error.code, error.message, status=502)
    try:
        data = json.loads(payload[:65_536])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return TagoError("TAGO_HTTP_ERROR", f"TAGO HTTP {exc.code}", status=502)
    header = data.get("response", {}).get("header", {}) if isinstance(data, dict) else {}
    service_header = (
        data.get("OpenAPI_ServiceResponse", {}).get("cmmMsgHeader", {})
        if isinstance(data, dict)
        else {}
    )
    code = str(
        header.get("resultCode")
        or service_header.get("returnReasonCode")
        or f"HTTP_{exc.code}"
    )
    message = str(
        header.get("resultMsg")
        or service_header.get("returnAuthMsg")
        or service_header.get("errMsg")
        or "TAGO request was rejected"
    )[:240]
    return TagoError(code, message, status=502)


def fetch_arrivals(
    *,
    city_code: str,
    node_id: str,
    service_key: str | None,
    timeout_seconds: float,
    fixture_mode: bool,
    fixture_path: Path,
) -> dict[str, Any]:
    if fixture_mode:
        return json.loads(fixture_path.read_text(encoding="utf-8"))
    if not service_key:
        raise TagoError(
            "TAGO_KEY_REQUIRED",
            "TAGO_SERVICE_KEY is not configured on the server",
            status=503,
        )
    if "\r" in service_key or "\n" in service_key or len(service_key) > 1024:
        raise TagoError("TAGO_KEY_INVALID", "TAGO_SERVICE_KEY has an invalid format", status=503)

    query = urlencode(
        {
            "pageNo": "1",
            "numOfRows": "100",
            "_type": "json",
            "cityCode": city_code,
            "nodeId": node_id,
        }
    )
    # Public Data Portal shows both encoded and decoded keys. Preserve existing
    # percent escapes while encoding all other URL-significant characters.
    url = f"{ARRIVALS_URL}?serviceKey={quote(service_key, safe='%')}&{query}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "busro-itda/0.1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(2_000_001)
    except HTTPError as exc:
        raise _error_from_http(exc) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise TagoError("TAGO_TIMEOUT", "TAGO request timed out", status=504) from exc
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise TagoError("TAGO_TIMEOUT", "TAGO request timed out", status=504) from exc
        raise TagoError("TAGO_UNAVAILABLE", "TAGO could not be reached", status=502) from exc

    if len(payload) > 2_000_000:
        raise TagoError("UPSTREAM_RESPONSE_TOO_LARGE", "TAGO response exceeded 2 MB")

    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if payload.lstrip().startswith(b"<"):
            raise _error_from_xml(payload) from exc
        raise TagoError("UPSTREAM_INVALID_JSON", "TAGO returned invalid JSON") from exc

    header = data.get("response", {}).get("header", {}) if isinstance(data, dict) else {}
    result_code = str(header.get("resultCode", ""))
    if result_code and result_code not in {"00", "0000"}:
        raise TagoError(result_code, str(header.get("resultMsg") or "TAGO request failed"))
    return data


def fetch_positions(
    *,
    city_code: str,
    route_id: str,
    service_key: str | None,
    timeout_seconds: float,
    fixture_mode: bool,
    fixture_path: Path,
) -> dict[str, Any]:
    """Fetch the official route-scoped TAGO vehicle-location response.

    The upstream host and operation are intentionally constants so caller input
    cannot turn this server-side proxy into an SSRF primitive.
    """
    if fixture_mode:
        return json.loads(fixture_path.read_text(encoding="utf-8"))
    if not service_key:
        raise TagoError(
            "TAGO_KEY_REQUIRED",
            "TAGO_SERVICE_KEY is not configured on the server",
            status=503,
        )
    if "\r" in service_key or "\n" in service_key or len(service_key) > 1024:
        raise TagoError("TAGO_KEY_INVALID", "TAGO_SERVICE_KEY has an invalid format", status=503)

    query = urlencode(
        {
            "serviceKey": service_key,
            "pageNo": "1",
            "numOfRows": "100",
            "_type": "json",
            "cityCode": city_code,
            "routeId": route_id,
        }
    )
    url = f"{POSITIONS_URL}?{query}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "busro-itda/0.2"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(2_000_001)
    except HTTPError as exc:
        raise _error_from_http(exc) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise TagoError("TAGO_TIMEOUT", "TAGO request timed out", status=504) from exc
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise TagoError("TAGO_TIMEOUT", "TAGO request timed out", status=504) from exc
        raise TagoError("TAGO_UNAVAILABLE", "TAGO could not be reached", status=502) from exc

    if len(payload) > 2_000_000:
        raise TagoError("UPSTREAM_RESPONSE_TOO_LARGE", "TAGO response exceeded 2 MB")
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if payload.lstrip().startswith(b"<"):
            raise _error_from_xml(payload) from exc
        raise TagoError("UPSTREAM_INVALID_JSON", "TAGO returned invalid JSON") from exc
    header = data.get("response", {}).get("header", {}) if isinstance(data, dict) else {}
    result_code = str(header.get("resultCode", ""))
    if result_code and result_code not in {"00", "0000"}:
        raise TagoError(result_code, str(header.get("resultMsg") or "TAGO request failed"))
    return data


def normalize_positions(
    data: dict[str, Any], *, route_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize the documented lower-case TAGO position fields."""
    response = data.get("response") if isinstance(data, dict) else None
    response = response if isinstance(response, dict) else {}
    header = response.get("header") if isinstance(response.get("header"), dict) else {}
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    container = body.get("items")
    items = container.get("item", []) if isinstance(container, dict) else []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []

    positions: list[dict[str, Any]] = []
    dropped = 0
    seen: set[tuple[str, str]] = set()
    for item in items[:500]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        node_id = str(item.get("nodeid") or "").strip()
        vehicle_no = str(item.get("vehicleno") or "").strip()
        try:
            node_order = int(item.get("nodeord"))
        except (TypeError, ValueError):
            node_order = 0
        if (
            not node_id
            or len(node_id) > 30
            or not vehicle_no
            or len(vehicle_no) > 32
            or any(ord(char) < 32 for char in vehicle_no)
            or not 1 <= node_order <= 9999
        ):
            dropped += 1
            continue
        identity = (route_id, vehicle_no)
        if identity in seen:
            dropped += 1
            continue
        seen.add(identity)

        def coordinate(name: str, minimum: float, maximum: float) -> float | None:
            try:
                value = float(item.get(name))
            except (TypeError, ValueError):
                return None
            return value if minimum <= value <= maximum else None

        positions.append(
            {
                "route_id": route_id,
                "route_name": str(item.get("routenm") or "")[:30],
                "route_type": str(item.get("routetp") or "")[:20],
                "vehicle_no": vehicle_no,
                "node_id": node_id,
                "node_name": str(item.get("nodenm") or "")[:60],
                "node_order": node_order,
                "latitude": coordinate("gpslati", -90.0, 90.0),
                "longitude": coordinate("gpslong", -180.0, 180.0),
            }
        )
    try:
        total_count = int(body.get("totalCount") or len(positions))
    except (TypeError, ValueError):
        total_count = len(positions)
    return positions, {
        "result_code": str(header.get("resultCode") or ""),
        "result_message": str(header.get("resultMsg") or ""),
        "total_count": total_count,
        "normalized_count": len(positions),
        "dropped_count": dropped + max(0, len(items) - 500),
        "truncated": len(items) > 500,
    }


def normalize_arrivals(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    response = data.get("response") if isinstance(data, dict) else None
    response = response if isinstance(response, dict) else {}
    header = response.get("header") if isinstance(response.get("header"), dict) else {}
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    items_container = body.get("items")
    if isinstance(items_container, dict):
        items = items_container.get("item", [])
    else:
        items = []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []

    arrivals: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            arrival_seconds = int(item.get("arrtime"))
        except (TypeError, ValueError):
            arrival_seconds = None
        try:
            remaining_stops = int(item.get("arrprevstationcnt"))
        except (TypeError, ValueError):
            remaining_stops = None
        arrivals.append(
            {
                "node_id": str(item.get("nodeid") or ""),
                "node_name": str(item.get("nodenm") or ""),
                "route_id": str(item.get("routeid") or ""),
                "route_no": str(item.get("routeno") or ""),
                "route_type": str(item.get("routetp") or ""),
                "arrival_seconds": arrival_seconds,
                "remaining_stops": remaining_stops,
                "vehicle_type": str(item.get("vehicletp") or ""),
            }
        )
    try:
        total_count = int(body.get("totalCount") or len(arrivals))
    except (TypeError, ValueError):
        total_count = len(arrivals)
    return arrivals, {
        "result_code": str(header.get("resultCode") or ""),
        "result_message": str(header.get("resultMsg") or ""),
        "total_count": total_count,
    }


def fetch_catalog(
    *,
    operation: str,
    parameters: dict[str, str],
    service_key: str | None,
    timeout_seconds: float,
    fixture_mode: bool,
    fixture_path: Path,
) -> dict[str, Any]:
    """Fetch one allow-listed TAGO route/station catalog operation.

    The service accepts the decoded Public Data Portal key and lets
    ``urlencode`` encode it exactly once. Neither a caller-supplied URL nor an
    operation name is ever interpolated into the upstream host/path.
    """
    url_base = CATALOG_OPERATIONS.get(operation)
    if url_base is None:
        raise TagoError("CATALOG_OPERATION_INVALID", "Unsupported TAGO catalog operation", status=500)
    allowed_parameters = CATALOG_PARAMETER_NAMES[operation]
    if set(parameters) - allowed_parameters:
        raise TagoError(
            "CATALOG_PARAMETER_INVALID", "Unsupported TAGO catalog parameter", status=500
        )
    if any(
        not isinstance(value, str)
        or len(value) > 120
        or any(ord(character) < 32 for character in value)
        for value in parameters.values()
    ):
        raise TagoError("CATALOG_PARAMETER_INVALID", "Invalid TAGO catalog parameter", status=500)
    if fixture_mode:
        document = json.loads(fixture_path.read_text(encoding="utf-8"))
        payload = document.get("operations", {}).get(operation)
        if not isinstance(payload, dict):
            raise TagoError("CATALOG_FIXTURE_MISSING", "Catalog fixture operation is missing", status=500)
        return payload
    if not service_key:
        raise TagoError(
            "TAGO_KEY_REQUIRED",
            "TAGO_SERVICE_KEY is not configured on the server",
            status=503,
        )
    if "\r" in service_key or "\n" in service_key or len(service_key) > 1024:
        raise TagoError("TAGO_KEY_INVALID", "TAGO_SERVICE_KEY has an invalid format", status=503)

    query_values = {"serviceKey": service_key, "_type": "json", **parameters}
    url = f"{url_base}?{urlencode(query_values)}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "busro-itda/0.3"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(2_000_001)
    except HTTPError as exc:
        raise _error_from_http(exc) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise TagoError("TAGO_TIMEOUT", "TAGO request timed out", status=504) from exc
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise TagoError("TAGO_TIMEOUT", "TAGO request timed out", status=504) from exc
        raise TagoError("TAGO_UNAVAILABLE", "TAGO could not be reached", status=502) from exc
    if len(payload) > 2_000_000:
        raise TagoError("UPSTREAM_RESPONSE_TOO_LARGE", "TAGO response exceeded 2 MB")
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if payload.lstrip().startswith(b"<"):
            raise _error_from_xml(payload) from exc
        raise TagoError("UPSTREAM_INVALID_JSON", "TAGO returned invalid JSON") from exc
    # The portal sometimes wraps provider/gateway failures in a HTTP 200
    # ``OpenAPI_ServiceResponse`` envelope instead of the normal ``response``
    # shape. Preserve its public reason code so ingestion can retry or classify
    # the target without collapsing it into an opaque malformed-response error.
    service_response = data.get("OpenAPI_ServiceResponse") if isinstance(data, dict) else None
    service_header = (
        service_response.get("cmmMsgHeader")
        if isinstance(service_response, dict)
        else None
    )
    if isinstance(service_header, dict):
        service_code = str(service_header.get("returnReasonCode") or "").strip()
        if service_code:
            service_message = str(
                service_header.get("returnAuthMsg")
                or service_header.get("errMsg")
                or "TAGO request failed"
            )[:240]
            raise TagoError(service_code, service_message)
    response = data.get("response") if isinstance(data, dict) else None
    header = response.get("header") if isinstance(response, dict) else None
    result_code = (
        str(header.get("resultCode")).strip()
        if isinstance(header, dict) and header.get("resultCode") is not None
        else ""
    )
    if not result_code:
        raise TagoError(
            "UPSTREAM_MALFORMED_RESPONSE",
            "TAGO catalog response did not include a result code",
        )
    if result_code not in {"00", "0000"}:
        raise TagoError(result_code, str(header.get("resultMsg") or "TAGO request failed"))
    return data


def _catalog_items(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    response = data.get("response") if isinstance(data, dict) else None
    response = response if isinstance(response, dict) else {}
    header = response.get("header") if isinstance(response.get("header"), dict) else {}
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    container = body.get("items")
    raw_items = container.get("item", []) if isinstance(container, dict) else []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []
    items = [item for item in raw_items[:500] if isinstance(item, dict)]

    def integer(name: str, fallback: int) -> int:
        try:
            return int(body.get(name))
        except (TypeError, ValueError):
            return fallback

    return items, {
        "result_code": str(header.get("resultCode") or ""),
        "result_message": str(header.get("resultMsg") or ""),
        "page_no": integer("pageNo", 1),
        "num_rows": integer("numOfRows", len(items)),
        "total_count": integer("totalCount", len(items)),
        "normalized_count": len(items),
        "dropped_count": max(0, len(raw_items) - len(items)),
        "truncated": len(raw_items) > 500,
    }


def _text(item: dict[str, Any], *names: str, limit: int = 120) -> str:
    for name in names:
        value = item.get(name)
        if value is not None:
            return str(value).strip()[:limit]
    return ""


def _integer(item: dict[str, Any], *names: str) -> int | None:
    for name in names:
        try:
            return int(item.get(name))
        except (TypeError, ValueError):
            continue
    return None


def _coordinate(item: dict[str, Any], name: str, minimum: float, maximum: float) -> float | None:
    try:
        value = float(item.get(name))
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None


def normalize_catalog(
    data: dict[str, Any], *, operation: str, fallback_city_code: str = "", fallback_route_id: str = ""
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize object-or-list TAGO response bodies to stable public fields."""
    items, metadata = _catalog_items(data)
    records: list[dict[str, Any]] = []
    for item in items:
        city_code = _text(item, "citycode", "cityCode", limit=9) or fallback_city_code
        route_id = _text(item, "routeid", "routeId", limit=64) or fallback_route_id
        if operation == "cities":
            record = {
                "city_code": city_code,
                "city_name": _text(item, "cityname", "cityName", limit=80),
            }
        elif operation in {"routes", "stop_routes"}:
            record = {
                "city_code": city_code,
                "route_id": route_id,
                "route_no": _text(item, "routeno", "routeNo", limit=40),
                "route_type": _text(item, "routetp", "routeTp", limit=40),
                "start_node_name": _text(item, "startnodenm", "startNodeNm", limit=80),
                "end_node_name": _text(item, "endnodenm", "endNodeNm", limit=80),
            }
        elif operation == "route_info":
            record = {
                "city_code": city_code,
                "route_id": route_id,
                "route_no": _text(item, "routeno", "routeNo", limit=40),
                "route_type": _text(item, "routetp", "routeTp", limit=40),
                "start_node_name": _text(item, "startnodenm", "startNodeNm", limit=80),
                "end_node_name": _text(item, "endnodenm", "endNodeNm", limit=80),
                "first_vehicle_time": _text(item, "startvehicletime", "startVehicleTime", limit=12),
                "last_vehicle_time": _text(item, "endvehicletime", "endVehicleTime", limit=12),
                "weekday_interval_minutes": _integer(item, "intervaltime", "intervalTime"),
                "saturday_interval_minutes": _integer(item, "intervalsattime", "intervalSatTime"),
                "sunday_interval_minutes": _integer(item, "intervalsuntime", "intervalSunTime"),
            }
        elif operation in {"route_stops", "stops", "nearby_stops"}:
            record = {
                "city_code": city_code,
                "route_id": route_id,
                "node_id": _text(item, "nodeid", "nodeId", limit=64),
                "node_no": _text(item, "nodeno", "nodeNo", limit=40),
                "node_name": _text(item, "nodenm", "nodeNm", limit=80),
                "node_order": _integer(item, "nodeord", "nodeOrd"),
                "latitude": _coordinate(item, "gpslati", -90.0, 90.0),
                "longitude": _coordinate(item, "gpslong", -180.0, 180.0),
                "up_down_code": _text(item, "updowncd", "upDownCd", limit=20),
            }
        else:
            raise TagoError("CATALOG_OPERATION_INVALID", "Unsupported catalog normalization", status=500)
        records.append(record)
    metadata["normalized_count"] = len(records)
    return records, metadata
