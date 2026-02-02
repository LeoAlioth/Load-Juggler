"""Base functions shared across all charging modes."""
import logging
from ..context import ChargeContext
from ...const import *

_LOGGER = logging.getLogger(__name__)


def check_soc_threshold_with_hysteresis(sensor, threshold_name, battery_soc, threshold, hysteresis, is_above_check=True):
    """
    Check if SOC is above/below a threshold with hysteresis.
    
    Args:
        sensor: Sensor object to store state
        threshold_name: Name of threshold for state tracking (e.g., "min_soc", "target_soc")
        battery_soc: Current battery SOC
        threshold: The SOC threshold to check against
        hysteresis: Hysteresis percentage
        is_above_check: If True, returns True when SOC is above threshold. If False, returns True when below.
    
    Returns:
        bool: Whether the condition is met (with hysteresis applied)
    """
    state_attr = f"_soc_above_{threshold_name}"
    was_above = getattr(sensor, state_attr, None)
    
    if is_above_check:
        # Check if SOC is above threshold
        if was_above is None:
            # First check - use simple comparison
            is_above = battery_soc >= threshold
        elif was_above:
            # Was above - stay above until we drop below threshold - hysteresis
            is_above = battery_soc >= (threshold - hysteresis)
        else:
            # Was below - stay below until we rise above threshold
            is_above = battery_soc >= threshold
        
        setattr(sensor, state_attr, is_above)
        return is_above
    else:
        # Check if SOC is below threshold (inverse logic)
        if was_above is None:
            is_below = battery_soc < threshold
        elif not was_above:  # was_below
            # Was below - stay below until we rise above threshold + hysteresis
            is_below = battery_soc < (threshold + hysteresis)
        else:
            # Was above - stay above until we drop below threshold
            is_below = battery_soc < threshold
        
        setattr(sensor, state_attr, not is_below)
        return is_below


def calculate_base_target_evse(context: ChargeContext, available_battery_current: float, allow_grid_import: bool = True):
    """Calculate base target EVSE current from available power sources."""
    state = context.state
    max_import_power = state[CONF_MAX_IMPORT_POWER]
    max_import_current = max_import_power / context.voltage
    
    if not context.allow_grid_charging or not allow_grid_import:
        remaining_available_import_current = 0
    else:
        remaining_available_import_current = max_import_current - context.total_import_current

    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_c_current

    if context.phases == 1:
        target_evse = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_import_current + context.phase_a_export_current + available_battery_current
        )
    elif context.phases == 2:
        target_evse = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + available_battery_current) / 2
        )
    elif context.phases == 3:
        target_evse = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            remaining_available_current_phase_c,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + context.phase_c_export_current + available_battery_current) / 3
        )
    else:
        target_evse = state.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
    
    return target_evse
