"""
Request validation and response schema for POST /api/routes/analyze/.

The response serializers are declared for drf-spectacular's schema only —
the view builds and returns a plain dict matching this exact shape (see
`_serialize_route` in views.py) rather than round-tripping real dataclasses
through DRF's serialization machinery, which would add indirection without
adding safety here (the shape is fixed, there's one producer).
"""

from datetime import datetime, timedelta, timezone

from dateutil.parser import isoparse
from rest_framework import serializers

from integrations.weather_client import MAX_FORECAST_DAYS
from routing.risk import RiskLevel

RISK_LEVEL_CHOICES = [level.name.lower() for level in RiskLevel]
SAMPLING_INTERVAL_CHOICES = [10, 25, 50]

# A departure outside this window can never get real forecast data —
# get_weather_series (integrations/weather_client.py) returns None for
# every checkpoint whose ETA falls outside what the provider actually
# returned, which is exactly the "insufficient data" dead end a user hit
# in practice. Rejecting it here, with a clear reason, is cheaper than a
# wasted routing+weather round trip that was always going to come back
# empty, and a clearer failure than a generic "could not recommend".
#
# DEPARTURE_PAST_GRACE absorbs normal submission latency / small
# client-server clock drift — not a business rule, just slack so a
# departure of "right now" isn't rejected for being a few seconds stale
# by the time the request lands.
DEPARTURE_PAST_GRACE = timedelta(minutes=15)
# Deliberately tighter than the provider's own 16-day ceiling
# (MAX_FORECAST_DAYS): forecast skill degrades well before that horizon,
# and this app would rather offer a window it can stand behind than the
# provider's absolute outer limit. Product decision, not a provider
# limitation — MAX_FORECAST_DAYS above still reflects the real ceiling.
DEPARTURE_MAX_FUTURE_DAYS = 7
assert DEPARTURE_MAX_FUTURE_DAYS <= MAX_FORECAST_DAYS, (
    "DEPARTURE_MAX_FUTURE_DAYS can't exceed what the provider can actually forecast"
)
DEPARTURE_MAX_FUTURE = timedelta(days=DEPARTURE_MAX_FUTURE_DAYS)

# Sanity ceiling on load weight, not a DB-sourced legal limit: 80,000 lb is
# the US federal standard max gross vehicle weight on the interstate
# system without a special permit. This field is cargo-only (the load
# rules in routing/risk.py top out at 40,000 lb), so 80,000 lb is a
# deliberately generous ceiling meant to catch fat-fingered/nonsense input
# (e.g. a stray extra digit), not to model real permitted-oversize limits.
MAX_LOAD_WEIGHT_LB = 80_000


class LocationSerializer(serializers.Serializer):
    """
    A location the user explicitly resolved to coordinates — by picking an
    autocomplete suggestion or clicking the map — rather than free text the
    backend would geocode blind. `label` is display-only, never
    re-geocoded; only `lat`/`lon` feed the routing request.
    """

    label = serializers.CharField(max_length=255)
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)


class RouteAnalysisRequestSerializer(serializers.Serializer):
    origin = LocationSerializer()
    destination = LocationSerializer()
    departure_datetime = serializers.CharField(
        help_text="ISO 8601 datetime with an explicit UTC offset or 'Z', "
        "e.g. 2026-03-01T08:00:00-05:00. A naive datetime (no offset) is rejected."
    )
    load_weight_lb = serializers.FloatField(
        min_value=0,
        max_value=MAX_LOAD_WEIGHT_LB,
        help_text=f"Cargo/load weight in pounds, 0-{MAX_LOAD_WEIGHT_LB:,}.",
    )
    sampling_interval_miles = serializers.ChoiceField(
        choices=SAMPLING_INTERVAL_CHOICES, help_text="Checkpoint sampling interval, in miles."
    )

    def validate_departure_datetime(self, value):
        try:
            parsed = isoparse(value)
        except (ValueError, TypeError) as exc:
            raise serializers.ValidationError("must be a valid ISO 8601 datetime") from exc
        if parsed.tzinfo is None:
            raise serializers.ValidationError(
                "must include an explicit UTC offset (e.g. +00:00 or Z)"
            )

        now = datetime.now(timezone.utc)
        if parsed < now - DEPARTURE_PAST_GRACE:
            raise serializers.ValidationError("departure cannot be in the past")
        if parsed > now + DEPARTURE_MAX_FUTURE:
            raise serializers.ValidationError(
                f"departure must be within the next {DEPARTURE_MAX_FUTURE_DAYS} days"
            )
        return parsed


class WeatherObservationSerializer(serializers.Serializer):
    wind_mph = serializers.FloatField()
    rain_in_hr = serializers.FloatField()
    snow_in_hr = serializers.FloatField()


class CheckpointRiskSerializer(serializers.Serializer):
    level = serializers.ChoiceField(choices=RISK_LEVEL_CHOICES)
    wind_level = serializers.ChoiceField(choices=RISK_LEVEL_CHOICES)
    rain_level = serializers.ChoiceField(choices=RISK_LEVEL_CHOICES)
    snow_level = serializers.ChoiceField(choices=RISK_LEVEL_CHOICES)
    wind_level_after_load_rule = serializers.ChoiceField(choices=RISK_LEVEL_CHOICES)
    reasons = serializers.ListField(child=serializers.ChoiceField(choices=["wind", "rain", "snow"]))
    prohibited = serializers.BooleanField()


class CheckpointSerializer(serializers.Serializer):
    sequence = serializers.IntegerField()
    distance_mi = serializers.FloatField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    eta_utc = serializers.DateTimeField()
    is_origin = serializers.BooleanField()
    is_destination = serializers.BooleanField()
    weather = WeatherObservationSerializer(
        allow_null=True, help_text="null when the forecast horizon didn't cover this checkpoint's ETA."
    )
    risk = CheckpointRiskSerializer(allow_null=True)


class MilesByLevelSerializer(serializers.Serializer):
    low = serializers.FloatField()
    moderate = serializers.FloatField()
    high = serializers.FloatField()
    severe = serializers.FloatField()
    no_travel = serializers.FloatField()


class RouteRiskSummarySerializer(serializers.Serializer):
    miles_by_level = MilesByLevelSerializer()
    unassessed_miles = serializers.FloatField()
    distance_weighted_avg_risk = serializers.FloatField(allow_null=True)
    has_no_travel = serializers.BooleanField()
    worst_checkpoint_sequence = serializers.IntegerField(allow_null=True)


class DurationBreakpointSerializer(serializers.Serializer):
    distance_mi = serializers.FloatField()
    cumulative_duration_s = serializers.FloatField()


class RouteResultSerializer(serializers.Serializer):
    route_id = serializers.CharField()
    distance_mi = serializers.FloatField()
    duration_s = serializers.FloatField()
    polyline = serializers.ListField(
        child=serializers.ListField(child=serializers.FloatField()),
        help_text="[[lat, lon], ...] in route order.",
    )
    duration_breakpoints = DurationBreakpointSerializer(
        many=True,
        help_text="(distance_mi, cumulative_duration_s) pairs aligned 1:1 with polyline "
        "vertices — lets the frontend interpolate a truck's position at any elapsed "
        "time, not just at a checkpoint.",
    )
    checkpoints = CheckpointSerializer(many=True)
    risk_summary = RouteRiskSummarySerializer()
    rank = serializers.IntegerField(
        allow_null=True, help_text="1 = recommended-best; null if this route couldn't be scored at all."
    )


class RecommendationSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["safe", "best_available_unsafe", "insufficient_data"])
    winner_route_id = serializers.CharField(allow_null=True)
    reason = serializers.CharField()


class HeatmapPointSerializer(serializers.Serializer):
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    risk_by_hour = serializers.ListField(
        child=serializers.IntegerField(allow_null=True, min_value=0, max_value=4),
        help_text="One ordinal risk level per hour_offset, aligned by index "
        "(0=low, 1=moderate, 2=high, 3=severe, 4=no_travel); null if that "
        "hour fell outside the forecast horizon.",
    )
    wind_by_hour = serializers.ListField(
        child=serializers.FloatField(allow_null=True),
        help_text="Sustained wind, mph, aligned with hour_offsets; null if unavailable.",
    )
    rain_by_hour = serializers.ListField(
        child=serializers.FloatField(allow_null=True),
        help_text="Rain rate, in/hr, aligned with hour_offsets; null if unavailable.",
    )
    snow_by_hour = serializers.ListField(
        child=serializers.FloatField(allow_null=True),
        help_text="Snow rate, in/hr, aligned with hour_offsets; null if unavailable.",
    )


class HeatmapSerializer(serializers.Serializer):
    start_utc = serializers.DateTimeField(help_text="hour_offset 0 — the trip's departure time, not request time.")
    hour_offsets = serializers.ListField(
        child=serializers.IntegerField(), help_text="0..48, hours after start_utc. Fixed for the whole response."
    )
    points = HeatmapPointSerializer(many=True)


class RouteAnalysisResponseSerializer(serializers.Serializer):
    routes = RouteResultSerializer(many=True)
    recommendation = RecommendationSerializer()
    heatmap = HeatmapSerializer()
    warnings = serializers.ListField(child=serializers.CharField())


class ErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()
    error_code = serializers.ChoiceField(
        choices=["no_route_found", "provider_error"],
        required=False,
        help_text="Present on routing/weather failures so the frontend can tell a confirmed "
        "'no route exists' apart from a provider/service failure without parsing `detail` text.",
    )


class GeocodeSuggestionSerializer(serializers.Serializer):
    label = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()


class GeocodeReverseResponseSerializer(serializers.Serializer):
    label = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
