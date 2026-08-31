"""Read-only benchmark for a transfer-layer SQLite journey search.

This file is intentionally independent from the production integration.  It
uses ``SQLiteJourneyPlanner``'s indexed, read-only route/transfer loaders, but
changes the low-transfer search order: a whole transfer layer is completed and
Pareto-pruned before the next layer is expanded.  Therefore the first layer
that reaches the destination proves the minimum transfer count without
expanding any destination-layer states.

The default case is the verified structural chain:

    991 (Sejong) -> B1 (Daejeon) -> 607 (Daejeon/Okcheon)

The requested destination is the static Okcheon namespace stop.  Its supplied
coordinate exercises the same 300 m endpoint snapping used by the planner and
lands on the active Daejeon 607 stop occurrence.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import time
from typing import Iterable


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from journey_planner import PlannerLimitError  # noqa: E402
from sqlite_journey_planner import (  # noqa: E402
    ENDPOINT_ACCESS_RADIUS_M,
    LocatedStop,
    SQLiteJourneyPlanner,
    RouteState,
    RouteStop,
    RouteStopList,
    _SearchContext,
)


EXPECTED_ROUTES = (
    "SJB293000331",
    "DJB30300128",
    "DJB30300074",
)


def _endpoint_exact_or_snap(
    planner: SQLiteJourneyPlanner,
    context: _SearchContext,
    *,
    node_id: str,
    city_code: str,
    access: dict[str, object],
    require: str,
) -> tuple[LocatedStop, ...]:
    """Use the exact namespace first and snap only when it is absent.

    This matches ``JourneyPlanner.plan``.  Adding every nearby route even when
    the requested node is already active changes semantics and needlessly
    multiplies start states.
    """

    exact = planner._exact_states(
        context,
        node_id=node_id,
        city_code=city_code,
        require=require,
    )
    if exact:
        return tuple(LocatedStop(stop, 0.0, False) for stop in exact)
    return planner._endpoint_states(
        context,
        node_id=node_id,
        city_code=city_code,
        access=access,
        require=require,
    )


@dataclass(frozen=True, slots=True)
class LayerLabel:
    stop: RouteStop
    ride_orders: int
    walking_m: float
    route_ids: tuple[str, ...]

    @property
    def ride_offset(self) -> int:
        # Riding to any later order X costs ``ride_offset + X``.  This is the
        # invariant needed to compare labels boarded at different orders.
        return self.ride_orders - self.stop.state.node_order


def _direction_segments(route: RouteStopList) -> dict[RouteState, int]:
    """Match the production direction-boundary rule exactly.

    A boundary exists only when two consecutive non-empty direction values
    differ.  Labels are never Pareto-compared across those boundaries.
    """

    segment = 0
    result: dict[RouteState, int] = {}
    for index, stop in enumerate(route.stops):
        if index:
            previous = route.stops[index - 1].direction.strip()
            current = stop.direction.strip()
            if previous and current and previous != current:
                segment += 1
        result[stop.state] = segment
    return result


def _dominates(left: LayerLabel, right: LayerLabel) -> bool:
    """Return whether ``left`` makes ``right`` unnecessary.

    Both labels are already known to be on the same directional sequence
    segment and transfer layer.  Earlier boarding has a superset of reachable
    downstream stops.  At any common downstream order, lexicographic
    ``(ride_orders, walking_m)`` comparison is equivalent to comparing
    ``(ride_offset, walking_m)``.
    """

    return (
        left.stop.state.node_order <= right.stop.state.node_order
        and (left.ride_offset, left.walking_m)
        <= (right.ride_offset, right.walking_m)
    )


def _prune_layer(
    labels: Iterable[LayerLabel],
    *,
    segment_by_state: dict[RouteState, int],
) -> dict[RouteState, LayerLabel]:
    # First remove multiple ways to reach the exact same Markov state.  Since
    # transfer depth is fixed for one layer, the lexicographically cheaper
    # metric can reproduce every continuation of the discarded label.
    exact: dict[RouteState, LayerLabel] = {}
    for label in labels:
        current = exact.get(label.stop.state)
        if current is None or (
            label.ride_orders,
            label.walking_m,
            label.route_ids,
        ) < (
            current.ride_orders,
            current.walking_m,
            current.route_ids,
        ):
            exact[label.stop.state] = label

    grouped: dict[tuple[str, int], list[LayerLabel]] = defaultdict(list)
    for label in exact.values():
        grouped[
            (
                label.stop.state.sequence_id,
                segment_by_state[label.stop.state],
            )
        ].append(label)

    retained: dict[RouteState, LayerLabel] = {}
    for values in grouped.values():
        values.sort(
            key=lambda item: (
                item.stop.state.node_order,
                item.ride_offset,
                item.walking_m,
                item.route_ids,
            )
        )
        frontier: list[LayerLabel] = []
        for candidate in values:
            if any(_dominates(current, candidate) for current in frontier):
                continue
            frontier = [
                current
                for current in frontier
                if not _dominates(candidate, current)
            ]
            frontier.append(candidate)
        for label in frontier:
            retained[label.stop.state] = label
    return retained


def benchmark(
    database_path: Path,
    *,
    transfer_radius_m: int = 300,
    max_transfers: int = 4,
    max_layer_states: int = 50_000,
) -> dict[str, object]:
    planner = SQLiteJourneyPlanner(
        database_path,
        max_queries=10_000,
        max_expansions=50_000,
        max_rows_per_lookup=50_000,
        route_cache_entries=128,
        transfer_cache_entries=4_096,
    )
    started = time.perf_counter()
    with closing(planner._connect()) as connection:
        context = _SearchContext(
            connection=connection,
            max_queries=planner.max_queries,
            max_expansions=planner.max_expansions,
            max_rows_per_lookup=planner.max_rows_per_lookup,
            max_stops_per_route=planner.max_stops_per_route,
            route_cache_entries=planner.route_cache_entries,
            transfer_cache_entries=planner.transfer_cache_entries,
            max_parallel_searches=planner.max_parallel_searches,
        )
        origin_access = {
            "city_code": "12",
            "node_id": "SJB293001072",
            "node_name": "조치원역뒤편",
            "latitude": 36.599743,
            "longitude": 127.295111,
        }
        destination_access = {
            "city_code": "33330",
            "node_id": "OCB276000024",
            "node_name": "옥천버스앞",
            "latitude": 36.299573,
            "longitude": 127.566392,
        }
        starts = _endpoint_exact_or_snap(
            planner,
            context,
            node_id="SJB293001072",
            city_code="12",
            access=origin_access,
            require="board",
        )
        destinations = _endpoint_exact_or_snap(
            planner,
            context,
            node_id="OCB276000024",
            city_code="33330",
            access=destination_access,
            require="alight",
        )
        if not starts or not destinations:
            raise RuntimeError("benchmark endpoints are not routable within 300 m")

        destinations_by_sequence: dict[str, list[object]] = defaultdict(list)
        for located in destinations:
            destinations_by_sequence[located.stop.state.sequence_id].append(located)

        layer = {
            located.stop.state: LayerLabel(
                stop=located.stop,
                ride_orders=0,
                walking_m=located.access_distance_m,
                route_ids=(located.stop.state.route_id,),
            )
            for located in starts
        }
        segment_by_state: dict[RouteState, int] = {}
        layer_sizes: list[int] = []
        terminal: tuple[int, float, tuple[str, ...]] | None = None

        for transfer_count in range(max_transfers + 1):
            if len(layer) > max_layer_states:
                raise PlannerLimitError(
                    f"benchmark layer exceeds {max_layer_states} states"
                )
            layer_sizes.append(len(layer))

            terminals: list[tuple[int, float, tuple[str, ...]]] = []
            for label in layer.values():
                route = planner._route_stops(context, label.stop)
                segment_by_state.update(_direction_segments(route))
                reachable = planner._reachable_ride_stops(route, label.stop.state)
                reachable_states = {stop.state for stop in reachable}
                for destination in destinations_by_sequence.get(
                    label.stop.state.sequence_id, ()
                ):
                    if destination.stop.state not in reachable_states:
                        continue
                    terminals.append(
                        (
                            label.ride_orders
                            + destination.stop.state.node_order
                            - label.stop.state.node_order,
                            label.walking_m + destination.access_distance_m,
                            label.route_ids,
                        )
                    )
            if terminals:
                terminal = min(terminals)
                break
            if transfer_count == max_transfers:
                break

            # Deduplicate exact Markov states while expanding.  Building a
            # list first can create hundreds of thousands of short-lived
            # labels even though only a few thousand states survive.
            candidates: dict[RouteState, LayerLabel] = {}
            for label in layer.values():
                context.expand()
                route = planner._route_stops(context, label.stop)
                segment_by_state.update(_direction_segments(route))
                reachable = planner._reachable_ride_stops(route, label.stop.state)
                transfers = planner._batched_transfer_targets(
                    context,
                    route_stops=route,
                    sources=reachable,
                    transfer_radius_m=transfer_radius_m,
                )
                for alight in reachable:
                    ridden = (
                        label.ride_orders
                        + alight.state.node_order
                        - label.stop.state.node_order
                    )
                    for target, distance, _evidence in transfers.get(
                        alight.state, ()
                    ):
                        target_route_ids = label.route_ids + (
                            target.state.route_id,
                        )
                        candidate = LayerLabel(
                            stop=target,
                            ride_orders=ridden,
                            walking_m=label.walking_m + distance,
                            route_ids=target_route_ids,
                        )
                        current = candidates.get(target.state)
                        if current is None or (
                            candidate.ride_orders,
                            candidate.walking_m,
                            candidate.route_ids,
                        ) < (
                            current.ride_orders,
                            current.walking_m,
                            current.route_ids,
                        ):
                            candidates[target.state] = candidate

            # Populate segment IDs once per surviving target sequence, not
            # once per raw transfer edge.
            loaded_targets: set[str] = set()
            for candidate in candidates.values():
                sequence_id = candidate.stop.state.sequence_id
                if sequence_id in loaded_targets:
                    continue
                loaded_targets.add(sequence_id)
                target_route = planner._route_stops(context, candidate.stop)
                segment_by_state.update(_direction_segments(target_route))
            layer = _prune_layer(
                candidates.values(),
                segment_by_state=segment_by_state,
            )

        elapsed = time.perf_counter() - started
        return {
            "status": "READY" if terminal is not None else "NO_PATH",
            "elapsed_seconds": round(elapsed, 6),
            "transfer_radius_m": transfer_radius_m,
            "endpoint_access_radius_m": ENDPOINT_ACCESS_RADIUS_M,
            "endpoint_snapping_exercised": True,
            "direction_boundaries_preserved": True,
            "queries": context.query_count,
            "expanded_states": context.expansion_count,
            "layer_sizes": layer_sizes,
            "transfers": None if terminal is None else len(terminal[2]) - 1,
            "ride_orders": None if terminal is None else terminal[0],
            "walking_m": None if terminal is None else round(terminal[1], 3),
            "route_ids": [] if terminal is None else list(terminal[2]),
            "route_cache_entries": len(context.route_cache),
            "route_transfer_cache_entries": len(context.route_transfer_cache),
            "database_bytes": database_path.stat().st_size,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        type=Path,
        default=SERVICE_DIR / "data" / "network_catalog.sqlite3",
    )
    parser.add_argument("--radius", type=int, default=300)
    parser.add_argument("--max-transfers", type=int, default=4)
    parser.add_argument("--max-layer-states", type=int, default=50_000)
    parser.add_argument("--assert-route", action="store_true")
    parser.add_argument("--max-seconds", type=float)
    args = parser.parse_args()

    result = benchmark(
        args.database.resolve(),
        transfer_radius_m=args.radius,
        max_transfers=args.max_transfers,
        max_layer_states=args.max_layer_states,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.assert_route and tuple(result["route_ids"]) != EXPECTED_ROUTES:
        raise SystemExit("expected 991 -> B1 -> 607 route was not returned")
    if args.max_seconds is not None and result["elapsed_seconds"] > args.max_seconds:
        raise SystemExit(
            f"elapsed time exceeded {args.max_seconds:.3f} seconds"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
