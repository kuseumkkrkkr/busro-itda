"""Bounded multi-target collector using only the local Busro HTTP API."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import signal
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_TARGETS = 10_000
MAX_FILE_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576


class CollectorError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class Target:
    kind: str
    city_code: str
    target_id: str

    @property
    def endpoint(self) -> str:
        return "/api/collect" if self.kind == "arrival" else "/api/positions/collect"

    @property
    def body(self) -> dict[str, str]:
        field = "node_id" if self.kind == "arrival" else "route_id"
        return {"city_code": self.city_code, field: self.target_id}

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.city_code}:{self.target_id}"


def _text(value: Any, field: str, maximum: int) -> str:
    text = str(value).strip() if isinstance(value, (str, int)) else ""
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise CollectorError("INVALID_TARGETS", f"invalid {field}")
    return text


def load_targets(path: Path) -> tuple[Target, ...]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise CollectorError("INVALID_TARGETS", "target file exceeds 1 MiB")
        document = json.loads(path.read_text(encoding="utf-8"))
    except CollectorError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CollectorError("INVALID_TARGETS", "cannot read target JSON") from exc
    if not isinstance(document, dict) or set(document) - {"arrivals", "positions"}:
        raise CollectorError("INVALID_TARGETS", "only arrivals and positions arrays are allowed")
    arrivals, positions = document.get("arrivals", []), document.get("positions", [])
    if not isinstance(arrivals, list) or not isinstance(positions, list):
        raise CollectorError("INVALID_TARGETS", "arrivals and positions must be arrays")
    if not 1 <= len(arrivals) + len(positions) <= MAX_TARGETS:
        raise CollectorError("INVALID_TARGETS", f"target count must be 1-{MAX_TARGETS}")

    targets: list[Target] = []
    for kind, values, id_field in (
        ("arrival", arrivals, "node_id"),
        ("position", positions, "route_id"),
    ):
        for index, value in enumerate(values):
            expected = {"city_code", id_field}
            if not isinstance(value, dict) or set(value) != expected:
                raise CollectorError("INVALID_TARGETS", f"invalid {kind} target at index {index}")
            targets.append(
                Target(
                    kind,
                    _text(value["city_code"], "city_code", 16),
                    _text(value[id_field], id_field, 128),
                )
            )
    return tuple(dict.fromkeys(targets))


def local_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise CollectorError("INVALID_BASE_URL", "base URL must be a loopback HTTP origin")
    try:
        parsed.port
    except ValueError as exc:
        raise CollectorError("INVALID_BASE_URL", "invalid port") from exc
    return value.rstrip("/")


def make_idempotency_key(target: Target, bucket: int, interval: int) -> str:
    raw = f"{target.label}|{bucket}|{interval}".encode("utf-8")
    return f"multi-v1-{target.kind}-{bucket}-{hashlib.sha256(raw).hexdigest()[:24]}"


class LocalApi:
    def __init__(self, base_url: str, timeout: float = 10, opener: Callable[..., Any] = urlopen):
        self.base_url, self.timeout, self.opener = local_origin(base_url), timeout, opener

    def collect(self, target: Target, key: str) -> dict[str, Any]:
        request = Request(
            self.base_url + target.endpoint,
            data=json.dumps(target.body, separators=(",", ":")).encode(),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Idempotency-Key": key,
                "User-Agent": "busro-multi-collector/1",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            try:
                error = json.loads(raw.decode()).get("error", {})
                code, message = error.get("code"), error.get("message")
            except (UnicodeError, json.JSONDecodeError, AttributeError):
                code, message = None, None
            raise CollectorError(
                str(code or f"HTTP_{exc.code}"), str(message or "local API request failed"), exc.code
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CollectorError("LOCAL_API_UNAVAILABLE", "local API unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CollectorError("RESPONSE_TOO_LARGE", "local API response exceeds 1 MiB")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CollectorError("INVALID_RESPONSE", "local API returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise CollectorError("INVALID_RESPONSE", "local API returned a non-object")
        return result


class RateLimiter:
    def __init__(self, rate: float, clock=time.monotonic, sleep=time.sleep):
        self.period, self.clock, self.sleep, self.next_at = 1 / rate, clock, sleep, 0.0

    def wait(self) -> None:
        delay = self.next_at - self.clock()
        if delay > 0:
            self.sleep(delay)
        self.next_at = max(self.next_at, self.clock()) + self.period


def collect_once(
    targets: tuple[Target, ...],
    api: LocalApi,
    *,
    budget: int,
    used: int,
    interval: int,
    bucket: int,
    limiter: RateLimiter,
    stopping: Callable[[], bool] = lambda: False,
) -> tuple[list[dict[str, Any]], int]:
    outcomes = []
    for target in targets:
        if stopping() or used >= budget:
            break
        limiter.wait()
        if stopping():
            break
        used += 1  # Every attempt, including an error, spends budget.
        try:
            result = api.collect(target, make_idempotency_key(target, bucket, interval))
            snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
            outcomes.append(
                {
                    "ok": True,
                    "target": target.label,
                    "created": bool(result.get("created")),
                    "snapshot_id": snapshot.get("snapshot_id"),
                }
            )
        except CollectorError as exc:
            outcomes.append(
                {"ok": False, "target": target.label, "error": exc.code, "status": exc.status}
            )
    return outcomes, used


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded multi-target local API collector")
    parser.add_argument("--targets-file", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--request-budget", type=int, default=1000)
    parser.add_argument("--requests-per-second", type=float, default=2)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    if args.interval < 30:
        parser.error("--interval must be at least 30")
    if not 1 <= args.request_budget <= 100_000:
        parser.error("--request-budget must be 1-100000")
    if not 0.1 <= args.requests_per_second <= 20:
        parser.error("--requests-per-second must be 0.1-20")
    if not 1 <= args.timeout <= 30:
        parser.error("--timeout must be 1-30")
    try:
        targets = load_targets(args.targets_file.resolve())
        api = LocalApi(args.base_url, args.timeout)
    except CollectorError as exc:
        parser.error(exc.message)

    interrupted = False

    def stop(_signum, _frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    limiter, used, failures, cycles = RateLimiter(args.requests_per_second), 0, 0, 0
    while not interrupted and used < args.request_budget:
        started = time.monotonic()
        outcomes, used = collect_once(
            targets,
            api,
            budget=args.request_budget,
            used=used,
            interval=args.interval,
            bucket=int(time.time()) // args.interval,
            limiter=limiter,
            stopping=lambda: interrupted,
        )
        cycles += 1
        for outcome in outcomes:
            failures += int(not outcome["ok"])
            _emit(outcome)
        _emit(
            {
                "event": "cycle_complete",
                "cycle": cycles,
                "attempted": len(outcomes),
                "budget_used": used,
                "budget_limit": args.request_budget,
            }
        )
        if args.once or interrupted or used >= args.request_budget:
            break
        deadline = started + args.interval
        while not interrupted and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))
    _emit(
        {
            "event": "collector_complete",
            "cycles": cycles,
            "failures": failures,
            "budget_used": used,
            "budget_limit": args.request_budget,
            "interrupted": interrupted,
        }
    )
    return 130 if interrupted else (1 if failures else 0)


if __name__ == "__main__":
    raise SystemExit(main())
