"""
Dynamic OCPP EVSE Calculations Module.

This module contains all the calculation logic for determining available charging current.
"""
import logging
from .context import ChargeContext, get_hub_state_config, get_charge_context_values
from .max_available import calculate_max_evse_available
from .utils import apply_ramping
from .modes import (
    calculate_standard_mode,
    calculate_eco_mode,
    calculate_solar_mode,
    calculate_excess_mode,
)
from ..const import *

_LOGGER = logging.getLogger(__name__)


def calculate_available_current_for_hub(sensor):
    """
    Calculate available current at hub level.
    This is the main function called by the charger sensor.
    """
    _LOGGER.debug("Calculating available current for hub")

    state = get_hub_state_config(sensor)
    charge_context = get_charge_context_values(sensor, state)

    # Calculate max_evse_available using context
    max_evse_available = calculate_max_evse_available(charge_context)
    charge_context.max_evse_available = max_evse_available

    target_evse_standard = calculate_standard_mode(sensor, charge_context)
    target_evse_eco = calculate_eco_mode(sensor, charge_context)
    target_evse_solar = calculate_solar_mode(sensor, charge_context)
    target_evse_excess = calculate_excess_mode(sensor, charge_context)

    charging_mode = state[CONF_CHARING_MODE]
    if charging_mode == 'Standard':
        target_evse = target_evse_standard
    elif charging_mode == 'Eco':
        target_evse = target_evse_eco
    elif charging_mode == 'Solar':
        target_evse = target_evse_solar
    elif charging_mode == 'Excess':
        target_evse = target_evse_excess
    else:
        target_evse = target_evse_standard

    # Clamp target_evse to max_current and max_evse_available
    target_evse = min(target_evse, charge_context.max_current, max_evse_available)

    # Clamp to available
    state[CONF_AVAILABLE_CURRENT] = min(max_evse_available, target_evse)

    # Apply ramping logic
    apply_ramping(sensor, state, target_evse, charge_context.min_current, 
                  CONF_EVSE_CURRENT_OFFERED, CONF_AVAILABLE_CURRENT)
    
    if state[CONF_AVAILABLE_CURRENT] < state[CONF_EVSE_MINIMUM_CHARGE_CURRENT]:
        state[CONF_AVAILABLE_CURRENT] = 0
    if state[CONF_AVAILABLE_CURRENT] > state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]:
        state[CONF_AVAILABLE_CURRENT] = state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]

    # Calculate available battery power for EV charging
    battery_soc = charge_context.battery_soc if charge_context.battery_soc is not None else 0
    battery_soc_min = charge_context.battery_soc_min if charge_context.battery_soc_min is not None else DEFAULT_BATTERY_SOC_MIN
    battery_soc_target = charge_context.battery_soc_target if charge_context.battery_soc_target is not None else 0
    battery_power = charge_context.battery_power if charge_context.battery_power is not None else 0
    battery_max_discharge_power = charge_context.battery_max_discharge_power if charge_context.battery_max_discharge_power is not None else DEFAULT_BATTERY_MAX_POWER
    
    _LOGGER.debug(f"Battery values: soc={battery_soc}%, min={battery_soc_min}%, target={battery_soc_target}%, max_discharge={battery_max_discharge_power}W")
    
    # Available battery power for EV charging depends on SOC level and charging mode
    # - Below min_soc: No battery power for EV (protect battery)
    # - Between min_soc and target_soc: In Eco mode, only minimum rate (no battery discharge)
    # - Above target_soc: Full battery discharge available for EV
    if battery_soc >= battery_soc_min:
        if battery_soc > battery_soc_target:
            # Above target - full discharge available
            available_battery_power = battery_max_discharge_power
        else:
            # Between min and target - battery is available but mode determines usage
            # Show full potential so user can see battery IS available
            available_battery_power = battery_max_discharge_power
    else:
        # Below min - no battery power for EV
        available_battery_power = 0
    
    _LOGGER.info(
        f"Hub calculation: battery_soc={charge_context.battery_soc}%, "
        f"battery_soc_min={charge_context.battery_soc_min}%, "
        f"battery_soc_target={charge_context.battery_soc_target}%, "
        f"mode={charging_mode}, "
        f"target_evse={target_evse}A, "
        f"max_evse_available={max_evse_available}A, "
        f"available_battery_power={available_battery_power}W"
    )
    
    # Log reason for target_evse value
    if target_evse == 0:
        if battery_soc < battery_soc_min:
            _LOGGER.warning(f"Target is 0 because battery_soc ({battery_soc}%) < battery_soc_min ({battery_soc_min}%)")
        elif max_evse_available < charge_context.min_current:
            _LOGGER.warning(f"Target is 0 because max_evse_available ({max_evse_available}A) < min_current ({charge_context.min_current}A)")
    
    return {
        CONF_AVAILABLE_CURRENT: round(state[CONF_AVAILABLE_CURRENT], 1),
        CONF_PHASES: charge_context.phases,
        CONF_CHARING_MODE: charging_mode,
        'calc_used': getattr(charge_context, 'calc_used', None),
        'max_evse_available': max_evse_available,
        'target_evse': target_evse,
        'target_evse_standard': target_evse_standard,
        'target_evse_eco': target_evse_eco,
        'target_evse_solar': target_evse_solar,
        'target_evse_excess': target_evse_excess,
        'excess_charge_start_time': getattr(sensor, '_excess_charge_start_time', None),
        'battery_soc': charge_context.battery_soc,
        'battery_soc_min': charge_context.battery_soc_min,
        'battery_soc_target': charge_context.battery_soc_target,
        'battery_power': charge_context.battery_power,
        'available_battery_power': round(available_battery_power, 1),
    }


# Re-export for backward compatibility
__all__ = [
    "calculate_available_current_for_hub",
    "ChargeContext",
    "calculate_max_evse_available",
    "calculate_standard_mode",
    "calculate_eco_mode", 
    "calculate_solar_mode",
    "calculate_excess_mode",
]
