"""
Calculation module for Load Juggler.

New architecture using SiteContext and LoadContext.
All calculations unified in target_calculator.py.
"""

from .models import SiteContext, LoadContext, PhaseConstraints, PhaseValues, CircuitGroup
from .target_calculator import (
    calculate_all_load_targets,
    excess_load_draw_power,
    excess_margin,
    reconstructed_export_power,
)
from .forecast import (
    ClippingForecast,
    FORECAST_TRIM_CLAMP_W,
    FORECAST_TRIM_MAX_STEP_S,
    FORECAST_TRIM_TAU_S,
    merge_forecast_series,
    clipping_forecast,
    battery_max_soc,
    export_trim,
    headroom_deficit_kwh,
    unexportable_power,
    recommended_charge_limit,
    yields_to_excess,
)

__all__ = [
    "SiteContext",
    "LoadContext",
    "PhaseConstraints",
    "PhaseValues",
    "CircuitGroup",
    "calculate_all_load_targets",
    "excess_load_draw_power",
    "excess_margin",
    "reconstructed_export_power",
    "ClippingForecast",
    "FORECAST_TRIM_CLAMP_W",
    "FORECAST_TRIM_MAX_STEP_S",
    "FORECAST_TRIM_TAU_S",
    "merge_forecast_series",
    "clipping_forecast",
    "battery_max_soc",
    "export_trim",
    "headroom_deficit_kwh",
    "unexportable_power",
    "recommended_charge_limit",
    "yields_to_excess",
]