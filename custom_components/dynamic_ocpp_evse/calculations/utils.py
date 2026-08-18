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

    Per phase the result is ``max(new, held * decay)`` — a rise is passed
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

    Phases that are None (not present on this site) stay None. Pure function —
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
) -> PhaseValues | None:
    """Compute per-phase household consumption from inverter output entities.

    Shared between HA integration (dynamic_ocpp_evse.py) and test simulation (run_tests.py).

    Parallel (AC-coupled): household = grid_consumption + inverter_output - grid_export
    Series (hybrid):       household = inverter_output - charger_draws

    Returns PhaseValues with per-phase household in Amps, or None if no inverter output data.
    """
    from .models import PhaseValues  # Local import to avoid circular

    if site.inverter_output_per_phase is None:
        return None

    # Accumulate charger draws per site phase
    ch_a = ch_b = ch_c = 0.0
    for c in site.chargers:
        a_d, b_d, c_d = c.get_site_phase_draw()
        ch_a += a_d
        ch_b += b_d
        ch_c += c_d

    if wiring_topology == "parallel":
        def _hh(inv_out, cons, exp):
            if cons is None:
                return None
            return max(0, (cons or 0) + (inv_out or 0) - (exp or 0))

        hh_a = _hh(site.inverter_output_per_phase.a, site.consumption.a, site.export_current.a)
        hh_b = _hh(site.inverter_output_per_phase.b, site.consumption.b, site.export_current.b)
        hh_c = _hh(site.inverter_output_per_phase.c, site.consumption.c, site.export_current.c)
    else:
        # Series: household = inverter_output - charger_draws
        hh_a = max(0, (site.inverter_output_per_phase.a or 0) - ch_a) if site.consumption.a is not None else None
        hh_b = max(0, (site.inverter_output_per_phase.b or 0) - ch_b) if site.consumption.b is not None else None
        hh_c = max(0, (site.inverter_output_per_phase.c or 0) - ch_c) if site.consumption.c is not None else None

    return PhaseValues(hh_a, hh_b, hh_c)
