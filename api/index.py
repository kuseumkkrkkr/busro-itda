"""Vercel Python function entrypoint."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "opendesign" / "mockups" / "busro-itda-glass" / "service"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from busro_vercel_runtime import RuntimeResponse, dispatch_request  # noqa: E402


HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")
MAX_BODY_BYTES = 65_536
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RESPONSE_TOO_LARGE_PAYLOAD = {
    "ok": False,
    "error": {
        "code": "RESPONSE_TOO_LARGE",
        "message": "Response exceeds the allowed size",
    },
}


class handler(BaseHTTPRequestHandler):
    server_version = "BusroItdaVercel/0.1"

    def do_OPTIONS(self) -> None:
        if not self._origin_allowed():
            self._write_json(403, {"ok": False, "error": {"code": "ORIGIN_DENIED", "message": "Origin is not allowed"}})
            return
        self.send_response(204)
        self._common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._origin_allowed():
            self._write_json(403, {"ok": False, "error": {"code": "ORIGIN_DENIED", "message": "Origin is not allowed"}})
            return
        try:
            path, query = self._request_target()
            body = self._json_body() if method == "POST" else None
            response = dispatch_request(method, path, query, body)
        except ValueError as exc:
            response = RuntimeResponse(400, {"ok": False, "error": {"code": "INVALID_REQUEST", "message": str(exc)}})
        self._write_json(
            response.status,
            response.payload,
            cache_control=response.cache_control,
            retry_after_seconds=response.retry_after_seconds,
        )

    def _request_target(self) -> tuple[str, dict[str, str]]:
        parsed = urlsplit(self.path)
        values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32)
        captured = values.pop("path", [""])
        if len(captured) != 1 or not captured[0]:
            raise ValueError("API path is required")
        path = f"/api/{captured[0].lstrip('/')}"
        query: dict[str, str] = {}
        for key, items in values.items():
            if len(items) != 1 or len(key) > 64 or len(items[0]) > 1024:
                raise ValueError("Query parameters are invalid")
            query[key] = items[0]
        return path, query

    def _json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length is invalid") from exc
        if not 0 < length <= MAX_BODY_BYTES:
            raise ValueError("JSON body is required and must be at most 65536 bytes")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON body is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _origin_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        if not HOST_RE.fullmatch(host):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        scheme = "http" if host.startswith(("127.0.0.1", "localhost")) else "https"
        return origin == f"{scheme}://{host}"

    def _common_headers(self) -> None:
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _write_json(
        self,
        status: int,
        payload: dict,
        *,
        cache_control: str = "private, no-store",
        retry_after_seconds: int | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            status = 413
            cache_control = "private, no-store"
            retry_after_seconds = None
            body = json.dumps(
                RESPONSE_TOO_LARGE_PAYLOAD,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Cache-Control", cache_control)
        if retry_after_seconds is not None:
            self.send_header("Retry-After", str(retry_after_seconds))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
