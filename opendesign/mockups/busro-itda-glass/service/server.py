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
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app import AppError, BusroService
from config import Settings


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


class BusroHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256

    def __init__(
        self,
        address: tuple[str, int],
        handler,
        *,
        service: BusroService,
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
                self._json_response(200, self.service.status())
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
                result, status = self.service.collect(
                    body, header_idempotency_key=self.headers.get("Idempotency-Key")
                )
                self._json_response(status, result)
            elif method == "POST" and parsed.path == "/api/simulate":
                self._json_response(200, self.service.simulate(self._json_body()))
            elif method == "POST" and parsed.path == "/api/positions/collect":
                body = self._json_body()
                result, status = self.service.collect_positions(
                    body, header_idempotency_key=self.headers.get("Idempotency-Key")
                )
                self._json_response(status, result)
            elif method == "POST" and parsed.path == "/api/replay":
                self._json_response(200, self.service.replay(self._json_body()))
            elif method == "POST" and parsed.path == "/api/mappings/validate":
                self._json_response(200, self.service.validate_mapping(self._json_body()))
            elif method == "POST" and parsed.path == "/api/network/hydrate":
                self._json_response(200, self.service.hydrate_network_route(self._json_body()))
            elif method == "POST" and parsed.path == "/api/journeys/generate":
                self._json_response(200, self.service.generate_journeys(self._json_body()))
            elif method == "POST" and parsed.path == "/api/osm/geometry":
                self._json_response(200, self.service.route_geometry(self._json_body()))
            else:
                raise AppError("NOT_FOUND", "API endpoint not found", status=404)
        except AppError as exc:
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

    service = BusroService(settings)
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
    server = BusroHTTPServer((settings.host, settings.port), Handler, service=service)
    mode = "fixture" if settings.fixture_mode else "live"
    print(f"Busro Itda web ({mode}) listening on http://{settings.host}:{settings.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
