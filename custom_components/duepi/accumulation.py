"""Pure accumulation helpers for Duepi runtime sensors."""

from __future__ import annotations


def pellet_rate(
    power: int | None,
    kg_per_hour_min: float,
    kg_per_hour_max: float,
) -> float:
    """Estimate pellet use for a valid power level from one through five."""
    if power is None or not 1 <= power <= 5:
        return 0.0
    position = (power - 1) / 4
    return kg_per_hour_min + position * (kg_per_hour_max - kg_per_hour_min)


def capped_elapsed_seconds(
    previous_tick: float | None,
    current_tick: float,
    scan_interval_seconds: float,
) -> float:
    """Return non-negative elapsed time capped at two polling intervals."""
    if previous_tick is None:
        return 0.0
    elapsed = max(0.0, current_tick - previous_tick)
    return min(elapsed, max(0.0, 2 * scan_interval_seconds))
