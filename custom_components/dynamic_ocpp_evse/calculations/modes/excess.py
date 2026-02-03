"""Excess charging mode calculation."""
import datetime
import logging
from ..context import ChargeContext
from .base import check_soc_threshold_with_hysteresis, calculate_base_target_evse
from ...const import *

_LOGGER = logging.getLogger(__name__)


def calculate_excess_mode(sensor, context: ChargeContext):
    """
    Calculate target current for Excess mode.
    
    Excess mode behavior with battery:
    - Below min_soc (with hysteresis): No charging
    - Between min_soc and target_soc: Export power only (no battery discharge for EV)
    - Between target_soc and 98%: Export power, slow battery discharge OK
    - At/above 98%: Match solar production (like Solar mode)
    """
    state = context.state
    voltage = context.voltage
    total_export_power = context.total_export_power
    base_threshold = state.get(CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD)
    
    battery_soc = context.battery_soc if context.battery_soc is not None else 100
    battery_soc_min = context.battery_soc_min if context.battery_soc_min is not None else DEFAULT_BATTERY_SOC_MIN
    battery_soc_target = context.battery_soc_target if context.battery_soc_target is not None else 80  # Default to 80%
    hysteresis = context.battery_soc_hysteresis if context.battery_soc_hysteresis is not None else DEFAULT_BATTERY_SOC_HYSTERESIS
    battery_power = context.battery_power if context.battery_power is not None else 0
    
    
    # If battery is nearly full (>= 98%), act like solar mode - match production
    if battery_soc >= 98:
        available_battery_current = max(0, -battery_power / voltage) if voltage else 0
        target_evse = calculate_base_target_evse(context, available_battery_current, allow_grid_import=False)
        _LOGGER.debug(f"Excess mode: Battery nearly full ({battery_soc}%), matching solar production {target_evse}A")
        return max(target_evse, 0)
    
    # Calculate threshold - add battery charge capacity if battery not at target
    if battery_soc < battery_soc_target:
        battery_max_charge_power = state.get(CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER)
    else:
        battery_max_charge_power = 0
    
    threshold = base_threshold + (battery_max_charge_power if battery_max_charge_power else 0)
    now = datetime.datetime.now()
    
    if total_export_power > threshold:
        _LOGGER.info(f"Excess mode: export {total_export_power}W > threshold {threshold}W, starting charge")
        sensor._excess_charge_start_time = now
    
    keep_charging = False
    if getattr(sensor, '_excess_charge_start_time', None) is not None and \
       (now - sensor._excess_charge_start_time).total_seconds() < 15 * 60:
        if total_export_power + context.min_current * voltage > threshold:
            sensor._excess_charge_start_time = now
        keep_charging = True
    
    if keep_charging:
        export_available_current = (total_export_power - threshold) / voltage + context.evse_current_per_phase
        target_evse = max(context.min_current, export_available_current)
    else:
        target_evse = 0
    
    target_evse = min(target_evse, context.max_current, context.max_evse_available)
    
    # Final check: if below minimum, set to 0
    if target_evse < context.min_current:
        _LOGGER.debug(f"Excess mode: Target {target_evse:.1f}A below minimum {context.min_current}A - setting to 0")
        return 0
    
    _LOGGER.debug(f"Excess mode: SOC {battery_soc}%, export {total_export_power}W, target {target_evse}A")
    return target_evse
