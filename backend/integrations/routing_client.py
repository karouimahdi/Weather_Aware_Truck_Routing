"""
OSRM (routing) + Nominatim (geocoding) client — both free, OSM-based, no
API key. This is the only module that knows either provider's
request/response shape. Everything it returns is translated into
routing/'s provider-agnostic types (Coordinate, DurationBreakpoint) so
the pure engine never touches raw provider JSON, and swapping providers
again would only mean rewriting this file.

Known capability loss versus the previously-used OpenRouteService: OSRM's
public demo server has no truck/HGV profile — only generic car-equivalent
routing. `load_lb` is accepted by get_route_options for interface
stability but is not sent anywhere; there is no weight-restriction
parameter on this provider. This is a real trade-off, not an oversight —
documented here and in the README, not silently absorbed.

Phase 4a (see docs/phase-4-routing-performance.md): the directions request
no longer sends `steps=true`. OSRM always returns per-step geometry at
full resolution regardless of the `overview` setting, which was the
single largest cost on long routes and was never read — only
`step.distance`, `step.duration`, and `step.mode` were. `step.mode` was
the only way this app detected ferry crossings; dropping steps means
ferry detection is gone, not degraded. That's an accepted trade documented
in the Phase 4 review: the assessment brief never mentions ferries, so
losing a beyond-brief feature to measurably speed up every request is the
right call. `annotations=duration,distance` replaces steps as the source
of per-segment data for ETA interpolation — it's tied to the same
full-resolution geometry `overview=full` already requests (so checkpoint
positioning accuracy is untouched, unlike `overview=simplified`, which
was tested and rejected: it moved checkpoints by a measured 3.85mi mean /
8.34mi max on a real route) and is substantially smaller than steps,
since it carries no maneuver text, names, intersections, or duplicate
geometry.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

import requests
from django.conf import settings
from django.core.cache import caches

from routing import similarity
from routing.checkpoints import Coordinate, cumulative_distances, destination_point, haversine_mi, locate_along_route
from routing.eta import DurationBreakpoint

logger = logging.getLogger(__name__)

# Separate from the "default" cache weather_client.py uses — see
# config/settings.py's CACHES comment for why sharing one small eviction
# pool with weather's much higher write volume silently broke this cache.
_route_cache = caches["routes"]

METERS_PER_MILE = 1609.344
GEOCODE_TIMEOUT_S = 10
# One retry follows a failed attempt (see _request_osrm_directions), so
# the per-attempt timeout is lower than the old single-shot 30s — worst
# case across both attempts is still bounded at ~30s, not left to compound
# to ~47s+ by adding a second full-length attempt on top of the first.
DIRECTIONS_TIMEOUT_S = 15
DIRECTIONS_MAX_ATTEMPTS = 2
DIRECTIONS_RETRY_DELAY_S = 0.5

# OSRM's road network doesn't change mid-session; this is a same-session/
# same-evaluation-pass optimization (a resubmitted or re-tested trip is
# common and currently re-fetches from OSRM every time, at full cost,
# even though load_weight_lb — the only other input — isn't sent to OSRM
# at all), not a claim that routes are valid for a day. The "routes"
# LocMemCache alias (config/settings.py) is process-local like every
# LocMemCache: this helps every request in a single-process dev/eval run,
# less in a multi-worker production deployment, where a hit in one worker
# doesn't help a request landed on another.
ROUTE_CACHE_TTL_S = 3600
ROUTE_CACHE_COORD_DECIMALS = 5  # ~1m precision — effectively exact-match only

# Nominatim's usage policy requires requests to identify the calling
# application (and asks for well under 1 req/sec — see the frontend's
# debounce delay for how that's respected without a server-side limiter,
# which would be overbuilt for this app's single-user scale).
NOMINATIM_USER_AGENT = "WeatherAwareTruckRouting/1.0 (assessment project)"

# OSRM codes that mean "we understood the request and confidently found no
# route" — as opposed to a malformed request or a provider/network failure.
# See https://project-osrm.org/docs/v5.24.0/api/#responses — NoRoute is "no
# route found between the given coordinates", NoSegment is "one of the
# supplied coordinates could not snap to the street network at all".
_NO_ROUTE_CODES = {"NoRoute", "NoSegment"}

# OSRM silently snaps a coordinate to the nearest node it can find even when
# that node is implausibly far away, rather than failing — observed live: a
# Manhattan origin with no reachable transatlantic road network snapped to
# Lisbon, Portugal (5,535 km away) and OSRM still returned `code: "Ok"` with
# a full (bogus) 2,150 km "route". `waypoints[].distance` is OSRM's own
# snap-distance-in-meters field; treating a huge snap as "no real route"
# is what catches this rather than rendering a fabricated route. A few
# miles comfortably covers a legitimate rural/remote pickup point.
MAX_SNAP_DISTANCE_MI = 15.0

# Phase 4b (see docs/phase-4-routing-performance.md §6.B/C, §8): OSRM's
# `alternatives=true` often returns fewer than 3 routes — it has no knob
# to ask for more. When that happens, this module tries a bounded number
# of fallback requests through a synthetic via point offset to either
# side of the main route's midpoint, same technique used by every
# comparable reference implementation inspected for this feature
# (malikali3419/truck-weather-routing, swatiXO/weather-aware-truck-routing,
# MTahir27/weather-awear — see the Phase 4 doc for what was verified vs.
# assumed from each). Not a hardcoded 3 countries/continents classifier —
# this never looks at where the points are, only at the geometry OSRM
# actually returns.
#
# This repo's deployment differs from all three reference repos in one
# way that changes how aggressive this can safely be: they use OSRM's
# public demo server as an occasional *fallback* behind a paid, keyed
# primary provider, while this app uses it as the *primary* for every
# single request. The demo server's own usage policy asks for ~1 request/
# second and documents no uptime/latency SLA at all. Firing unlimited
# concurrent fallback requests on top of the one already sent would risk
# that policy far more than it does for a repo that rarely touches this
# server. Deliberately conservative limits follow from that: only ONE
# fallback round (not malikali3419's escalating multiple rounds), and at
# most 2 concurrent requests in flight at any moment, not 3+.
TARGET_ROUTES = 3
# A single midpoint anchor (the prior version of this constant set) tries
# only 2 via candidates and can miss a real third route entirely — verified
# live on a Sousse->Tunis request: both candidates at the midpoint were
# correctly rejected (one a near-duplicate of the main route, one 74%
# slower), yet a wider sweep of anchor/offset combinations on the same live
# route found 11 of 40 combinations were genuine, reasonably-paced, distinct
# routes. Two anchors at 35%/65% of the route, offset 22% of straight-line
# distance — malikali3419's own validated round-1 values — are confirmed
# live to recover that exact case (both accepted, ratio 1.43 and 1.22) while
# every other previously-correct trip in this review's test set still
# behaves the same or better. Still one bounded round: 4 via requests total,
# not 6 (dropping the plain midpoint anchor rather than adding a 3rd).
VIA_ANCHOR_FRACTIONS = (0.35, 0.65)  # of main route's distance_mi
VIA_OFFSET_FRACTION = 0.22  # of straight-line trip distance, each side
VIA_OFFSET_MIN_MI = 9.3  # ~15km
VIA_OFFSET_MAX_MI = 93.2  # ~150km
VIA_FALLBACK_MAX_WORKERS = 2

# Up to 8 via requests total now, not 4: any anchor whose primary offset
# fails to fetch a route gets one retry at VIA_OFFSET_MIN_MI (see
# _add_via_fallback_routes' fetch() closure) — confirmed live this
# recovers real candidates near a water crossing where the primary offset
# lands off the routable network entirely. Still sequential per anchor,
# still capped at VIA_FALLBACK_MAX_WORKERS=2 concurrent at any instant —
# the retry only ever adds to the *count* of requests across the one
# bounded round, never to how many are in flight together, which is the
# dimension the OSRM demo server's rate policy actually cares about.


class RoutingProviderError(Exception):
    """The provider itself failed: network error, timeout, malformed request,
    rate limit, or an unparseable/unexpected response. Not evidence the
    locations are actually unreachable — see RouteNotFoundError for that."""


class RouteNotFoundError(RoutingProviderError):
    """The provider reliably determined no drivable route exists for these
    coordinates and profile. A subclass of RoutingProviderError (so any
    existing blanket `except RoutingProviderError` still catches it) but
    callers that need to tell "no route" apart from "the provider is
    broken" — to pick the right HTTP status and message — should catch
    this first."""


@dataclass(frozen=True)
class RouteOption:
    route_id: str
    polyline: list[Coordinate]
    distance_mi: float
    duration_s: float
    duration_breakpoints: list[DurationBreakpoint]


@dataclass(frozen=True)
class GeocodeSuggestion:
    label: str
    coordinate: Coordinate


def _nominatim_get(path: str, params: dict, error_context: str) -> dict:
    try:
        response = requests.get(
            f"{settings.NOMINATIM_BASE_URL}{path}",
            params={**params, "format": "jsonv2"},
            headers={"User-Agent": NOMINATIM_USER_AGENT},
            timeout=GEOCODE_TIMEOUT_S,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RoutingProviderError(f"{error_context} failed") from exc
    return response.json()


def suggest(query: str, limit: int = 5) -> list[GeocodeSuggestion]:
    """
    Ranked place candidates for a partial, as-you-type query — the backing
    call for a search-as-you-type input. Returns a list rather than a
    single best guess: a human looking at real candidates and picking one
    is what prevents a bad match from silently reaching routing (see
    AnalyzeRoutesView's docstring for the incident this policy exists
    because of).

    Known limitation: Nominatim's /search isn't a dedicated autocomplete
    endpoint — it ranks complete-ish place names well but is noticeably
    weaker on short prefixes (e.g. "tun") than a purpose-built autocomplete
    API. Not papered over; the frontend's minimum query length and
    debounce are tuned around this.
    """
    data = _nominatim_get("/search", {"q": query, "limit": limit}, f"search for {query!r}")
    results = []
    for item in data:
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        label = item.get("display_name", query)
        results.append(GeocodeSuggestion(label=label, coordinate=Coordinate(lat=lat, lon=lon)))
    return results


def reverse_geocode(coordinate: Coordinate) -> str:
    """Human-readable label for a point the user picked directly on the map."""
    data = _nominatim_get(
        "/reverse", {"lat": coordinate.lat, "lon": coordinate.lon}, "reverse geocoding"
    )
    label = data.get("display_name")
    if not label:
        return f"{coordinate.lat:.5f}, {coordinate.lon:.5f}"
    return label


def get_route_options(
    origin: Coordinate,
    destination: Coordinate,
    load_lb: float,  # kept for interface stability, see module docstring — unused, no HGV weight param on this provider
    max_alternatives: int = 3,
) -> list[RouteOption]:
    """
    OSRM's `alternatives=true` returns however many genuinely different
    routes it finds in one call — unlike OpenRouteService's native
    alternative-routes feature, it has no short-trip-only distance cap, so
    no multi-request workaround is needed here. It may still return fewer
    than `max_alternatives` (commonly 1-2, not always 3); the caller must
    report that honestly, never pad the list.

    The full fetched set (independent of `max_alternatives`, which OSRM's
    `alternatives=true` doesn't take a count for anyway — slicing happens
    here, client-side) is what gets cached, keyed only on origin/
    destination, so the cache stays correct regardless of how many
    alternatives a given caller asks for.
    """
    key = _route_cache_key(origin, destination)
    routes = _route_cache.get(key)
    if routes is None:
        routes = _fetch_route_options(origin, destination)
        _route_cache.set(key, routes, ROUTE_CACHE_TTL_S)
    return routes[:max_alternatives]


def _route_cache_key(origin: Coordinate, destination: Coordinate) -> str:
    return (
        f"osrm_route:{round(origin.lat, ROUTE_CACHE_COORD_DECIMALS)}:"
        f"{round(origin.lon, ROUTE_CACHE_COORD_DECIMALS)}:"
        f"{round(destination.lat, ROUTE_CACHE_COORD_DECIMALS)}:"
        f"{round(destination.lon, ROUTE_CACHE_COORD_DECIMALS)}"
    )


def _fetch_route_options(origin: Coordinate, destination: Coordinate) -> list[RouteOption]:
    coordinates = f"{origin.lon},{origin.lat};{destination.lon},{destination.lat}"
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{coordinates}"
    params = {
        "alternatives": "true",
        "overview": "full",
        "geometries": "geojson",
        "annotations": "duration,distance",
    }

    response = _request_osrm_directions(url, params)

    try:
        data = response.json()
    except ValueError as exc:
        logger.warning(
            "OSRM returned a non-JSON response (HTTP %s): %s",
            response.status_code,
            response.text[:500],
        )
        raise RoutingProviderError(
            f"routing provider returned an unreadable response (HTTP {response.status_code})"
        ) from exc

    code = data.get("code")

    if code in _NO_ROUTE_CODES:
        logger.info("OSRM found no route (code=%s): %s", code, data.get("message"))
        raise RouteNotFoundError(
            data.get("message") or "no route found for the selected locations and profile"
        )

    if code != "Ok":
        # InvalidQuery / InvalidValue / InvalidUrl / InvalidService / etc —
        # a request-construction or service-level problem, not evidence the
        # locations are unreachable.
        logger.warning(
            "OSRM error (HTTP %s): code=%s message=%s",
            response.status_code,
            code,
            data.get("message"),
        )
        raise RoutingProviderError(f"routing provider error: {code or response.status_code}")

    routes = data.get("routes", [])
    if not routes:
        raise RouteNotFoundError("routing provider returned no routes")

    _check_waypoints_snapped_nearby(data.get("waypoints", []), [origin, destination])

    native_routes = [_parse_osrm_route(i, route) for i, route in enumerate(routes)]
    selected = _dedupe_routes(native_routes)
    if len(selected) < TARGET_ROUTES:
        selected = _add_via_fallback_routes(selected, origin, destination)
    ranked = sorted(selected, key=lambda r: r.duration_s)
    # Every via-point fallback is fetched as its own single-route request
    # and so comes back locally indexed "route-0" — reassign ids here,
    # after native and fallback routes are merged and sorted, so the final
    # list the caller sees never has two routes sharing an id (route_id is
    # a React key and the field recommend.py's winner_route_id matches on).
    return [replace(route, route_id=f"route-{i}") for i, route in enumerate(ranked)]


def _dedupe_routes(routes: list[RouteOption]) -> list[RouteOption]:
    """OSRM's own "alternatives" have occasionally been near-duplicates of
    each other in testing — never pad the result with one, even when it
    came from OSRM itself rather than a fallback via point."""
    kept: list[RouteOption] = []
    for route in routes:
        if similarity.is_distinct(route.polyline, [r.polyline for r in kept]):
            kept.append(route)
    return kept


def _add_via_fallback_routes(
    selected: list[RouteOption], origin: Coordinate, destination: Coordinate
) -> list[RouteOption]:
    """
    One bounded round of fallback requests through synthetic via points
    offset to either side of the main route at two points along it (see
    VIA_ANCHOR_FRACTIONS) — see the TARGET_ROUTES comment above for why
    this is one round of ≤2 concurrent requests at a time (4 total, not
    unbounded), not the multi-round escalation some reference
    implementations use.
    """
    if not selected:
        return selected  # the caller's earlier empty-routes check already handles this; defensive only

    main = selected[0]
    straight_mi = haversine_mi(origin, destination)
    offset_mi = min(max(straight_mi * VIA_OFFSET_FRACTION, VIA_OFFSET_MIN_MI), VIA_OFFSET_MAX_MI)
    cumulative = cumulative_distances(main.polyline)
    via_anchors: list[tuple[Coordinate, float, float]] = []  # (anchor, bearing, +/-90 sign)
    for fraction in VIA_ANCHOR_FRACTIONS:
        anchor, bearing = locate_along_route(main.polyline, cumulative, main.distance_mi * fraction)
        via_anchors.append((anchor, bearing, 90.0))
        via_anchors.append((anchor, bearing, -90.0))

    def fetch(via_anchor: tuple[Coordinate, float, float]) -> RouteOption | None:
        anchor, bearing, sign = via_anchor
        via = destination_point(anchor, bearing + sign, offset_mi)
        route = _fetch_via_route(origin, via, destination)
        if route is None and offset_mi > VIA_OFFSET_MIN_MI:
            # The primary offset fails this way most often near a
            # coastline, lake, or other obstacle — the straight-line
            # fraction that sets offset_mi has no idea what's actually
            # beside the route it's offsetting from, and can push the via
            # point somewhere nothing snaps nearby. One retry at the
            # minimum offset (much closer to the route itself) recovers a
            # real, successfully-fetched candidate in exactly that case —
            # confirmed live on a Brussels->London request where 3 of 4
            # primary offsets landed in the North Sea/Channel and all 3
            # succeeded at VIA_OFFSET_MIN_MI. That specific trip still
            # correctly tops out at 2 routes overall, not 3 — the recovered
            # candidates get a fair shot at the same is_distinct/
            # has_backtrack_spur/MAX_DURATION_RATIO checks below as any
            # other candidate, and two of them were then correctly rejected
            # as near-duplicates of the one already-accepted route, which
            # is this app's existing, deliberate "never pad with a
            # near-duplicate" behavior, not a bug this retry should (or
            # does) override. The win here is giving every geometrically
            # plausible candidate a fair evaluation instead of losing it to
            # an accident of which side of the route the offset landed on
            # — most route pairs aren't hugging a water crossing, so this
            # recovers real alternatives there instead. Sequential, not
            # concurrent, so this never raises how many requests are in
            # flight at once.
            via = destination_point(anchor, bearing + sign, VIA_OFFSET_MIN_MI)
            route = _fetch_via_route(origin, via, destination)
        if route is not None and similarity.has_backtrack_spur(route.polyline, via):
            return None
        return route

    with ThreadPoolExecutor(max_workers=VIA_FALLBACK_MAX_WORKERS) as pool:
        candidates = [route for route in pool.map(fetch, via_anchors) if route is not None]

    fastest_s = min(r.duration_s for r in selected)
    selected = list(selected)
    for candidate in sorted(candidates, key=lambda r: r.duration_s):
        if len(selected) >= TARGET_ROUTES:
            break
        if candidate.duration_s > fastest_s * similarity.MAX_DURATION_RATIO:
            continue
        if similarity.is_distinct(candidate.polyline, [r.polyline for r in selected]):
            selected.append(candidate)
    return selected


def _fetch_via_route(origin: Coordinate, via: Coordinate, destination: Coordinate) -> RouteOption | None:
    """A synthetic via point isn't guaranteed to be anywhere near a road
    the way a user-picked origin/destination is — unlike get_route_options'
    own waypoints, a bad snap here just means this one candidate doesn't
    work, not that no route exists between origin and destination at all
    (the main request already proved one does)."""
    coordinates = f"{origin.lon},{origin.lat};{via.lon},{via.lat};{destination.lon},{destination.lat}"
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{coordinates}"
    params = {
        "alternatives": "false",
        "overview": "full",
        "geometries": "geojson",
        "annotations": "duration,distance",
    }
    try:
        response = _request_osrm_directions(url, params)
        data = response.json()
    except (RoutingProviderError, ValueError) as exc:
        logger.info("Via-point fallback request failed, skipping candidate: %s", exc)
        return None

    if data.get("code") != "Ok":
        return None
    routes = data.get("routes", [])
    waypoints = data.get("waypoints", [])
    if not routes or len(waypoints) != 3:
        return None
    via_snap_mi = waypoints[1].get("distance", 0) / METERS_PER_MILE
    if via_snap_mi > MAX_SNAP_DISTANCE_MI:
        return None

    try:
        return _parse_osrm_route(0, routes[0])
    except RoutingProviderError:
        return None


def _request_osrm_directions(url: str, params: dict) -> requests.Response:
    """
    One bounded retry — only on a network error or a 5xx, never on a 4xx —
    since OSRM's demo server documents no uptime/latency SLA ("we provide
    no guarantees wrt. uptime, latency, or data updates") and a single
    transient blip is plausible and was previously indistinguishable from
    a persistent outage. A 4xx means OSRM understood and rejected the
    request; retrying it unchanged won't fix that.

    Deliberately not response.raise_for_status(): OSRM puts its actual
    diagnosis (code + message) in the JSON body even on a 4xx, and that
    body is the evidence the caller's whole job is to read correctly —
    raising on status first would throw it away.
    """
    last_exc: requests.RequestException | None = None
    for attempt in range(1, DIRECTIONS_MAX_ATTEMPTS + 1):
        is_last_attempt = attempt == DIRECTIONS_MAX_ATTEMPTS
        try:
            response = requests.get(url, params=params, timeout=DIRECTIONS_TIMEOUT_S)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning(
                "OSRM request failed (attempt %d/%d): %s", attempt, DIRECTIONS_MAX_ATTEMPTS, exc
            )
        else:
            if response.status_code < 500 or is_last_attempt:
                return response
            logger.warning(
                "OSRM returned HTTP %d (attempt %d/%d)",
                response.status_code,
                attempt,
                DIRECTIONS_MAX_ATTEMPTS,
            )
        if not is_last_attempt:
            time.sleep(DIRECTIONS_RETRY_DELAY_S)

    raise RoutingProviderError(
        "directions request failed — the routing service may be unavailable"
    ) from last_exc


def _check_waypoints_snapped_nearby(waypoints: list[dict], requested: list[Coordinate]) -> None:
    """
    Guards against the silent-mismatch failure mode described in this
    module's MAX_SNAP_DISTANCE_MI comment: `code: "Ok"` alone is not proof
    the route is real, because OSRM will snap an unroutable point to
    whatever node is nearest *anywhere in its graph* instead of failing.
    """
    for waypoint, point in zip(waypoints, requested):
        snap_distance_mi = waypoint.get("distance", 0) / METERS_PER_MILE
        if snap_distance_mi > MAX_SNAP_DISTANCE_MI:
            logger.info(
                "OSRM snapped (%s, %s) to a point %.0f mi away — treating as no route",
                point.lat,
                point.lon,
                snap_distance_mi,
            )
            raise RouteNotFoundError(
                f"no routable road network found near ({point.lat:.4f}, {point.lon:.4f})"
            )


def _parse_osrm_route(index: int, route: dict) -> RouteOption:
    try:
        coords = route["geometry"]["coordinates"]
        polyline = [Coordinate(lat=lat, lon=lon) for lon, lat in coords]

        distance_mi = route["distance"] / METERS_PER_MILE
        duration_s = float(route["duration"])

        segment_durations_s = [
            d for leg in route.get("legs", []) for d in leg["annotation"]["duration"]
        ]
        segment_distances_m = [
            d for leg in route.get("legs", []) for d in leg["annotation"]["distance"]
        ]
        breakpoints = _build_duration_breakpoints(
            segment_distances_m, segment_durations_s, distance_mi, duration_s
        )
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise RoutingProviderError(f"malformed route at index {index}: {exc}") from exc

    return RouteOption(
        route_id=f"route-{index}",
        polyline=polyline,
        distance_mi=distance_mi,
        duration_s=duration_s,
        duration_breakpoints=breakpoints,
    )


def _build_duration_breakpoints(
    segment_distances_m: list[float],
    segment_durations_s: list[float],
    total_distance_mi: float,
    total_duration_s: float,
) -> list[DurationBreakpoint]:
    """
    One breakpoint per geometry vertex (via `annotations=duration,distance`,
    paired 1:1 with the route's `overview=full` geometry) — finer-grained
    than the old per-turn-step breakpoints, since a step can span many
    vertices. The final point is anchored to the route's authoritative
    distance/duration via max(), not a blind overwrite: summing many small
    per-segment values can drift a hair past or short of the route's
    reported total (measured with two different providers now — the same
    float-summation risk recurs because it's inherent to summing floats,
    not a quirk of one provider), and max() is correct regardless of which
    way a given route's rounding drifts — see eta.py's non-decreasing
    invariant.
    """
    breakpoints = [DurationBreakpoint(distance_mi=0.0, cumulative_duration_s=0.0)]
    cum_distance = 0.0
    cum_duration = 0.0
    for dist_m, dur_s in zip(segment_distances_m, segment_durations_s):
        cum_distance += dist_m / METERS_PER_MILE
        cum_duration += dur_s
        breakpoints.append(
            DurationBreakpoint(distance_mi=cum_distance, cumulative_duration_s=cum_duration)
        )

    final = DurationBreakpoint(
        distance_mi=max(total_distance_mi, cum_distance),
        cumulative_duration_s=max(total_duration_s, cum_duration),
    )
    if len(breakpoints) == 1:
        breakpoints.append(final)
    else:
        breakpoints[-1] = final
    return breakpoints
