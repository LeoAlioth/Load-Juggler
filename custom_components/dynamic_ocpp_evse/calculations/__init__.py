"""
Dynamic OCPP EVSE Calculations Module.

This module contains all the calculation logic for determining available charging current.
"""
import logging
from .context import ChargeContext, get_hub_state_config, get_charge_context_values, determine_phases
from .max_available import calculate_charger_available_current
from .utils import apply_ramping
from .modes import (
    calculate_standard_mode,
    calculate_eco_mode,
    calculate_solar_mode,
    calculate_excess_mode,
)
from ..const import *

_LOGGER = logging.getLogger(__name__)


def calculate_available_current_for_charger(sensor):
    """
    Calculate available current for a specific charger.
    This is the main function called by the charger sensor.
    
    Flow:
    1. Charger reports its phase configuration to site
    2. Site calculates what's available for this charger based on its phases
    3. Charger applies its charging mode to the available current
    4. Returns the final current allocation for this charger
    """
    _LOGGER.debug(f"Calculating available current for charger: {sensor._attr_name}")

    state = get_hub_state_config(sensor)
    charge_context = get_charge_context_values(sensor, state)

    # Determine charger phases - use detected phases if available, otherwise determine from current data
    if sensor._detected_phases is not None:
        charger_phases = sensor._detected_phases
        _LOGGER.debug(f"Using remembered detected phases: {charger_phases}")
    else:
        # First time or no charging yet - determine from sensor data
        charger_phases, calc_used = determine_phases(sensor, state)
        _LOGGER.debug(f"Determined phases from sensor: {charger_phases} (method: {calc_used})")
        
        # If we detected phases from actual charging (method starts with "1-"), remember them
        if calc_used and calc_used.startswith("1-"):
            sensor._detected_phases = charger_phases
            _LOGGER.info(f"Detected and remembered {charger_phases} phases for charger {sensor._attr_name}")
    
    # Get site's available current for this charger's phase configuration
    charger_max_available = calculate_charger_available_current(charge_context, charger_phases)
    charge_context.max_evse_available = charger_max_available

    # Calculate target current based on charging mode
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

    # Clamp target_evse to max_current and charger_max_available
    target_evse = min(target_evse, charge_context.max_current, charger_max_available)

    # Clamp to available
    state[CONF_AVAILABLE_CURRENT] = min(charger_max_available, target_evse)

    # Apply ramping logic
    apply_ramping(sensor, state, target_evse, charge_context.min_current, 
                  CONF_EVSE_CURRENT_OFFERED, CONF_AVAILABLE_CURRENT)
    
    # IMPORTANT: Re-clamp to charger_max_available after ramping
    if state[CONF_AVAILABLE_CURRENT] > charger_max_available:
        _LOGGER.debug(f"Re-clamping after ramping: {state[CONF_AVAILABLE_CURRENT]}A -> {charger_max_available}A")
        state[CONF_AVAILABLE_CURRENT] = charger_max_available
        sensor._last_ramp_value = charger_max_available
    
    if state[CONF_AVAILABLE_CURRENT] < state[CONF_EVSE_MINIMUM_CHARGE_CURRENT]:
        state[CONF_AVAILABLE_CURRENT] = 0
    if state[CONF_AVAILABLE_CURRENT] > state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]:
        state[CONF_AVAILABLE_CURRENT] = state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]

    # Calculate available battery power for EV charging
    battery_soc = charge_context.battery_soc if charge_context.battery_soc is not None else 0
    battery_soc_min = charge_context.battery_soc_min if charge_context.battery_soc_min is not None else DEFAULT_BATTERY_SOC_MIN
    battery_soc_target = charge_context.battery_soc_target if charge_context.battery_soc_target is not None else 80  # Default to 80%
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
        f"Charger calculation: battery_soc={charge_context.battery_soc}%, "
        f"battery_soc_min={charge_context.battery_soc_min}%, "
        f"battery_soc_target={charge_context.battery_soc_target}%, "
        f"mode={charging_mode}, "
        f"target_evse={target_evse}A, "
        f"charger_max_available={charger_max_available}A, "
        f"available_battery_power={available_battery_power}W"
    )
    
    # Log reason for target_evse value
    if target_evse == 0:
        if battery_soc < battery_soc_min:
            _LOGGER.warning(f"Target is 0 because battery_soc ({battery_soc}%) < battery_soc_min ({battery_soc_min}%)")
        elif charger_max_available < charge_context.min_current:
            _LOGGER.warning(f"Target is 0 because charger_max_available ({charger_max_available}A) < min_current ({charge_context.min_current}A)")
    
    return {
        CONF_AVAILABLE_CURRENT: round(state[CONF_AVAILABLE_CURRENT], 1),
        CONF_PHASES: charger_phases,
        CONF_CHARING_MODE: charging_mode,
        'calc_used': None,  # No longer relevant with new architecture
        'charger_max_available': charger_max_available,
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
        # Site available per-phase current (A)
        'site_available_current_phase_a': charge_context.site_available_current_phase_a,
        'site_available_current_phase_b': charge_context.site_available_current_phase_b,
        'site_available_current_phase_c': charge_context.site_available_current_phase_c,
        # Site battery available power (W)
        'site_battery_available_power': charge_context.site_battery_available_power,
        # Site grid available power (W)
        'site_grid_available_power': charge_context.site_grid_available_power,
        # Total site available power (W) - grid + battery
        'total_site_available_power': charge_context.total_site_available_power,
        # NEW: Add context fields for site power information
        'total_evse_power': charge_context.total_evse_power,
        'net_site_consumption': charge_context.net_site_consumption,
        'solar_surplus_power': charge_context.solar_surplus_power,
        'solar_surplus_current': charge_context.solar_surplus_current,
    }


# Keep old function name for backward compatibility
calculate_available_current_for_hub = calculate_available_current_for_charger

def calculate_hub_state_centralized(hass, hub_entry, chargers):
    """
    Centralized hub state calculation - called once per hub update cycle.
    
    This function:
    1. Runs calculation for each charger ONCE per cycle
    2. Calculates targets using centralized logic (preventing double-counting)
    3. Distributes current among chargers ONCE
    4. Returns complete commands for all chargers
    
    Args:
        hass: HomeAssistant instance
        hub_entry: Hub config entry
        chargers: List of charger config entries
        
    Returns:
        dict with:
            - hub_state: Hub-level data
            - charger_commands: Dict of {charger_id: {allocated, target, ...}}
    """
    from .target_calculator import calculate_all_charger_targets
    from .. import distribute_current_to_chargers
    from .utils import get_sensor_data
    
    _LOGGER.debug(f"Hub centralized calculation for {len(chargers)} chargers")
    
    # Build minimal hub state from hub config
    voltage = hub_entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
    
    # Read phase currents/powers from hub sensors
    def get_phase_current(entity_id):
        if not entity_id or entity_id == 'None':
            return 0
        value = get_sensor_data(hass, entity_id)
        if value is None:
            return 0
        
        entity_state = hass.states.get(entity_id)
        if entity_state and entity_state.attributes.get('unit_of_measurement') == 'W':
            return value / voltage if voltage > 0 else 0
        return value
    
    grid_current_l1 = get_phase_current(hub_entry.data.get(CONF_PHASE_A_CURRENT_ENTITY_ID))
    grid_current_l2 = get_phase_current(hub_entry.data.get(CONF_PHASE_B_CURRENT_ENTITY_ID))
    grid_current_l3 = get_phase_current(hub_entry.data.get(CONF_PHASE_C_CURRENT_ENTITY_ID))
    
    # Export is negative current
    export_current_l1 = max(0, -grid_current_l1)
    export_current_l2 = max(0, -grid_current_l2)
    export_current_l3 = max(0, -grid_current_l3)
    
    total_export_current = export_current_l1 + export_current_l2 + export_current_l3
    total_export_power = total_export_current * voltage
    
    # Read battery state
    battery_soc = None
    battery_power = None
    battery_soc_entity = hub_entry.data.get(CONF_BATTERY_SOC_ENTITY_ID)
    battery_power_entity = hub_entry.data.get(CONF_BATTERY_POWER_ENTITY_ID)
    
    if battery_soc_entity and battery_soc_entity != 'None':
        battery_soc = get_sensor_data(hass, battery_soc_entity)
    if battery_power_entity and battery_power_entity != 'None':
        battery_power = get_sensor_data(hass, battery_power_entity)
    
    # Get battery SOC targets from hub number entities
    hub_entity_id = hub_entry.data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")
    battery_soc_min = get_sensor_data(hass, f"number.{hub_entity_id}_home_battery_soc_min")
    battery_soc_target = get_sensor_data(hass, f"number.{hub_entity_id}_home_battery_soc_target")
    
    if battery_soc_min is None:
        battery_soc_min = hub_entry.data.get(CONF_BATTERY_SOC_MIN, DEFAULT_BATTERY_SOC_MIN)
    if battery_soc_target is None:
        battery_soc_target = hub_entry.data.get(CONF_BATTERY_SOC_TARGET, DEFAULT_BATTERY_SOC_TARGET)
    
    # Build hub_state for target calculator
    hub_state = {
        "battery_soc": battery_soc,
        "battery_soc_min": battery_soc_min,
        "battery_soc_target": battery_soc_target,
        "battery_power": battery_power,
        "total_export_current": total_export_current,
        "total_export_power": total_export_power,
        "voltage": voltage,
        CONF_EXCESS_EXPORT_THRESHOLD: hub_entry.data.get(CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD),
        "charger_phases": {},
    }
    
    # Get phases for each charger (from their sensor attributes if available)
    for charger in chargers:
        charger_entity_id = charger.data.get(CONF_ENTITY_ID)
        charger_sensor = f"sensor.{charger_entity_id}_available_current"
        phases = get_sensor_data(hass, charger_sensor, attribute=CONF_PHASES)
        hub_state["charger_phases"][charger.entry_id] = phases if phases else 3
    
    # Calculate targets for all chargers centrally (prevents double-counting solar)
    charger_targets = calculate_all_charger_targets(hass, hub_entry, hub_state, chargers)
    
    # Calculate total available current
    # For now use a simple calculation - this should eventually use max_available logic
    main_breaker_rating = hub_entry.data.get(CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING)
    total_available_current = main_breaker_rating * 3  # Simplified
    
    _LOGGER.debug(f"Total available: {total_available_current}A, Targets: {charger_targets}")
    
    # Distribute current among chargers
    allocations = distribute_current_to_chargers(
        hass, hub_entry.entry_id, total_available_current, charger_targets
    )
    
    # Build charger commands
    charger_commands = {}
    for charger in chargers:
        charger_id = charger.entry_id
        charger_commands[charger_id] = {
            "allocated_current": allocations.get(charger_id, 0),
            "target_current": charger_targets.get(charger_id, 0),
            "charging_mode": get_sensor_data(hass, f"select.{charger.data.get(CONF_ENTITY_ID)}_charging_mode"),
        }
    
    return {
        "hub_state": hub_state,
        "charger_commands": charger_commands,
    }


# Re-export for backward compatibility
__all__ = [
    "calculate_available_current_for_hub",
    "calculate_available_current_for_charger",
    "calculate_hub_state_centralized",
    "ChargeContext",
    "calculate_charger_available_current",
    "calculate_standard_mode",
    "calculate_eco_mode", 
    "calculate_solar_mode",
    "calculate_excess_mode",
]
