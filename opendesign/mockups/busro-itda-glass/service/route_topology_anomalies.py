"""Pure, identifier-agnostic predicates for hard route-topology anomalies."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


EARTH_RADIUS_METERS = 6_371_008.8
SINGLE_POINT_ROUTE_SPIKE_ERROR_CODE = "SINGLE_POINT_ROUTE_SPIKE"
SINGLE_POINT_SPIKE_LEG_METERS = 20_000.0
SINGLE_POINT_SPIKE_BYPASS_METERS = 5_000.0


@dataclass(frozen=True, slots=True)
class SinglePointRouteSpike:
    """Bounded evidence for one implausible middle point in an ordered route."""

    previous_order: int
    middle_order: int
    following_order: int
    previous_to_middle_meters: float
    middle_to_following_meters: float
    previous_to_following_meters: float

    def bounded_evidence(self) -> str:
        """Return deterministic evidence without provider text or identifiers."""

        def bounded_order(value: int) -> int:
            return max(-2_147_483_648, min(2_147_483_647, int(value)))

        def bounded_meters(value: float) -> int:
            return max(0, min(99_999_999, int(round(value))))

        orders = "/".join(
            str(bounded_order(value))
            for value in (
                self.previous_order,
                self.middle_order,
                self.following_order,
            )
        )
        distances = "/".join(
            str(bounded_meters(value))
            for value in (
                self.previous_to_middle_meters,
                self.middle_to_following_meters,
                self.previous_to_following_meters,
            )
        )
        return (
            "same-direction single-point route spike; "
            f"orders={orders}; distances_m={distances}"
        )


def _value(point: Mapping[str, Any] | Any, field: str) -> Any:
    if isinstance(point, Mapping):
        return point.get(field)
    return getattr(point, field, None)


def _coordinate(point: Mapping[str, Any] | Any) -> tuple[float, float] | None:
    latitude = _value(point, "latitude")
    longitude = _value(point, "longitude")
    if latitude is None or longitude is None:
        return None
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90.0 <= latitude <= 90.0
        or not -180.0 <= longitude <= 180.0
    ):
        return None
    return latitude, longitude


def _order(point: Mapping[str, Any] | Any) -> int | None:
    try:
        return int(_value(point, "node_order"))
    except (TypeError, ValueError, OverflowError):
        return None


def _direction(point: Mapping[str, Any] | Any) -> str:
    value = _value(point, "direction")
    return "" if value is None else str(value).strip()


def _haversine_meters(
    left: tuple[float, float], right: tuple[float, float]
) -> float:
    latitude_a = math.radians(left[0])
    latitude_b = math.radians(right[0])
    delta_latitude = latitude_b - latitude_a
    delta_longitude = math.radians(right[1] - left[1])
    value = (
        math.sin(delta_latitude / 2.0) ** 2
        + math.cos(latitude_a)
        * math.cos(latitude_b)
        * math.sin(delta_longitude / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_METERS * math.asin(min(1.0, math.sqrt(value)))


def single_point_route_spike(
    previous: Mapping[str, Any] | Any,
    middle: Mapping[str, Any] | Any,
    following: Mapping[str, Any] | Any,
) -> SinglePointRouteSpike | None:
    """Detect one A->far B->near A spike in three consecutive route points.

    The rule intentionally applies only when all three rows have the same
    non-empty direction. Legitimate long-distance runs remain valid when the
    first and third points are also far apart.
    """

    directions = (_direction(previous), _direction(middle), _direction(following))
    if (
        not directions[0]
        or directions[0] != directions[1]
        or directions[0] != directions[2]
    ):
        return None
    coordinates = (
        _coordinate(previous),
        _coordinate(middle),
        _coordinate(following),
    )
    if any(coordinate is None for coordinate in coordinates):
        return None
    orders = (_order(previous), _order(middle), _order(following))
    if any(order is None for order in orders):
        return None
    previous_coordinate, middle_coordinate, following_coordinate = coordinates
    assert previous_coordinate is not None
    assert middle_coordinate is not None
    assert following_coordinate is not None
    previous_to_middle = _haversine_meters(previous_coordinate, middle_coordinate)
    middle_to_following = _haversine_meters(middle_coordinate, following_coordinate)
    previous_to_following = _haversine_meters(
        previous_coordinate, following_coordinate
    )
    if not (
        previous_to_middle > SINGLE_POINT_SPIKE_LEG_METERS
        and middle_to_following > SINGLE_POINT_SPIKE_LEG_METERS
        and previous_to_following < SINGLE_POINT_SPIKE_BYPASS_METERS
    ):
        return None
    previous_order, middle_order, following_order = orders
    assert previous_order is not None
    assert middle_order is not None
    assert following_order is not None
    return SinglePointRouteSpike(
        previous_order=previous_order,
        middle_order=middle_order,
        following_order=following_order,
        previous_to_middle_meters=previous_to_middle,
        middle_to_following_meters=middle_to_following,
        previous_to_following_meters=previous_to_following,
    )


__all__ = [
    "SINGLE_POINT_ROUTE_SPIKE_ERROR_CODE",
    "SINGLE_POINT_SPIKE_BYPASS_METERS",
    "SINGLE_POINT_SPIKE_LEG_METERS",
    "SinglePointRouteSpike",
    "single_point_route_spike",
]
