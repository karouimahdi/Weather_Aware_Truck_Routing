import pytest

from routing.risk import (
    RiskLevel,
    WeatherObservation,
    apply_load_rule,
    classify_checkpoint,
    classify_rain,
    classify_snow,
    classify_wind,
)

# --- Wind boundaries: <25 LOW, 25-<35 MODERATE, 35-<45 HIGH, 45-<55 SEVERE, >=55 NO_TRAVEL ---


@pytest.mark.parametrize(
    "mph,expected",
    [
        (0, RiskLevel.LOW),
        (24.9, RiskLevel.LOW),
        (25, RiskLevel.MODERATE),
        (34.9, RiskLevel.MODERATE),
        (35, RiskLevel.HIGH),
        (44.9, RiskLevel.HIGH),
        (45, RiskLevel.SEVERE),
        (54.9, RiskLevel.SEVERE),
        (55, RiskLevel.NO_TRAVEL),
        (100, RiskLevel.NO_TRAVEL),
    ],
)
def test_classify_wind_boundaries(mph, expected):
    assert classify_wind(mph) == expected


# --- Rain boundaries: <0.10 LOW, 0.10-<0.25 MODERATE, 0.25-<0.50 HIGH, 0.50-1.00 SEVERE, >1.00 NO_TRAVEL ---


@pytest.mark.parametrize(
    "in_hr,expected",
    [
        (0, RiskLevel.LOW),
        (0.099, RiskLevel.LOW),
        (0.10, RiskLevel.MODERATE),
        (0.249, RiskLevel.MODERATE),
        (0.25, RiskLevel.HIGH),
        (0.499, RiskLevel.HIGH),
        (0.50, RiskLevel.SEVERE),
        (1.00, RiskLevel.SEVERE),  # inclusive upper bound per the table
        (1.001, RiskLevel.NO_TRAVEL),
        (5, RiskLevel.NO_TRAVEL),
    ],
)
def test_classify_rain_boundaries(in_hr, expected):
    assert classify_rain(in_hr) == expected


# --- Snow boundaries: <0.5 LOW, 0.5-<1.0 MODERATE, 1.0-<2.0 HIGH, 2.0-3.0 SEVERE, >3.0 NO_TRAVEL ---


@pytest.mark.parametrize(
    "in_hr,expected",
    [
        (0, RiskLevel.LOW),
        (0.49, RiskLevel.LOW),
        (0.5, RiskLevel.MODERATE),
        (0.99, RiskLevel.MODERATE),
        (1.0, RiskLevel.HIGH),
        (1.99, RiskLevel.HIGH),
        (2.0, RiskLevel.SEVERE),
        (3.0, RiskLevel.SEVERE),  # inclusive upper bound per the table
        (3.001, RiskLevel.NO_TRAVEL),
        (10, RiskLevel.NO_TRAVEL),
    ],
)
def test_classify_snow_boundaries(in_hr, expected):
    assert classify_snow(in_hr) == expected


def test_negative_values_rejected():
    with pytest.raises(ValueError):
        WeatherObservation(wind_mph=-1, rain_in_hr=0, snow_in_hr=0)
    with pytest.raises(ValueError):
        WeatherObservation(wind_mph=0, rain_in_hr=-0.1, snow_in_hr=0)
    with pytest.raises(ValueError):
        WeatherObservation(wind_mph=0, rain_in_hr=0, snow_in_hr=-0.1)


# --- Load rules (applied to wind only) ---
# wind >= 55 -> NO_TRAVEL (any load)
# 45 <= wind < 55 and load > 30,000 -> NO_TRAVEL
# 35 <= wind < 45 and load > 40,000 -> SEVERE


@pytest.mark.parametrize(
    "mph,load_lb,expected",
    [
        # >=55: always NO_TRAVEL regardless of load, including a trivially light load
        (55, 1, RiskLevel.NO_TRAVEL),
        (60, 0, RiskLevel.NO_TRAVEL),
        # 45-<55 band: load boundary at exactly 30,000 (not > 30,000) stays SEVERE
        (45, 30_000, RiskLevel.SEVERE),
        (45, 30_001, RiskLevel.NO_TRAVEL),
        (54.9, 30_001, RiskLevel.NO_TRAVEL),
        # 35-<45 band: load boundary at exactly 40,000 (not > 40,000) stays HIGH (base wind level)
        (35, 40_000, RiskLevel.HIGH),
        (35, 40_001, RiskLevel.SEVERE),
        (44.9, 40_001, RiskLevel.SEVERE),
        # below 35mph: load never escalates wind risk
        (34.9, 1_000_000, RiskLevel.MODERATE),
        (0, 1_000_000, RiskLevel.LOW),
    ],
)
def test_apply_load_rule(mph, load_lb, expected):
    assert apply_load_rule(mph, load_lb) == expected


def test_load_rule_never_downgrades_below_base_wind_level():
    # 40mph base HIGH, light load (10,000 lb) must stay HIGH, not drop to something lower
    assert apply_load_rule(40, 10_000) == RiskLevel.HIGH


# --- classify_checkpoint: aggregation, reasons, prohibited flag, determinism ---


def test_checkpoint_takes_max_across_hazards():
    # wind LOW, rain SEVERE, snow LOW -> overall SEVERE, reason is rain only
    obs = WeatherObservation(wind_mph=10, rain_in_hr=0.75, snow_in_hr=0)
    result = classify_checkpoint(obs, load_lb=10_000)
    assert result.level == RiskLevel.SEVERE
    assert result.reasons == ("rain",)
    assert result.prohibited is False


def test_checkpoint_ties_list_every_contributing_hazard():
    # wind HIGH (40mph), rain HIGH (0.3 in/hr), snow LOW -> tie between wind and rain
    obs = WeatherObservation(wind_mph=40, rain_in_hr=0.3, snow_in_hr=0)
    result = classify_checkpoint(obs, load_lb=1_000)
    assert result.level == RiskLevel.HIGH
    assert set(result.reasons) == {"wind", "rain"}


def test_checkpoint_load_rule_escalation_is_reflected_in_final_level():
    # 40mph wind alone is HIGH, but load > 40,000 lb escalates wind to SEVERE,
    # which must show up as the checkpoint's final level and reason.
    obs = WeatherObservation(wind_mph=40, rain_in_hr=0, snow_in_hr=0)
    result = classify_checkpoint(obs, load_lb=45_000)
    assert result.wind_level == RiskLevel.HIGH
    assert result.wind_level_after_load_rule == RiskLevel.SEVERE
    assert result.level == RiskLevel.SEVERE
    assert result.reasons == ("wind",)


def test_checkpoint_no_travel_sets_prohibited_flag():
    obs = WeatherObservation(wind_mph=60, rain_in_hr=0, snow_in_hr=0)
    result = classify_checkpoint(obs, load_lb=1_000)
    assert result.level == RiskLevel.NO_TRAVEL
    assert result.prohibited is True


def test_checkpoint_no_travel_from_rain_alone_still_prohibited():
    # Load rules only ever apply to wind; a calm-wind, torrential-rain checkpoint
    # must still come out NO_TRAVEL and prohibited.
    obs = WeatherObservation(wind_mph=5, rain_in_hr=1.5, snow_in_hr=0)
    result = classify_checkpoint(obs, load_lb=1_000)
    assert result.level == RiskLevel.NO_TRAVEL
    assert result.prohibited is True
    assert result.reasons == ("rain",)


def test_classify_checkpoint_is_deterministic():
    obs = WeatherObservation(wind_mph=42, rain_in_hr=0.3, snow_in_hr=1.2)
    first = classify_checkpoint(obs, load_lb=38_000)
    second = classify_checkpoint(obs, load_lb=38_000)
    assert first == second
