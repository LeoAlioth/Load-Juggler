"""Utility functions for Dynamic OCPP EVSE calculations."""

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
