"""
Corridor grid generation for the weather heatmap: a band of points around
every route's polyline, built by walking each route at evenly-spaced
distances and adding points on either side, perpendicular to the direction
of travel at that point.

Pure geometry, no weather/risk knowledge — risk classification for each
point happens in the view layer by reusing routing/risk.py directly, the
same as it does for checkpoints. Point count is bounded by construction
(SAMPLES_PER_ROUTE * len(LATERAL_OFFSETS_MI) per route, capped further by
however many routes exist — never grows with trip distance), which is
what keeps the heatmap payload small regardless of a 50-mile vs.
2500-mile trip.
"""

from __future__ import annotations

from routing.checkpoints import (
    Coordinate,
    cumulative_distances,
    destination_point,
    locate_along_route,
)

SAMPLES_PER_ROUTE = 11
LATERAL_OFFSETS_MI = (-25.0, 0.0, 25.0)
# ~3 miles — merges points from different routes/offsets that land on
# effectively the same spot rather than paying for near-duplicate weather
# lookups.
DEDUP_DECIMALS = 2


def generate_corridor_grid(polylines: list[list[Coordinate]]) -> list[Coordinate]:
    """One grid per analysis, built from every route's polyline combined.
    Points from different routes/offsets that round to the same ~3-mile
    cell are merged, so overlapping route segments don't pay for duplicate
    weather lookups."""
    seen: dict[tuple[float, float], Coordinate] = {}

    for polyline in polylines:
        if len(polyline) < 2:
            continue
        cumulative = cumulative_distances(polyline)
        total = cumulative[-1]
        if total <= 0:
            continue

        for i in range(SAMPLES_PER_ROUTE):
            target_mi = total * i / (SAMPLES_PER_ROUTE - 1)
            center, bearing = locate_along_route(polyline, cumulative, target_mi)
            for offset_mi in LATERAL_OFFSETS_MI:
                point = center if offset_mi == 0 else destination_point(center, bearing + 90, offset_mi)
                key = (round(point.lat, DEDUP_DECIMALS), round(point.lon, DEDUP_DECIMALS))
                seen.setdefault(key, point)

    return list(seen.values())
