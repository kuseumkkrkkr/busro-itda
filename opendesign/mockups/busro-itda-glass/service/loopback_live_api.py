"""Strict HTTP client for reusing a live TAGO service on another loopback port.

The client intentionally has no generic proxy surface.  It accepts one literal
loopback origin, a fixed set of API paths, JSON objects, and (for mutations)
only an optional ``Idempotency-Key`` supplied explicitly by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import math
import re
import socket
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit


MAX_TOTAL_TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_TARGET_LENGTH = 4096
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_ADMISSION_WAIT_SECONDS = 0.25

GET_PATHS = frozenset(
    {
        "/api/arrivals",
        "/api/positions",
        "/api/cities",
        "/api/routes",
        "/api/routes/info",
        "/api/routes/stops",
        "/api/stops",
        "/api/stops/nearby",
        "/api/stops/routes",
    }
)
POST_PATHS = frozenset(
    {
        "/api/collect",
        "/api/positions/collect",
        "/api/mappings/validate",
        "/api/network/hydrate",
    }
)

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SENSITIVE_DETAIL_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "service_key",
    "token",
)


class LoopbackApiError(Exception):
    """Sanitized error with the same public contract as ``app.AppError``."""

    def __init__(self, code: str, message: str, *, status: int = 502, details: Any = None):
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


@dataclass(frozen=True)
class LoopbackOrigin:
    host: str
    port: int

    @property
    def serialized(self) -> str:
        authority = f"[{self.host}]" if self.host == "::1" else self.host
        return f"http://{authority}:{self.port}"


def validate_loopback_origin(value: str, *, listener_port: int) -> LoopbackOrigin:
    """Validate an origin without DNS, credentials, or URL suffixes."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise LoopbackApiError(
            "INVALID_LOOPBACK_ORIGIN",
            "Loopback live API origin must be a literal HTTP origin",
            status=400,
        )
    try:
        local_port = int(listener_port)
    except (TypeError, ValueError) as exc:
        raise LoopbackApiError(
            "INVALID_LISTENER_PORT", "Listener port must be between 1 and 65535", status=400
        ) from exc
    if not 1 <= local_port <= 65535:
        raise LoopbackApiError(
            "INVALID_LISTENER_PORT", "Listener port must be between 1 and 65535", status=400
        )

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise LoopbackApiError(
            "INVALID_LOOPBACK_ORIGIN",
            "Loopback live API origin has an invalid port",
            status=400,
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or port is None
        or not 1 <= port <= 65535
    ):
        raise LoopbackApiError(
            "INVALID_LOOPBACK_ORIGIN",
            "Loopback live API origin must be literal 127.0.0.1 or ::1 HTTP with an explicit port",
            status=400,
        )
    expected_authority = f"127.0.0.1:{port}" if parsed.hostname == "127.0.0.1" else f"[::1]:{port}"
    if parsed.netloc != expected_authority:
        raise LoopbackApiError(
            "INVALID_LOOPBACK_ORIGIN",
            "Loopback live API origin is not in canonical literal form",
            status=400,
        )
    if port == local_port:
        raise LoopbackApiError(
            "LOOPBACK_PROXY_LOOP",
            "Loopback live API must use a different listener port",
            status=400,
        )
    return LoopbackOrigin(parsed.hostname, port)


def _positive_timeout(value: float) -> float:
    try:
        requested = float(value)
    except (TypeError, ValueError) as exc:
        raise LoopbackApiError(
            "INVALID_LOOPBACK_TIMEOUT", "Loopback API timeout must be positive", status=400
        ) from exc
    if not math.isfinite(requested) or requested <= 0:
        raise LoopbackApiError(
            "INVALID_LOOPBACK_TIMEOUT", "Loopback API timeout must be positive", status=400
        )
    return min(requested, MAX_TOTAL_TIMEOUT_SECONDS)


def _clean_message(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    printable = "".join(char if char.isprintable() else " " for char in value)
    clean = " ".join(printable.split())[:512]
    return clean or fallback


def _safe_details(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _clean_message(value, "")[:256]
    if isinstance(value, list):
        return [_safe_details(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:20]:
            if not isinstance(key, str):
                continue
            clean_key = _clean_message(key, "")[:64]
            lowered = clean_key.lower()
            if not clean_key or any(marker in lowered for marker in _SENSITIVE_DETAIL_MARKERS):
                continue
            result[clean_key] = _safe_details(item, depth=depth + 1)
        return result
    return None


def _upstream_error(status: int, payload: Any) -> LoopbackApiError:
    error = payload.get("error") if isinstance(payload, dict) else None
    raw_code = error.get("code") if isinstance(error, dict) else None
    code = (
        raw_code
        if isinstance(raw_code, str) and _ERROR_CODE_RE.fullmatch(raw_code)
        else "LOOPBACK_UPSTREAM_HTTP_ERROR"
    )
    message = _clean_message(
        error.get("message") if isinstance(error, dict) else None,
        "Loopback live API request failed",
    )
    details = _safe_details(error.get("details")) if isinstance(error, dict) and "details" in error else None
    public_status = status if 400 <= status <= 599 else 502
    return LoopbackApiError(code, message, status=public_status, details=details)


class LoopbackLiveApiClient:
    """Bounded, no-redirect client for the fixed live TAGO API surface."""

    def __init__(
        self,
        origin: str,
        *,
        listener_port: int,
        timeout_seconds: float = MAX_TOTAL_TIMEOUT_SECONDS,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        admission_wait_seconds: float = DEFAULT_ADMISSION_WAIT_SECONDS,
        connection_factory: Callable[..., Any] = http.client.HTTPConnection,
    ) -> None:
        self.endpoint = validate_loopback_origin(origin, listener_port=listener_port)
        self.timeout_seconds = _positive_timeout(timeout_seconds)
        try:
            concurrency = int(max_concurrency)
            admission_wait = float(admission_wait_seconds)
        except (TypeError, ValueError) as exc:
            raise LoopbackApiError(
                "INVALID_LOOPBACK_CAPACITY", "Loopback API capacity is invalid", status=400
            ) from exc
        if not 1 <= concurrency <= 32 or not math.isfinite(admission_wait) or admission_wait < 0:
            raise LoopbackApiError(
                "INVALID_LOOPBACK_CAPACITY", "Loopback API capacity is invalid", status=400
            )
        self.max_concurrency = concurrency
        self.admission_wait_seconds = min(admission_wait, 1.0)
        self._connection_factory = connection_factory
        self._slots = threading.BoundedSemaphore(concurrency)

    @property
    def origin(self) -> str:
        return self.endpoint.serialized

    def get(self, target: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        return self.request("GET", target, timeout_seconds=timeout_seconds)

    def probe_status(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Read only the fixed control status used to reject proxy chaining."""
        return self.request(
            "GET",
            "/api/status",
            timeout_seconds=timeout_seconds,
            _allow_status_probe=True,
        )

    def post(
        self,
        target: str,
        body: dict[str, Any],
        *,
        allow_mutation: bool,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            target,
            body=body,
            allow_mutation=allow_mutation,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
        )

    def request(
        self,
        method: str,
        target: str,
        *,
        body: dict[str, Any] | None = None,
        allow_mutation: bool = False,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
        _allow_status_probe: bool = False,
    ) -> dict[str, Any]:
        clean_method = str(method).upper()
        path = self._validate_target(
            clean_method,
            target,
            allow_status_probe=_allow_status_probe,
        )
        encoded_body: bytes | None = None
        headers = {"Accept": "application/json", "User-Agent": "busro-loopback-live/1"}

        if clean_method == "GET":
            if body is not None or idempotency_key is not None or allow_mutation:
                raise LoopbackApiError(
                    "INVALID_LOOPBACK_REQUEST", "GET does not accept mutation options", status=400
                )
        elif clean_method == "POST":
            if not allow_mutation:
                raise LoopbackApiError(
                    "LOOPBACK_MUTATION_DISABLED",
                    "Loopback live API mutations require explicit caller approval",
                    status=403,
                )
            if not isinstance(body, dict):
                raise LoopbackApiError(
                    "INVALID_JSON_OBJECT", "Loopback API POST body must be a JSON object", status=400
                )
            try:
                encoded_body = json.dumps(
                    body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError) as exc:
                raise LoopbackApiError(
                    "INVALID_JSON_OBJECT", "Loopback API POST body must be JSON serializable", status=400
                ) from exc
            if len(encoded_body) > MAX_REQUEST_BYTES:
                raise LoopbackApiError(
                    "BODY_TOO_LARGE",
                    f"Loopback API JSON body exceeds {MAX_REQUEST_BYTES} bytes",
                    status=413,
                )
            headers["Content-Type"] = "application/json"
            if idempotency_key is not None:
                key = str(idempotency_key)
                if not _IDEMPOTENCY_RE.fullmatch(key):
                    raise LoopbackApiError(
                        "INVALID_IDEMPOTENCY_KEY",
                        "Idempotency-Key must be 8-128 safe ASCII characters",
                        status=400,
                    )
                headers["Idempotency-Key"] = key

        timeout = self.timeout_seconds if timeout_seconds is None else _positive_timeout(timeout_seconds)
        timeout = min(timeout, self.timeout_seconds, MAX_TOTAL_TIMEOUT_SECONDS)
        deadline = time.monotonic() + timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._deadline_error()
        if not self._slots.acquire(timeout=min(self.admission_wait_seconds, remaining)):
            if time.monotonic() >= deadline:
                raise self._deadline_error()
            raise LoopbackApiError(
                "LOOPBACK_API_BUSY",
                "Loopback live API capacity is busy; retry shortly",
                status=429,
                details={"retry_after_seconds": 1},
            )

        finished = threading.Event()
        outcome: dict[str, Any] = {}

        def run() -> None:
            try:
                outcome["payload"] = self._perform(
                    clean_method, path, encoded_body, headers, deadline=deadline
                )
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                self._slots.release()
                finished.set()

        try:
            worker = threading.Thread(target=run, name="busro-loopback-api", daemon=True)
            worker.start()
        except Exception as exc:
            self._slots.release()
            raise LoopbackApiError(
                "LOOPBACK_API_UNAVAILABLE", "Loopback live API is unavailable", status=503
            ) from exc

        remaining = deadline - time.monotonic()
        if remaining <= 0 or not finished.wait(timeout=remaining):
            raise self._deadline_error()
        error = outcome.get("error")
        if error is not None:
            if isinstance(error, LoopbackApiError):
                raise error
            raise LoopbackApiError(
                "LOOPBACK_API_UNAVAILABLE", "Loopback live API is unavailable", status=503
            ) from error
        payload = outcome.get("payload")
        if not isinstance(payload, dict):
            raise LoopbackApiError(
                "INVALID_LOOPBACK_RESPONSE", "Loopback live API returned a non-object", status=502
            )
        return payload

    @staticmethod
    def _validate_target(
        method: str,
        target: str,
        *,
        allow_status_probe: bool = False,
    ) -> str:
        if (
            not isinstance(target, str)
            or not target
            or len(target) > MAX_TARGET_LENGTH
            or target != target.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in target)
        ):
            raise LoopbackApiError(
                "LOOPBACK_API_PATH_NOT_ALLOWED", "Loopback live API path is not allowed", status=404
            )
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment or "#" in target:
            raise LoopbackApiError(
                "LOOPBACK_API_PATH_NOT_ALLOWED", "Loopback live API path is not allowed", status=404
            )
        allowed = GET_PATHS if method == "GET" else POST_PATHS if method == "POST" else frozenset()
        if allow_status_probe and method == "GET":
            allowed = frozenset({"/api/status"})
        if parsed.path not in allowed or (method == "POST" and (parsed.query or "?" in target)):
            raise LoopbackApiError(
                "LOOPBACK_API_PATH_NOT_ALLOWED", "Loopback live API path is not allowed", status=404
            )
        return target

    @staticmethod
    def _deadline_error() -> LoopbackApiError:
        return LoopbackApiError(
            "LOOPBACK_API_DEADLINE_EXCEEDED",
            "Loopback live API exceeded its total time limit",
            status=504,
        )

    def _perform(
        self,
        method: str,
        target: str,
        body: bytes | None,
        headers: dict[str, str],
        *,
        deadline: float,
    ) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._deadline_error()
        connection = self._connection_factory(
            self.endpoint.host, self.endpoint.port, timeout=remaining
        )
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = int(response.status)
        except (TimeoutError, socket.timeout) as exc:
            raise self._deadline_error() from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise LoopbackApiError(
                "LOOPBACK_API_UNAVAILABLE", "Loopback live API is unavailable", status=503
            ) from exc
        finally:
            try:
                connection.close()
            except Exception:
                pass

        if len(raw) > MAX_RESPONSE_BYTES:
            raise LoopbackApiError(
                "LOOPBACK_RESPONSE_TOO_LARGE",
                "Loopback live API response exceeds 2 MiB",
                status=502,
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise LoopbackApiError(
                "INVALID_LOOPBACK_RESPONSE", "Loopback live API returned invalid JSON", status=502
            ) from exc
        if not isinstance(payload, dict):
            raise LoopbackApiError(
                "INVALID_LOOPBACK_RESPONSE", "Loopback live API returned a non-object", status=502
            )
        if not 200 <= status <= 299 or payload.get("ok") is False:
            raise _upstream_error(status, payload)
        return payload
