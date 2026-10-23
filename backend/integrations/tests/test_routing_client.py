from unittest.mock import patch

from routing.checkpoints import Coordinate, cumulative_distances, destination_point, locate_along_route
from routing.eta import DurationBreakpoint

from integrations.routing_client import (
    TARGET_ROUTES,
    VIA_ANCHOR_FRACTIONS,
    VIA_OFFSET_FRACTION,
    VIA_OFFSET_MAX_MI,
    VIA_OFFSET_MIN_MI,
    RouteOption,
    _add_via_fallback_routes,
)
from routing.checkpoints import haversine_mi


def _line(lat0, lon0, lat1, lon1, n):
    return [
        Coordinate(lat=lat0 + (lat1 - lat0) * i / (n - 1), lon=lon0 + (lon1 - lon0) * i / (n - 1))
        for i in range(n)
    ]


def _route(route_id, polyline, distance_mi, duration_s):
    return RouteOption(
        route_id=route_id,
        polyline=polyline,
        distance_mi=distance_mi,
        duration_s=duration_s,
        duration_breakpoints=[
            DurationBreakpoint(distance_mi=0.0, cumulative_duration_s=0.0),
            DurationBreakpoint(distance_mi=distance_mi, cumulative_duration_s=duration_s),
        ],
    )


ORIGIN = Coordinate(lat=0.0, lon=0.0)
DESTINATION = Coordinate(lat=2.9, lon=0.0)  # ~200mi north along the same meridian
MAIN_POLYLINE = _line(ORIGIN.lat, ORIGIN.lon, DESTINATION.lat, DESTINATION.lon, 60)
MAIN_DISTANCE_MI = cumulative_distances(MAIN_POLYLINE)[-1]
MAIN = _route("route-0", MAIN_POLYLINE, MAIN_DISTANCE_MI, duration_s=4 * 3600)


def _via_points_for(main):
    """Exactly replicates the production anchor/offset math in
    _add_via_fallback_routes, so tests can address each of the 4 via
    points individually regardless of internal ordering."""
    straight_mi = haversine_mi(ORIGIN, DESTINATION)
    offset_mi = min(max(straight_mi * VIA_OFFSET_FRACTION, VIA_OFFSET_MIN_MI), VIA_OFFSET_MAX_MI)
    cumulative = cumulative_distances(main.polyline)
    points = []
    for fraction in VIA_ANCHOR_FRACTIONS:
        anchor, bearing = locate_along_route(main.polyline, cumulative, main.distance_mi * fraction)
        points.append(destination_point(anchor, bearing + 90, offset_mi))
        points.append(destination_point(anchor, bearing - 90, offset_mi))
    return points


def _rounded(coord):
    return (round(coord.lat, 6), round(coord.lon, 6))


def test_second_anchor_recovers_a_route_the_first_anchor_misses():
    # Reproduces the Sousse->Tunis shape found live: the first anchor's two
    # via candidates are both legitimately rejected (one unreachable, one
    # too slow), and only the second anchor's candidate is a genuine,
    # distinct, reasonably-paced third route. A single-anchor search (the
    # prior behavior) would have returned just the 1 native route.
    anchor1_plus, anchor1_minus, anchor2_plus, anchor2_minus = _via_points_for(MAIN)

    good_candidate = _route(
        "via",
        _line(0.0, 0.0, 1.5, 1.2, 20) + _line(1.5, 1.2, 2.9, 0.0, 20)[1:],
        distance_mi=230.0,
        duration_s=5 * 3600,  # 1.25x the main route's 4h — within MAX_DURATION_RATIO
    )
    too_slow = _route("via", MAIN_POLYLINE, distance_mi=210.0, duration_s=20 * 3600)

    responses = {
        _rounded(anchor1_plus): None,  # unreachable via point
        _rounded(anchor1_minus): too_slow,
        _rounded(anchor2_plus): None,
        _rounded(anchor2_minus): good_candidate,
    }

    def fake_fetch(origin, via, destination):
        # .get(..., None), not a strict lookup: a primary via point mapped
        # to None here triggers production code's own minimum-offset retry
        # (see _add_via_fallback_routes), which queries a *different*
        # coordinate this fixture never enumerated. Defaulting to None
        # keeps these anchors "genuinely unreachable" through the retry
        # too, matching this test's own stated scenario, rather than the
        # retry's exact coordinate needing to be reproduced here.
        return responses.get(_rounded(via))

    with patch("integrations.routing_client._fetch_via_route", side_effect=fake_fetch):
        result = _add_via_fallback_routes([MAIN], ORIGIN, DESTINATION)

    assert len(result) == 2
    assert result[1].route_id == "via"
    assert result[1].duration_s == good_candidate.duration_s


def test_all_candidates_rejected_returns_honest_shortfall_not_padding():
    via_points = _via_points_for(MAIN)
    too_slow = _route("via", MAIN_POLYLINE, distance_mi=210.0, duration_s=30 * 3600)

    with patch("integrations.routing_client._fetch_via_route", return_value=too_slow):
        result = _add_via_fallback_routes([MAIN], ORIGIN, DESTINATION)

    assert result == [MAIN]


def test_never_exceeds_target_routes_even_with_surplus_candidates():
    anchor1_plus, anchor1_minus, anchor2_plus, anchor2_minus = _via_points_for(MAIN)

    def make_candidate(tag, lat_bend, lon_bend):
        polyline = _line(0.0, 0.0, lat_bend, lon_bend, 20) + _line(lat_bend, lon_bend, 2.9, 0.0, 20)[1:]
        return _route(tag, polyline, distance_mi=220.0, duration_s=5 * 3600)

    responses = {
        _rounded(anchor1_plus): make_candidate("a", 1.4, 1.0),
        _rounded(anchor1_minus): make_candidate("b", 1.4, -1.0),
        _rounded(anchor2_plus): make_candidate("c", 1.6, 1.3),
        _rounded(anchor2_minus): make_candidate("d", 1.6, -1.3),
    }

    def fake_fetch(origin, via, destination):
        return responses[_rounded(via)]

    with patch("integrations.routing_client._fetch_via_route", side_effect=fake_fetch):
        result = _add_via_fallback_routes([MAIN], ORIGIN, DESTINATION)

    assert len(result) == TARGET_ROUTES
