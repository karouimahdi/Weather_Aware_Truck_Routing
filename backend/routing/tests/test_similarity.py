from routing.checkpoints import Coordinate
from routing import similarity


def _line(lat0, lon0, lat1, lon1, n):
    return [
        Coordinate(lat=lat0 + (lat1 - lat0) * i / (n - 1), lon=lon0 + (lon1 - lon0) * i / (n - 1))
        for i in range(n)
    ]


# --- has_backtrack_spur ---
#
# These two fixtures reproduce the shapes measured live against real OSRM
# via-fallback candidates during the 3-route investigation (see
# docs/phase-4-routing-performance.md addendum): a genuine dead-end
# out-and-back spur overlaps 0.59-0.96 in that measurement; a legitimate,
# meaningfully-distinct detour that merely passes near its own approach
# road (a realistic junction/frontage-road share) overlapped 0.08-0.23.
# SPUR_MATCH_FRACTION = 0.05 flagged both; this is the regression guard.


def test_dead_end_backtrack_is_flagged():
    # Approaches the via, then retraces almost the identical road back down
    # before continuing elsewhere — the literal "drive up a dead end and
    # back" shape.
    polyline = (
        _line(0.0, 0.0, 0.43, 0.0, 25)
        + _line(0.43, 0.0, 0.0, 0.0, 25)[1:]
        + _line(0.0, 0.0, -0.3, 0.0, 15)[1:]
    )
    via = Coordinate(lat=0.43, lon=0.0)
    assert similarity.has_backtrack_spur(polyline, via) is True


def test_legitimate_detour_near_the_via_is_not_flagged():
    # Approaches the via, dips back a short way along the same road (a
    # realistic frontage-road/junction overlap), then heads off on a
    # genuinely different corridor. Not a dead end, just real-world road
    # geometry near the via point.
    polyline = (
        _line(0.0, 0.0, 0.43, 0.0, 25)
        + _line(0.43, 0.0, 0.35, 0.0, 6)[1:]
        + _line(0.35, 0.0, 0.75, 0.4, 25)[1:]
    )
    via = Coordinate(lat=0.43, lon=0.0)
    assert similarity.has_backtrack_spur(polyline, via) is False


def test_short_route_window_saturation_does_not_misfire():
    # SPUR_WINDOW_MI (40mi) spans the entire route on short trips, which is
    # exactly the condition that made 0.05 misfire on live Fort Worth->Dallas
    # and LA->Phoenix candidates. A route that simply goes out, bends onto a
    # different corridor, and arrives — never literally reversing — must not
    # be flagged just because it's short.
    polyline = _line(0.0, 0.0, 0.2, 0.0, 15) + _line(0.2, 0.0, 0.35, 0.25, 15)[1:]
    via = Coordinate(lat=0.2, lon=0.0)
    assert similarity.has_backtrack_spur(polyline, via) is False


# --- overlap / is_distinct (latitude correction + near-duplicate rejection) ---


def test_overlap_is_symmetric_only_when_footprints_match():
    a = _line(40.0, -90.0, 41.0, -90.0, 50)
    b = _line(40.0, -90.0, 40.5, -90.0, 25)  # sits entirely inside a's footprint
    assert similarity.overlap(b, a) > 0.9  # all of b's cells are in a's
    assert similarity.overlap(a, b) < 0.6  # but not vice versa


def test_is_distinct_rejects_near_duplicate_even_if_reversed():
    route = _line(40.0, -90.0, 41.0, -89.0, 40)
    reversed_route = list(reversed(route))
    assert similarity.is_distinct(reversed_route, [route]) is False


def test_is_distinct_accepts_genuinely_different_corridor():
    route = _line(40.0, -90.0, 41.0, -89.0, 40)
    other = _line(40.0, -90.0, 39.0, -91.5, 40)
    assert similarity.is_distinct(other, [route]) is True
