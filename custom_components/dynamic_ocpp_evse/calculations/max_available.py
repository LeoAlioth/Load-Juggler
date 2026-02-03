"""Per-charger available current calculation for specific phase configuration."""
import logging
from .context import ChargeContext
from ..const import *

_LOGGER = logging.getLogger(__name__)


def calculate_charger_available_current(context: ChargeContext, charger_phases: int):
    """
    Calculate available current for a specific charger based on its phase configuration.
    
    Args:
        context: ChargeContext with site-wide data
        charger_phases: Number of phases this specific charger is using (1, 2, or 3)
    
    Returns:
        float: Maximum current available per phase for this charger
    """
    state = context.state
    max_import_power = state[CONF_MAX_IMPORT_POWER]
    max_import_current = max_import_power / context.voltage
    
    remaining_available_import_current = max_import_current - context.total_import_current

    # Calculate remaining available current per phase (what site has available)
    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_c_current
    
    _LOGGER.debug(f"Calculating available current for {charger_phases}-phase charger")
    _LOGGER.debug(f"Site remaining - Phase A: {remaining_available_current_phase_a}A, Phase B: {remaining_available_current_phase_b}A, Phase C: {remaining_available_current_phase_c}A")

    # Battery discharge logic - respects min_soc threshold
    battery_power = context.battery_power if context.battery_power is not None else 0
    battery_max_discharge_power = context.battery_max_discharge_power if context.battery_max_discharge_power is not None else DEFAULT_BATTERY_MAX_POWER
    battery_soc = context.battery_soc if context.battery_soc is not None else 0
    battery_soc_min = context.battery_soc_min if context.battery_soc_min is not None else DEFAULT_BATTERY_SOC_MIN

    # Calculate available battery power
    if battery_soc >= battery_soc_min:
        if battery_power > 0:
            available_battery_power = max(0, battery_max_discharge_power - battery_power)
        else:
            available_battery_power = battery_max_discharge_power
    else:
        available_battery_power = 0
    
    available_battery_current = (available_battery_power / context.voltage) if context.voltage else 0
    _LOGGER.debug(f"Battery available: {available_battery_power}W = {available_battery_current}A")
    
    # Calculate available current based on charger's phase configuration
    # Take minimum across the phases the charger is actually using
    if charger_phases == 1:
        # Single phase - use phase A only
        charger_available = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_import_current + context.phase_a_export_current + available_battery_current
        )
        _LOGGER.debug(f"Charger available (1 phase on A): {charger_available}A")
        
    elif charger_phases == 2:
        # Two phases - use A and B, take minimum
        charger_available = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + available_battery_current) / 2
        )
        _LOGGER.debug(f"Charger available (2 phases on A+B): {charger_available}A per phase")
        
    elif charger_phases == 3:
        # Three phases - use A, B, and C, take minimum
        charger_available = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            remaining_available_current_phase_c,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + context.phase_c_export_current + available_battery_current) / 3
        )
        _LOGGER.debug(f"Charger available (3 phases on A+B+C): {charger_available}A per phase")
        
    else:
        _LOGGER.warning(f"Invalid phase count {charger_phases}, defaulting to minimum")
        charger_available = state.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
    
    return charger_available
