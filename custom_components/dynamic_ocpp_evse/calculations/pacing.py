"""Pacing - use the planned energy leisurely over the time left (pure Python).

GitHub issue 22 asked for a way to spread the energy a load may use over the
hours that remain instead of racing through it. A paced Excess-mode load is held
to ``energy x 1000 / hours_left`` watts, which this module turns into the
per-phase current the engine allocates in, and gates by the load's minimum
current: a load cannot run below it, so a cap under the minimum means off, and
an off load restarts only once the cap clears the minimum by a margin.
"""

from __future__ import annotations

# hours_left never drops below this: the cap would otherwise grow without bound
# as the horizon nears (or has passed), spending whatever is left in one burst.
PACE_MIN_HOURS = 1.0
# An off load restarts once the cap exceeds its minimum current by this
# fraction; a running one rides down to the minimum itself. The band is what
# stops a cap hovering at the minimum from starting and stopping the load.
PACE_START_MARGIN = 0.2
# Total draw (A, all legs) from which a load counts as running.
PACE_RUNNING_DRAW_A = 1.0


def pace_watts(energy_kwh: float, hours_left: float) -> float:
    """Power (W) that spends ``energy_kwh`` in ``hours_left`` hours.

    A negative energy (the planning sensor publishes its raw value) is 0. The
    one figure behind both the per-load pace cap and the hub-level cap of the
    forecast-driven Excess pool (calculations/forecast_excess.py).
    """
    return max(energy_kwh, 0.0) * 1000.0 / max(hours_left, PACE_MIN_HOURS)


def pace_cap_current(
    energy_kwh: float, hours_left: float, voltage: float, phases: int
) -> float:
    """Per-phase current (A) that spends ``energy_kwh`` in ``hours_left`` hours."""
    if voltage <= 0:
        return 0.0
    return pace_watts(energy_kwh, hours_left) / (voltage * max(int(phases or 1), 1))


def pace_limit(limit: float, cap: float, min_current: float, running: bool) -> float:
    """``limit`` held to the pacing ``cap`` (both per-phase A).

    0 while the cap is under the minimum current, and - for a load that is not
    running - while it is under the minimum plus the start margin.
    """
    if limit <= 0:
        return limit
    if cap < min_current:
        return 0.0
    if not running and cap < min_current * (1 + PACE_START_MARGIN):
        return 0.0
    return min(limit, cap)
