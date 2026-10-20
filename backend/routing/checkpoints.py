"""
Checkpoint generation: walk a route's polyline and place samples every N
miles of ACTUAL distance along the road, not every N geometry vertices and
not straight-line origin-to-destination distance.

Pure, no I/O. The 10/25/50-mile constraint from the assessment's UI is
enforced at the API boundary (serializer), not here — this function is
useful for any positive interval, and keeping it generic is what makes it
cheaply testable at arbitrary values.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_MI = 3958.7613
_EPSILON_MI = 1e-6


@dataclass(frozen=True)
class Coordinate:
    lat: float
    lon: float

    def __post_init__(self) -> None:
        if not (-90 <= self.lat <= 90):
            raise ValueError(f"lat out of range: {self.lat!r}")
        if not (-180 <= self.lon <= 180):
            raise ValueError(f"lon out of range: {self.lon!r}")


@dataclass(frozen=True)
class Checkpoint:
    sequence: int
    coordinate: Coordinate
    distance_mi: float
    is_origin: bool
    is_destination: bool


def haversine_mi(a: Coordinate, b: Coordinate) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (a.lat, a.lon, b.lat, b.lon))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MI * asin(sqrt(h))


def _cumulative_distances(polyline: list[Coordinate]) -> list[float]:
    cumulative = [0.0]
    for prev, cur in zip(polyline, polyline[1:]):
        cumulative.append(cumulative[-1] + haversine_mi(prev, cur))
    return cumulative


def _interpolate(
    polyline: list[Coordinate], cumulative: list[float], target_mi: float
) -> Coordinate:
    """Find the polyline segment bracketing target_mi and linearly interpolate."""
    for i in range(len(cumulative) - 1):
        seg_start, seg_end = cumulative[i], cumulative[i + 1]
        if seg_start <= target_mi <= seg_end:
            if seg_end == seg_start:  # zero-length segment (duplicate point)
                return polyline[i]
            frac = (target_mi - seg_start) / (seg_end - seg_start)
            a, b = polyline[i], polyline[i + 1]
            return Coordinate(
                lat=a.lat + frac * (b.lat - a.lat),
                lon=a.lon + frac * (b.lon - a.lon),
            )
    return polyline[-1]


def generate_checkpoints(
    polyline: list[Coordinate],
    total_distance_mi: float,
    interval_mi: float,
) -> list[Checkpoint]:
    """
    Always includes the origin (mile 0) and the destination (mile
    total_distance_mi). Interior checkpoints sit at exact multiples of
    interval_mi strictly between 0 and total_distance_mi. If
    total_distance_mi happens to be an exact multiple of interval_mi, no
    duplicate checkpoint is created at that mile — the destination
    checkpoint alone represents it.

    total_distance_mi is the routing provider's authoritative distance,
    which is normally not identical to the sum of haversine distances
    between the (simplified) polyline vertices. Rather than silently
    trusting one or the other, cumulative polyline distance is rescaled
    so it lands exactly on total_distance_mi at the final vertex —
    checkpoints are then placed at the correct fraction of the real route
    even though their lat/lon is interpolated from a simplified geometry.
    This is a documented approximation: a sparse polyline means long,
    straight interpolation segments between vertices, which can deviate
    from the true curved road. Denser provider geometry reduces this.
    """
    if interval_mi <= 0:
        raise ValueError(f"interval_mi must be positive, got {interval_mi!r}")
    if total_distance_mi <= 0:
        raise ValueError(f"total_distance_mi must be positive, got {total_distance_mi!r}")
    if len(polyline) < 2:
        raise ValueError("polyline must have at least 2 points")

    cumulative = _cumulative_distances(polyline)
    polyline_total = cumulative[-1]
    if polyline_total <= 0:
        raise ValueError("polyline has zero total length")

    scale = total_distance_mi / polyline_total
    cumulative = [c * scale for c in cumulative]

    targets = [0.0]
    next_target = interval_mi
    while next_target < total_distance_mi - _EPSILON_MI:
        targets.append(next_target)
        next_target += interval_mi
    targets.append(total_distance_mi)

    checkpoints = []
    last_index = len(targets) - 1
    for i, target in enumerate(targets):
        coordinate = _interpolate(polyline, cumulative, target)
        checkpoints.append(
            Checkpoint(
                sequence=i,
                coordinate=coordinate,
                distance_mi=target,
                is_origin=(i == 0),
                is_destination=(i == last_index),
            )
        )
    return checkpoints
