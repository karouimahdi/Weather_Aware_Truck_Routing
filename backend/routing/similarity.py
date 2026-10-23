"""
Route similarity: is a candidate route a genuine alternative, or a
near-duplicate of one already chosen — or an out-and-back spur through a
via point that landed on a dead-end road? Pure geometry, no I/O; see
integrations/routing_client.py for where this gets called against real
OSRM responses when native alternatives fall short of 3.

Overlap is measured on a local, mile-based grid rather than raw lat/lon
degrees. A fixed-size degree cell is not a fixed metric distance: 1
degree of longitude shrinks by cos(latitude) relative to 1 degree of
latitude, so hashing on raw (lat, lon) would make cells narrower
east-west the further north a route runs — a real inconsistency found in
one of the reference implementations this technique is adapted from (it
applied this correction in its heatmap grid but not in its route-overlap
function). Projecting both axes to miles first avoids that distortion
uniformly.
"""

from __future__ import annotations

from math import cos, pi, radians

from routing.checkpoints import Coordinate, EARTH_RADIUS_MI, cumulative_distances, haversine_mi

MILES_PER_DEGREE_LAT = EARTH_RADIUS_MI * pi / 180
CELL_MI = 1.5  # ~2.4km grid cell — the scale alternatives are compared at

# A candidate that overlaps an already-chosen route this much (in either
# direction — a short route can sit entirely inside a long one's
# footprint without the reverse being true) is a near-duplicate, not a
# genuine third option.
MAX_OVERLAP = 0.85

# A candidate slower than this multiple of the fastest already-chosen
# route isn't a reasonable alternative, even if geometrically distinct.
# 1.5 rejected a real, legitimate via-candidate on a live Sousse->Tunis
# request at ratio 1.5039 — 0.26% over the line. Raised to 1.6 (matching
# malikali3419's independently-validated value) after confirming live that
# it does not let any previously-confirmed-bad candidate through: a
# measured 1.91x (Salt Lake City->Cheyenne) and 2.29x (LA->Phoenix)
# candidate both still correctly rejected, and the newly-admitted
# Fort Worth->Dallas candidate (1.57x) was independently confirmed
# non-spur and low-overlap (<=0.17) against both existing routes.
MAX_DURATION_RATIO = 1.6

# A via point that lands on a dead-end road makes the route drive up it
# and straight back. Looking only within this distance of the via point
# (not the whole route) keeps the spur check cheap and avoids flagging
# routes that simply cross their own path far from the via (e.g. a loop
# at the origin city, unrelated to the via point at all).
SPUR_WINDOW_MI = 40.0
# Calibrated against 6 live OSRM via-fallback candidates (real routes, not
# synthetic): genuine dead-end spurs measured at 0.595-0.955 overlap;
# legitimate, meaningfully-distinct detour candidates measured at
# 0.081-0.200 (see routing/tests/test_similarity.py for the fixture shapes
# that reproduce both). 0.05 used to sit below *both* clusters — for any
# route shorter than ~2*SPUR_WINDOW_MI, the before/after windows cover
# nearly the whole route, so almost any candidate shares a few grid cells
# with itself (a shared highway, a metro grid) and got misclassified as a
# backtrack. 0.35 sits in the measured gap with margin on both sides.
SPUR_MATCH_FRACTION = 0.35


def _cell(coord: Coordinate) -> tuple[int, int]:
    lat_mi = coord.lat * MILES_PER_DEGREE_LAT
    lon_mi = coord.lon * MILES_PER_DEGREE_LAT * max(cos(radians(coord.lat)), 0.2)
    return (round(lat_mi / CELL_MI), round(lon_mi / CELL_MI))


def _footprint(polyline: list[Coordinate]) -> set[tuple[int, int]]:
    return {_cell(point) for point in polyline}


def overlap(a: list[Coordinate], b: list[Coordinate]) -> float:
    """Share of `a`'s footprint cells that are also in `b`'s (0..1)."""
    cells_a = _footprint(a)
    if not cells_a:
        return 1.0
    cells_b = _footprint(b)
    return len(cells_a & cells_b) / len(cells_a)


def is_distinct(candidate: list[Coordinate], existing: list[list[Coordinate]]) -> bool:
    """A route only counts as a genuine alternative if it doesn't mostly
    retrace one already selected, checked in both directions."""
    return all(
        overlap(candidate, other) < MAX_OVERLAP and overlap(other, candidate) < MAX_OVERLAP
        for other in existing
    )


def has_backtrack_spur(polyline: list[Coordinate], via: Coordinate) -> bool:
    """True if the path shortly after the point nearest `via` mostly
    retraces the path shortly before it — the signature of a via point
    that forced a drive up a dead-end road and back, rather than a real
    alternative route."""
    if len(polyline) < 2:
        return False
    cumulative = cumulative_distances(polyline)
    via_index = min(range(len(polyline)), key=lambda i: haversine_mi(polyline[i], via))
    via_mi = cumulative[via_index]

    before = [p for p, m in zip(polyline, cumulative) if via_mi - SPUR_WINDOW_MI <= m <= via_mi]
    after = [p for p, m in zip(polyline, cumulative) if via_mi <= m <= via_mi + SPUR_WINDOW_MI]
    if len(before) < 2 or len(after) < 2:
        return False
    return overlap(after, before) > SPUR_MATCH_FRACTION
