"""
Per-route risk aggregation and cross-route recommendation ranking.

Pure, no I/O. Two responsibilities live here because they are tightly
coupled and both small: (1) turn a route's ordered checkpoint risks into
route-level numbers (miles per level, distance-weighted average risk), and
(2) rank routes against each other using those numbers.

Segment-ownership policy: checkpoint i owns the road segment
from checkpoint (i-1) to itself; that segment's length is attributed to
checkpoint i's risk level. The origin (i=0) owns no preceding segment (0
miles) — it still participates in has-No-Travel / worst-checkpoint checks,
since its weather is a real hazard the driver meets immediately, even
though it contributes no mileage.

Missing weather policy: a checkpoint with risk=None (forecast unavailable
— see routing/eta.py and the weather client's horizon check) is excluded
from miles_by_level and the average-risk calculation, and its segment is
counted separately as `unassessed_miles`. It is never backfilled with a
guess. If every checkpoint on a route is unassessed, the route cannot be
scored at all (see `RouteCandidate` / `recommend_routes` below).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from routing.checkpoints import Checkpoint
from routing.risk import CheckpointRisk, RiskLevel


@dataclass(frozen=True)
class CheckpointAssessment:
    checkpoint: Checkpoint
    risk: CheckpointRisk | None  # None == forecast unavailable at this checkpoint


@dataclass(frozen=True)
class RouteRiskSummary:
    miles_by_level: dict[RiskLevel, float]
    unassessed_miles: float
    distance_weighted_avg_risk: float | None  # None if no assessed mileage at all
    has_no_travel: bool
    worst_assessment: CheckpointAssessment | None  # None if nothing was assessable

    @property
    def severe_miles(self) -> float:
        return self.miles_by_level.get(RiskLevel.SEVERE, 0.0)

    @property
    def high_miles(self) -> float:
        return self.miles_by_level.get(RiskLevel.HIGH, 0.0)

    @property
    def fully_unassessed(self) -> bool:
        return self.worst_assessment is None


def summarize_route_risk(assessments: list[CheckpointAssessment]) -> RouteRiskSummary:
    if len(assessments) < 2:
        raise ValueError("need at least origin and destination assessments")

    miles_by_level: dict[RiskLevel, float] = {level: 0.0 for level in RiskLevel}
    unassessed_miles = 0.0
    weighted_level_sum = 0.0
    assessed_miles = 0.0

    for prev, cur in zip(assessments, assessments[1:]):
        segment_miles = cur.checkpoint.distance_mi - prev.checkpoint.distance_mi
        if cur.risk is None:
            unassessed_miles += segment_miles
            continue
        miles_by_level[cur.risk.level] += segment_miles
        weighted_level_sum += segment_miles * cur.risk.level.value
        assessed_miles += segment_miles

    assessed = [a for a in assessments if a.risk is not None]
    has_no_travel = any(a.risk.prohibited for a in assessed)
    worst_assessment = (
        max(assessed, key=lambda a: a.risk.level) if assessed else None
    )
    avg_risk = (weighted_level_sum / assessed_miles) if assessed_miles > 0 else None

    return RouteRiskSummary(
        miles_by_level=miles_by_level,
        unassessed_miles=unassessed_miles,
        distance_weighted_avg_risk=avg_risk,
        has_no_travel=has_no_travel,
        worst_assessment=worst_assessment,
    )


@dataclass(frozen=True)
class RouteCandidate:
    route_id: str
    distance_mi: float
    duration_s: float
    risk_summary: RouteRiskSummary


class RecommendationStatus(Enum):
    SAFE = "safe"  # winner has no No-Travel segment
    BEST_AVAILABLE_UNSAFE = "best_available_unsafe"  # every candidate has a No-Travel segment
    INSUFFICIENT_DATA = "insufficient_data"  # no candidates, or every candidate fully unassessed


@dataclass(frozen=True)
class Recommendation:
    status: RecommendationStatus
    winner: RouteCandidate | None
    reason: str
    ranked: list[RouteCandidate]  # best-to-worst, for candidates that could be ranked at all


def _ranking_key(candidate: RouteCandidate) -> tuple:
    s = candidate.risk_summary
    # avg_risk=None (fully unassessed) must sort worst — treated as +inf.
    avg_risk = s.distance_weighted_avg_risk if s.distance_weighted_avg_risk is not None else float("inf")
    return (s.has_no_travel, s.severe_miles, s.high_miles, avg_risk, candidate.duration_s)


def _distinguishing_reason(winner: RouteCandidate, runner_up: RouteCandidate | None) -> str:
    """Walk the same criteria _ranking_key sorts on, in the same order, and
    name the first one where the winner and runner-up actually differ."""
    if runner_up is None:
        return "Only eligible route available."

    w, r = winner.risk_summary, runner_up.risk_summary
    if w.has_no_travel != r.has_no_travel:
        return "Recommended because the alternatives include a No Travel segment and this route does not."
    if w.severe_miles != r.severe_miles:
        return f"Recommended for fewer Severe-risk miles ({w.severe_miles:.1f} vs {r.severe_miles:.1f} on the next-best route)."
    if w.high_miles != r.high_miles:
        return f"Recommended for fewer High-risk miles ({w.high_miles:.1f} vs {r.high_miles:.1f} on the next-best route)."

    w_avg = w.distance_weighted_avg_risk if w.distance_weighted_avg_risk is not None else float("inf")
    r_avg = r.distance_weighted_avg_risk if r.distance_weighted_avg_risk is not None else float("inf")
    if w_avg != r_avg:
        return f"Recommended for a lower average risk ({w_avg:.2f} vs {r_avg:.2f} on the next-best route)."

    if winner.duration_s != runner_up.duration_s:
        return f"Recommended for a shorter travel time ({winner.duration_s / 3600:.1f}h vs {runner_up.duration_s / 3600:.1f}h on the next-best route)."

    return "Tied with the next-best route on every ranking criterion; listed first arbitrarily."


def recommend_routes(candidates: list[RouteCandidate]) -> Recommendation:
    """
    Lexicographic ranking, ascending (lower is better) on:
    (has_no_travel, severe_miles, high_miles, avg_risk, duration_s)

    Safety override: a route with any No Travel segment is never presented
    as a safe recommendation, even if it ranks best among the candidates —
    "shortest ETA" never compensates for a hard prohibition. If every
    candidate has a No Travel segment, the best-ranked one among them is
    still returned, but tagged BEST_AVAILABLE_UNSAFE so the caller cannot
    present it as "safe". If no candidate has any assessed mileage at all,
    the result is INSUFFICIENT_DATA — not a guess.
    """
    if not candidates:
        return Recommendation(
            status=RecommendationStatus.INSUFFICIENT_DATA,
            winner=None,
            reason="No route candidates were provided.",
            ranked=[],
        )

    scorable = [c for c in candidates if not c.risk_summary.fully_unassessed]
    if not scorable:
        return Recommendation(
            status=RecommendationStatus.INSUFFICIENT_DATA,
            winner=None,
            reason="Weather forecast data was unavailable for every checkpoint on every route.",
            ranked=[],
        )

    ranked = sorted(scorable, key=_ranking_key)
    winner = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    reason = _distinguishing_reason(winner, runner_up)

    if winner.risk_summary.has_no_travel:
        status = RecommendationStatus.BEST_AVAILABLE_UNSAFE
        reason = (
            "No candidate route is fully safe — every route includes a No Travel "
            "segment. " + reason + " This is the least-unsafe option, not a safe "
            "recommendation."
        )
    else:
        status = RecommendationStatus.SAFE

    return Recommendation(status=status, winner=winner, reason=reason, ranked=ranked)
