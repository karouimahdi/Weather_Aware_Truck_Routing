from datetime import datetime, timezone

import pytest
import requests
from django.core.cache import cache

from integrations.weather_client import WeatherProviderError, get_weather_observations
from routing.checkpoints import Coordinate

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def _location_payload(lat, lon, hours, winds, rains, snows):
    return {
        "latitude": lat,
        "longitude": lon,
        "hourly": {"time": hours, "wind_speed_10m": winds, "rain": rains, "snowfall": snows},
    }


def test_single_coordinate_parses_matching_hour(requests_mock):
    requests_mock.get(
        OPEN_METEO_URL,
        json=_location_payload(
            40.0, -74.0,
            ["2026-03-01T12:00", "2026-03-01T13:00"],
            [10.0, 42.0],
            [0.0, 0.3],
            [0.0, 0.0],
        ),
    )
    eta = datetime(2026, 3, 1, 13, 0, tzinfo=timezone.utc)
    [observation] = get_weather_observations([(Coordinate(lat=40.0, lon=-74.0), eta)])
    assert observation.wind_mph == 42.0
    assert observation.rain_in_hr == 0.3


def test_nearest_hour_rounding():
    # 13:40 should round to 14:00, not 13:00.
    from integrations.weather_client import _nearest_hour

    assert _nearest_hour(datetime(2026, 3, 1, 13, 40, tzinfo=timezone.utc)) == datetime(
        2026, 3, 1, 14, 0, tzinfo=timezone.utc
    )
    assert _nearest_hour(datetime(2026, 3, 1, 13, 20, tzinfo=timezone.utc)) == datetime(
        2026, 3, 1, 13, 0, tzinfo=timezone.utc
    )


def test_multiple_distinct_coordinates_batched_in_one_request(requests_mock):
    requests_mock.get(
        OPEN_METEO_URL,
        json=[
            _location_payload(40.0, -74.0, ["2026-03-01T12:00"], [10.0], [0.0], [0.0]),
            _location_payload(41.0, -75.0, ["2026-03-01T12:00"], [20.0], [0.2], [0.0]),
        ],
    )
    eta = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    results = get_weather_observations(
        [
            (Coordinate(lat=40.0, lon=-74.0), eta),
            (Coordinate(lat=41.0, lon=-75.0), eta),
        ]
    )
    assert requests_mock.call_count == 1
    assert results[0].wind_mph == 10.0
    assert results[1].wind_mph == 20.0


def test_duplicate_rounded_coordinate_and_hour_is_deduplicated(requests_mock):
    requests_mock.get(
        OPEN_METEO_URL,
        json=_location_payload(40.0, -74.0, ["2026-03-01T12:00"], [15.0], [0.0], [0.0]),
    )
    eta = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    # Two checkpoints land on the same rounded coordinate and the same hour.
    results = get_weather_observations(
        [
            (Coordinate(lat=40.04, lon=-74.04), eta),
            (Coordinate(lat=40.02, lon=-74.02), eta),
        ]
    )
    request = requests_mock.request_history[0]
    assert request.qs["latitude"] == ["40.0"]
    assert len(results) == 2
    assert results[0].wind_mph == results[1].wind_mph == 15.0


def test_result_is_cached_across_calls(requests_mock):
    requests_mock.get(
        OPEN_METEO_URL,
        json=_location_payload(40.0, -74.0, ["2026-03-01T12:00"], [15.0], [0.0], [0.0]),
    )
    eta = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    target = [(Coordinate(lat=40.0, lon=-74.0), eta)]

    get_weather_observations(target)
    get_weather_observations(target)

    assert requests_mock.call_count == 1


def test_eta_outside_returned_horizon_is_unavailable_not_guessed(requests_mock):
    requests_mock.get(
        OPEN_METEO_URL,
        json=_location_payload(40.0, -74.0, ["2026-03-01T12:00"], [15.0], [0.0], [0.0]),
    )
    far_future_eta = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)
    [observation] = get_weather_observations([(Coordinate(lat=40.0, lon=-74.0), far_future_eta)])
    assert observation is None


def test_result_order_matches_input_order_with_mixed_dedup(requests_mock):
    requests_mock.get(
        OPEN_METEO_URL,
        json=[
            _location_payload(40.0, -74.0, ["2026-03-01T12:00"], [11.0], [0.0], [0.0]),
            _location_payload(41.0, -75.0, ["2026-03-01T12:00"], [22.0], [0.0], [0.0]),
        ],
    )
    eta = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    results = get_weather_observations(
        [
            (Coordinate(lat=41.0, lon=-75.0), eta),
            (Coordinate(lat=40.0, lon=-74.0), eta),
            (Coordinate(lat=41.0, lon=-75.0), eta),  # duplicate, different position
        ]
    )
    assert [r.wind_mph for r in results] == [22.0, 11.0, 22.0]


def test_location_count_mismatch_raises(requests_mock):
    requests_mock.get(
        OPEN_METEO_URL,
        json=_location_payload(40.0, -74.0, ["2026-03-01T12:00"], [11.0], [0.0], [0.0]),
    )
    eta = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(WeatherProviderError):
        get_weather_observations(
            [
                (Coordinate(lat=40.0, lon=-74.0), eta),
                (Coordinate(lat=41.0, lon=-75.0), eta),
            ]
        )


def test_malformed_response_raises(requests_mock):
    requests_mock.get(OPEN_METEO_URL, json={"latitude": 40.0, "longitude": -74.0})
    eta = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(WeatherProviderError):
        get_weather_observations([(Coordinate(lat=40.0, lon=-74.0), eta)])


def test_network_failure_raises(requests_mock):
    requests_mock.get(OPEN_METEO_URL, exc=requests.exceptions.ConnectTimeout)
    eta = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(WeatherProviderError):
        get_weather_observations([(Coordinate(lat=40.0, lon=-74.0), eta)])


def test_empty_targets_returns_empty_list():
    assert get_weather_observations([]) == []
