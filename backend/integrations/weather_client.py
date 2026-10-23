"""
Open-Meteo client: hourly wind/rain/snow forecast. get_weather_observations
batches every checkpoint in one HTTP call, deduplicated by (coordinate
rounded to ~0.1 degrees, nearest forecast hour); get_weather_series fetches
every hour in a window for the heatmap grid instead of one nearest-hour
value per point. Both share one process-local cache so repeated or nearby
lookups don't refetch identical data.

No API key needed. Units (mph, inches) are requested directly from the
provider, so there is exactly one place a unit conversion could go wrong:
never — we don't do any, for this provider.

Fallback to MET Norway's Locationforecast API (also free, keyless) when
Open-Meteo is rate limited or down — confirmed live that Open-Meteo's
free tier rate-limits by IP, and a shared-IP host (Render's free tier,
this app's own deployment target) can get 429s from traffic that has
nothing to do with this app's own request volume. Rather than trade that
away for a provider with a hard US-only coverage limit (NOAA/NWS was
tried and reverted — global coverage mattered more here), this mirrors
the pattern a comparable reference implementation
(malikali3419/truck-weather-routing) already ships: try Open-Meteo
first, and on a 429/5xx/timeout, switch to MET Norway for that request
*and* cache a short "primary blocked" flag so the next several requests
go straight to the fallback instead of re-spending a request (and
seconds) on a primary that's still rate limited. The flag expires on its
own and Open-Meteo gets tried again after that.

A checkpoint whose ETA falls outside the forecast horizon either
provider actually returned comes back as None ("unavailable"), never
backfilled with today's weather.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from django.conf import settings
from django.core.cache import cache

from routing.checkpoints import Coordinate
from routing.risk import WeatherObservation

logger = logging.getLogger(__name__)

# A batch covering ~100 checkpoints across 3 long-haul routes normally
# returns in under a second, but a real analysis hit a 10s timeout once
# under transient network jitter — 10s leaves no margin for a single slow
# response on an external dependency.
REQUEST_TIMEOUT_S = 25
COORD_ROUNDING_DECIMALS = 1  # ~0.1 degree cache-key granularity
CACHE_TTL_S = 1800
MIN_FORECAST_DAYS = 3
MAX_FORECAST_DAYS = 16  # Open-Meteo's own ceiling

# After a primary failure, skip it for a while instead of spending a
# request (and seconds) on it for every trip in the meantime: longer for
# a confirmed rate limit, which only clears on Open-Meteo's own schedule,
# shorter for a timeout/outage, which more often clears within minutes.
PRIMARY_BLOCKED_CACHE_KEY = "weather:primary-blocked"
PRIMARY_BLOCKED_FOR_RATE_LIMIT_S = 10 * 60
PRIMARY_BLOCKED_FOR_OUTAGE_S = 2 * 60

# MET Norway's Locationforecast: free, global, keyless, but one location
# per request (no batching), so points are fetched concurrently instead.
# Its usage policy asks for an identifying User-Agent — same requirement
# Nominatim already gets in routing_client.py.
MET_NORWAY_BASE_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
MET_NORWAY_USER_AGENT = "WeatherAwareTruckRouting/1.0 (assessment project)"
MET_NORWAY_MAX_WORKERS = 8
MPH_PER_MS = 2.23694
MM_PER_INCH = 25.4
SNOW_TEMP_C = 0.5  # at or below this, MET's precipitation is treated as snow, not rain
SNOW_CM_PER_MM = 0.7  # Open-Meteo's own water-to-snow convention, reused for consistency

RoundedCoord = tuple[float, float]


class WeatherProviderError(Exception):
    """Both weather providers failed, timed out, or returned a response we
    can't parse."""


class _PrimaryRetryableError(WeatherProviderError):
    """Open-Meteo failed in a way the MET Norway fallback might not —
    rate limited, erroring server-side, or unreachable. Any other error
    (a malformed response, an unexpected request shape) means retrying
    with a different provider wouldn't help, so it's raised as a plain
    WeatherProviderError instead and never reaches this branch."""

    def __init__(self, message: str, *, rate_limited: bool):
        super().__init__(message)
        self.rate_limited = rate_limited


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
    if not cache.get(PRIMARY_BLOCKED_CACHE_KEY):
        try:
            return _fetch_open_meteo_series(unique_coords, forecast_days)
        except _PrimaryRetryableError as exc:
            logger.warning("Open-Meteo unavailable (%s), falling back to MET Norway", exc)
            blocked_for = (
                PRIMARY_BLOCKED_FOR_RATE_LIMIT_S if exc.rate_limited else PRIMARY_BLOCKED_FOR_OUTAGE_S
            )
            cache.set(PRIMARY_BLOCKED_CACHE_KEY, True, blocked_for)
    return _fetch_met_norway_series(unique_coords)


def _fetch_open_meteo_series(
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
        response = requests.get(settings.OPEN_METEO_BASE_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    except requests.RequestException as exc:
        raise _PrimaryRetryableError(f"Open-Meteo request failed: {exc}", rate_limited=False) from exc

    if response.status_code == 429:
        raise _PrimaryRetryableError("Open-Meteo rate limit reached", rate_limited=True)
    if response.status_code >= 500:
        raise _PrimaryRetryableError(
            f"Open-Meteo returned HTTP {response.status_code}", rate_limited=False
        )
    if response.status_code >= 400:
        # A 4xx other than 429 means this app built a bad request — a
        # different provider wouldn't fix that, so this is a hard failure,
        # not a fallback trigger.
        raise WeatherProviderError(f"Open-Meteo request error: HTTP {response.status_code}")

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


def _fetch_met_norway_series(
    unique_coords: list[RoundedCoord],
) -> dict[RoundedCoord, dict[datetime, WeatherObservation]]:
    with ThreadPoolExecutor(max_workers=min(MET_NORWAY_MAX_WORKERS, len(unique_coords))) as pool:
        series_list = list(pool.map(_fetch_met_norway_point, unique_coords))
    return dict(zip(unique_coords, series_list))


def _fetch_met_norway_point(coord: RoundedCoord) -> dict[datetime, WeatherObservation]:
    lat, lon = coord
    try:
        response = requests.get(
            MET_NORWAY_BASE_URL,
            params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"},
            headers={"User-Agent": MET_NORWAY_USER_AGENT},
            timeout=REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise WeatherProviderError(f"backup weather request failed: {exc}") from exc

    try:
        return _parse_met_norway_series(response.json())
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherProviderError(f"malformed backup weather response: {exc}") from exc


def _parse_met_norway_series(data: dict) -> dict[datetime, WeatherObservation]:
    """
    MET reports precipitation for the period that STARTS at each entry's
    timestamp (next 1h near-term, next 6h — averaged down to an hourly
    rate — further out). WeatherObservation (and Open-Meteo's own
    convention) wants the rate for the hour that ENDS at a given
    timestamp, so each period's rate is attributed to the *following*
    entry, not the one it's nested under. Precipitation type (rain vs.
    snow) is inferred from that same preceding entry's temperature —
    MET's compact product doesn't separate them itself.
    """
    entries = data["properties"]["timeseries"]
    times: list[datetime] = []
    winds_mph: list[float] = []
    temps_c: list[float] = []
    precip_rates_mm_hr: list[float] = []  # rate for the period starting at this entry

    for entry in entries:
        details = entry["data"]["instant"]["details"]
        times.append(datetime.fromisoformat(entry["time"].replace("Z", "+00:00")).astimezone(timezone.utc))
        winds_mph.append(details.get("wind_speed", 0.0) * MPH_PER_MS)
        temps_c.append(details.get("air_temperature", 10.0))

        forecast_data = entry["data"]
        if "next_1_hours" in forecast_data:
            rate = forecast_data["next_1_hours"]["details"].get("precipitation_amount", 0.0)
        elif "next_6_hours" in forecast_data:
            rate = forecast_data["next_6_hours"]["details"].get("precipitation_amount", 0.0) / 6
        else:
            rate = 0.0
        precip_rates_mm_hr.append(rate)

    series: dict[datetime, WeatherObservation] = {}
    for i, hour in enumerate(times):
        j = max(i - 1, 0)  # the period ending at i is the one that started at i-1
        mm_per_hr = precip_rates_mm_hr[j]
        if temps_c[j] <= SNOW_TEMP_C:
            rain_in_hr, snow_in_hr = 0.0, (mm_per_hr * SNOW_CM_PER_MM) / 2.54
        else:
            rain_in_hr, snow_in_hr = mm_per_hr / MM_PER_INCH, 0.0
        series[hour] = WeatherObservation(wind_mph=winds_mph[i], rain_in_hr=rain_in_hr, snow_in_hr=snow_in_hr)
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
