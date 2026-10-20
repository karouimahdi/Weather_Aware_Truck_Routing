"""
Pure weather-risk classification for truck routing.

No I/O here. This module has to be re-runnable over an entire route's
checkpoints in isolation, with no Django or network dependency, so the
risk policy can be re-tuned and re-tested in milliseconds.

Boundary convention (resolved ambiguity):
the assessment's table is lower-bound-inclusive / upper-bound-exclusive
for every tier EXCEPT the Severe/No-Travel seam for rain and snow, where
the table itself writes Severe's upper bound inclusive ("0.50-1.00",
"2.0-3.0") and No Travel strictly greater ("> 1.00", "> 3.0"). Wind's
Severe/No-Travel seam has no such gap (Severe "< 55", No Travel ">= 55").
Each classifier below is written to match its row exactly, so there is
never a value that falls into neither tier nor a value shared by two.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class RiskLevel(IntEnum):
    """Ordinal scale. Comparisons and max() rely on this declaration order."""

    LOW = 0
    MODERATE = 1
    HIGH = 2
    SEVERE = 3
    NO_TRAVEL = 4


@dataclass(frozen=True)
class WeatherObservation:
    """Raw weather at a checkpoint, already in the units the risk table uses."""

    wind_mph: float
    rain_in_hr: float
    snow_in_hr: float

    def __post_init__(self) -> None:
        if self.wind_mph < 0:
            raise ValueError(f"wind_mph cannot be negative, got {self.wind_mph!r}")
        if self.rain_in_hr < 0:
            raise ValueError(f"rain_in_hr cannot be negative, got {self.rain_in_hr!r}")
        if self.snow_in_hr < 0:
            raise ValueError(f"snow_in_hr cannot be negative, got {self.snow_in_hr!r}")


@dataclass(frozen=True)
class CheckpointRisk:
    """
    Full classification detail for one checkpoint.

    Every intermediate value is kept, not just the final level: a more
    severe hazard must never hide a less severe one from the UI, and the
    reason behind a level must always be inspectable.
    """

    wind_level: RiskLevel
    rain_level: RiskLevel
    snow_level: RiskLevel
    wind_level_after_load_rule: RiskLevel
    level: RiskLevel
    reasons: tuple[str, ...]
    prohibited: bool


def classify_wind(wind_mph: float) -> RiskLevel:
    """Sustained wind speed -> risk level. Gusts are informational only (see README)."""
    if wind_mph < 25:
        return RiskLevel.LOW
    if wind_mph < 35:
        return RiskLevel.MODERATE
    if wind_mph < 45:
        return RiskLevel.HIGH
    if wind_mph < 55:
        return RiskLevel.SEVERE
    return RiskLevel.NO_TRAVEL


def classify_rain(rain_in_hr: float) -> RiskLevel:
    if rain_in_hr < 0.10:
        return RiskLevel.LOW
    if rain_in_hr < 0.25:
        return RiskLevel.MODERATE
    if rain_in_hr < 0.50:
        return RiskLevel.HIGH
    if rain_in_hr <= 1.00:
        return RiskLevel.SEVERE
    return RiskLevel.NO_TRAVEL


def classify_snow(snow_in_hr: float) -> RiskLevel:
    if snow_in_hr < 0.5:
        return RiskLevel.LOW
    if snow_in_hr < 1.0:
        return RiskLevel.MODERATE
    if snow_in_hr < 2.0:
        return RiskLevel.HIGH
    if snow_in_hr <= 3.0:
        return RiskLevel.SEVERE
    return RiskLevel.NO_TRAVEL


def apply_load_rule(wind_mph: float, load_lb: float) -> RiskLevel:
    """
    Load-dependent escalation of the WIND level only — the assessment states
    these rules against wind speed, not rain or snow.

    These rules only ever escalate; a light load never earns a discount
    below the base wind classification.
    """
    if wind_mph >= 55:
        return RiskLevel.NO_TRAVEL
    if 45 <= wind_mph < 55 and load_lb > 30_000:
        return RiskLevel.NO_TRAVEL
    if 35 <= wind_mph < 45 and load_lb > 40_000:
        return RiskLevel.SEVERE
    return classify_wind(wind_mph)


def classify_checkpoint(observation: WeatherObservation, load_lb: float) -> CheckpointRisk:
    """
    Classify one checkpoint's weather into a final risk level.

    Aggregation policy: the final level is the MAX of the (load-adjusted)
    wind level, rain level, and snow level — not a weighted sum. An
    arbitrary numerical weighting across incompatible hazards (mph vs.
    in/hr) would need a justification the assessment doesn't provide;
    "the worst hazard present governs" is the conservative, defensible
    reading, and it is spelled out here rather than hidden in a formula.

    `reasons` lists every hazard whose own level equals the final level —
    when two hazards tie for worst, both are surfaced rather than picking
    one arbitrarily, so the UI tooltip never hides a tied contributor.
    """
    wind_level = classify_wind(observation.wind_mph)
    rain_level = classify_rain(observation.rain_in_hr)
    snow_level = classify_snow(observation.snow_in_hr)
    wind_level_after_load_rule = apply_load_rule(observation.wind_mph, load_lb)

    final_level = max(wind_level_after_load_rule, rain_level, snow_level)

    reasons = tuple(
        name
        for name, level in (
            ("wind", wind_level_after_load_rule),
            ("rain", rain_level),
            ("snow", snow_level),
        )
        if level == final_level
    )

    return CheckpointRisk(
        wind_level=wind_level,
        rain_level=rain_level,
        snow_level=snow_level,
        wind_level_after_load_rule=wind_level_after_load_rule,
        level=final_level,
        reasons=reasons,
        prohibited=final_level is RiskLevel.NO_TRAVEL,
    )
