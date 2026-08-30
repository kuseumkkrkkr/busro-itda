from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4


def local_api_base(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise argparse.ArgumentTypeError("base URL must be a local HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("base URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/api"}:
        raise argparse.ArgumentTypeError("base URL path must be /api")
    return value.rstrip("/") if parsed.path == "/api" else value.rstrip("/") + "/api"


def request_json(
    base: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(base + path, data=payload, method=method, headers=headers)
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=12) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {"ok": False, "error": {"code": "HTTP_ERROR", "message": raw[:200]}}
        return exc.code, detail
    except URLError as exc:
        raise RuntimeError(f"local API is unavailable: {exc.reason}") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(base: str, *, city_code: str, node_id: str, route_id: str) -> dict[str, Any]:
    status_code, before = request_json(base, "/status")
    require(status_code == 200 and before.get("ok") is True, "status endpoint is not ready")
    require(before.get("mode") == "live", "server is not in live mode")
    tago = before.get("tago") or {}
    require(tago.get("configured") is True, "TAGO key is not configured on the server")
    require(tago.get("key_exposed") is False, "server status exposed key material")
    before_storage = before.get("storage") or {}

    arrival_key = "live-arrival-" + uuid4().hex
    first_code, first = request_json(
        base,
        "/collect",
        method="POST",
        body={"city_code": city_code, "node_id": node_id},
        idempotency_key=arrival_key,
    )
    second_code, second = request_json(
        base,
        "/collect",
        method="POST",
        body={"city_code": city_code, "node_id": node_id},
        idempotency_key=arrival_key,
    )
    require(first_code == 201 and first.get("created") is True, "arrival snapshot was not created")
    require(second_code == 200 and second.get("created") is False, "arrival idempotency failed")
    require(
        (first.get("snapshot") or {}).get("snapshot_id")
        == (second.get("snapshot") or {}).get("snapshot_id"),
        "arrival retry returned a different snapshot",
    )

    position_key = "live-position-" + uuid4().hex
    first_position_code, first_position = request_json(
        base,
        "/positions/collect",
        method="POST",
        body={"city_code": city_code, "route_id": route_id},
        idempotency_key=position_key,
    )
    second_position_code, second_position = request_json(
        base,
        "/positions/collect",
        method="POST",
        body={"city_code": city_code, "route_id": route_id},
        idempotency_key=position_key,
    )
    require(
        first_position_code == 201 and first_position.get("created") is True,
        "position snapshot was not created",
    )
    require(
        second_position_code == 200 and second_position.get("created") is False,
        "position idempotency failed",
    )
    require(
        (first_position.get("snapshot") or {}).get("snapshot_id")
        == (second_position.get("snapshot") or {}).get("snapshot_id"),
        "position retry returned a different snapshot",
    )

    after_code, after = request_json(base, "/status")
    require(after_code == 200 and after.get("ok") is True, "final status endpoint failed")
    require((after.get("tago") or {}).get("key_exposed") is False, "final status exposed key material")
    after_storage = after.get("storage") or {}
    require(
        int(after_storage.get("snapshots", -1)) == int(before_storage.get("snapshots", -2)) + 1,
        "arrival snapshot count did not increase exactly once",
    )
    require(
        int(after_storage.get("position_snapshots", -1))
        == int(before_storage.get("position_snapshots", -2)) + 1,
        "position snapshot count did not increase exactly once",
    )

    arrival_snapshot = first["snapshot"]
    position_snapshot = first_position["snapshot"]
    return {
        "ok": True,
        "mode": "live",
        "key_exposed": False,
        "arrival": {
            "created_status": first_code,
            "retry_status": second_code,
            "snapshot_id": arrival_snapshot["snapshot_id"],
            "source": arrival_snapshot["source"],
            "captured_at": arrival_snapshot["captured_at"],
        },
        "position": {
            "created_status": first_position_code,
            "retry_status": second_position_code,
            "snapshot_id": position_snapshot["snapshot_id"],
            "source": position_snapshot["source"],
            "captured_at": position_snapshot["captured_at"],
            "passage_events": len(first_position.get("passages") or []),
        },
        "storage": {
            "snapshots_before": before_storage.get("snapshots"),
            "snapshots_after": after_storage.get("snapshots"),
            "position_snapshots_before": before_storage.get("position_snapshots"),
            "position_snapshots_after": after_storage.get("position_snapshots"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify live TAGO persistence and idempotency without reading or printing the service key."
    )
    parser.add_argument("--base", type=local_api_base, default="http://127.0.0.1:8791/api")
    parser.add_argument("--city-code", default="25")
    parser.add_argument("--node-id", default="DJB8001793")
    parser.add_argument("--route-id", default="DJB30300052")
    args = parser.parse_args()
    try:
        result = run(
            args.base,
            city_code=args.city_code,
            node_id=args.node_id,
            route_id=args.route_id,
        )
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
