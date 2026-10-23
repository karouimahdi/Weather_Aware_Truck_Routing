"""
POST /api/routes/analyze/

Orchestrates the full pipeline for one trip: up to 3 route alternatives ->
checkpoints every N miles -> per-checkpoint ETA -> one batched weather
lookup across every checkpoint on every route -> risk classification ->
per-route aggregation -> lexicographic recommendation. Also builds the
0-48h corridor heatmap in the same response (one extra weather call for
the grid's full hourly series) so the frontend's forecast slider never
triggers a network request — it's just picking an index into data that's
already here.

Origin/destination arrive pre-resolved to coordinates (see
GeocodeSuggestView / GeocodeReverseView) — this view never geocodes free
text itself. That's deliberate: a blind backend geocode of a typo like
"Indianapolis, NL" silently matched a low-confidence country-level
fallback (the Netherlands) instead of failing, sending a Chicago-to-
Netherlands request into routing. Requiring the frontend to resolve a
location first (autocomplete pick or map click) means a human always
looks at the actual candidate before it reaches routing.

Known simplification: upstream weather-provider failures are surfaced as
a flat 502 regardless of cause (network timeout vs. a malformed
response) — not yet split into distinct failure modes.
"""

import logging

from django.core.cache import cache
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.routing_client import (
    RouteNotFoundError,
    RoutingProviderError,
    get_route_options,
    reverse_geocode,
    suggest,
)
from integrations.weather_client import (
    WeatherProviderError,
    get_weather_observations,
    get_weather_series,
)
from routing.checkpoints import Coordinate, generate_checkpoints
from routing.eta import compute_checkpoint_etas
from routing.heatmap import generate_corridor_grid
from routing.recommend import (
    CheckpointAssessment,
    RouteCandidate,
    recommend_routes,
    summarize_route_risk,
)
from routing.risk import RiskLevel, classify_checkpoint

from .serializers import (
    ErrorSerializer,
    GeocodeReverseResponseSerializer,
    GeocodeSuggestionSerializer,
    RouteAnalysisRequestSerializer,
    RouteAnalysisResponseSerializer,
)

logger = logging.getLogger(__name__)

REQUESTED_ALTERNATIVES = 3
HEATMAP_HOURS = 48
# Place names/labels don't change minute to minute; short-TTL caching here
# cuts duplicate calls to Nominatim across users/tabs typing overlapping
# prefixes (e.g. "chic", "chica", "chicago" from more than one session),
# on top of the frontend's own per-query debounce. Same manual
# cache.get/cache.set idiom as weather_client.py and routing_client.py's
# route cache, not a framework decorator, for consistency with how every
# other external call in this codebase is cached.
GEOCODE_CACHE_TTL_S = 300
GEOCODE_COORD_DECIMALS = 5  # ~1m precision, matches routing_client.py's route cache key


class AnalyzeRoutesView(APIView):
    @extend_schema(
        request=RouteAnalysisRequestSerializer,
        responses={
            200: RouteAnalysisResponseSerializer,
            400: ErrorSerializer,
            404: ErrorSerializer,
            502: ErrorSerializer,
        },
    )
    def post(self, request):
        serializer = RouteAnalysisRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        warnings = []
        origin = Coordinate(lat=payload["origin"]["lat"], lon=payload["origin"]["lon"])
        destination = Coordinate(lat=payload["destination"]["lat"], lon=payload["destination"]["lon"])

        # RouteNotFoundError (404) and RoutingProviderError (502) are
        # deliberately separate branches, not one blanket except: a
        # confirmed "no road/ferry connection for this vehicle profile" is
        # a different fact — and a different frontend experience — than
        # "the routing provider itself is down, rate-limited, or sent back
        # something we couldn't parse". Collapsing them risks exactly what
        # this split exists to prevent: showing a sea-crossing message for
        # what's actually a transient provider failure, or vice versa.
        try:
            route_options = get_route_options(
                origin,
                destination,
                payload["load_weight_lb"],
                max_alternatives=REQUESTED_ALTERNATIVES,
            )
        except RouteNotFoundError as exc:
            logger.info("No route found: %s", exc)
            return Response({"detail": str(exc), "error_code": "no_route_found"}, status=404)
        except RoutingProviderError as exc:
            logger.warning("Routing provider failure: %s", exc)
            return Response({"detail": str(exc), "error_code": "provider_error"}, status=502)

        if len(route_options) < REQUESTED_ALTERNATIVES:
            # Deliberately not phrased as a search failure: by the time
            # this fires, get_route_options has already tried native
            # alternatives *and* the via-point fallback search (see
            # integrations/routing_client.py) and both honestly came up
            # short. That's usually the road network itself — a route with
            # only one practical corridor (e.g. a single Channel crossing,
            # confirmed live for a Brussels->London request: OSRM's graph
            # has exactly one crossing, the Eurotunnel shuttle, no ferry
            # data) genuinely doesn't have 3 meaningfully different paths
            # to offer, not a shortfall in how hard this app looked.
            route_word = "route" if len(route_options) == 1 else "routes"
            warnings.append(
                f"Found {len(route_options)} genuinely distinct {route_word} for this trip. "
                f"The road network doesn't offer additional meaningfully different paths here "
                f"— showing every real option rather than padding the list."
            )

        per_route_etas = []
        for option in route_options:
            checkpoints = generate_checkpoints(
                option.polyline, option.distance_mi, payload["sampling_interval_miles"]
            )
            etas = compute_checkpoint_etas(
                checkpoints, option.duration_breakpoints, payload["departure_datetime"]
            )
            per_route_etas.append(etas)

        all_targets = [
            (eta.checkpoint.coordinate, eta.eta_utc) for etas in per_route_etas for eta in etas
        ]
        try:
            weather_results = get_weather_observations(all_targets)
        except WeatherProviderError as exc:
            logger.warning("Weather provider failure: %s", exc)
            return Response({"detail": str(exc), "error_code": "provider_error"}, status=502)

        cursor = 0
        candidates = []
        routes_payload = []
        for option, etas in zip(route_options, per_route_etas):
            rows = []
            for eta in etas:
                observation = weather_results[cursor]
                cursor += 1
                risk = (
                    classify_checkpoint(observation, payload["load_weight_lb"])
                    if observation is not None
                    else None
                )
                rows.append((eta, observation, risk))

            assessments = [
                CheckpointAssessment(checkpoint=eta.checkpoint, risk=risk) for eta, _, risk in rows
            ]
            summary = summarize_route_risk(assessments)
            candidates.append(
                RouteCandidate(
                    route_id=option.route_id,
                    distance_mi=option.distance_mi,
                    duration_s=option.duration_s,
                    risk_summary=summary,
                )
            )
            routes_payload.append((option, rows, summary))

        if any(summary.unassessed_miles for _, _, summary in routes_payload):
            warnings.append("Weather forecast data was unavailable for some checkpoints.")

        recommendation = recommend_routes(candidates)

        grid_points = generate_corridor_grid([option.polyline for option in route_options])
        try:
            grid_series = get_weather_series(grid_points, payload["departure_datetime"], HEATMAP_HOURS)
        except WeatherProviderError as exc:
            logger.warning("Weather provider failure (heatmap): %s", exc)
            return Response({"detail": str(exc), "error_code": "provider_error"}, status=502)

        response = {
            "routes": [
                _serialize_route(option, rows, summary, recommendation)
                for option, rows, summary in routes_payload
            ],
            "recommendation": {
                "status": recommendation.status.value,
                "winner_route_id": recommendation.winner.route_id if recommendation.winner else None,
                "reason": recommendation.reason,
            },
            "heatmap": _serialize_heatmap(
                grid_points, grid_series, payload["departure_datetime"], payload["load_weight_lb"]
            ),
            "warnings": warnings,
        }
        return Response(response)


def _serialize_heatmap(grid_points, grid_series, start_utc, load_lb):
    return {
        "start_utc": start_utc,
        "hour_offsets": list(range(HEATMAP_HOURS + 1)),
        "points": [
            {
                "lat": point.lat,
                "lon": point.lon,
                "risk_by_hour": [
                    classify_checkpoint(observation, load_lb).level.value if observation is not None else None
                    for observation in point_series
                ],
                # Raw values alongside the computed risk level: the frontend's
                # heatmap layer toggle (risk/wind/rain/snow) needs the actual
                # readings, not just the ordinal that risk already collapses
                # them into. Same null-when-unavailable convention as risk_by_hour.
                "wind_by_hour": [o.wind_mph if o is not None else None for o in point_series],
                "rain_by_hour": [o.rain_in_hr if o is not None else None for o in point_series],
                "snow_by_hour": [o.snow_in_hr if o is not None else None for o in point_series],
            }
            for point, point_series in zip(grid_points, grid_series)
        ],
    }


def _serialize_route(option, rows, summary, recommendation):
    rank = next(
        (i + 1 for i, candidate in enumerate(recommendation.ranked) if candidate.route_id == option.route_id),
        None,
    )
    return {
        "route_id": option.route_id,
        "distance_mi": option.distance_mi,
        "duration_s": option.duration_s,
        "polyline": [[c.lat, c.lon] for c in option.polyline],
        "duration_breakpoints": [
            {"distance_mi": bp.distance_mi, "cumulative_duration_s": bp.cumulative_duration_s}
            for bp in option.duration_breakpoints
        ],
        "checkpoints": [_serialize_checkpoint(eta, observation, risk) for eta, observation, risk in rows],
        "risk_summary": _serialize_summary(summary),
        "rank": rank,
    }


def _serialize_checkpoint(eta, observation, risk):
    return {
        "sequence": eta.checkpoint.sequence,
        "distance_mi": eta.checkpoint.distance_mi,
        "lat": eta.checkpoint.coordinate.lat,
        "lon": eta.checkpoint.coordinate.lon,
        "eta_utc": eta.eta_utc,
        "is_origin": eta.checkpoint.is_origin,
        "is_destination": eta.checkpoint.is_destination,
        "weather": (
            {
                "wind_mph": observation.wind_mph,
                "rain_in_hr": observation.rain_in_hr,
                "snow_in_hr": observation.snow_in_hr,
            }
            if observation is not None
            else None
        ),
        "risk": _serialize_risk(risk) if risk is not None else None,
    }


def _serialize_risk(risk):
    return {
        "level": risk.level.name.lower(),
        "wind_level": risk.wind_level.name.lower(),
        "rain_level": risk.rain_level.name.lower(),
        "snow_level": risk.snow_level.name.lower(),
        "wind_level_after_load_rule": risk.wind_level_after_load_rule.name.lower(),
        "reasons": list(risk.reasons),
        "prohibited": risk.prohibited,
    }


def _serialize_summary(summary):
    worst_sequence = (
        summary.worst_assessment.checkpoint.sequence if summary.worst_assessment else None
    )
    return {
        "miles_by_level": {level.name.lower(): summary.miles_by_level[level] for level in RiskLevel},
        "unassessed_miles": summary.unassessed_miles,
        "distance_weighted_avg_risk": summary.distance_weighted_avg_risk,
        "has_no_travel": summary.has_no_travel,
        "worst_checkpoint_sequence": worst_sequence,
    }


class GeocodeSuggestView(APIView):
    """GET /api/geocode/suggest/?q=<partial text> — ranked place candidates
    for a search-as-you-type origin/destination input."""

    @extend_schema(
        parameters=[OpenApiParameter("q", str, description="Partial place name, e.g. 'Indianap'")],
        responses={200: GeocodeSuggestionSerializer(many=True), 400: ErrorSerializer, 502: ErrorSerializer},
    )
    def get(self, request):
        query = request.query_params.get("q", "").strip()
        if not query:
            return Response({"detail": "query parameter 'q' is required"}, status=400)

        cache_key = f"geocode_suggest:{query.lower()}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        try:
            results = suggest(query)
        except RoutingProviderError as exc:
            logger.warning("Geocode suggest failure: %s", exc)
            return Response({"detail": str(exc), "error_code": "provider_error"}, status=502)

        payload = [{"label": r.label, "lat": r.coordinate.lat, "lon": r.coordinate.lon} for r in results]
        cache.set(cache_key, payload, GEOCODE_CACHE_TTL_S)
        return Response(payload)


class GeocodeReverseView(APIView):
    """GET /api/geocode/reverse/?lat=<lat>&lon=<lon> — human-readable label
    for a point picked directly on the map."""

    @extend_schema(
        parameters=[
            OpenApiParameter("lat", float, required=True),
            OpenApiParameter("lon", float, required=True),
        ],
        responses={200: GeocodeReverseResponseSerializer, 400: ErrorSerializer, 502: ErrorSerializer},
    )
    def get(self, request):
        try:
            lat = float(request.query_params["lat"])
            lon = float(request.query_params["lon"])
        except (KeyError, ValueError):
            return Response({"detail": "query parameters 'lat' and 'lon' (floats) are required"}, status=400)

        try:
            coordinate = Coordinate(lat=lat, lon=lon)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        cache_key = (
            f"geocode_reverse:{round(lat, GEOCODE_COORD_DECIMALS)}:{round(lon, GEOCODE_COORD_DECIMALS)}"
        )
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        try:
            label = reverse_geocode(coordinate)
        except RoutingProviderError as exc:
            logger.warning("Reverse geocode failure: %s", exc)
            return Response({"detail": str(exc), "error_code": "provider_error"}, status=502)

        payload = {"label": label, "lat": lat, "lon": lon}
        cache.set(cache_key, payload, GEOCODE_CACHE_TTL_S)
        return Response(payload)
