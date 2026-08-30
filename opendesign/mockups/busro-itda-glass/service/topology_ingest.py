"""Resumable nationwide TAGO route-topology ingestion.

This is an explicit operator command, not a web-request side effect or hidden
background worker.  It discovers TAGO-native city/route identifiers, stages
bounded route-stop pages, and activates a sequence only after the complete
ordered list validates.  No URL, query string, or service key is logged.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping
import uuid

from network_catalog import CatalogError, CatalogLimitError, CatalogValidationError, NetworkCatalog
from tago import TagoError, fetch_catalog, normalize_catalog


PROVIDER = "TAGO"
FATAL_ACCESS_CODES = frozenset(
    {
        "30",
        "TAGO_KEY_REQUIRED",
        "TAGO_KEY_INVALID",
        "SERVICE_ACCESS_DENIED_ERROR",
        "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
    }
)


class RequestBudgetExhausted(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error_message(value: Any) -> str:
    text = " ".join(str(value or "Upstream request failed").split())
    return text[:240] or "Upstream request failed"


def _public_tago_message(code: str) -> str:
    """Return fixed text so an upstream body can never persist a key/query."""
    if code in FATAL_ACCESS_CODES:
        return "TAGO route/station API authorization is unavailable"
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

    def validate(self) -> None:
        if not 1 <= self.request_budget <= 100_000:
            raise ValueError("request_budget must be 1..100000")
        if not 0 <= self.requests_per_second <= 20:
            raise ValueError("requests_per_second must be 0..20")
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
        self.catalog = catalog
        self.fetcher = fetcher
        self.config = config
        self.clock = clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.run_id = "ing_" + uuid.uuid4().hex[:24]
        self.requests_used = 0
        self.discovery_failures = 0
        self._last_request_started: float | None = None

    def _request(
        self,
        operation: str,
        parameters: dict[str, str],
        *,
        target: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        if self.requests_used >= self.config.request_budget:
            raise RequestBudgetExhausted("request budget exhausted")
        if self.config.requests_per_second > 0 and self._last_request_started is not None:
            interval = 1.0 / self.config.requests_per_second
            remaining = interval - (self.monotonic() - self._last_request_started)
            if remaining > 0:
                self.sleeper(remaining)
        self._last_request_started = self.monotonic()
        try:
            return self.fetcher(operation, parameters)
        finally:
            # Attempted upstream calls consume quota even when they fail.
            self.requests_used += 1
            self.catalog.update_topology_run(self.run_id, requests_used=1)
            if target is not None:
                self.catalog.record_topology_target_request(
                    provider=PROVIDER, city_code=target[0], route_id=target[1]
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
                self.catalog.update_topology_discovery(
                    provider=PROVIDER,
                    scope_key="cities",
                    status="FAILED",
                    next_page=1,
                    total_count=None,
                    request_increment=1,
                    error_code=exc.code,
                    error_message=_public_tago_message(exc.code),
                )
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
                    self.catalog.update_topology_discovery(
                        provider=PROVIDER,
                        scope_key=scope,
                        status="FAILED",
                        next_page=page,
                        total_count=progress.get("total_count"),
                        request_increment=1,
                        error_code=exc.code,
                        error_message=_public_tago_message(exc.code),
                    )
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

    def run(self) -> dict[str, Any]:
        self.catalog.create_topology_run(
            run_id=self.run_id,
            provider=PROVIDER,
            target_source=self.config.target_source,
            request_budget=self.config.request_budget,
            target_limit=self.config.target_limit,
        )
        final_status = "COMPLETE"
        processed = 0
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
            if self.config.refresh_complete:
                self.catalog.queue_topology_refresh(provider=PROVIDER)
            while self.config.target_limit is None or processed < self.config.target_limit:
                target = self.catalog.claim_topology_target(
                    provider=PROVIDER, run_id=self.run_id
                )
                if target is None:
                    break
                processed += 1
                try:
                    outcome = self._ingest_target(target)
                    counters = {
                        "targets_processed": 1,
                        "unchanged" if outcome == "UNCHANGED" else "succeeded": 1,
                    }
                    self.catalog.update_topology_run(self.run_id, **counters)
                except RequestBudgetExhausted:
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
                    final_status = "BUDGET_EXHAUSTED"
                    break
                except TagoError as exc:
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
                    if exc.code in FATAL_ACCESS_CODES:
                        final_status = "DATA_GAP"
                        break
                    final_status = "PARTIAL"
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
                    final_status = "PARTIAL"
                except Exception:
                    # Unknown exception text may contain implementation or
                    # transport details, so persist only a fixed safe marker.
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
                    final_status = "FAILED"
                    break
        except RequestBudgetExhausted:
            final_status = "BUDGET_EXHAUSTED"
        except TagoError as exc:
            final_status = "DATA_GAP" if exc.code in FATAL_ACCESS_CODES else "FAILED"
        except CatalogError:
            final_status = "FAILED"
        except Exception:
            final_status = "FAILED"
        coverage = self.catalog.topology_coverage(provider=PROVIDER)
        if final_status == "COMPLETE" and coverage["complete"] < coverage["targets"]:
            final_status = "PARTIAL"
        run = self.catalog.finish_topology_run(self.run_id, final_status)
        return {
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover and ingest nationwide TAGO route-stop topology"
    )
    parser.add_argument("--catalog-db", type=Path, required=True)
    parser.add_argument("--service-key-stdin", action="store_true", required=True)
    parser.add_argument("--request-budget", type=int, default=9_000)
    parser.add_argument("--requests-per-second", type=float, default=2.0)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-route-pages", type=int, default=10)
    parser.add_argument("--max-discovery-pages", type=int, default=200)
    parser.add_argument("--target-limit", type=int)
    parser.add_argument("--target-source", choices=("tago", "catalog"), default="tago")
    parser.add_argument("--trust-catalog-identifiers", action="store_true")
    parser.add_argument(
        "--refresh-complete",
        action="store_true",
        help="re-fetch completed routes and store only changed sequence hashes",
    )
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 0.5 <= args.timeout_seconds <= 30:
        raise SystemExit("--timeout-seconds must be 0.5..30")
    service_key = getpass.getpass("TAGO decoded service key: ")
    if not service_key:
        raise SystemExit("TAGO service key is required")
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
    )
    catalog = NetworkCatalog(args.catalog_db)

    def live_fetch(operation: str, parameters: dict[str, str]) -> dict[str, Any]:
        return fetch_catalog(
            operation=operation,
            parameters=parameters,
            service_key=service_key,
            timeout_seconds=args.timeout_seconds,
            fixture_mode=False,
            fixture_path=Path("unused"),
        )

    result = TopologyIngestor(
        catalog=catalog, fetcher=live_fetch, config=config
    ).run()
    # The summary contains counters/status only; no key, URL, or query values.
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
