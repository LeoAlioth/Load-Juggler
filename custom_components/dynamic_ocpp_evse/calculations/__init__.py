"""
Calculation module for Load Juggler.

New architecture using SiteContext and LoadContext.
All calculations unified in target_calculator.py.
"""

from .models import SiteContext, LoadContext, PhaseValues, CircuitGroup
from .target_calculator import (
    calculate_all_load_targets,
    discharge_headroom_unknown,
    excess_load_draw_power,
    household_unknown,
    excess_margin,
    grid_overdraw,
    reconstructed_export_power,
    sun_power,
)
from .forecast import (
    FORECAST_EARLY_START_FACTOR,
    FORECAST_LOOKAHEAD_DAYS,
    merge_forecast_series,
    scale_forecast_series,
    clipping_forecast,
    select_clipping_window,
    first_production_at,
    hours_to_shed,
    reservation_is_due,
    battery_max_soc,
    headroom_deficit_kwh,
    recommended_charge_limit,
    yields_to_excess,
)

__all__ = [
    "SiteContext",
    "LoadContext",
    "PhaseValues",
    "CircuitGroup",
    "calculate_all_load_targets",
    "discharge_headroom_unknown",
    "household_unknown",
    "excess_load_draw_power",
    "excess_margin",
    "grid_overdraw",
    "reconstructed_export_power",
    "sun_power",
    "FORECAST_EARLY_START_FACTOR",
    "FORECAST_LOOKAHEAD_DAYS",
    "merge_forecast_series",
    "scale_forecast_series",
    "clipping_forecast",
    "select_clipping_window",
    "first_production_at",
    "hours_to_shed",
    "reservation_is_due",
    "battery_max_soc",
    "headroom_deficit_kwh",
    "recommended_charge_limit",
    "yields_to_excess",
]