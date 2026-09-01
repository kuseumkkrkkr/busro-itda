"""Resumable nationwide TAGO route-topology ingestion.

This is an explicit operator command, not a web-request side effect or hidden
background worker.  It discovers TAGO-native city/route identifiers, stages
bounded route-stop pages, and activates a sequence only after the complete
ordered list validates.  No URL, query string, or service key is logged.
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import ipaddress
import json
from pathlib import Path
import re
import socket
import sys
import threading
import time
from typing import Any, Callable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from network_catalog import CatalogError, CatalogLimitError, CatalogValidationError, NetworkCatalog
from route_topology_anomalies import single_point_route_spike
from tago import TagoError, fetch_catalog, normalize_catalog


PROVIDER = "TAGO"
MAX_LOCAL_API_RESPONSE_BYTES = 2_000_000
MIN_LOCAL_API_PORT = 1
MAX_LOCAL_API_PORT = 65_535
LOCAL_API_OPERATIONS = {
    "cities": ("/api/cities", "cities", {}, frozenset()),
    "routes": (
        "/api/routes",
        "routes",
        {
            "cityCode": "city_code",
            "routeNo": "route_no",
            "pageNo": "page",
            "numOfRows": "limit",
        },
        frozenset({"cityCode"}),
    ),
    "route_stops": (
        "/api/routes/stops",
        "stops",
        {
            "cityCode": "city_code",
            "routeId": "route_id",
            "pageNo": "page",
            "numOfRows": "limit",
        },
        frozenset({"cityCode", "routeId"}),
    ),
}
LOCAL_API_PATHS = frozenset(
    {"/api/status", *(spec[0] for spec in LOCAL_API_OPERATIONS.values())}
)
LOCAL_API_RAW_FIELDS = {
    "cities": {"city_code": "citycode", "city_name": "cityname"},
    "routes": {
        "city_code": "citycode",
        "route_id": "routeid",
        "route_no": "routeno",
        "route_type": "routetp",
        "start_node_name": "startnodenm",
        "end_node_name": "endnodenm",
    },
    "route_stops": {
        "city_code": "citycode",
        "route_id": "routeid",
        "node_id": "nodeid",
        "node_no": "nodeno",
        "node_name": "nodenm",
        "node_order": "nodeord",
        "latitude": "gpslati",
        "longitude": "gpslong",
        "up_down_code": "updowncd",
    },
}
FATAL_ACCESS_CODES = frozenset(
    {
        "30",
        "TAGO_KEY_REQUIRED",
        "TAGO_KEY_INVALID",
        "SERVICE_ACCESS_DENIED_ERROR",
        "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
    }
)
UPSTREAM_DAILY_QUOTA_CODES = frozenset(
    {
        "22",
        "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR",
    }
)
_ONLY_ROUTE_CITY_CODE = re.compile(r"^[0-9A-Za-z_.-]{1,96}$")
_ONLY_ROUTE_ID = re.compile(r"^[0-9A-Za-z가-힣_.:-]{1,96}$")


class RequestBudgetExhausted(RuntimeError):
    pass


class IngestStopped(RuntimeError):
    def __init__(self, status: str):
        super().__init__("topology ingest stopped")
        self.status = status


class TopologyProcessLocked(RuntimeError):
    pass


class SinglePointRouteSpikeQuarantined(RuntimeError):
    """The target's bounded anomaly evidence is already persisted."""


def _only_route_target(value: str) -> tuple[str, str]:
    city_code, separator, route_id = str(value).partition(":")
    if (
        not separator
        or not _ONLY_ROUTE_CITY_CODE.fullmatch(city_code)
        or not _ONLY_ROUTE_ID.fullmatch(route_id)
    ):
        raise argparse.ArgumentTypeError(
            "--only-route must use CITY_CODE:ROUTE_ID with valid TAGO identifiers"
        )
    return city_code, route_id


class _AppendUniqueOnlyRoute(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: tuple[str, str],
        option_string: str | None = None,
    ) -> None:
        selected = list(getattr(namespace, self.dest, None) or ())
        if values in selected:
            parser.error(f"duplicate --only-route target: {values[0]}:{values[1]}")
        selected.append(values)
        setattr(namespace, self.dest, selected)


@contextmanager
def _catalog_process_lock(catalog_path: Path) -> Iterator[None]:
    """Hold a crash-released OS lock for one catalog ingest CLI process."""
    path = Path(catalog_path)
    lock_path = path.with_name(path.name + ".topology.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    unlock: Callable[[], None] | None = None
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            raise TopologyProcessLocked(
                "another topology ingest CLI process already owns this catalog"
            ) from None
        acquired = True
        yield
    finally:
        if acquired and unlock is not None:
            try:
                handle.seek(0)
                unlock()
            except OSError:
                pass
        handle.close()


_STATUS_PRIORITY = {
    "COMPLETE": 0,
    "PARTIAL": 1,
    "BUDGET_EXHAUSTED": 2,
    "DATA_GAP": 3,
    "FAILED": 4,
}

# These codes describe an upstream/transport failure that is safe to retry
# from page one.  Route-shape validation failures and quarantined spikes are
# deliberately excluded; they need an operator or a better source.
TRANSIENT_RETRY_ERROR_CODES = frozenset(
    {
        "04",
        "99",
        "TAGO_TIMEOUT",
        "LOCAL_API_TIMEOUT",
        "LOCAL_API_HTTP_ERROR",
        "UPSTREAM_MALFORMED_RESPONSE",
    }
)


def _stronger_status(current: str, candidate: str) -> str:
    return (
        candidate
        if _STATUS_PRIORITY[candidate] > _STATUS_PRIORITY[current]
        else current
    )


def _safe_local_error_code(value: Any) -> str:
    candidate = str(value or "LOCAL_API_ERROR")
    if not 1 <= len(candidate) <= 64 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
        for character in candidate
    ):
        return "LOCAL_API_ERROR"
    return candidate


class _RejectRedirects(HTTPRedirectHandler):
    """Keep every proxy request on its originally validated loopback URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _local_live_api_origin(value: str) -> str:
    """Validate and canonicalize one literal loopback HTTP origin."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or any(ord(character) < 32 for character in value)
    ):
        raise argparse.ArgumentTypeError("local live API must be a loopback HTTP origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("local live API has an invalid host or port") from exc
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise argparse.ArgumentTypeError(
            "local live API must be HTTP with no userinfo, path, query, or fragment"
        )
    hostname = parsed.hostname
    if not hostname or "%" in hostname or port is None:
        raise argparse.ArgumentTypeError(
            "local live API must use a literal loopback address and explicit port"
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "local live API must use a literal loopback address"
        ) from exc
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    if not address.is_loopback and not (mapped is not None and mapped.is_loopback):
        raise argparse.ArgumentTypeError("local live API address must be loopback")
    if not MIN_LOCAL_API_PORT <= port <= MAX_LOCAL_API_PORT:
        raise argparse.ArgumentTypeError(
            f"local live API port must be {MIN_LOCAL_API_PORT}..{MAX_LOCAL_API_PORT}"
        )
    rendered_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"http://{rendered_host}:{port}"


class LocalLiveApiFetcher:
    """Fetch allow-listed catalog data from an already-running local service."""

    def __init__(
        self,
        origin: str,
        *,
        timeout_seconds: float,
        open_url: Callable[..., Any] | None = None,
    ):
        self.origin = _local_live_api_origin(origin)
        if not 0.5 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be 0.5..30")
        self.timeout_seconds = timeout_seconds
        self._open_url_override = open_url
        self._opener_local = threading.local()

    def _open_url(self, request: Request, *, timeout: float):
        if self._open_url_override is not None:
            return self._open_url_override(request, timeout=timeout)
        opener = getattr(self._opener_local, "opener", None)
        if opener is None:
            opener = build_opener(ProxyHandler({}), _RejectRedirects())
            self._opener_local.opener = opener
        return opener.open(request, timeout=timeout)

    @staticmethod
    def _http_error_code(error: HTTPError) -> str:
        try:
            raw = error.read(MAX_LOCAL_API_RESPONSE_BYTES + 1)
            if len(raw) > MAX_LOCAL_API_RESPONSE_BYTES:
                return "LOCAL_API_HTTP_ERROR"
            payload = json.loads(raw)
        except (AttributeError, OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return "LOCAL_API_HTTP_ERROR"
        if not isinstance(payload, dict):
            return "LOCAL_API_HTTP_ERROR"
        details = payload.get("error")
        if not isinstance(details, dict):
            return "LOCAL_API_HTTP_ERROR"
        return _safe_local_error_code(details.get("code"))

    def _json_get(self, path: str, query: Mapping[str, str] | None = None) -> dict[str, Any]:
        if path not in LOCAL_API_PATHS:
            raise TagoError(
                "LOCAL_API_PATH_INVALID", "Unsupported local API endpoint", status=500
            )
        encoded_query = urlencode(dict(query or {}))
        url = f"{self.origin}{path}" + (f"?{encoded_query}" if encoded_query else "")
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "busro-itda-topology-ingest/0.1",
            },
            method="GET",
        )
        try:
            with self._open_url(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status is None and hasattr(response, "getcode"):
                    status = response.getcode()
                if status == 429:
                    raise RequestBudgetExhausted("local Busro API request budget exhausted")
                if status != 200:
                    raise TagoError(
                        "LOCAL_API_HTTP_ERROR", "Local Busro API request failed", status=502
                    )
                headers = getattr(response, "headers", {})
                raw_length = headers.get("Content-Length") if hasattr(headers, "get") else None
                if raw_length is not None:
                    try:
                        content_length = int(raw_length)
                    except (TypeError, ValueError) as exc:
                        raise TagoError(
                            "LOCAL_API_INVALID_RESPONSE",
                            "Local Busro API returned an invalid response",
                        ) from exc
                    if content_length < 0:
                        raise TagoError(
                            "LOCAL_API_INVALID_RESPONSE",
                            "Local Busro API returned an invalid response",
                        )
                    if content_length > MAX_LOCAL_API_RESPONSE_BYTES:
                        raise TagoError(
                            "LOCAL_API_RESPONSE_TOO_LARGE",
                            "Local Busro API response exceeded 2 MB",
                        )
                raw = response.read(MAX_LOCAL_API_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code == 429:
                raise RequestBudgetExhausted(
                    "local Busro API request budget exhausted"
                ) from None
            code = self._http_error_code(exc)
            raise TagoError(
                code, "Local Busro API request failed", status=502
            ) from None
        except (TimeoutError, socket.timeout) as exc:
            raise TagoError(
                "LOCAL_API_TIMEOUT", "Local Busro API request timed out", status=504
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise TagoError(
                    "LOCAL_API_TIMEOUT", "Local Busro API request timed out", status=504
                ) from exc
            raise TagoError(
                "LOCAL_API_UNAVAILABLE", "Local Busro API is unavailable", status=502
            ) from exc
        except OSError as exc:
            raise TagoError(
                "LOCAL_API_UNAVAILABLE", "Local Busro API is unavailable", status=502
            ) from exc
        if len(raw) > MAX_LOCAL_API_RESPONSE_BYTES:
            raise TagoError(
                "LOCAL_API_RESPONSE_TOO_LARGE", "Local Busro API response exceeded 2 MB"
            )
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TagoError(
                "LOCAL_API_INVALID_JSON", "Local Busro API returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise TagoError(
                "LOCAL_API_INVALID_RESPONSE", "Local Busro API returned an invalid response"
            )
        if payload.get("ok") is not True:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            candidate = error.get("code") if isinstance(error, dict) else None
            code = _safe_local_error_code(candidate)
            raise TagoError(code, "Local Busro API request failed", status=502)
        return payload

    def verify_ready(self) -> None:
        status = self._json_get("/api/status")
        tago = status.get("tago") if isinstance(status.get("tago"), dict) else {}
        if not (
            status.get("service") == "busro-itda-data-service"
            and status.get("mode") == "live"
            and tago.get("configured") is True
            and tago.get("state") == "ready"
            and tago.get("key_exposed") is False
        ):
            raise TagoError(
                "LOCAL_API_NOT_READY",
                "Local Busro API is not live and TAGO-ready",
                status=503,
            )

    def __call__(self, operation: str, parameters: dict[str, str]) -> dict[str, Any]:
        spec = LOCAL_API_OPERATIONS.get(operation)
        if spec is None:
            raise TagoError(
                "CATALOG_OPERATION_INVALID", "Unsupported local catalog operation", status=500
            )
        path, output_key, parameter_names, required_parameters = spec
        if (
            not isinstance(parameters, dict)
            or set(parameters) - set(parameter_names)
            or required_parameters - set(parameters)
            or any(
                not isinstance(value, str)
                or len(value) > 120
                or any(ord(character) < 32 for character in value)
                for value in parameters.values()
            )
        ):
            raise TagoError(
                "CATALOG_PARAMETER_INVALID", "Invalid local catalog parameters", status=500
            )
        query = {
            public_name: parameters[upstream_name]
            for upstream_name, public_name in parameter_names.items()
            if upstream_name in parameters
        }
        payload = self._json_get(path, query)
        if payload.get("mode") != "live":
            raise TagoError(
                "LOCAL_API_NOT_LIVE", "Local Busro API catalog response was not live", status=503
            )
        records = payload.get(output_key)
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise TagoError(
                "LOCAL_API_INVALID_RESPONSE", "Local Busro API returned an invalid catalog"
            )
        fields = LOCAL_API_RAW_FIELDS[operation]
        raw_items = [
            {raw_name: item.get(public_name) for public_name, raw_name in fields.items()}
            for item in records
        ]
        upstream = payload.get("upstream")
        upstream = upstream if isinstance(upstream, dict) else {}
        return {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"},
                "body": {
                    "items": {"item": raw_items},
                    "pageNo": upstream.get("page_no", query.get("page", 1)),
                    "numOfRows": upstream.get("num_rows", query.get("limit", len(raw_items))),
                    "totalCount": upstream.get("total_count", len(raw_items)),
                },
            }
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error_message(value: Any) -> str:
    text = " ".join(str(value or "Upstream request failed").split())
    return text[:240] or "Upstream request failed"


def _public_tago_message(code: str) -> str:
    """Return fixed text so an upstream body can never persist a key/query."""
    if code in FATAL_ACCESS_CODES:
        return "TAGO route/station API authorization is unavailable"
    if code in UPSTREAM_DAILY_QUOTA_CODES:
        return "TAGO daily request quota is exhausted"
    if code == "TAGO_TIMEOUT":
        return "TAGO request timed out"
    return "TAGO request failed"


@dataclass(frozen=True, slots=True)
class IngestConfig:
    request_budget: int = 9_000
    requests_per_second: float = 2.0
    page_size: int = 100
    max_route_pages: int = 10
    max_discovery_pages: int = 200
    target_limit: int | None = None
    target_source: str = "tago"
    trust_catalog_identifiers: bool = False
    refresh_complete: bool = False
    workers: int = 1
    only_routes: tuple[tuple[str, str], ...] = ()

    def validate(self) -> None:
        if not 1 <= self.request_budget <= 100_000:
            raise ValueError("request_budget must be 1..100000")
        if self.requests_per_second != 0 and not 0.1 <= self.requests_per_second <= 20:
            raise ValueError("requests_per_second must be 0 or 0.1..20")
        if not 1 <= self.page_size <= 100:
            raise ValueError("page_size must be 1..100")
        if not 1 <= self.max_route_pages <= 100:
            raise ValueError("max_route_pages must be 1..100")
        if not 1 <= self.max_discovery_pages <= 1_000:
            raise ValueError("max_discovery_pages must be 1..1000")
        if self.target_limit is not None and not 1 <= self.target_limit <= 500_000:
            raise ValueError("target_limit must be 1..500000")
        if self.target_source not in {"tago", "catalog"}:
            raise ValueError("target_source must be tago or catalog")
        if self.target_source == "catalog" and not self.trust_catalog_identifiers:
            raise ValueError(
                "catalog mode requires --trust-catalog-identifiers after provider-namespace verification"
            )
        if not 1 <= self.workers <= 16:
            raise ValueError("workers must be 1..16")
        validated_routes: list[tuple[str, str]] = []
        for target in self.only_routes:
            if not isinstance(target, tuple) or len(target) != 2:
                raise ValueError("only_routes must contain (city_code, route_id) pairs")
            city_code, route_id = target
            if not isinstance(city_code, str) or not isinstance(route_id, str):
                raise ValueError("only_routes identifiers must be strings")
            if (
                not _ONLY_ROUTE_CITY_CODE.fullmatch(city_code)
                or not _ONLY_ROUTE_ID.fullmatch(route_id)
            ):
                raise ValueError(
                    "--only-route must use CITY_CODE:ROUTE_ID with valid TAGO identifiers"
                )
            validated_routes.append((city_code, route_id))
        if len(set(validated_routes)) != len(validated_routes):
            raise ValueError("duplicate --only-route target")
        if self.only_routes and self.target_limit is not None:
            raise ValueError("--target-limit cannot be combined with --only-route")


class _SerializedCatalog:
    """Serialize catalog calls while leaving network fetches concurrent."""

    def __init__(self, catalog: NetworkCatalog, lock: threading.RLock):
        self._catalog = catalog
        self._lock = lock

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._catalog, name)
        if not callable(attribute):
            return attribute

        def serialized(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return attribute(*args, **kwargs)

        return serialized


class TopologyIngestor:
    def __init__(
        self,
        *,
        catalog: NetworkCatalog,
        fetcher: Callable[[str, dict[str, str]], dict[str, Any]],
        config: IngestConfig,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        config.validate()
        self._catalog_lock = threading.RLock()
        self.catalog = _SerializedCatalog(catalog, self._catalog_lock)
        self.fetcher = fetcher
        self.config = config
        self.clock = clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.run_id = "ing_" + uuid.uuid4().hex[:24]
        self.requests_used = 0
        self.discovery_failures = 0
        self._last_request_started: float | None = None
        self._request_start_lock = threading.Lock()
        self._stop_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._stop_status: str | None = None

    def _set_stop(self, status: str) -> None:
        if status not in {"BUDGET_EXHAUSTED", "DATA_GAP", "FAILED"}:
            raise ValueError("invalid terminal ingest status")
        with self._stop_lock:
            if self._stop_status is None:
                self._stop_status = status
            else:
                self._stop_status = _stronger_status(self._stop_status, status)
            self._stop_event.set()

    def _stopped_status(self) -> str | None:
        with self._stop_lock:
            return self._stop_status

    def _on_request_started(self, started_at: float) -> None:
        """Test/observability hook invoked inside the global request gate."""

    def _request(
        self,
        operation: str,
        parameters: dict[str, str],
        *,
        target: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        # One start gate owns the global rate slot and exact request budget.
        # Network I/O remains outside the gate so up to `workers` calls can
        # overlap without producing a start burst.
        with self._request_start_lock:
            with self._stop_lock:
                stopped = self._stop_status
                if stopped is not None:
                    raise IngestStopped(stopped)
                if self.requests_used >= self.config.request_budget:
                    self._set_stop("BUDGET_EXHAUSTED")
                    raise RequestBudgetExhausted("request budget exhausted")
            if self.config.requests_per_second > 0 and self._last_request_started is not None:
                interval = 1.0 / self.config.requests_per_second
                remaining = interval - (self.monotonic() - self._last_request_started)
                if remaining > 0:
                    self.sleeper(remaining)
            # The second stop check and admission are atomic with fatal stop
            # publication. Lock order is always request-start -> stop.
            with self._stop_lock:
                stopped = self._stop_status
                if stopped is not None:
                    raise IngestStopped(stopped)
                if self.requests_used >= self.config.request_budget:
                    self._set_stop("BUDGET_EXHAUSTED")
                    raise RequestBudgetExhausted("request budget exhausted")
                # This admission is the request start. Keep all blocking
                # database work after the network call so a fatal peer cannot
                # leave a waiter between stop-check and admission.
                self.requests_used += 1
                self._last_request_started = self.monotonic()
                started_at = self._last_request_started
            self._on_request_started(started_at)
        try:
            try:
                return self.fetcher(operation, parameters)
            except RequestBudgetExhausted:
                self._set_stop("BUDGET_EXHAUSTED")
                raise
            except TagoError as exc:
                if exc.code in UPSTREAM_DAILY_QUOTA_CODES:
                    self._set_stop("BUDGET_EXHAUSTED")
                elif exc.code in FATAL_ACCESS_CODES:
                    self._set_stop("DATA_GAP")
                raise
            except Exception:
                self._set_stop("FAILED")
                raise
        finally:
            if target is None:
                self.catalog.record_topology_request_attempt(
                    run_id=self.run_id,
                    provider=PROVIDER,
                )
            else:
                self.catalog.record_topology_request_attempt(
                    run_id=self.run_id,
                    provider=PROVIDER,
                    city_code=target[0],
                    route_id=target[1],
                )

    def _discover_tago_targets(self) -> None:
        city_progress = self.catalog.topology_discovery_progress(
            provider=PROVIDER, scope_key="cities"
        )
        if city_progress["status"] != "COMPLETE":
            try:
                raw = self._request("cities", {})
                cities, metadata = normalize_catalog(raw, operation="cities")
                cities = [item for item in cities if item.get("city_code") and item.get("city_name")]
                if not cities:
                    raise CatalogValidationError("TAGO city discovery returned no usable identifiers")
                self.catalog.upsert_topology_cities(provider=PROVIDER, cities=cities)
                self.catalog.update_topology_discovery(
                    provider=PROVIDER,
                    scope_key="cities",
                    status="COMPLETE",
                    next_page=1,
                    total_count=int(metadata.get("total_count") or len(cities)),
                    request_increment=1,
                )
            except RequestBudgetExhausted:
                self.catalog.update_topology_discovery(
                    provider=PROVIDER,
                    scope_key="cities",
                    status="DEFERRED",
                    next_page=1,
                    total_count=None,
                    error_code="REQUEST_BUDGET_EXHAUSTED",
                    error_message="Request budget exhausted before city discovery completed",
                )
                raise
            except TagoError as exc:
                quota_exhausted = exc.code in UPSTREAM_DAILY_QUOTA_CODES
                self.catalog.update_topology_discovery(
                    provider=PROVIDER,
                    scope_key="cities",
                    status="DEFERRED" if quota_exhausted else "FAILED",
                    next_page=1,
                    total_count=None,
                    request_increment=1,
                    error_code=exc.code,
                    error_message=_public_tago_message(exc.code),
                )
                if quota_exhausted:
                    raise RequestBudgetExhausted(
                        "TAGO daily request quota exhausted"
                    ) from exc
                raise

        for city in self.catalog.topology_cities(provider=PROVIDER):
            city_code = city["city_code"]
            scope = f"routes:{city_code}"
            progress = self.catalog.topology_discovery_progress(
                provider=PROVIDER, scope_key=scope
            )
            if progress["status"] == "COMPLETE":
                continue
            page = int(progress.get("next_page") or 1)
            if page > self.config.max_discovery_pages:
                self.catalog.update_topology_discovery(
                    provider=PROVIDER,
                    scope_key=scope,
                    status="FAILED",
                    next_page=page,
                    total_count=progress.get("total_count"),
                    error_code="ROUTE_DISCOVERY_DATA_GAP",
                    error_message="TAGO route discovery exceeded configured page bound",
                )
                self.discovery_failures += 1
                continue
            while page <= self.config.max_discovery_pages:
                try:
                    raw = self._request(
                        "routes",
                        {
                            "cityCode": city_code,
                            "pageNo": str(page),
                            "numOfRows": str(self.config.page_size),
                        },
                    )
                    routes, metadata = normalize_catalog(
                        raw, operation="routes", fallback_city_code=city_code
                    )
                    routes = [
                        item
                        for item in routes
                        if item.get("city_code") == city_code and item.get("route_id")
                    ]
                    self.catalog.upsert_topology_targets(
                        provider=PROVIDER,
                        routes=routes,
                        discovery_source="TAGO_CITY_ROUTE_DISCOVERY",
                    )
                    total = max(0, int(metadata.get("total_count") or len(routes)))
                    complete = (page - 1) * self.config.page_size + len(routes) >= total
                    self.catalog.update_topology_discovery(
                        provider=PROVIDER,
                        scope_key=scope,
                        status="COMPLETE" if complete else "IN_PROGRESS",
                        next_page=page + 1,
                        total_count=total,
                        request_increment=1,
                    )
                    if complete:
                        break
                    if not routes:
                        raise CatalogValidationError(
                            "TAGO route discovery ended before reported total_count"
                        )
                    page += 1
                except RequestBudgetExhausted:
                    self.catalog.update_topology_discovery(
                        provider=PROVIDER,
                        scope_key=scope,
                        status="DEFERRED",
                        next_page=page,
                        total_count=progress.get("total_count"),
                        error_code="REQUEST_BUDGET_EXHAUSTED",
                        error_message="Request budget exhausted during route discovery",
                    )
                    raise
                except TagoError as exc:
                    quota_exhausted = exc.code in UPSTREAM_DAILY_QUOTA_CODES
                    self.catalog.update_topology_discovery(
                        provider=PROVIDER,
                        scope_key=scope,
                        status="DEFERRED" if quota_exhausted else "FAILED",
                        next_page=page,
                        total_count=progress.get("total_count"),
                        request_increment=1,
                        error_code=exc.code,
                        error_message=_public_tago_message(exc.code),
                    )
                    if quota_exhausted:
                        raise RequestBudgetExhausted(
                            "TAGO daily request quota exhausted"
                        ) from exc
                    if exc.code in FATAL_ACCESS_CODES:
                        raise
                    self.discovery_failures += 1
                    break
                except CatalogError:
                    # A provider data defect belongs to this city scope. Keep
                    # already validated targets and continue discovering other
                    # cities; access/key failures are handled above and remain
                    # fatal for the run.
                    self.catalog.update_topology_discovery(
                        provider=PROVIDER,
                        scope_key=scope,
                        status="FAILED",
                        next_page=page,
                        total_count=progress.get("total_count"),
                        request_increment=1,
                        error_code="ROUTE_DISCOVERY_DATA_GAP",
                        error_message="TAGO route discovery data failed validation",
                    )
                    self.discovery_failures += 1
                    break
            else:
                self.catalog.update_topology_discovery(
                    provider=PROVIDER,
                    scope_key=scope,
                    status="FAILED",
                    next_page=page,
                    total_count=progress.get("total_count"),
                    error_code="ROUTE_DISCOVERY_DATA_GAP",
                    error_message="TAGO route discovery exceeded configured page bound",
                )
                self.discovery_failures += 1

    def _ingest_target(self, target: Mapping[str, Any]) -> str:
        city_code = str(target["city_code"])
        route_id = str(target["route_id"])
        page = int(target.get("next_page") or 1)
        total_count = target.get("total_count")
        while True:
            if page > self.config.max_route_pages:
                raise CatalogLimitError("route stop list exceeded configured page bound")
            raw = self._request(
                "route_stops",
                {
                    "cityCode": city_code,
                    "routeId": route_id,
                    "pageNo": str(page),
                    "numOfRows": str(self.config.page_size),
                },
                target=(city_code, route_id),
            )
            stops, metadata = normalize_catalog(
                raw,
                operation="route_stops",
                fallback_city_code=city_code,
                fallback_route_id=route_id,
            )
            if any(
                stop.get("city_code") != city_code or stop.get("route_id") != route_id
                for stop in stops
            ):
                raise CatalogValidationError("TAGO route-stop page crossed route identifiers")
            total_count = max(0, int(metadata.get("total_count") or len(stops)))
            if total_count > self.config.page_size * self.config.max_route_pages:
                raise CatalogLimitError("route stop list exceeded configured row bound")
            self.catalog.stage_topology_page(
                provider=PROVIDER,
                city_code=city_code,
                route_id=route_id,
                page_no=page,
                items=stops,
                total_count=total_count,
            )
            complete = (page - 1) * self.config.page_size + len(stops) >= total_count
            if complete:
                break
            if not stops:
                raise CatalogValidationError(
                    "TAGO route-stop pages ended before reported total_count"
                )
            page += 1

        staged = self.catalog.staged_topology_route(
            provider=PROVIDER, city_code=city_code, route_id=route_id
        )
        if total_count is not None and len(staged) < int(total_count):
            raise CatalogValidationError("complete route-stop sequence was not staged")
        ordered = sorted(
            staged,
            key=lambda item: (
                item.get("node_order") is None,
                item.get("node_order") if item.get("node_order") is not None else 0,
            ),
        )
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
        spike = next(
            (
                evidence
                for stop_index in range(2, len(sequence_rows))
                if (
                    evidence := single_point_route_spike(
                        sequence_rows[stop_index - 2],
                        sequence_rows[stop_index - 1],
                        sequence_rows[stop_index],
                    )
                )
                is not None
            ),
            None,
        )
        if spike is not None:
            active = self.catalog.active_route_sequence_info(
                city_code=city_code, route_id=route_id
            )
            self.catalog.quarantine_topology_route_spike(
                provider=PROVIDER,
                city_code=city_code,
                route_id=route_id,
                expected_sequence_id=(active["sequence_id"] if active else None),
                evidence=spike,
            )
            raise SinglePointRouteSpikeQuarantined
        digest = self.catalog.route_sequence_sha256(
            city_code=city_code, route_id=route_id, ordered_stops=sequence_rows
        )
        active = self.catalog.active_route_sequence_info(
            city_code=city_code, route_id=route_id
        )
        if active is not None and active["sha256"] == digest:
            self.catalog.finish_topology_target(
                provider=PROVIDER,
                city_code=city_code,
                route_id=route_id,
                unchanged=True,
                content_sha256=digest,
                sequence_id=active["sequence_id"],
            )
            return "UNCHANGED"
        captured_at = self.clock().astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        result = self.catalog.hydrate_route_sequence(
            city_code=city_code,
            route_id=route_id,
            ordered_stops=sequence_rows,
            source="TAGO_ROUTE_STOPS_LIVE_BATCH",
            captured_at=captured_at,
        )
        self.catalog.finish_topology_target(
            provider=PROVIDER,
            city_code=city_code,
            route_id=route_id,
            unchanged=False,
            content_sha256=digest,
            sequence_id=result["sequence_id"],
        )
        return "COMPLETE"

    def _process_target(self, target: Mapping[str, Any]) -> str:
        try:
            outcome = self._ingest_target(target)
            counters = {
                "targets_processed": 1,
                "unchanged" if outcome == "UNCHANGED" else "succeeded": 1,
            }
            self.catalog.update_topology_run(self.run_id, **counters)
            return "COMPLETE"
        except SinglePointRouteSpikeQuarantined:
            self.catalog.update_topology_run(
                self.run_id, targets_processed=1, failed=1
            )
            return "PARTIAL"
        except IngestStopped as exc:
            budget_stop = exc.status == "BUDGET_EXHAUSTED"
            self.catalog.defer_or_fail_topology_target(
                provider=PROVIDER,
                city_code=target["city_code"],
                route_id=target["route_id"],
                deferred=True,
                error_code=(
                    "REQUEST_BUDGET_EXHAUSTED" if budget_stop else "INGEST_STOPPED"
                ),
                error_message=(
                    "Request budget exhausted; rerun resumes from staged page"
                    if budget_stop
                    else "Ingest stopped; rerun resumes from staged page"
                ),
            )
            self.catalog.update_topology_run(
                self.run_id, targets_processed=1, deferred=1
            )
            return exc.status
        except RequestBudgetExhausted:
            self._set_stop("BUDGET_EXHAUSTED")
            self.catalog.defer_or_fail_topology_target(
                provider=PROVIDER,
                city_code=target["city_code"],
                route_id=target["route_id"],
                deferred=True,
                error_code="REQUEST_BUDGET_EXHAUSTED",
                error_message="Request budget exhausted; rerun resumes from staged page",
            )
            self.catalog.update_topology_run(
                self.run_id, targets_processed=1, deferred=1
            )
            return "BUDGET_EXHAUSTED"
        except TagoError as exc:
            if exc.code in UPSTREAM_DAILY_QUOTA_CODES:
                self.catalog.defer_or_fail_topology_target(
                    provider=PROVIDER,
                    city_code=target["city_code"],
                    route_id=target["route_id"],
                    deferred=True,
                    error_code=exc.code,
                    error_message=_public_tago_message(exc.code),
                )
                self.catalog.update_topology_run(
                    self.run_id, targets_processed=1, deferred=1
                )
                return "BUDGET_EXHAUSTED"
            status = "DATA_GAP" if exc.code in FATAL_ACCESS_CODES else "PARTIAL"
            if status == "DATA_GAP":
                self._set_stop(status)
            self.catalog.defer_or_fail_topology_target(
                provider=PROVIDER,
                city_code=target["city_code"],
                route_id=target["route_id"],
                deferred=False,
                error_code=exc.code,
                error_message=_public_tago_message(exc.code),
            )
            self.catalog.update_topology_run(
                self.run_id, targets_processed=1, failed=1
            )
            return status
        except CatalogError as exc:
            self.catalog.defer_or_fail_topology_target(
                provider=PROVIDER,
                city_code=target["city_code"],
                route_id=target["route_id"],
                deferred=False,
                error_code="INVALID_ROUTE_TOPOLOGY",
                error_message=_safe_error_message(exc),
            )
            self.catalog.update_topology_run(
                self.run_id, targets_processed=1, failed=1
            )
            return "PARTIAL"
        except Exception:
            # Unknown exception text may contain implementation or transport
            # details. Stop new starts immediately and persist a fixed marker.
            self._set_stop("FAILED")
            self.catalog.defer_or_fail_topology_target(
                provider=PROVIDER,
                city_code=target["city_code"],
                route_id=target["route_id"],
                deferred=False,
                error_code="UNEXPECTED_COLLECTOR_ERROR",
                error_message="Unexpected collector failure",
            )
            self.catalog.update_topology_run(
                self.run_id, targets_processed=1, failed=1
            )
            return "FAILED"

    def _run_targets_sequential(self) -> str:
        final_status = "COMPLETE"
        if self.config.only_routes:
            for city_code, route_id in self.config.only_routes:
                if self._stop_event.is_set():
                    break
                target = self.catalog.claim_specific_topology_target(
                    provider=PROVIDER,
                    run_id=self.run_id,
                    city_code=city_code,
                    route_id=route_id,
                    refresh_complete=self.config.refresh_complete,
                )
                if target is None:
                    continue
                outcome = self._process_target(target)
                final_status = _stronger_status(final_status, outcome)
                if self._stop_event.is_set():
                    break
            return final_status

        claimed = 0
        while self.config.target_limit is None or claimed < self.config.target_limit:
            if self._stop_event.is_set():
                break
            target = self.catalog.claim_topology_target(
                provider=PROVIDER, run_id=self.run_id
            )
            if target is None:
                break
            claimed += 1
            outcome = self._process_target(target)
            final_status = _stronger_status(final_status, outcome)
            if self._stop_event.is_set():
                break
        return final_status

    def _run_targets_parallel(self) -> str:
        final_status = "COMPLETE"
        claimed = 0
        exhausted = False
        selected_targets = iter(self.config.only_routes)
        futures: dict[Any, Mapping[str, Any]] = {}
        with ThreadPoolExecutor(
            max_workers=self.config.workers,
            thread_name_prefix="busro-topology",
        ) as executor:
            try:
                while True:
                    while (
                        not exhausted
                        and not self._stop_event.is_set()
                        and len(futures) < self.config.workers
                        and (
                            self.config.target_limit is None
                            or claimed < self.config.target_limit
                        )
                    ):
                        if self.config.only_routes:
                            try:
                                city_code, route_id = next(selected_targets)
                            except StopIteration:
                                exhausted = True
                                break
                            target = self.catalog.claim_specific_topology_target(
                                provider=PROVIDER,
                                run_id=self.run_id,
                                city_code=city_code,
                                route_id=route_id,
                                refresh_complete=self.config.refresh_complete,
                            )
                            if target is None:
                                continue
                        else:
                            target = self.catalog.claim_topology_target(
                                provider=PROVIDER, run_id=self.run_id
                            )
                        if target is None:
                            exhausted = True
                            break
                        claimed += 1
                        futures[executor.submit(self._process_target, target)] = target
                    if not futures:
                        break
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        futures.pop(future, None)
                        outcome = future.result()
                        final_status = _stronger_status(final_status, outcome)
                    if self._stop_event.is_set() and not futures:
                        break
            except KeyboardInterrupt:
                self._set_stop("FAILED")
                raise
        stopped = self._stopped_status()
        return _stronger_status(final_status, stopped) if stopped else final_status

    def run(self) -> dict[str, Any]:
        self.catalog.create_topology_run(
            run_id=self.run_id,
            provider=PROVIDER,
            target_source=self.config.target_source,
            request_budget=self.config.request_budget,
            target_limit=self.config.target_limit,
        )
        final_status = "COMPLETE"
        explicit_error: str | None = None
        try:
            if self.config.target_source == "tago":
                self._discover_tago_targets()
                if self.discovery_failures:
                    final_status = "PARTIAL"
            else:
                self.catalog.seed_topology_targets_from_catalog(
                    provider=PROVIDER,
                    identifiers_verified_for_provider=self.config.trust_catalog_identifiers,
                )
            if self.config.refresh_complete and not self.config.only_routes:
                self.catalog.queue_topology_refresh(provider=PROVIDER)
            target_status = (
                self._run_targets_sequential()
                if self.config.workers == 1
                else self._run_targets_parallel()
            )
            final_status = _stronger_status(final_status, target_status)
        except IngestStopped as exc:
            final_status = _stronger_status(final_status, exc.status)
        except KeyboardInterrupt:
            self._set_stop("FAILED")
            final_status = "FAILED"
        except RequestBudgetExhausted:
            self._set_stop("BUDGET_EXHAUSTED")
            final_status = _stronger_status(final_status, "BUDGET_EXHAUSTED")
        except TagoError as exc:
            status = "DATA_GAP" if exc.code in FATAL_ACCESS_CODES else "FAILED"
            if status == "DATA_GAP":
                self._set_stop(status)
            final_status = _stronger_status(final_status, status)
        except CatalogValidationError as exc:
            self._set_stop("FAILED")
            final_status = "FAILED"
            explicit_error = _safe_error_message(exc)
        except CatalogError:
            self._set_stop("FAILED")
            final_status = "FAILED"
        except Exception:
            self._set_stop("FAILED")
            final_status = "FAILED"
        stopped = self._stopped_status()
        if stopped is not None:
            final_status = _stronger_status(final_status, stopped)
        coverage = self.catalog.topology_coverage(provider=PROVIDER)
        if (
            final_status == "COMPLETE"
            and not self.config.only_routes
            and coverage["complete"] < coverage["targets"]
        ):
            final_status = "PARTIAL"
        run = self.catalog.finish_topology_run(self.run_id, final_status)
        result = {
            "ok": final_status in {"COMPLETE", "PARTIAL", "BUDGET_EXHAUSTED"},
            "run": run,
            "coverage": coverage,
            "discovery_failures": self.discovery_failures,
            "notice": (
                "TAGO route/station API authorization is required"
                if final_status == "DATA_GAP"
                else None
            ),
        }
        if explicit_error is not None:
            result["error"] = explicit_error
        return result


def run_fill_loop(
    *,
    catalog: NetworkCatalog,
    fetcher: Callable[[str, dict[str, str]], dict[str, Any]],
    config: IngestConfig,
    max_cycles: int = 1,
    pause_seconds: float = 0.0,
    max_no_progress_cycles: int = 2,
    retry_exhausted_transient: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run resumable topology ingestion cycles until the queue is drained.

    Each cycle has its own request budget and run row, so a process crash or a
    daily TAGO quota stop leaves the SQLite checkpoint available for the next
    invocation.  The loop never retries permanent validation failures and
    stops on access/quota errors instead of spinning.
    """
    if not 1 <= int(max_cycles) <= 365:
        raise ValueError("max_cycles must be 1..365")
    if not 0 <= float(pause_seconds) <= 86_400:
        raise ValueError("pause_seconds must be 0..86400")
    if not 1 <= int(max_no_progress_cycles) <= 20:
        raise ValueError("max_no_progress_cycles must be 1..20")

    requeued = 0
    if retry_exhausted_transient:
        requeued = catalog.requeue_topology_failures(
            provider=PROVIDER,
            error_codes=TRANSIENT_RETRY_ERROR_CODES,
            min_attempts=3,
        )

    runs: list[dict[str, Any]] = []
    last_coverage: dict[str, Any] | None = None
    previous_marker: tuple[Any, ...] | None = None
    no_progress_cycles = 0
    terminal_reason = "MAX_CYCLES"
    for cycle in range(1, int(max_cycles) + 1):
        result = TopologyIngestor(
            catalog=catalog,
            fetcher=fetcher,
            config=config,
        ).run()
        coverage = result.get("coverage") or {}
        last_coverage = coverage if isinstance(coverage, dict) else None
        run_summary = dict(result.get("run") or {})
        run_summary["coverage"] = coverage
        runs.append(run_summary)
        statuses = coverage.get("statuses") or {}
        marker = (
            int(coverage.get("complete") or 0),
            int(coverage.get("hydrated_active_sequences") or 0),
            int(statuses.get("PENDING") or 0),
            int(statuses.get("DEFERRED") or 0),
            int(statuses.get("FAILED") or 0),
        )
        if previous_marker == marker:
            no_progress_cycles += 1
        else:
            no_progress_cycles = 0
        previous_marker = marker

        discovery = coverage.get("discovery") or {}
        target_count = int(coverage.get("targets") or 0)
        complete_count = int(coverage.get("complete") or 0)
        if (
            bool(discovery.get("complete"))
            and target_count > 0
            and complete_count >= target_count
        ):
            terminal_reason = "COMPLETE"
            return {
                "ok": True,
                "status": "COMPLETE",
                "cycles": cycle,
                "requeued_transient_failures": requeued,
                "runs": runs,
                "coverage": coverage,
                "stop_reason": terminal_reason,
            }

        run_status = str((result.get("run") or {}).get("status") or "")
        if run_status in {"DATA_GAP", "BUDGET_EXHAUSTED", "FAILED"}:
            terminal_reason = run_status
            break
        if no_progress_cycles >= int(max_no_progress_cycles):
            terminal_reason = "NO_PROGRESS"
            break
        if cycle < int(max_cycles) and float(pause_seconds) > 0:
            sleeper(float(pause_seconds))

    final_coverage = last_coverage or catalog.topology_coverage(provider=PROVIDER)
    return {
        "ok": terminal_reason in {"COMPLETE", "MAX_CYCLES", "NO_PROGRESS"},
        "status": "COMPLETE" if terminal_reason == "COMPLETE" else "PARTIAL",
        "cycles": len(runs),
        "requeued_transient_failures": requeued,
        "runs": runs,
        "coverage": final_coverage,
        "stop_reason": terminal_reason,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover and ingest nationwide TAGO route-stop topology"
    )
    parser.add_argument("--catalog-db", type=Path, required=True)
    access_mode = parser.add_mutually_exclusive_group(required=True)
    access_mode.add_argument("--service-key-stdin", action="store_true")
    access_mode.add_argument(
        "--local-live-api",
        type=_local_live_api_origin,
        metavar="HTTP_LOOPBACK_ORIGIN",
        help="reuse a live TAGO-ready Busro service on a literal loopback HTTP origin",
    )
    parser.add_argument("--request-budget", type=int, default=9_000)
    parser.add_argument("--requests-per-second", type=float, default=2.0)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-route-pages", type=int, default=10)
    parser.add_argument("--max-discovery-pages", type=int, default=200)
    parser.add_argument("--target-limit", type=int)
    parser.add_argument(
        "--fill-loop",
        action="store_true",
        help="repeat resumable ingestion cycles until the queue is drained or a safe stop",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=4,
        help="maximum fill-loop cycles (1..365; each cycle has its own request budget)",
    )
    parser.add_argument(
        "--cycle-pause-seconds",
        type=float,
        default=5.0,
        help="pause between fill-loop cycles (0..86400)",
    )
    parser.add_argument(
        "--max-no-progress-cycles",
        type=int,
        default=2,
        help="stop after this many cycles without coverage movement (1..20)",
    )
    parser.add_argument(
        "--retry-exhausted-transient",
        action="store_true",
        help="requeue exhausted upstream/timeout failures once before the fill loop",
    )
    parser.add_argument("--target-source", choices=("tago", "catalog"), default="tago")
    parser.add_argument("--trust-catalog-identifiers", action="store_true")
    parser.add_argument(
        "--only-route",
        action=_AppendUniqueOnlyRoute,
        type=_only_route_target,
        default=[],
        metavar="CITY_CODE:ROUTE_ID",
        help=(
            "after target discovery, claim only this exact TAGO target; "
            "repeat for multiple routes"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="parallel route workers inside this one CLI process (1..16)",
    )
    parser.add_argument(
        "--refresh-complete",
        action="store_true",
        help="re-fetch completed routes and store only changed sequence hashes",
    )
    parser.add_argument(
        "--repair-corrupt-retries",
        action="store_true",
        help=(
            "before ingestion, requeue only failed routes whose staged page metadata "
            "proves pages from different retry responses were mixed"
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 0.5 <= args.timeout_seconds <= 30:
        raise SystemExit("--timeout-seconds must be 0.5..30")
    if not 1 <= args.max_cycles <= 365:
        raise SystemExit("--max-cycles must be 1..365")
    if not 0 <= args.cycle_pause_seconds <= 86_400:
        raise SystemExit("--cycle-pause-seconds must be 0..86400")
    if not 1 <= args.max_no_progress_cycles <= 20:
        raise SystemExit("--max-no-progress-cycles must be 1..20")
    config = IngestConfig(
        request_budget=args.request_budget,
        requests_per_second=args.requests_per_second,
        page_size=args.page_size,
        max_route_pages=args.max_route_pages,
        max_discovery_pages=args.max_discovery_pages,
        target_limit=args.target_limit,
        target_source=args.target_source,
        trust_catalog_identifiers=args.trust_catalog_identifiers,
        refresh_complete=args.refresh_complete,
        workers=args.workers,
        only_routes=tuple(args.only_route),
    )
    try:
        config.validate()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if args.service_key_stdin:
        # Browser relays and CI pass the approved key over a pipe.  On Windows
        # getpass insists on a console and can block forever when stdin is a
        # pipe, so use a non-echoing prompt only for an interactive terminal.
        if sys.stdin.isatty():
            service_key = getpass.getpass("TAGO decoded service key: ")
        else:
            service_key = sys.stdin.readline().strip()
        if not service_key:
            raise SystemExit("TAGO service key is required")

        def live_fetch(operation: str, parameters: dict[str, str]) -> dict[str, Any]:
            return fetch_catalog(
                operation=operation,
                parameters=parameters,
                service_key=service_key,
                timeout_seconds=args.timeout_seconds,
                fixture_mode=False,
                fixture_path=Path("unused"),
            )

    else:
        local_fetch = LocalLiveApiFetcher(
            args.local_live_api, timeout_seconds=args.timeout_seconds
        )
        try:
            local_fetch.verify_ready()
        except TagoError as exc:
            raise SystemExit("Local Busro API is not live and TAGO-ready") from exc
        live_fetch = local_fetch
    try:
        with _catalog_process_lock(args.catalog_db):
            catalog = NetworkCatalog(args.catalog_db)
            repaired_corrupt_retries = (
                catalog.repair_corrupt_topology_retries(provider=PROVIDER)
                if args.repair_corrupt_retries
                else 0
            )
            if args.fill_loop:
                result = run_fill_loop(
                    catalog=catalog,
                    fetcher=live_fetch,
                    config=config,
                    max_cycles=args.max_cycles,
                    pause_seconds=args.cycle_pause_seconds,
                    max_no_progress_cycles=args.max_no_progress_cycles,
                    retry_exhausted_transient=args.retry_exhausted_transient,
                )
            else:
                result = TopologyIngestor(
                    catalog=catalog, fetcher=live_fetch, config=config
                ).run()
            if args.repair_corrupt_retries:
                result["repaired_corrupt_retries"] = repaired_corrupt_retries
    except TopologyProcessLocked as exc:
        raise SystemExit(str(exc)) from None
    # The summary contains counters/status only; no key, URL, or query values.
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
