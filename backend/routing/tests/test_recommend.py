import pytest

from routing.checkpoints import Coordinate, generate_checkpoints
from routing.recommend import (
    CheckpointAssessment,
    RecommendationStatus,
    RouteCandidate,
    recommend_routes,
    summarize_route_risk,
)
from routing.risk import RiskLevel, WeatherObservation, classify_checkpoint

LOAD = 10_000


def make_checkpoints(total_mi=100.0, interval_mi=25.0):
    polyline = [Coordinate(lat=0, lon=0), Coordinate(lat=1.45, lon=0)]
    return generate_checkpoints(polyline, total_distance_mi=total_mi, interval_mi=interval_mi)


def assess(checkpoints, winds):
    """winds: list of wind_mph per checkpoint, same length as checkpoints."""
    assessments = []
    for cp, wind in zip(checkpoints, winds):
        obs = WeatherObservation(wind_mph=wind, rain_in_hr=0, snow_in_hr=0)
        risk = classify_checkpoint(obs, load_lb=LOAD)
        assessments.append(CheckpointAssessment(checkpoint=cp, risk=risk))
    return assessments


# --- summarize_route_risk ---


def test_origin_owns_no_mileage():
    checkpoints = make_checkpoints()  # 5 checkpoints: 0,25,50,75,100
    # Make the ORIGIN severe-wind but everything else calm: origin's risk must
    # not contribute any miles, since it owns the segment before itself (none).
    winds = [50, 0, 0, 0, 0]
    assessments = assess(checkpoints, winds)
    summary = summarize_route_risk(assessments)
    assert summary.severe_miles == 0
    assert summary.miles_by_level[RiskLevel.LOW] == 100
    # but the origin's hazard must still be visible for safety purposes
    assert summary.worst_assessment.checkpoint.distance_mi == 0
    assert summary.worst_assessment.risk.level == RiskLevel.SEVERE


def test_distance_weighted_average_risk():
    checkpoints = make_checkpoints()  # segments: 0-25,25-50,50-75,75-100 (25mi each)
    # checkpoint[1]=25mi -> MODERATE(1), checkpoint[2]=50mi -> LOW(0),
    # checkpoint[3]=75mi -> HIGH(2), checkpoint[4]=100mi -> LOW(0)
    winds = [0, 30, 0, 40, 0]
    assessments = assess(checkpoints, winds)
    summary = summarize_route_risk(assessments)
    # (25*1 + 25*0 + 25*2 + 25*0) / 100 = 75/100 = 0.75
    assert summary.distance_weighted_avg_risk == pytest.approx(0.75)


def test_unequal_segment_lengths_are_weighted_correctly():
    checkpoints = make_checkpoints(total_mi=110.0, interval_mi=25.0)  # 0,25,50,75,100,110
    # last segment is only 10 miles; give it a high risk and confirm it
    # doesn't get the same weight as a full 25-mile segment.
    winds = [0, 0, 0, 0, 0, 40]  # only final 10mi segment is HIGH
    assessments = assess(checkpoints, winds)
    summary = summarize_route_risk(assessments)
    assert summary.miles_by_level[RiskLevel.HIGH] == 10
    assert summary.distance_weighted_avg_risk == pytest.approx((10 * 2) / 110)


def test_missing_weather_excluded_from_aggregation():
    checkpoints = make_checkpoints()
    assessments = assess(checkpoints, [0, 0, 0, 0, 0])
    # blank out the 3rd checkpoint's (50mi) forecast
    assessments[2] = CheckpointAssessment(checkpoint=assessments[2].checkpoint, risk=None)
    summary = summarize_route_risk(assessments)
    assert summary.unassessed_miles == 25  # the 25-50mi segment
    assert summary.distance_weighted_avg_risk == pytest.approx(0.0)
    assert not summary.fully_unassessed


def test_fully_unassessed_route():
    checkpoints = make_checkpoints()
    assessments = [
        CheckpointAssessment(checkpoint=cp, risk=None) for cp in checkpoints
    ]
    summary = summarize_route_risk(assessments)
    assert summary.fully_unassessed
    assert summary.distance_weighted_avg_risk is None
    assert summary.worst_assessment is None
    assert summary.has_no_travel is False  # unknown, not known-unsafe


def test_has_no_travel_detected():
    checkpoints = make_checkpoints()
    winds = [0, 0, 60, 0, 0]  # NO_TRAVEL at 50mi
    assessments = assess(checkpoints, winds)
    summary = summarize_route_risk(assessments)
    assert summary.has_no_travel is True


# --- recommend_routes: lexicographic ranking ---


def candidate(route_id, severe_miles=0, high_miles=0, avg_risk=0.0, duration_s=3600, no_travel=False):
    from routing.recommend import RouteRiskSummary

    miles_by_level = {level: 0.0 for level in RiskLevel}
    miles_by_level[RiskLevel.SEVERE] = severe_miles
    miles_by_level[RiskLevel.HIGH] = high_miles
    summary = RouteRiskSummary(
        miles_by_level=miles_by_level,
        unassessed_miles=0,
        distance_weighted_avg_risk=avg_risk,
        has_no_travel=no_travel,
        worst_assessment="placeholder",  # anything non-None; only fully_unassessed cares about None
    )
    return RouteCandidate(route_id=route_id, distance_mi=100, duration_s=duration_s, risk_summary=summary)


def test_fewest_severe_miles_wins_first():
    a = candidate("A", severe_miles=0, high_miles=50, avg_risk=2.0, duration_s=10000)
    b = candidate("B", severe_miles=5, high_miles=0, avg_risk=0.1, duration_s=1000)
    result = recommend_routes([a, b])
    assert result.status == RecommendationStatus.SAFE
    assert result.winner.route_id == "A"


def test_high_miles_is_second_tiebreaker():
    a = candidate("A", severe_miles=2, high_miles=10, avg_risk=2.0, duration_s=10000)
    b = candidate("B", severe_miles=2, high_miles=5, avg_risk=3.0, duration_s=1000)
    result = recommend_routes([a, b])
    assert result.winner.route_id == "B"


def test_avg_risk_is_third_tiebreaker():
    a = candidate("A", severe_miles=2, high_miles=5, avg_risk=2.0, duration_s=5000)
    b = candidate("B", severe_miles=2, high_miles=5, avg_risk=1.0, duration_s=9000)
    result = recommend_routes([a, b])
    assert result.winner.route_id == "B"


def test_duration_is_final_tiebreaker():
    a = candidate("A", severe_miles=2, high_miles=5, avg_risk=1.0, duration_s=9000)
    b = candidate("B", severe_miles=2, high_miles=5, avg_risk=1.0, duration_s=5000)
    result = recommend_routes([a, b])
    assert result.winner.route_id == "B"


def test_no_travel_disqualifies_even_with_better_other_metrics():
    # A has a No Travel segment but otherwise looks perfect; B is merely "High" risk.
    a = candidate("A", severe_miles=0, high_miles=0, avg_risk=0.0, duration_s=100, no_travel=True)
    b = candidate("B", severe_miles=0, high_miles=20, avg_risk=1.5, duration_s=9000, no_travel=False)
    result = recommend_routes([a, b])
    assert result.status == RecommendationStatus.SAFE
    assert result.winner.route_id == "B"


def test_all_routes_no_travel_yields_best_available_unsafe():
    a = candidate("A", severe_miles=10, duration_s=100, no_travel=True)
    b = candidate("B", severe_miles=2, duration_s=9000, no_travel=True)
    result = recommend_routes([a, b])
    assert result.status == RecommendationStatus.BEST_AVAILABLE_UNSAFE
    assert result.winner.route_id == "B"
    assert "not a safe recommendation" in result.reason


def test_no_candidates_is_insufficient_data():
    result = recommend_routes([])
    assert result.status == RecommendationStatus.INSUFFICIENT_DATA
    assert result.winner is None


def test_fully_unassessed_candidates_excluded_and_reported_insufficient():
    from routing.recommend import RouteRiskSummary

    summary = RouteRiskSummary(
        miles_by_level={level: 0.0 for level in RiskLevel},
        unassessed_miles=100,
        distance_weighted_avg_risk=None,
        has_no_travel=False,
        worst_assessment=None,
    )
    a = RouteCandidate(route_id="A", distance_mi=100, duration_s=3600, risk_summary=summary)
    result = recommend_routes([a])
    assert result.status == RecommendationStatus.INSUFFICIENT_DATA
    assert result.winner is None


def test_reason_mentions_distinguishing_criterion():
    a = candidate("A", severe_miles=0, high_miles=0, avg_risk=0.0, duration_s=100)
    b = candidate("B", severe_miles=5, high_miles=0, avg_risk=0.0, duration_s=50)
    result = recommend_routes([a, b])
    assert result.winner.route_id == "A"
    assert "Severe" in result.reason
