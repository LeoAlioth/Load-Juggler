"""
Calculation module for Load Juggler.

New architecture using SiteContext and LoadContext.
All calculations unified in target_calculator.py.
"""

from .models import SiteContext, LoadContext, PhaseConstraints, PhaseValues, CircuitGroup
from .target_calculator import calculate_all_charger_targets, excess_margin
from .forecast import (
    ClippingForecast,
    merge_forecast_series,
    clipping_forecast,
    battery_max_soc,
    headroom_deficit_kwh,
    unexportable_power,
    recommended_charge_limit,
)

__all__ = [
    "SiteContext",
    "LoadContext",
    "PhaseConstraints",
    "PhaseValues",
    "CircuitGroup",
    "calculate_all_charger_targets",
    "excess_margin",
    "ClippingForecast",
    "merge_forecast_series",
    "clipping_forecast",
    "battery_max_soc",
    "headroom_deficit_kwh",
    "unexportable_power",
    "recommended_charge_limit",
]