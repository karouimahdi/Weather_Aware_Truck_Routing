"""
Open-Meteo client: hourly wind/rain/snow forecast. get_weather_observations
batches every checkpoint in one HTTP call, deduplicated by (coordinate
rounded to ~0.1 degrees, nearest forecast hour); get_weather_series fetches
every hour in a window for the heatmap grid instead of one nearest-hour
value per point. Both share one process-local cache so repeated or nearby
lookups don't refetch identical data.

No API key needed. Units (mph, inches) are requested directly from the
provider, so there is exactly one place a unit conversion could go wrong:
never — we don't do any.

A checkpoint whose ETA falls outside the forecast horizon the provider
actually returned comes back as None ("unavailable"), never backfilled
with today's weather.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests
from django.conf import settings
from django.core.cache import cache

from routing.checkpoints import Coordinate
from routing.risk import WeatherObservation

# A batch covering ~100 checkpoints across 3 long-haul routes normally
# returns in under a second, but a real analysis hit a 10s timeout once
# under transient network jitter — 10s leaves no margin for a single slow
# response on an external dependency.
REQUEST_TIMEOUT_S = 25
COORD_ROUNDING_DECIMALS = 1  # ~0.1 degree cache-key granularity
CACHE_TTL_S = 1800
MIN_FORECAST_DAYS = 3
MAX_FORECAST_DAYS = 16  # Open-Meteo's own ceiling

RoundedCoord = tuple[float, float]


class WeatherProviderError(Exception):
    """Open-Meteo failed, timed out, or returned a response we can't parse."""


def get_weather_observations(
    targets: list[tuple[Coordinate, datetime]]
) -> list[WeatherObservation | None]:
    """One result per (coordinate, eta_utc) input, in the same order."""
    if not targets:
        return []

    keys = [(_round(coord), _nearest_hour(eta)) for coord, eta in targets]

    results: dict[tuple[RoundedCoord, datetime], WeatherObservation | None] = {}
    missing: set[tuple[RoundedCoord, datetime]] = set()
    for key in keys:
        cached = cache.get(_cache_key(*key), "MISS")
        if cached == "MISS":
            missing.add(key)
        else:
            results[key] = cached

    if missing:
        fetched = _fetch_batch(list(missing))
        for key, observation in fetched.items():
            cache.set(_cache_key(*key), observation, CACHE_TTL_S)
            results[key] = observation

    return [results[key] for key in keys]


def get_weather_series(
    coordinates: list[Coordinate], start_utc: datetime, hours: int
) -> list[list[WeatherObservation | None]]:
    """
    Full hourly series per coordinate, from start_utc's hour through
    start_utc + `hours` hours inclusive (hours+1 entries per coordinate) —
    the heatmap's data source. Distinct from get_weather_observations:
    that returns one nearest-hour value per (coordinate, time) *pair*;
    this returns every hour in a window for a list of *places* that don't
    carry individual times of their own.

    Writes into the same (rounded-coordinate, hour) cache entries
    get_weather_observations uses, so an hour already fetched for a
    nearby checkpoint is reused here for free, and vice versa for future
    analyses — it doesn't read the cache first, to keep this one request
    rather than a batch reconciled against partial hits.
    """
    if not coordinates or hours < 0:
        return []

    start_hour = _nearest_hour(start_utc)
    hour_offsets = [start_hour + timedelta(hours=h) for h in range(hours + 1)]
    unique_coords = sorted({_round(c) for c in coordinates})
    forecast_days = min(
        MAX_FORECAST_DAYS,
        max(MIN_FORECAST_DAYS, (hour_offsets[-1] - _today_utc_midnight()).days + 1),
    )

    series_by_coord = _fetch_hourly_series_for_coords(unique_coords, forecast_days)
    for coord, series in series_by_coord.items():
        for hour, observation in series.items():
            cache.set(_cache_key(coord, hour), observation, CACHE_TTL_S)

    return [
        [series_by_coord.get(_round(coordinate), {}).get(hour) for hour in hour_offsets]
        for coordinate in coordinates
    ]


def _fetch_batch(
    keys: list[tuple[RoundedCoord, datetime]]
) -> dict[tuple[RoundedCoord, datetime], WeatherObservation | None]:
    unique_coords = sorted({coord for coord, _ in keys})
    latest_hour = max(hour for _, hour in keys)
    forecast_days = min(
        MAX_FORECAST_DAYS,
        max(MIN_FORECAST_DAYS, (latest_hour - _today_utc_midnight()).days + 1),
    )
    series_by_coord = _fetch_hourly_series_for_coords(unique_coords, forecast_days)
    return {(coord, hour): series_by_coord.get(coord, {}).get(hour) for coord, hour in keys}


def _fetch_hourly_series_for_coords(
    unique_coords: list[RoundedCoord], forecast_days: int
) -> dict[RoundedCoord, dict[datetime, WeatherObservation]]:
    params = {
        "latitude": ",".join(str(lat) for lat, _ in unique_coords),
        "longitude": ",".join(str(lon) for _, lon in unique_coords),
        "hourly": "wind_speed_10m,rain,snowfall",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "UTC",
        "forecast_days": forecast_days,
    }
    try:
        response = requests.get(
            settings.OPEN_METEO_BASE_URL, params=params, timeout=REQUEST_TIMEOUT_S
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise WeatherProviderError(f"weather request failed: {exc}") from exc

    data = response.json()
    # Open-Meteo returns a single object for 1 coordinate, a list for >1.
    locations = data if isinstance(data, list) else [data]
    if len(locations) != len(unique_coords):
        raise WeatherProviderError(
            f"expected {len(unique_coords)} locations in response, got {len(locations)}"
        )

    return {unique_coords[i]: _parse_hourly_series(locations[i]) for i in range(len(unique_coords))}


def _parse_hourly_series(location: dict) -> dict[datetime, WeatherObservation]:
    try:
        hourly = location["hourly"]
        times, winds, rains, snows = (
            hourly["time"],
            hourly["wind_speed_10m"],
            hourly["rain"],
            hourly["snowfall"],
        )
    except KeyError as exc:
        raise WeatherProviderError(f"malformed weather response: missing {exc}") from exc

    series = {}
    for time_str, wind, rain, snow in zip(times, winds, rains, snows):
        if wind is None or rain is None or snow is None:
            continue  # provider gap for this hour — treated as "no data", not zero
        hour = datetime.fromisoformat(time_str).replace(tzinfo=timezone.utc)
        series[hour] = WeatherObservation(wind_mph=wind, rain_in_hr=rain, snow_in_hr=snow)
    return series


def _round(coordinate: Coordinate) -> RoundedCoord:
    return (round(coordinate.lat, COORD_ROUNDING_DECIMALS), round(coordinate.lon, COORD_ROUNDING_DECIMALS))


def _nearest_hour(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    if dt.minute >= 30:
        dt = dt + timedelta(hours=1)
    return dt.replace(minute=0, second=0, microsecond=0)


def _cache_key(coord: RoundedCoord, hour: datetime) -> str:
    return f"weather:{coord[0]}:{coord[1]}:{hour.isoformat()}"


def _today_utc_midnight() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
