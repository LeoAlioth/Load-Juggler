"""Utility functions for Load Juggler calculations."""

from __future__ import annotations
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
) -> tuple[PhaseValues, PhaseValues]:
    """Rebuild the grid readings the site would show with our loads switched off.

    Grid CTs measure the whole site, managed draws included. Per phase the raw
    signed meter reading is ``consumption - export`` (positive = importing); the
    load's own draw comes off it and the result is re-split into the
    import/export pair the engine works with.

    Phases that are None (not present on this site) stay None. Pure function -
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
        true_grid = cons - (exp or 0) - draws[i]
        adj_consumption.append(max(0.0, true_grid))
        adj_export.append(max(0.0, -true_grid))

    return PhaseValues(*adj_consumption), PhaseValues(*adj_export)


def compute_household_per_phase(
    site: SiteContext,
    wiring_topology: str,
    draws: tuple[float, float, float] | None = None,
) -> PhaseValues | None:
    """Compute per-phase household consumption from inverter output entities.

    Shared between HA integration (engine/hub_calculation.py) and test simulation (run_tests.py).

    Parallel (AC-coupled): household = grid_consumption + inverter_output - grid_export
    Series (hybrid):       household = inverter_output - load_draws
    Off-grid, either:      household = inverter_output - load_draws

    Off-grid the wiring changes nothing about what the output contains: with
    no grid for a parallel inverter to feed beside, everything the site
    consumes - our own loads included - comes out of the inverters. The
    parallel formula leans on the feedback loop having taken the draws off its
    grid terms; off-grid those are synthetic zeros it leaves alone, so the
    parallel household was the whole output and a charger's own draw was read
    as house load (a car meant to get 21.7 A settled at 10.9 A,
    dev/tests/test_offgrid_parallel_household.py).

    ``draws`` is the per-site-phase managed draw the series formula subtracts.
    The HA engine passes the SAME smoothed draw it subtracts everywhere else
    (``hub_calculation._managed_phase_draws``), because the inverter output it
    is subtracted from is smoothed too: a smoothed output minus a raw
    draw reads the household low for every cycle the output filter lags a
    charger's start. None sums every load's own reading - the single-cycle
    scenario runner, where there is no filter state for the two to disagree on.

    Returns PhaseValues with per-phase household in Amps, or None if no inverter output data.
    """
    from .models import PhaseValues  # Local import to avoid circular

    if site.inverter_output_per_phase is None:
        return None

    if draws is None:
        # Every load's own draw per site phase
        draws = [0.0, 0.0, 0.0]
        for c in site.loads:
            for i, d in enumerate(c.get_site_phase_draw()):
                draws[i] += d

    out = site.inverter_output_per_phase
    per_phase = zip(
        (out.a, out.b, out.c),
        (site.consumption.a, site.consumption.b, site.consumption.c),
        (site.export_current.a, site.export_current.b, site.export_current.c),
        draws,
    )
    if wiring_topology == "parallel" and not site.is_off_grid:
        household = [
            None if cons is None else max(0, (cons or 0) + (inv or 0) - (exp or 0))
            for inv, cons, exp, _ in per_phase
        ]
    else:
        # Series, and off-grid either wiring: household = inverter_output - load_draws
        household = [
            None if cons is None else max(0, (inv or 0) - draw)
            for inv, cons, _, draw in per_phase
        ]
    return PhaseValues(*household)
