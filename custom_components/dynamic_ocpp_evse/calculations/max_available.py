"""Maximum EVSE available current calculation."""
import logging
from .context import ChargeContext
from ..const import *

_LOGGER = logging.getLogger(__name__)


def calculate_max_evse_available(context: ChargeContext):
    """Calculate maximum available EVSE current based on context."""
    state = context.state
    max_import_power = state[CONF_MAX_IMPORT_POWER]
    max_import_current = max_import_power / context.voltage
    
    remaining_available_import_current = max_import_current - context.total_import_current

    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_c_current
    
    _LOGGER.debug(f"Calculating max EVSE available current with context: {context}")
    _LOGGER.debug(f"Max import current: {max_import_current}A, Total import current: {context.total_import_current}A, Remaining available import current: {remaining_available_import_current}A")
    _LOGGER.debug(f"Remaining available current - Phase A: {remaining_available_current_phase_a}A, Phase B: {remaining_available_current_phase_b}A, Phase C: {remaining_available_current_phase_c}A")

    # Battery discharge logic
    battery_power = context.battery_power if context.battery_power is not None else 0
    battery_max_discharge_power = context.battery_max_discharge_power if context.battery_max_discharge_power is not None else 0
    battery_soc = context.battery_soc if context.battery_soc is not None else 0
    battery_soc_target = context.battery_soc_target if context.battery_soc_target is not None else 0

    if battery_soc > battery_soc_target:
        available_battery_power = max(0, battery_max_discharge_power - battery_power)
    else:
        available_battery_power = max(0, -battery_power)
    available_battery_current = (available_battery_power / context.voltage) if context.voltage else 0
    
    if context.phases == 1:
        max_evse_available = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_import_current + context.phase_a_export_current + available_battery_current
        )
        _LOGGER.debug(f"Max EVSE available (1 phase): {max_evse_available}A")
        return max_evse_available
    elif context.phases == 2:
        max_evse_available = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + available_battery_current) / 2
        )
        _LOGGER.debug(f"Max EVSE available (2 phases): {max_evse_available}A")
        return max_evse_available
    elif context.phases == 3:
        max_evse_available = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            remaining_available_current_phase_c,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + context.phase_c_export_current + available_battery_current) / 3
        )
        _LOGGER.debug(f"Max EVSE available (3 phases): {max_evse_available}A")
        return max_evse_available
    else:
        return state.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
