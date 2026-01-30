"""Eco charging mode calculation."""
import logging
from ..context import ChargeContext
from .base import check_soc_threshold_with_hysteresis, calculate_base_target_evse
from ...const import *

_LOGGER = logging.getLogger(__name__)


def calculate_eco_mode(sensor, context: ChargeContext):
    """
    Calculate target current for Eco mode.
    
    Eco mode behavior with battery:
    - Below min_soc (with hysteresis): No charging
    - Between min_soc and target_soc: Minimum rate charging (no battery discharge)
    - At target_soc: Solar production rate
    - Above target_soc: Full speed (like Standard mode)
    """
    battery_soc = context.battery_soc if context.battery_soc is not None else 100
    battery_soc_min = context.battery_soc_min if context.battery_soc_min is not None else DEFAULT_BATTERY_SOC_MIN
    battery_soc_target = context.battery_soc_target if context.battery_soc_target is not None else 50
    hysteresis = context.battery_soc_hysteresis if context.battery_soc_hysteresis is not None else DEFAULT_BATTERY_SOC_HYSTERESIS
    battery_power = context.battery_power if context.battery_power is not None else 0
    battery_max_discharge_power = context.battery_max_discharge_power if context.battery_max_discharge_power is not None else 0
    
    # Check if we're above minimum SOC (with hysteresis)
    above_min_soc = check_soc_threshold_with_hysteresis(
        sensor, "eco_min", battery_soc, battery_soc_min, hysteresis, is_above_check=True
    )
    
    if not above_min_soc:
        _LOGGER.debug(f"Eco mode: Battery SOC {battery_soc}% below minimum {battery_soc_min}% - no charging")
        return 0
    
    # Check if we're above target SOC (with hysteresis)
    above_target_soc = check_soc_threshold_with_hysteresis(
        sensor, "eco_target", battery_soc, battery_soc_target, hysteresis, is_above_check=True
    )
    
    if above_target_soc:
        # Above target_soc - full speed (like Standard mode)
        available_battery_power = max(0, battery_max_discharge_power)
        available_battery_current = available_battery_power / context.voltage if context.voltage else 0
        target_evse = calculate_base_target_evse(context, available_battery_current, allow_grid_import=True)
        # Clamp to max_evse_available
        target_evse = min(target_evse, context.max_evse_available, context.max_current)
        _LOGGER.debug(f"Eco mode: SOC {battery_soc}% > target {battery_soc_target}%, full speed {target_evse}A")
        return target_evse
    
    # Between min_soc and target_soc
    # Check if at target_soc (battery is charging = has solar)
    battery_is_charging = battery_power < 0
    at_target_with_solar = battery_soc >= battery_soc_target and battery_is_charging
    
    if at_target_with_solar:
        # At target with solar - charge at solar rate
        available_battery_current = max(0, -battery_power / context.voltage) if context.voltage else 0
        target_evse = calculate_base_target_evse(context, available_battery_current, allow_grid_import=False)
        target_evse = max(target_evse, context.min_current)
        # Clamp to max_evse_available
        target_evse = min(target_evse, context.max_evse_available, context.max_current)
        _LOGGER.debug(f"Eco mode: SOC {battery_soc}% at target with solar, rate {target_evse}A")
        return target_evse
    
    # Between min_soc and target_soc without solar - minimum rate only (clamped to available)
    target_evse = min(context.min_current, context.max_evse_available, context.max_current)
    _LOGGER.debug(f"Eco mode: SOC {battery_soc}% between min {battery_soc_min}% and target {battery_soc_target}% - min rate {target_evse}A (clamped from {context.min_current}A)")
    return target_evse
