"""HTTP entrypoint. Run with: python server.py --fixture"""

from __future__ import annotations

import argparse
from dataclasses import replace
import getpass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from app import AppError, BusroService
from config import Settings
from loopback_live_api import GET_PATHS as LOOPBACK_GET_PATHS
from loopback_live_api import LoopbackApiError, LoopbackLiveApiClient


WEB_ROOT = Path(__file__).resolve().parent.parent
STATIC_FILES = frozenset(
    {
        "index.html", "tokens.css", "glass.css", "screens.css", "nationwide.css",
        "api.js", "map.js", "components.jsx", "nationwide.jsx", "screens.jsx", "app.jsx",
        "components.compiled.js", "nationwide.compiled.js", "screens.compiled.js", "app.compiled.js",
    }
)
OPERATOR_ENDPOINTS = frozenset(
    {
        "/api/collect",
        "/api/positions/collect",
        "/api/mappings/validate",
        "/api/network/hydrate",
    }
)
def _require_direct_live_upstream(status: dict[str, Any]) -> None:
    """Reject unavailable, fixture, or already-proxied status responses."""
    tago = status.get("tago") if isinstance(status, dict) else None
    if (
        not isinstance(tago, dict)
        or status.get("mode") != "live"
        or tago.get("state") != "ready"
        or tago.get("configured") is not True
        or tago.get("key_exposed") is not False
        or tago.get("credential_scope") == "loopback_upstream"
        or tago.get("connection") == "loopback_proxy"
    ):
        raise LoopbackApiError(
            "LOOPBACK_UPSTREAM_NOT_DIRECT_LIVE",
            "Loopback upstream must be a direct live TAGO service",
            status=503,
        )


class BusroHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256

    def __init__(
        self,
        address: tuple[str, int],
        handler,
        *,
        service: BusroService,
        live_api: LoopbackLiveApiClient | None = None,
        shared_live_storage: bool = False,
        shared_storage_baseline_consistent: bool = False,
        max_concurrent_requests: int = 200,
        request_timeout_seconds: float = 10.0,
    ):
        if not 1 <= int(max_concurrent_requests) <= 200:
            raise ValueError("max_concurrent_requests must be between 1 and 200")
        if not 1.0 <= float(request_timeout_seconds) <= 30.0:
            raise ValueError("request_timeout_seconds must be between 1 and 30")
        self.max_concurrent_requests = int(max_concurrent_requests)
        self.request_timeout_seconds = float(request_timeout_seconds)
        self._request_slots = threading.BoundedSemaphore(self.max_concurrent_requests)
        super().__init__(address, handler)
        self.service = service
        self.live_api = live_api
        self.shared_live_storage = bool(shared_live_storage)
        self.shared_storage_baseline_consistent = bool(shared_storage_baseline_consistent)
        self.shared_storage_write_verified = False
        self.shared_storage_failed = False
        self.live_upstream_direct = live_api is not None
        self.live_upstream_transport_identifiers = False
        self.live_upstream_attested_at = 0.0
        self.live_upstream_attestation_revision = 0
        self._live_attestation_lock = threading.Lock()

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout_seconds)
        return request, client_address

    def process_request(self, request, client_address) -> None:
        # Queue above the supported 200-active-request envelope instead of
        # creating unbounded handler threads under hostile connection bursts.
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "BusroItda/0.1"

    @property
    def service(self) -> BusroService:
        return self.server.service  # type: ignore[attr-defined]

    @property
    def live_api(self) -> LoopbackLiveApiClient | None:
        return self.server.live_api  # type: ignore[attr-defined]

    def do_OPTIONS(self) -> None:
        if not self._host_allowed() or not self._request_origin_allowed():
            return
        self.send_response(204)
        self._common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Idempotency-Key, Authorization, X-Busro-Operator-Token",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._host_allowed() or not self._request_origin_allowed():
            return
        parsed = urlsplit(self.path)
        try:
            if method == "POST" and parsed.path in OPERATOR_ENDPOINTS:
                self._require_operator()
            if method == "GET" and not parsed.path.startswith("/api/"):
                self._static_response(parsed.path)
                return
            query = self._query(parsed.query)
            if method == "GET" and parsed.path == "/api/status":
                self._json_response(200, self._status_payload())
            elif method == "GET" and self.live_api and parsed.path in LOOPBACK_GET_PATHS:
                self._attest_live_api()
                self._require_upstream_route_id(query.get("route_id"))
                self._json_response(200, self.live_api.get(self._proxy_target(parsed.path, query)))
            elif method == "GET" and parsed.path == "/api/arrivals":
                self._json_response(200, self.service.arrivals(query))
            elif method == "GET" and parsed.path == "/api/history":
                self._json_response(200, self.service.history(query))
            elif method == "GET" and parsed.path == "/api/positions":
                self._json_response(200, self.service.positions(query))
            elif method == "GET" and parsed.path == "/api/cities":
                self._json_response(200, self.service.cities(query))
            elif method == "GET" and parsed.path == "/api/routes":
                self._json_response(200, self.service.routes(query))
            elif method == "GET" and parsed.path == "/api/routes/info":
                self._json_response(200, self.service.route_info(query))
            elif method == "GET" and parsed.path == "/api/routes/stops":
                self._json_response(200, self.service.route_stops(query))
            elif method == "GET" and parsed.path == "/api/stops":
                self._json_response(200, self.service.stops(query))
            elif method == "GET" and parsed.path == "/api/stops/nearby":
                self._json_response(200, self.service.nearby_stops(query))
            elif method == "GET" and parsed.path == "/api/stops/routes":
                self._json_response(200, self.service.stop_routes(query))
            elif method == "GET" and parsed.path == "/api/network/status":
                self._json_response(200, self.service.network_status(query))
            elif method == "GET" and parsed.path == "/api/network/cities":
                self._json_response(200, self.service.network_cities(query))
            elif method == "GET" and parsed.path == "/api/network/stops":
                self._json_response(200, self.service.network_stops(query))
            elif method == "GET" and parsed.path == "/api/network/routes":
                self._json_response(200, self.service.network_routes(query))
            elif method == "GET" and parsed.path == "/api/sources":
                self._json_response(200, self.service.sources(query))
            elif method == "GET" and parsed.path == "/api/passages":
                self._json_response(200, self.service.passage_history(query))
            elif method == "POST" and parsed.path == "/api/collect":
                body = self._json_body()
                if self.live_api:
                    self._require_shared_mutation_ready()
                    result = self.live_api.post(
                        parsed.path,
                        body,
                        allow_mutation=True,
                        idempotency_key=self.headers.get("Idempotency-Key"),
                    )
                    self._verify_shared_snapshot(result, position=False)
                    status = 201 if result.get("created") is True else 200
                else:
                    result, status = self.service.collect(
                        body, header_idempotency_key=self.headers.get("Idempotency-Key")
                    )
                self._json_response(status, result)
            elif method == "POST" and parsed.path == "/api/simulate":
                self._json_response(200, self.service.simulate(self._json_body()))
            elif method == "POST" and parsed.path == "/api/positions/collect":
                body = self._json_body()
                if self.live_api:
                    self._require_shared_mutation_ready()
                    self._require_upstream_route_id(body.get("route_id"))
                    result = self.live_api.post(
                        parsed.path,
                        body,
                        allow_mutation=True,
                        idempotency_key=self.headers.get("Idempotency-Key"),
                    )
                    self._verify_shared_snapshot(result, position=True)
                    status = 201 if result.get("created") is True else 200
                else:
                    result, status = self.service.collect_positions(
                        body, header_idempotency_key=self.headers.get("Idempotency-Key")
                    )
                self._json_response(status, result)
            elif method == "POST" and parsed.path == "/api/replay":
                self._json_response(200, self.service.replay(self._json_body()))
            elif method == "POST" and parsed.path == "/api/mappings/validate":
                body = self._json_body()
                payload = (
                    self._proxy_mapping_validation(parsed.path, body)
                    if self.live_api
                    else self.service.validate_mapping(body)
                )
                self._json_response(200, payload)
            elif method == "POST" and parsed.path == "/api/network/hydrate":
                body = self._json_body()
                if self.live_api:
                    self._require_shared_mutation_ready()
                    self._require_upstream_route_id(body.get("route_id"))
                    payload = self.live_api.post(
                        parsed.path,
                        body,
                        allow_mutation=True,
                    )
                    self._verify_shared_route(payload, body)
                else:
                    payload = self.service.hydrate_network_route(body)
                self._json_response(200, payload)
            elif method == "POST" and parsed.path == "/api/journeys/generate":
                self._json_response(200, self.service.generate_journeys(self._json_body()))
            elif method == "POST" and parsed.path == "/api/osm/geometry":
                self._json_response(200, self.service.route_geometry(self._json_body()))
            else:
                raise AppError("NOT_FOUND", "API endpoint not found", status=404)
        except (AppError, LoopbackApiError) as exc:
            retry_after = None
            if exc.status == 429 and isinstance(exc.details, dict):
                try:
                    retry_after = min(
                        86_400,
                        max(1, int(exc.details.get("retry_after_seconds", 1))),
                    )
                except (TypeError, ValueError):
                    retry_after = 1
            self._json_response(
                exc.status,
                exc.payload(),
                retry_after_seconds=retry_after,
            )
        except Exception:
            # Keep implementation and secret-bearing exception text out of responses.
            self._json_response(
                500,
                {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
            )

    @staticmethod
    def _proxy_target(path: str, query: dict[str, str]) -> str:
        return path if not query else f"{path}?{urlencode(query)}"

    def _status_payload(self) -> dict[str, Any]:
        local = self.service.status()
        if not self.live_api:
            return local
        try:
            self._attest_live_api(force=True)
        except LoopbackApiError as exc:
            local["tago"] = {
                "configured": False,
                "state": "upstream_unavailable",
                "key_exposed": False,
                "credential_scope": "loopback_upstream",
                "connection": "loopback_proxy",
                "error_code": exc.code,
            }
            local["capabilities"].update(
                {
                    "live_arrivals": False,
                    "snapshot_collection": False,
                    "live_positions": False,
                    "position_snapshot_collection": False,
                    "route_stop_mapping_validation": False,
                    "verified_route_hydration": False,
                    "transport_route_identifiers": False,
                }
            )
            return local

        mutation_ready = self._shared_mutation_ready()
        transport_route_identifiers = (
            "hangul_ascii_safe"
            if self.server.live_upstream_transport_identifiers  # type: ignore[attr-defined]
            else "legacy_ascii_only"
        )
        local["capabilities"].update(
            {
                "snapshot_collection": mutation_ready,
                "position_snapshot_collection": mutation_ready,
                "verified_route_hydration": "/api/network/hydrate" if mutation_ready else False,
                "route_stop_mapping_validation": "/api/mappings/validate",
                "transport_route_identifiers": transport_route_identifiers,
            }
        )
        local["tago"] = {
            "configured": False,
            "state": "ready",
            "key_exposed": False,
            "credential_scope": "loopback_upstream",
            "connection": "loopback_proxy",
        }
        local["loopback_live_api"] = {
            "ready": True,
            "origin_exposed": False,
            "shared_storage_asserted": self.server.shared_live_storage,  # type: ignore[attr-defined]
            "baseline_consistent": self.server.shared_storage_baseline_consistent,  # type: ignore[attr-defined]
            "write_verified": self.server.shared_storage_write_verified,  # type: ignore[attr-defined]
            "failed": self.server.shared_storage_failed,  # type: ignore[attr-defined]
            "transport_route_identifiers": transport_route_identifiers,
        }
        return local

    def _attest_live_api(self, *, force: bool = False) -> None:
        if not self.live_api:
            raise LoopbackApiError(
                "LOOPBACK_API_UNAVAILABLE", "Loopback live API is unavailable", status=503
            )
        now = time.monotonic()
        server = self.server  # type: ignore[assignment]
        observed_revision = server.live_upstream_attestation_revision  # type: ignore[attr-defined]
        if not force:
            if not server.live_upstream_direct:  # type: ignore[attr-defined]
                raise LoopbackApiError(
                    "LOOPBACK_UPSTREAM_NOT_ATTESTED",
                    "Loopback upstream direct-live status must be revalidated",
                    status=503,
                )
            if now - server.live_upstream_attested_at <= 5.0:  # type: ignore[attr-defined]
                return
        with server._live_attestation_lock:  # type: ignore[attr-defined]
            if server.live_upstream_attestation_revision != observed_revision:  # type: ignore[attr-defined]
                if server.live_upstream_direct:  # type: ignore[attr-defined]
                    return
                raise LoopbackApiError(
                    "LOOPBACK_UPSTREAM_NOT_ATTESTED",
                    "Loopback upstream direct-live status must be revalidated",
                    status=503,
                )
            now = time.monotonic()
            if not force:
                if not server.live_upstream_direct:  # type: ignore[attr-defined]
                    raise LoopbackApiError(
                        "LOOPBACK_UPSTREAM_NOT_ATTESTED",
                        "Loopback upstream direct-live status must be revalidated",
                        status=503,
                    )
                if now - server.live_upstream_attested_at <= 5.0:  # type: ignore[attr-defined]
                    return
            try:
                upstream = self.live_api.probe_status()
                _require_direct_live_upstream(upstream)
            except LoopbackApiError:
                server.live_upstream_direct = False  # type: ignore[attr-defined]
                server.live_upstream_transport_identifiers = False  # type: ignore[attr-defined]
                server.live_upstream_attested_at = time.monotonic()  # type: ignore[attr-defined]
                server.live_upstream_attestation_revision += 1  # type: ignore[attr-defined]
                raise
            server.live_upstream_direct = True  # type: ignore[attr-defined]
            capabilities = upstream.get("capabilities")
            server.live_upstream_transport_identifiers = bool(  # type: ignore[attr-defined]
                isinstance(capabilities, dict)
                and capabilities.get("transport_route_identifiers") == "hangul_ascii_safe"
            )
            server.live_upstream_attested_at = time.monotonic()  # type: ignore[attr-defined]
            server.live_upstream_attestation_revision += 1  # type: ignore[attr-defined]

    def _shared_mutation_ready(self) -> bool:
        return bool(
            self.server.shared_live_storage  # type: ignore[attr-defined]
            and self.server.shared_storage_baseline_consistent  # type: ignore[attr-defined]
            and not self.server.shared_storage_failed  # type: ignore[attr-defined]
        )

    def _require_shared_mutation_ready(self) -> None:
        self._attest_live_api()
        if not self._shared_mutation_ready():
            code = (
                "LOOPBACK_SHARED_STORAGE_FAILED"
                if self.server.shared_storage_failed  # type: ignore[attr-defined]
                else "LOOPBACK_SHARED_STORAGE_REQUIRED"
            )
            raise LoopbackApiError(
                code,
                "Loopback writes require one healthy, verified shared storage configuration",
                status=503,
            )

    def _proxy_mapping_validation(
        self, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self._attest_live_api()
        self._require_upstream_route_id(body.get("route_id"))
        assert self.live_api is not None
        return self.live_api.post(path, body, allow_mutation=True)

    def _require_upstream_route_id(self, value: Any) -> None:
        route_id = str(value or "")
        if route_id and not route_id.isascii() and not self.server.live_upstream_transport_identifiers:  # type: ignore[attr-defined]
            raise LoopbackApiError(
                "LOOPBACK_UPSTREAM_ROUTE_ID_UNSUPPORTED",
                "The running upstream must be restarted on the current server version for Hangul route identifiers",
                status=503,
            )

    def _latch_shared_storage_failure(self) -> None:
        self.server.shared_storage_failed = True  # type: ignore[attr-defined]
        self.server.shared_storage_write_verified = False  # type: ignore[attr-defined]

    def _verify_shared_snapshot(self, payload: dict[str, Any], *, position: bool) -> None:
        snapshot = payload.get("snapshot")
        snapshot_id = snapshot.get("snapshot_id") if isinstance(snapshot, dict) else None
        if not isinstance(snapshot_id, str) or not 8 <= len(snapshot_id) <= 128:
            self._latch_shared_storage_failure()
            raise LoopbackApiError(
                "INVALID_LOOPBACK_RESPONSE",
                "Loopback collection response omitted its snapshot identifier",
                status=502,
            )
        stored = (
            self.service.store.get_position_snapshot(snapshot_id)
            if position
            else self.service.store.get_snapshot(snapshot_id)
        )
        if stored is None:
            self._latch_shared_storage_failure()
            raise LoopbackApiError(
                "LOOPBACK_SHARED_STORAGE_MISMATCH",
                "Loopback collection is not visible in the local history store",
                status=502,
            )
        if not self.server.shared_storage_failed:  # type: ignore[attr-defined]
            self.server.shared_storage_write_verified = True  # type: ignore[attr-defined]

    def _verify_shared_route(self, payload: dict[str, Any], body: dict[str, Any]) -> None:
        sequence = payload.get("sequence")
        sequence_id = sequence.get("sequence_id") if isinstance(sequence, dict) else None
        if not isinstance(sequence_id, str):
            self._latch_shared_storage_failure()
            raise LoopbackApiError(
                "INVALID_LOOPBACK_RESPONSE",
                "Loopback hydration response omitted its sequence identifier",
                status=502,
            )
        try:
            active = self.service.network_catalog.active_route_sequence_info(
                city_code=str(body.get("city_code") or ""),
                route_id=str(body.get("route_id") or ""),
            )
        except Exception as exc:
            self._latch_shared_storage_failure()
            raise LoopbackApiError(
                "LOOPBACK_SHARED_STORAGE_MISMATCH",
                "Loopback hydration is not visible in the local network catalog",
                status=502,
            ) from exc
        if not active or active.get("sequence_id") != sequence_id:
            self._latch_shared_storage_failure()
            raise LoopbackApiError(
                "LOOPBACK_SHARED_STORAGE_MISMATCH",
                "Loopback hydration is not visible in the local network catalog",
                status=502,
            )
        if not self.server.shared_storage_failed:  # type: ignore[attr-defined]
            self.server.shared_storage_write_verified = True  # type: ignore[attr-defined]

    @staticmethod
    def _query(raw_query: str) -> dict[str, str]:
        try:
            values = parse_qs(raw_query, keep_blank_values=True, max_num_fields=20)
        except ValueError as exc:
            raise AppError("INVALID_QUERY", "Query string has too many fields") from exc
        duplicates = [key for key, value in values.items() if len(value) != 1]
        if duplicates:
            raise AppError("DUPLICATE_QUERY_PARAMETER", "Query parameters must not be repeated", details=duplicates)
        return {key: value[0] for key, value in values.items()}

    def _json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise AppError("UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json", status=415)
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise AppError("LENGTH_REQUIRED", "Content-Length is required", status=411)
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise AppError("INVALID_CONTENT_LENGTH", "Content-Length must be an integer") from exc
        if length < 0 or length > self.service.settings.max_body_bytes:
            raise AppError(
                "BODY_TOO_LARGE",
                f"JSON body exceeds {self.service.settings.max_body_bytes} bytes",
                status=413,
            )
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError("INVALID_JSON", "Request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise AppError("INVALID_JSON_OBJECT", "Request body must be a JSON object")
        return value

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        allowed = any(host == value or host.startswith(value + ":") for value in self.service.settings.allowed_hosts)
        if not allowed:
            self._json_response(403, {"ok": False, "error": {"code": "HOST_NOT_ALLOWED", "message": "Host is not allowed"}})
        return allowed

    def _request_origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if origin not in self.service.settings.allowed_origins and not self._origin_matches_host(origin):
            self._json_response(403, {"ok": False, "error": {"code": "ORIGIN_NOT_ALLOWED", "message": "Origin is not allowed"}})
            return False
        return True

    def _origin_matches_host(self, origin: str) -> bool:
        """Allow the web UI served by this HTTP listener on any configured port.

        Browsers attach an Origin header to same-origin JSON POSTs.  Comparing
        the serialized origin to the already-validated Host header keeps a
        configurable local port usable without widening cross-origin access.
        """
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return False
        request_host = self.headers.get("Host", "").strip().lower()
        return bool(request_host and parsed.netloc.lower() == request_host)

    def _client_is_loopback(self) -> bool:
        try:
            address = ipaddress.ip_address(str(self.client_address[0]).split("%", 1)[0])
        except ValueError:
            return False
        if address.is_loopback:
            return True
        return bool(address.version == 6 and address.ipv4_mapped and address.ipv4_mapped.is_loopback)

    def _require_operator(self) -> None:
        expected = self.service.settings.operator_token
        if not expected and self._client_is_loopback():
            return
        supplied = self.headers.get("X-Busro-Operator-Token")
        if supplied is None:
            authorization = self.headers.get("Authorization", "")
            scheme, separator, value = authorization.partition(" ")
            if separator and scheme.lower() == "bearer":
                supplied = value
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            raise AppError(
                "OPERATOR_AUTH_REQUIRED",
                "Operator authorization is required for this endpoint",
                status=403,
            )

    def _json_response(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if retry_after_seconds is not None:
            self.send_header("Retry-After", str(retry_after_seconds))
        self.end_headers()
        self._write_response(encoded)

    def _static_response(self, request_path: str) -> None:
        name = "index.html" if request_path in {"", "/"} else request_path.removeprefix("/")
        if name not in STATIC_FILES or "/" in name or "\\" in name:
            raise AppError("NOT_FOUND", "Web asset not found", status=404)
        path = WEB_ROOT / name
        try:
            size = path.stat().st_size
            if size > 2_000_000:
                raise AppError("STATIC_ASSET_TOO_LARGE", "Web asset is too large", status=500)
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise AppError("NOT_FOUND", "Web asset not found", status=404) from exc
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith((".js", ".jsx")):
            content_type = "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store" if name == "index.html" else "public, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://unpkg.com; "
            "font-src https://fonts.gstatic.com https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'",
        )
        self.end_headers()
        self._write_response(payload)

    def _write_response(self, payload: bytes) -> None:
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            # Browsers routinely cancel stale searches and navigations. Treat a
            # disconnected client as a completed request instead of printing a
            # per-thread traceback or retrying an already-finished upstream call.
            self.close_connection = True

    def _common_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin in self.service.settings.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def log_message(self, format: str, *args: Any) -> None:
        # Deliberately omit query strings from logs.
        print(f"{self.client_address[0]} - {self.command} {urlsplit(self.path).path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="버스로 잇다 local data service")
    parser.add_argument("--fixture", action="store_true", help="use clearly labelled local fixture data")
    parser.add_argument(
        "--service-key-stdin",
        action="store_true",
        help="read a decoded TAGO key without echo; never stores it in argv or files",
    )
    parser.add_argument(
        "--local-live-api",
        help="reuse one direct live TAGO service at a literal loopback HTTP origin",
    )
    parser.add_argument(
        "--shared-live-storage",
        action="store_true",
        help="assert that this process and --local-live-api share both SQLite stores",
    )
    parser.add_argument("--host", help="bind host; defaults to BUSRO_HOST or 127.0.0.1")
    parser.add_argument("--port", type=int, help="bind port; defaults to BUSRO_PORT or 8791")
    parser.add_argument("--db", type=Path, help="SQLite path; defaults to BUSRO_DB_PATH")
    parser.add_argument("--catalog-db", type=Path, help="dedicated nationwide catalog SQLite path")
    parser.add_argument("--import-stops", type=Path, help="import the official nationwide stop CSV before serving")
    parser.add_argument(
        "--quarantine-invalid-stops",
        action="store_true",
        help="exclude invalid stop rows without coordinate correction and record quality counts",
    )
    parser.add_argument("--import-routes", type=Path, help="import the official route CSV before serving")
    parser.add_argument("--import-only", action="store_true", help="exit after requested catalog imports")
    parser.add_argument("--stops-source-url", default="https://www.data.go.kr/data/15067528/fileData.do")
    parser.add_argument("--stops-source-date", default="2025-10-31")
    parser.add_argument("--routes-source-url", default="https://www.data.go.kr/tcs/dss/selectFileDataDetailView.do?publicDataPk=15105964")
    parser.add_argument("--routes-source-date", default="2026-07-16")
    args = parser.parse_args()

    if args.shared_live_storage and not args.local_live_api:
        parser.error("--shared-live-storage requires --local-live-api")
    if args.local_live_api and (args.fixture or args.service_key_stdin):
        parser.error("--local-live-api cannot be combined with --fixture or --service-key-stdin")

    settings = Settings.from_env(fixture_override=True if args.fixture else None)
    if args.service_key_stdin:
        if args.fixture:
            parser.error("--service-key-stdin cannot be combined with --fixture")
        service_key = getpass.getpass("TAGO decoded service key: ").strip()
        if not service_key:
            parser.error("a non-empty TAGO service key is required")
        settings = replace(settings, fixture_mode=False, tago_service_key=service_key)
    if args.host:
        settings = replace(settings, host=args.host)
    if args.port:
        if not 1 <= args.port <= 65_535:
            parser.error("--port must be between 1 and 65535")
        settings = replace(settings, port=args.port)
    if args.db:
        settings = replace(settings, db_path=args.db.expanduser().resolve())
    if args.catalog_db:
        settings = replace(settings, network_catalog_path=args.catalog_db.expanduser().resolve())

    if args.local_live_api and settings.tago_service_key:
        parser.error("--local-live-api cannot be combined with a local TAGO service key")

    service = BusroService(settings)
    live_api: LoopbackLiveApiClient | None = None
    shared_storage_baseline_consistent = False
    if args.local_live_api:
        try:
            live_api = LoopbackLiveApiClient(
                args.local_live_api,
                listener_port=settings.port,
                timeout_seconds=settings.tago_timeout_seconds,
                max_concurrency=settings.tago_max_concurrent_calls,
                admission_wait_seconds=settings.tago_admission_timeout_seconds,
            )
            upstream_status = live_api.probe_status()
            _require_direct_live_upstream(upstream_status)
        except LoopbackApiError as exc:
            parser.error(f"local live API is not ready ({exc.code})")
        if args.shared_live_storage:
            local_status = service.status()
            shared_storage_baseline_consistent = (
                upstream_status.get("storage") == local_status.get("storage")
                and (upstream_status.get("network_catalog") or {}).get("topology")
                == (local_status.get("network_catalog") or {}).get("topology")
            )
            if not shared_storage_baseline_consistent:
                parser.error(
                    "--shared-live-storage baseline differs between local and upstream stores"
                )
    imports: list[dict[str, Any]] = []
    if args.import_stops:
        imports.append(
            service.network_catalog.import_stops_csv(
                args.import_stops.expanduser().resolve(),
                source_url=args.stops_source_url,
                source_date=args.stops_source_date,
                quarantine_invalid_rows=args.quarantine_invalid_stops,
            )
        )
    if args.import_routes:
        imports.append(
            service.network_catalog.import_routes_csv(
                args.import_routes.expanduser().resolve(),
                source_url=args.routes_source_url,
                source_date=args.routes_source_date,
            )
        )
    if imports:
        print(json.dumps({"ok": True, "imports": imports}, ensure_ascii=False))
    if args.import_only:
        if not imports:
            parser.error("--import-only requires --import-stops or --import-routes")
        return
    server = BusroHTTPServer(
        (settings.host, settings.port),
        Handler,
        service=service,
        live_api=live_api,
        shared_live_storage=args.shared_live_storage,
        shared_storage_baseline_consistent=shared_storage_baseline_consistent,
    )
    mode = "fixture" if settings.fixture_mode else ("live via loopback" if live_api else "live")
    print(f"Busro Itda web ({mode}) listening on http://{settings.host}:{settings.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
