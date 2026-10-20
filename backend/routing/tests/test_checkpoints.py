import pytest

from routing.checkpoints import Coordinate, generate_checkpoints, haversine_mi

NYC = Coordinate(lat=40.7128, lon=-74.0060)
LA = Coordinate(lat=34.0522, lon=-118.2437)


def test_haversine_known_distance_nyc_to_la():
    # Great-circle (not driving) distance between the two coordinates, ~2446 mi.
    assert haversine_mi(NYC, LA) == pytest.approx(2446, abs=5)


def straight_line_polyline(n_points: int, lat_step: float) -> list[Coordinate]:
    """Points along a meridian (fixed lon) so cumulative distance is easy to reason about."""
    return [Coordinate(lat=i * lat_step, lon=0.0) for i in range(n_points)]


def test_exact_multiple_interval_no_duplicate_destination():
    # ~100 miles total via a straight 2-point polyline, interval 25 -> 0,25,50,75,100 (5 checkpoints)
    polyline = [Coordinate(lat=0, lon=0), Coordinate(lat=1.45, lon=0)]
    total = 100.0
    checkpoints = generate_checkpoints(polyline, total_distance_mi=total, interval_mi=25)
    distances = [c.distance_mi for c in checkpoints]
    assert distances == [0, 25, 50, 75, 100]
    assert checkpoints[0].is_origin and not checkpoints[0].is_destination
    assert checkpoints[-1].is_destination and not checkpoints[-1].is_origin
    assert all(not c.is_origin and not c.is_destination for c in checkpoints[1:-1])


def test_non_exact_multiple_keeps_single_destination_checkpoint():
    # total 110 miles, interval 25 -> interior at 25,50,75,100, then destination at 110
    # (not a duplicate at 100 and 110).
    polyline = [Coordinate(lat=0, lon=0), Coordinate(lat=1.6, lon=0)]
    checkpoints = generate_checkpoints(polyline, total_distance_mi=110.0, interval_mi=25)
    distances = [c.distance_mi for c in checkpoints]
    assert distances == [0, 25, 50, 75, 100, 110]
    assert checkpoints[-1].distance_mi == 110
    assert checkpoints[-1].is_destination


def test_route_shorter_than_interval_has_only_origin_and_destination():
    polyline = [Coordinate(lat=0, lon=0), Coordinate(lat=0.1, lon=0)]
    checkpoints = generate_checkpoints(polyline, total_distance_mi=5.0, interval_mi=25)
    assert len(checkpoints) == 2
    assert checkpoints[0].is_origin
    assert checkpoints[1].is_destination
    assert checkpoints[1].distance_mi == 5.0


def test_checkpoints_are_sequential_and_monotonic():
    polyline = [Coordinate(lat=0, lon=0), Coordinate(lat=2.9, lon=0)]
    checkpoints = generate_checkpoints(polyline, total_distance_mi=200.0, interval_mi=50)
    sequences = [c.sequence for c in checkpoints]
    assert sequences == list(range(len(checkpoints)))
    distances = [c.distance_mi for c in checkpoints]
    assert distances == sorted(distances)
    assert len(distances) == len(set(distances))  # no duplicates


def test_scale_normalization_when_provider_total_differs_from_polyline_sum():
    # The 2-point polyline's own haversine sum is ~100 mi, but the provider
    # claims 120 mi (denser real road vs. simplified straight polyline).
    # Checkpoints must be placed at fractions of the PROVIDER total, with
    # interpolated coordinates still drawn from the polyline's shape.
    polyline = [Coordinate(lat=0, lon=0), Coordinate(lat=1.45, lon=0)]
    checkpoints = generate_checkpoints(polyline, total_distance_mi=120.0, interval_mi=60)
    distances = [c.distance_mi for c in checkpoints]
    assert distances == [0, 60, 120]
    # midpoint checkpoint (mile 60 of 120 = halfway) should interpolate to the
    # polyline's own halfway point regardless of the distance rescaling.
    midpoint = checkpoints[1]
    assert midpoint.coordinate.lat == pytest.approx(0.725, abs=1e-6)


def test_interpolated_midpoint_matches_expected_coordinate_on_dense_polyline():
    polyline = straight_line_polyline(11, lat_step=0.1)  # lat 0.0 .. 1.0
    total = sum(
        haversine_mi(polyline[i], polyline[i + 1]) for i in range(len(polyline) - 1)
    )
    checkpoints = generate_checkpoints(polyline, total_distance_mi=total, interval_mi=total / 2)
    midpoint = checkpoints[1]
    assert midpoint.coordinate.lat == pytest.approx(0.5, abs=1e-3)
    assert midpoint.coordinate.lon == pytest.approx(0.0, abs=1e-9)


def test_duplicate_consecutive_points_do_not_crash():
    polyline = [
        Coordinate(lat=0, lon=0),
        Coordinate(lat=0, lon=0),  # zero-length segment
        Coordinate(lat=1.0, lon=0),
    ]
    checkpoints = generate_checkpoints(polyline, total_distance_mi=70.0, interval_mi=25)
    assert checkpoints[0].distance_mi == 0
    assert checkpoints[-1].distance_mi == 70.0


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(polyline=[Coordinate(0, 0), Coordinate(1, 1)], total_distance_mi=0, interval_mi=10),
        dict(polyline=[Coordinate(0, 0), Coordinate(1, 1)], total_distance_mi=-5, interval_mi=10),
        dict(polyline=[Coordinate(0, 0), Coordinate(1, 1)], total_distance_mi=100, interval_mi=0),
        dict(polyline=[Coordinate(0, 0), Coordinate(1, 1)], total_distance_mi=100, interval_mi=-10),
        dict(polyline=[Coordinate(0, 0)], total_distance_mi=100, interval_mi=10),
        dict(polyline=[], total_distance_mi=100, interval_mi=10),
    ],
)
def test_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        generate_checkpoints(**kwargs)


def test_malformed_coordinate_out_of_range_raises():
    with pytest.raises(ValueError):
        Coordinate(lat=999, lon=0)
    with pytest.raises(ValueError):
        Coordinate(lat=0, lon=-999)
