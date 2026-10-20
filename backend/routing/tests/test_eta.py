from datetime import datetime, timedelta, timezone

import pytest

from routing.checkpoints import Coordinate, generate_checkpoints
from routing.eta import (
    DurationBreakpoint,
    compute_checkpoint_etas,
    interpolate_duration_s,
)

UTC = timezone.utc


def test_interpolate_duration_within_a_single_uniform_segment():
    # 0..100 miles takes 0..7200s (uniform speed), check midpoint and quarter point
    bp = [
        DurationBreakpoint(distance_mi=0, cumulative_duration_s=0),
        DurationBreakpoint(distance_mi=100, cumulative_duration_s=7200),
    ]
    assert interpolate_duration_s(bp, 50) == pytest.approx(3600)
    assert interpolate_duration_s(bp, 25) == pytest.approx(1800)
    assert interpolate_duration_s(bp, 0) == 0
    assert interpolate_duration_s(bp, 100) == 7200


def test_interpolate_duration_across_uneven_provider_steps():
    # Step 1: 0-10mi takes 600s (highway-ish). Step 2: 10-20mi takes 1800s (slow/city).
    bp = [
        DurationBreakpoint(distance_mi=0, cumulative_duration_s=0),
        DurationBreakpoint(distance_mi=10, cumulative_duration_s=600),
        DurationBreakpoint(distance_mi=20, cumulative_duration_s=2400),
    ]
    # 5mi into the fast step -> half of 600s
    assert interpolate_duration_s(bp, 5) == pytest.approx(300)
    # 15mi: fully past the fast step (600s) + half of the slow step (900s)
    assert interpolate_duration_s(bp, 15) == pytest.approx(1500)
    # avg-speed interpolation would wrongly give 20mi/20mi*2400 == 1800 at mile 15; must not match
    assert interpolate_duration_s(bp, 15) != pytest.approx(1800)


def test_interpolate_rejects_distance_beyond_route_total():
    bp = [
        DurationBreakpoint(distance_mi=0, cumulative_duration_s=0),
        DurationBreakpoint(distance_mi=100, cumulative_duration_s=7200),
    ]
    with pytest.raises(ValueError):
        interpolate_duration_s(bp, 150)


@pytest.mark.parametrize(
    "bad_breakpoints",
    [
        [DurationBreakpoint(distance_mi=0, cumulative_duration_s=0)],  # too short
        [  # doesn't start at (0, 0)
            DurationBreakpoint(distance_mi=5, cumulative_duration_s=0),
            DurationBreakpoint(distance_mi=100, cumulative_duration_s=7200),
        ],
        [  # distance goes backwards
            DurationBreakpoint(distance_mi=0, cumulative_duration_s=0),
            DurationBreakpoint(distance_mi=50, cumulative_duration_s=3600),
            DurationBreakpoint(distance_mi=40, cumulative_duration_s=7200),
        ],
        [  # duration goes backwards
            DurationBreakpoint(distance_mi=0, cumulative_duration_s=0),
            DurationBreakpoint(distance_mi=50, cumulative_duration_s=3600),
            DurationBreakpoint(distance_mi=100, cumulative_duration_s=1000),
        ],
    ],
)
def test_malformed_breakpoints_rejected(bad_breakpoints):
    with pytest.raises(ValueError):
        interpolate_duration_s(bad_breakpoints, 10)


def test_compute_checkpoint_etas_end_to_end():
    polyline = [Coordinate(lat=0, lon=0), Coordinate(lat=1.45, lon=0)]
    checkpoints = generate_checkpoints(polyline, total_distance_mi=100.0, interval_mi=25)
    bp = [
        DurationBreakpoint(distance_mi=0, cumulative_duration_s=0),
        DurationBreakpoint(distance_mi=100, cumulative_duration_s=7200),  # 2 hours flat
    ]
    departure = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    etas = compute_checkpoint_etas(checkpoints, bp, departure)

    assert [e.elapsed_duration_s for e in etas] == [0, 1800, 3600, 5400, 7200]
    assert etas[0].eta_utc == departure
    assert etas[-1].eta_utc == departure + timedelta(hours=2)
    assert etas[2].eta_utc == departure + timedelta(hours=1)


def test_compute_checkpoint_etas_requires_timezone_aware_departure():
    polyline = [Coordinate(lat=0, lon=0), Coordinate(lat=1.0, lon=0)]
    checkpoints = generate_checkpoints(polyline, total_distance_mi=50.0, interval_mi=25)
    bp = [
        DurationBreakpoint(distance_mi=0, cumulative_duration_s=0),
        DurationBreakpoint(distance_mi=50, cumulative_duration_s=3600),
    ]
    naive_departure = datetime(2026, 3, 1, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError):
        compute_checkpoint_etas(checkpoints, bp, naive_departure)


def test_compute_checkpoint_etas_converts_non_utc_input_to_utc():
    # departure given as UTC-5 (e.g. US Eastern standard time, fixed offset)
    eastern = timezone(timedelta(hours=-5))
    polyline = [Coordinate(lat=0, lon=0), Coordinate(lat=1.0, lon=0)]
    checkpoints = generate_checkpoints(polyline, total_distance_mi=50.0, interval_mi=25)
    bp = [
        DurationBreakpoint(distance_mi=0, cumulative_duration_s=0),
        DurationBreakpoint(distance_mi=50, cumulative_duration_s=3600),
    ]
    departure = datetime(2026, 3, 1, 9, 0, 0, tzinfo=eastern)  # 09:00 -05:00 == 14:00 UTC
    etas = compute_checkpoint_etas(checkpoints, bp, departure)
    assert etas[0].eta_utc == datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC)
    assert etas[0].eta_utc.tzinfo == UTC
