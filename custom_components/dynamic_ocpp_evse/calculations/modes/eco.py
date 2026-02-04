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
    - Between min_soc and target_soc: Use available solar/export power, minimum of min_current (prevents grid export)
    - At target_soc with solar: Solar production rate
    - Above target_soc: Full speed (like Standard mode)
    
    Eco mode behavior without battery:
    - Use available solar/export power, minimum of min_current (prevents grid export)
    """
    # Check if battery is configured
    has_battery = context.battery_soc is not None or context.battery_power is not None
    
    if not has_battery:
        # No battery configured - use available solar/export power, minimum of min_current
        # This prevents exporting to grid by utilizing solar for EV charging
        available_solar = max(0, context.solar_surplus_current)  # Use new context field
        target_evse = max(available_solar, context.min_current)  # At least minimum rate
        target_evse = min(target_evse, context.max_evse_available, context.max_current)
        
        if target_evse < context.min_current:
            _LOGGER.debug(f"Eco mode (no battery): Target {target_evse:.1f}A below minimum {context.min_current}A - setting to 0")
            return 0
        _LOGGER.debug(f"Eco mode (no battery): Charging at {target_evse:.1f}A (solar: {available_solar:.1f}A, minimum: {context.min_current}A)")
        return target_evse
    
    battery_soc = context.battery_soc if context.battery_soc is not None else 100
    battery_soc_min = context.battery_soc_min if context.battery_soc_min is not None else DEFAULT_BATTERY_SOC_MIN
    battery_soc_target = context.battery_soc_target if context.battery_soc_target is not None else 80  # Default to 80%, not 50%
    hysteresis = context.battery_soc_hysteresis if context.battery_soc_hysteresis is not None else DEFAULT_BATTERY_SOC_HYSTERESIS
    battery_power = context.battery_power if context.battery_power is not None else 0
    battery_max_discharge_power = context.battery_max_discharge_power if context.battery_max_discharge_power is not None else 0
    
    _LOGGER.info(f"Eco mode inputs: soc={battery_soc}%, min={battery_soc_min}%, target={battery_soc_target}%, context.target={context.battery_soc_target}")
    
    # Check if below min SOC - act like no-battery system
    below_min_soc = check_soc_threshold_with_hysteresis(
        sensor, "eco_min", battery_soc, battery_soc_min, hysteresis, is_above_check=False
    )
    
    if below_min_soc:
        # Below min SOC - act like no-battery system (use solar/export + minimum)
        available_solar = max(0, context.solar_surplus_current)
        target_evse = max(available_solar, context.min_current)
        target_evse = min(target_evse, context.max_evse_available, context.max_current)
        
        if target_evse < context.min_current:
            _LOGGER.debug(f"Eco mode (below min SOC): Target {target_evse:.1f}A below minimum {context.min_current}A - setting to 0")
            return 0
        _LOGGER.debug(f"Eco mode: SOC {battery_soc:.1f}% < min {battery_soc_min}%, acting like no-battery - charging at {target_evse:.1f}A")
        return target_evse
    
    # Check if we're above target SOC (with hysteresis)
    above_target_soc = check_soc_threshold_with_hysteresis(
        sensor, "eco_target", battery_soc, battery_soc_target, hysteresis, is_above_check=True
    )
    
    if above_target_soc:
        # Above target_soc - full speed (like Standard mode)
        # If battery is charging, use that power; otherwise use discharge limit
        if battery_power < 0:
            available_battery_power = abs(battery_power)
            _LOGGER.debug(f"Eco mode: Battery charging at {available_battery_power}W, treating as available power")
        else:
            available_battery_power = max(0, battery_max_discharge_power)
        available_battery_current = available_battery_power / context.voltage if context.voltage else 0
        target_evse = calculate_base_target_evse(context, available_battery_current, allow_grid_import=True)
        # Clamp to max_evse_available
        target_evse = min(target_evse, context.max_evse_available, context.max_current)
        # Final check: if below minimum, set to 0
        if target_evse < context.min_current:
            _LOGGER.debug(f"Eco mode: Target {target_evse:.1f}A below minimum {context.min_current}A - setting to 0")
            return 0
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
        # Final check: if below minimum, set to 0
        if target_evse < context.min_current:
            _LOGGER.debug(f"Eco mode: Target {target_evse:.1f}A below minimum {context.min_current}A - setting to 0")
            return 0
        _LOGGER.debug(f"Eco mode: SOC {battery_soc}% at target with solar, rate {target_evse}A")
        return target_evse
    
    # Between min_soc and target_soc without solar charging
    # Use available solar/export power if available, otherwise charge at minimum rate
    # This prevents exporting to grid by utilizing solar for EV charging
    target_evse = calculate_base_target_evse(context, 0, allow_grid_import=False)
    target_evse = max(target_evse, context.min_current)  # At least minimum rate
    target_evse = min(target_evse, context.max_evse_available, context.max_current)
    
    # Final check: if below minimum, set to 0
    if target_evse < context.min_current:
        _LOGGER.debug(f"Eco mode: Target {target_evse:.1f}A below minimum {context.min_current}A - setting to 0")
        return 0
    _LOGGER.debug(f"Eco mode: SOC {battery_soc}% between min {battery_soc_min}% and target {battery_soc_target}% - charging at {target_evse}A (using solar/export, minimum {context.min_current}A)")
    return target_evse
