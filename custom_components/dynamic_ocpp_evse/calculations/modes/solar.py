"""Solar charging mode calculation."""
import logging
from ..context import ChargeContext
from .base import check_soc_threshold_with_hysteresis, calculate_base_target_evse
from ...const import *

_LOGGER = logging.getLogger(__name__)


def calculate_solar_mode(sensor, context: ChargeContext):
    """
    Calculate target current for Solar mode.
    
    Solar mode behavior with battery:
    - Below target_soc (with hysteresis): No charging
    - At/above target_soc: Charge at solar production rate only (no battery discharge for EV)
    
    Note: Charging starts when battery is charging (has excess solar) at target_soc,
    stops when SOC drops below target_soc - hysteresis.
    """
    battery_soc = context.battery_soc if context.battery_soc is not None else 100
    battery_soc_target = context.battery_soc_target if context.battery_soc_target is not None else 80  # Default to 80%
    hysteresis = context.battery_soc_hysteresis if context.battery_soc_hysteresis is not None else DEFAULT_BATTERY_SOC_HYSTERESIS
    battery_power = context.battery_power if context.battery_power is not None else 0
    
    # Check if we're above target SOC (with hysteresis)
    above_target_soc = check_soc_threshold_with_hysteresis(
        sensor, "solar_target", battery_soc, battery_soc_target, hysteresis, is_above_check=True
    )
    
    # For starting to charge, battery should be charging (negative power = charging) or SOC above target
    battery_is_charging = battery_power < 0
    
    if not above_target_soc and not battery_is_charging:
        _LOGGER.debug(f"Solar mode: Battery SOC {battery_soc}% below target {battery_soc_target}% and not charging - no EV charging")
        return 0
    
    if not above_target_soc:
        _LOGGER.debug(f"Solar mode: Battery SOC {battery_soc}% below target {battery_soc_target}% - no EV charging")
        return 0
    
    # At/above target_soc - use solar only (no battery discharge for EV)
    # Only consider current battery discharge that's already happening
    available_battery_current = max(0, -battery_power / context.voltage) if context.voltage else 0
    
    target_evse = calculate_base_target_evse(context, available_battery_current, allow_grid_import=False)
    
    _LOGGER.debug(f"Solar mode: SOC {battery_soc}% >= target {battery_soc_target}%, solar rate {target_evse}A")
    return max(target_evse, 0)
