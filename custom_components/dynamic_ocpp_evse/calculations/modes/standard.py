"""Standard charging mode calculation."""
import logging
from ..context import ChargeContext
from ..utils import is_number
from .base import check_soc_threshold_with_hysteresis, calculate_base_target_evse
from ...const import *

_LOGGER = logging.getLogger(__name__)


def calculate_standard_mode(sensor, context: ChargeContext):
    """
    Calculate target current for Standard mode.
    
    Standard mode behavior with battery:
    - Below min_soc (with hysteresis): No charging
    - Above min_soc: Full speed charging with battery discharge available
    """
    state = context.state
    
    # Get battery parameters
    battery_soc = context.battery_soc if context.battery_soc is not None else 100
    battery_soc_min = context.battery_soc_min if context.battery_soc_min is not None else DEFAULT_BATTERY_SOC_MIN
    hysteresis = context.battery_soc_hysteresis if context.battery_soc_hysteresis is not None else DEFAULT_BATTERY_SOC_HYSTERESIS
    battery_power = context.battery_power if context.battery_power is not None else 0
    battery_max_discharge_power = context.battery_max_discharge_power if context.battery_max_discharge_power is not None else 0
    
    # Check if we're above minimum SOC (with hysteresis)
    above_min_soc = check_soc_threshold_with_hysteresis(
        sensor, "standard_min", battery_soc, battery_soc_min, hysteresis, is_above_check=True
    )
    
    if not above_min_soc:
        _LOGGER.debug(f"Standard mode: Battery SOC {battery_soc}% below minimum {battery_soc_min}% - no charging")
        return 0
    
    # Above min_soc - full battery discharge available
    available_battery_power = max(0, battery_max_discharge_power)
    available_battery_current = available_battery_power / context.voltage if context.voltage else 0
    
    target_evse = calculate_base_target_evse(context, available_battery_current, allow_grid_import=True)
    
    # Apply power buffer logic
    power_buffer = state.get(CONF_POWER_BUFFER, 0)
    if power_buffer is None or not is_number(power_buffer):
        power_buffer = 0
    buffer_current = power_buffer / context.voltage if context.voltage else 0
    
    target_evse_buffered = target_evse - buffer_current
    
    if target_evse_buffered < context.min_current:
        if target_evse >= context.min_current:
            _LOGGER.debug(f"Standard mode: buffered target {target_evse_buffered}A < min {context.min_current}A, using min_current")
            result = min(context.min_current, context.max_evse_available, context.max_current)
            return result
        else:
            _LOGGER.debug(f"Standard mode: both targets below min {context.min_current}A")
            result = min(target_evse, context.max_evse_available, context.max_current)
            return result
    else:
        # Clamp to max_evse_available
        result = min(target_evse_buffered, context.max_evse_available, context.max_current)
        _LOGGER.debug(f"Standard mode: SOC {battery_soc}% >= min {battery_soc_min}%, target {result}A")
        return result
