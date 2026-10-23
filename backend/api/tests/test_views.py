import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from django.test import Client

from integrations.routing_client import GeocodeSuggestion, RouteOption, RoutingProviderError
from integrations.weather_client import WeatherProviderError
from routing.checkpoints import Coordinate
from routing.eta import DurationBreakpoint
from routing.risk import WeatherObservation

URL = "/api/routes/analyze/"
CALM = WeatherObservation(wind_mph=10, rain_in_hr=0, snow_in_hr=0)


def _future_departure() -> str:
    # Computed relative to "now", not a fixed calendar date: a hardcoded
    # past-tense date would fail the serializer's own
    # departure-cannot-be-in-the-past validation the moment real time
    # passes it by — this fixture should never go stale.
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


def make_route(route_id, distance_mi=100.0, duration_s=7200.0):
    polyline = [Coordinate(lat=40.0, lon=-74.0), Coordinate(lat=41.45, lon=-74.0)]
    breakpoints = [
        DurationBreakpoint(distance_mi=0.0, cumulative_duration_s=0.0),
        DurationBreakpoint(distance_mi=distance_mi, cumulative_duration_s=duration_s),
    ]
    return RouteOption(
        route_id=route_id,
        polyline=polyline,
        distance_mi=distance_mi,
        duration_s=duration_s,
        duration_breakpoints=breakpoints,
    )


def base_payload(**overrides):
    payload = {
        "origin": {"label": "New York, NY, USA", "lat": 40.0, "lon": -74.0},
        "destination": {"label": "Boston, MA, USA", "lat": 41.45, "lon": -74.0},
        "departure_datetime": _future_departure(),
        "load_weight_lb": 20000,
        "sampling_interval_miles": 25,
    }
    payload.update(overrides)
    return payload


def post(client, payload):
    return client.post(URL, data=json.dumps(payload), content_type="application/json")


@pytest.fixture
def client():
    return Client()


@pytest.fixture(autouse=True)
def mock_weather_series():
    # Every 200-status analyze response now also builds a heatmap, which
    # makes its own get_weather_series call — mocked here once for the
    # whole file so existing tests stay deterministic and don't silently
    # start hitting the real Open-Meteo API.
    with patch("api.views.get_weather_series") as mock:
        mock.side_effect = lambda points, start, hours: [[CALM] * (hours + 1) for _ in points]
        yield mock


@pytest.fixture(autouse=True)
def clear_cache():
    # The geocode suggest/reverse views now cache successful responses
    # (api/views.py) in the process-local "default" LocMemCache, which
    # Django's test runner does not reset between tests on its own. Several
    # tests in this file reuse the same lat/lon or query string across a
    # success case and a failure case — without clearing here, a later
    # failure-path test could read back an earlier test's cached success
    # response instead of actually exercising the mocked failure.
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def test_happy_path_returns_recommendation(client):
    routes = [make_route("route-0"), make_route("route-1"), make_route("route-2")]
    with patch("api.views.get_route_options", return_value=routes), patch(
        "api.views.get_weather_observations"
    ) as mock_weather:
        mock_weather.side_effect = lambda targets: [CALM for _ in targets]
        response = post(client, base_payload())

    assert response.status_code == 200
    body = response.json()
    assert len(body["routes"]) == 3
    assert body["recommendation"]["status"] == "safe"
    assert body["recommendation"]["winner_route_id"] is not None
    assert body["warnings"] == []
    # 100mi / 25mi interval -> 5 checkpoints (0,25,50,75,100) per route
    assert len(body["routes"][0]["checkpoints"]) == 5
    assert body["routes"][0]["checkpoints"][0]["risk"]["level"] == "low"
    assert body["heatmap"]["hour_offsets"] == list(range(49))
    assert len(body["heatmap"]["points"]) > 0
    assert len(body["heatmap"]["points"][0]["risk_by_hour"]) == 49


def test_fewer_than_requested_alternatives_adds_warning(client):
    with patch("api.views.get_route_options", return_value=[make_route("route-0")]), patch(
        "api.views.get_weather_observations", side_effect=lambda targets: [CALM for _ in targets]
    ):
        response = post(client, base_payload())

    assert response.status_code == 200
    assert any("Found 1 genuinely distinct route" in w for w in response.json()["warnings"])


def test_missing_field_returns_400(client):
    payload = base_payload()
    del payload["origin"]
    response = post(client, payload)
    assert response.status_code == 400
    assert "origin" in response.json()


def test_naive_departure_datetime_rejected(client):
    response = post(client, base_payload(departure_datetime="2026-03-01T08:00:00"))
    assert response.status_code == 400
    assert "departure_datetime" in response.json()


def test_invalid_sampling_interval_rejected(client):
    response = post(client, base_payload(sampling_interval_miles=15))
    assert response.status_code == 400
    assert "sampling_interval_miles" in response.json()


def test_routing_provider_failure_returns_502(client):
    with patch("api.views.get_route_options", side_effect=RoutingProviderError("ORS is down")):
        response = post(client, base_payload())
    assert response.status_code == 502
    assert "ORS is down" in response.json()["detail"]


def test_weather_provider_failure_returns_502(client):
    with patch("api.views.get_route_options", return_value=[make_route("route-0")]), patch(
        "api.views.get_weather_observations", side_effect=WeatherProviderError("Open-Meteo timed out")
    ):
        response = post(client, base_payload())
    assert response.status_code == 502
    assert "Open-Meteo timed out" in response.json()["detail"]


def test_all_weather_unavailable_yields_insufficient_data(client):
    with patch("api.views.get_route_options", return_value=[make_route("route-0")]), patch(
        "api.views.get_weather_observations", side_effect=lambda targets: [None for _ in targets]
    ):
        response = post(client, base_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"]["status"] == "insufficient_data"
    assert body["recommendation"]["winner_route_id"] is None
    assert any("unavailable" in w for w in body["warnings"])
    assert body["routes"][0]["rank"] is None


def test_no_travel_route_is_not_recommended_over_a_safe_one(client):
    severe_wind = WeatherObservation(wind_mph=60, rain_in_hr=0, snow_in_hr=0)
    unsafe_route = make_route("unsafe")
    safe_route = make_route("safe")

    def weather_side_effect(targets):
        # unsafe route's checkpoints all come first (route order == target order)
        return [severe_wind] * 5 + [CALM] * 5

    with patch("api.views.get_route_options", return_value=[unsafe_route, safe_route]), patch(
        "api.views.get_weather_observations", side_effect=weather_side_effect
    ):
        response = post(client, base_payload())

    body = response.json()
    assert body["recommendation"]["status"] == "safe"
    assert body["recommendation"]["winner_route_id"] == "safe"
    unsafe_payload = next(r for r in body["routes"] if r["route_id"] == "unsafe")
    assert unsafe_payload["risk_summary"]["has_no_travel"] is True


def test_schema_and_docs_endpoints_are_served(client):
    schema_response = client.get("/api/schema/")
    assert schema_response.status_code == 200

    docs_response = client.get("/api/docs/")
    assert docs_response.status_code == 200


def test_geocode_suggest_returns_candidates(client):
    candidates = [
        GeocodeSuggestion(label="Indianapolis, IN, USA", coordinate=Coordinate(lat=39.79, lon=-86.14)),
        GeocodeSuggestion(label="West Indianapolis, IN, USA", coordinate=Coordinate(lat=39.76, lon=-86.2)),
    ]
    with patch("api.views.suggest", return_value=candidates) as mock_suggest:
        response = client.get("/api/geocode/suggest/", {"q": "Indianap"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0] == {"label": "Indianapolis, IN, USA", "lat": 39.79, "lon": -86.14}
    mock_suggest.assert_called_once_with("Indianap")


def test_geocode_suggest_requires_query_param(client):
    response = client.get("/api/geocode/suggest/")
    assert response.status_code == 400


def test_geocode_suggest_provider_failure_returns_502(client):
    with patch("api.views.suggest", side_effect=RoutingProviderError("ORS is down")):
        response = client.get("/api/geocode/suggest/", {"q": "Chicago"})
    assert response.status_code == 502


def test_geocode_reverse_returns_label(client):
    with patch("api.views.reverse_geocode", return_value="Green District Salads, Indianapolis, IN, USA"):
        response = client.get("/api/geocode/reverse/", {"lat": "39.768", "lon": "-86.158"})

    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "Green District Salads, Indianapolis, IN, USA"
    assert body["lat"] == 39.768
    assert body["lon"] == -86.158


def test_geocode_reverse_requires_lat_lon(client):
    response = client.get("/api/geocode/reverse/", {"lat": "39.768"})
    assert response.status_code == 400


def test_geocode_reverse_rejects_out_of_range_coordinates(client):
    response = client.get("/api/geocode/reverse/", {"lat": "999", "lon": "0"})
    assert response.status_code == 400


def test_geocode_reverse_provider_failure_returns_502(client):
    with patch("api.views.reverse_geocode", side_effect=RoutingProviderError("ORS is down")):
        response = client.get("/api/geocode/reverse/", {"lat": "39.768", "lon": "-86.158"})
    assert response.status_code == 502
