"""
Dynamic OCPP EVSE - Main calculation module.

This file maintains backward compatibility by re-exporting from the calculations package.
All calculation logic has been refactored into the calculations/ directory.
"""

# Re-export everything from the calculations package for backward compatibility
from .calculations import (
    calculate_available_current_for_hub,
    ChargeContext,
    calculate_charger_available_current,
    calculate_standard_mode,
    calculate_eco_mode,
    calculate_solar_mode,
    calculate_excess_mode,
)

# Re-export utility functions that might be used elsewhere
from .calculations.utils import (
    is_number,
    get_sensor_data,
    get_sensor_attribute,
    apply_ramping,
)

from .calculations.context import (
    get_hub_state_config,
    get_charge_context_values,
    determine_phases,
)

__all__ = [
    # Main function
    "calculate_available_current_for_hub",
    # Context
    "ChargeContext",
    "get_hub_state_config",
    "get_charge_context_values",
    "determine_phases",
    # Calculations
    "calculate_charger_available_current",
    # Modes
    "calculate_standard_mode",
    "calculate_eco_mode",
    "calculate_solar_mode",
    "calculate_excess_mode",
    # Utilities
    "is_number",
    "get_sensor_data",
    "get_sensor_attribute",
    "apply_ramping",
]
