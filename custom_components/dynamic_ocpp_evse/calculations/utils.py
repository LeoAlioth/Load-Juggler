"""Utility functions for Load Juggler calculations."""

from __future__ import annotations
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SiteContext, PhaseValues


def is_number(value):
    """Check if a value can be converted to a float."""
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def hold_per_phase_floor(
    new: PhaseValues | None,
    held: PhaseValues | None,
    decay: float,
) -> PhaseValues | None:
    """Asymmetric per-phase floor hold: fast to rise, slow to fall.

    Per phase the result is ``max(new, held * decay)`` - a rise is passed
    through instantly, a fall is bounded by the decayed previous value.

    None is pass-through in both directions: a phase that is None in ``new``
    stays None (the phase does not exist on this site), and a None in ``held``
    means there is nothing to hold, so ``new`` is taken as-is.

    ``decay`` is the per-cycle retention factor (0..1); the caller derives it
    from wall-clock time so the bridge length is independent of cycle length.
    """
    from .models import PhaseValues  # Local import to avoid circular

    if new is None:
        return None
    if held is None:
        return new

    decay = min(1.0, max(0.0, decay))

    def _floor(n, h):
        if n is None:
            return None
        if h is None:
            return n
        return max(n, h * decay)

    return PhaseValues(
        _floor(new.a, held.a),
        _floor(new.b, held.b),
        _floor(new.c, held.c),
    )


def managed_phase_draws(site: SiteContext) -> list[float]:
    """Per site phase (A, ``[a, b, c]``), what the loads we manage draw.

    A load whose Dynamic Control is OFF is left out: its draw is household
    consumption, which is what an unmanaged load is (see
    ``LoadContext.dynamic_control``).
    """
    draws = [0.0, 0.0, 0.0]
    for load in site.loads:
        if load.dynamic_control:
            for i, draw in enumerate(load.get_site_phase_draw()):
                draws[i] += draw
    return draws


def grid_without_managed_draws(
    consumption: PhaseValues,
    export: PhaseValues,
    draws: tuple[float, float, float],
    gridless: tuple[bool, bool, bool] = (False, False, False),
) -> tuple[PhaseValues, PhaseValues]:
    """Rebuild the grid readings the site would show with our loads switched off.

    Grid CTs measure the whole site, managed draws included. Per phase the raw
    signed meter reading is ``consumption - export`` (positive = importing); the
    load's own draw comes off it and the result is re-split into the
    import/export pair the engine works with.

    Phases that are None (not present on this site) stay None. A ``gridless``
    phase (an inverter feeds it, no grid CT reads it) keeps its synthetic zeros:
    they never contained the load's draw, so taking it off would fabricate export.
    Pure function -
    the caller owns the logging and writes the result back onto the site.
    """
    from .models import PhaseValues  # Local import to avoid circular

    adj_consumption: list[float | None] = []
    adj_export: list[float | None] = []
    for i in range(3):
        cons = (consumption.a, consumption.b, consumption.c)[i]
        exp = (export.a, export.b, export.c)[i]
        if cons is None:
            adj_consumption.append(None)
            adj_export.append(None)
            continue
        if gridless[i]:
            adj_consumption.append(cons)
            adj_export.append(exp)
            continue
        true_grid = cons - (exp or 0) - draws[i]
        adj_consumption.append(max(0.0, true_grid))
        adj_export.append(max(0.0, -true_grid))

    return PhaseValues(*adj_consumption), PhaseValues(*adj_export)


def household_per_phase(
    grid: Sequence[float | None],
    net: PhaseValues | None,
    draws: Sequence[float],
) -> PhaseValues | None:
    """Per-phase household (A), the one sum for every site arrangement:

        household[p] = max(0, grid[p] + net[p] - draws[p])

    ``grid`` is the signed per-phase grid reading (+ import), before the
    feedback loop and after phase remaps (None = no such phase, off-grid 0 on
    the phases with outputs); ``net`` is Σ inverters (output - input)
    (``engine/fleet.net_output``, None = no output sensors); ``draws`` is the
    smoothed managed draw per site phase, the one the feedback loop subtracts.
    Returns None when there are no output sensors.
    """
    from .models import PhaseValues  # Local import to avoid circular

    if net is None:
        return None
    return PhaseValues(*[
        None if g is None else max(0, g + (n or 0) - d)
        for g, n, d in zip(grid, (net.a, net.b, net.c), draws)
    ])
