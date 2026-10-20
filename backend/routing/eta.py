"""
ETA calculation: departure time + cumulative travel duration up to each
checkpoint, where duration is interpolated from the routing provider's
per-segment duration annotations (its turn-by-turn "steps", each with its
own distance and duration) rather than (distance / average speed).

Pure, no I/O. The provider-specific step format is translated into a
provider-agnostic list of DurationBreakpoints by the routing client
(integrations/routing_client.py) before it reaches this module.

Timezone policy: departure must be a timezone-aware datetime. It is
converted to UTC immediately and every ETA is computed and returned in
UTC. This sidesteps per-checkpoint local-timezone / DST-transition
handling for routes that cross time zones — a documented limitation, not
a silent assumption (see README "Known limitations").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from routing.checkpoints import Checkpoint

_EPSILON_MI = 1e-6


@dataclass(frozen=True)
class DurationBreakpoint:
    """Cumulative (distance, duration) at one point along the route."""

    distance_mi: float
    cumulative_duration_s: float


@dataclass(frozen=True)
class CheckpointETA:
    checkpoint: Checkpoint
    eta_utc: datetime
    elapsed_duration_s: float


def _validate_breakpoints(breakpoints: list[DurationBreakpoint]) -> None:
    if len(breakpoints) < 2:
        raise ValueError("need at least 2 duration breakpoints (start and end)")
    if breakpoints[0].distance_mi != 0 or breakpoints[0].cumulative_duration_s != 0:
        raise ValueError("first breakpoint must be (distance=0, duration=0)")
    for prev, cur in zip(breakpoints, breakpoints[1:]):
        if cur.distance_mi < prev.distance_mi:
            raise ValueError("breakpoint distances must be non-decreasing")
        if cur.cumulative_duration_s < prev.cumulative_duration_s:
            raise ValueError("breakpoint durations must be non-decreasing")


def interpolate_duration_s(
    breakpoints: list[DurationBreakpoint], target_distance_mi: float
) -> float:
    """Elapsed duration (seconds) at target_distance_mi, linearly interpolated
    within the bracketing provider step."""
    _validate_breakpoints(breakpoints)
    last = breakpoints[-1]
    if target_distance_mi > last.distance_mi + _EPSILON_MI:
        raise ValueError(
            f"target_distance_mi {target_distance_mi} exceeds route total {last.distance_mi}"
        )
    target_distance_mi = min(target_distance_mi, last.distance_mi)

    for prev, cur in zip(breakpoints, breakpoints[1:]):
        if prev.distance_mi <= target_distance_mi <= cur.distance_mi:
            span = cur.distance_mi - prev.distance_mi
            if span == 0:
                return prev.cumulative_duration_s
            frac = (target_distance_mi - prev.distance_mi) / span
            return prev.cumulative_duration_s + frac * (
                cur.cumulative_duration_s - prev.cumulative_duration_s
            )
    return last.cumulative_duration_s


def compute_checkpoint_etas(
    checkpoints: list[Checkpoint],
    breakpoints: list[DurationBreakpoint],
    departure: datetime,
) -> list[CheckpointETA]:
    if departure.tzinfo is None:
        raise ValueError("departure must be timezone-aware")
    departure_utc = departure.astimezone(timezone.utc)

    results = []
    for checkpoint in checkpoints:
        elapsed_s = interpolate_duration_s(breakpoints, checkpoint.distance_mi)
        eta = departure_utc + timedelta(seconds=elapsed_s)
        results.append(
            CheckpointETA(checkpoint=checkpoint, eta_utc=eta, elapsed_duration_s=elapsed_s)
        )
    return results
