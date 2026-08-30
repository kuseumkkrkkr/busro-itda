"""Explicit snapshot worker; never starts implicitly and therefore cannot spend quota unnoticed."""

from __future__ import annotations

import argparse
import signal
import time

from app import AppError, BusroService
from config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect TAGO arrival or vehicle-position snapshots")
    parser.add_argument("--city-code", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--node-id", help="collect arrival ETA for one stop")
    target.add_argument("--route-id", help="collect vehicle positions and reconstruct passages")
    parser.add_argument("--interval", type=int, default=300, help="seconds between snapshots (minimum 30)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("--interval must be at least 30 seconds")

    settings = Settings.from_env(fixture_override=True if args.fixture else None)
    service = BusroService(settings)
    stopping = False

    def stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    while not stopping:
        try:
            if args.route_id:
                result, status = service.collect_positions(
                    {"city_code": args.city_code, "route_id": args.route_id}
                )
            else:
                result, status = service.collect(
                    {"city_code": args.city_code, "node_id": args.node_id}
                )
            snapshot = result["snapshot"]
            passage_count = len(result.get("passages", []))
            print(
                f"{status} {snapshot['captured_at']} {snapshot['snapshot_id']} "
                f"created={result['created']} passages={passage_count}"
            )
        except AppError as exc:
            print(f"ERROR {exc.status} {exc.code}: {exc.message}")
            if exc.status in {401, 403, 503}:
                raise SystemExit(2) from exc
        if args.once:
            break
        deadline = time.monotonic() + args.interval
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))


if __name__ == "__main__":
    main()
