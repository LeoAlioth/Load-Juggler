"""Charge context and state gathering for Dynamic OCPP EVSE."""
import logging
from dataclasses import dataclass
from .utils import get_sensor_data, get_sensor_attribute, is_number
from ..const import *

_LOGGER = logging.getLogger(__name__)


@dataclass
class ChargeContext:
    """Context object containing all data needed for charging calculations."""
    state: dict
    phases: int
    voltage: float
    total_import_current: float
    grid_phase_a_current: float
    grid_phase_b_current: float
    grid_phase_c_current: float
    phase_a_export_current: float
    phase_b_export_current: float
    phase_c_export_current: float
    evse_current_per_phase: float
    max_evse_available: float
    min_current: float
    max_current: float
    total_export_power: float
    # Battery-related fields
    battery_soc: float = None
    battery_power: float = None
    battery_soc_target: float = None
    battery_soc_min: float = None
    battery_soc_hysteresis: float = None
    battery_max_charge_power: float = None
    battery_max_discharge_power: float = None
    allow_grid_charging: bool = True
    allow_grid_charging_entity_id: str = None


def determine_phases(sensor, state):
    """Determine number of phases from charger data."""
    phases = 0
    calc_used = ""
    
    evse_import_entity = sensor.config_entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
    if state[CONF_EVSE_CURRENT_OFFERED] is not None and evse_import_entity:
        evse_state = sensor.hass.states.get(evse_import_entity)
        if evse_state:
            evse_attributes = evse_state.attributes
            for attr, value in evse_attributes.items():
                if attr.startswith('L') and is_number(value) and float(value) > 1:
                    phases += 1
            if phases > 0:
                calc_used = f"1-{phases}"

    if phases == 0 and state[CONF_PHASES] is not None and is_number(state[CONF_PHASES]):
        phases = state[CONF_PHASES]
        calc_used = f"2-{phases}"

    if phases == 0:
        phases = 3
        calc_used = f"3-{phases}"
    return phases, calc_used


def get_hub_state_config(sensor):
    """Get state configuration from hub entry."""
    hass = sensor.hass
    hub_entry = sensor.hub_entry
    charger_entry = sensor.config_entry
    
    state = {}
    
    # Get phases from charger sensor
    try:
        charger_entity_id = charger_entry.data.get(CONF_ENTITY_ID)
        state[CONF_PHASES] = get_sensor_attribute(hass, f"sensor.{charger_entity_id}_available_current", CONF_PHASES)
    except:
        state[CONF_PHASES] = None
    
    # Hub-level configuration
    state[CONF_MAIN_BREAKER_RATING] = hub_entry.data.get(CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING)
    state[CONF_INVERT_PHASES] = hub_entry.data.get(CONF_INVERT_PHASES, False)
    
    # Get charging mode from hub
    charging_mode_entity = hub_entry.data.get(CONF_CHARGIN_MODE_ENTITY_ID)
    state[CONF_CHARING_MODE] = get_sensor_data(hass, charging_mode_entity) if charging_mode_entity else "Standard"
    
    # Phase voltage
    voltage = hub_entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
    state[CONF_PHASE_VOLTAGE] = voltage
    
    # Helper function to convert power to current if needed
    def get_phase_current(entity_id):
        if not entity_id or entity_id == 'None':
            return None
        value = get_sensor_data(hass, entity_id)
        if value is None:
            return None
        
        entity_state = hass.states.get(entity_id)
        if entity_state and entity_state.attributes.get('unit_of_measurement') == 'W':
            return value / voltage if voltage > 0 else 0
        else:
            return value
    
    # Grid phase currents from hub
    state[CONF_PHASE_A_CURRENT] = get_phase_current(hub_entry.data.get(CONF_PHASE_A_CURRENT_ENTITY_ID))
    
    phase_b_entity = hub_entry.data.get(CONF_PHASE_B_CURRENT_ENTITY_ID)
    if phase_b_entity and phase_b_entity != 'None':
        state[CONF_PHASE_B_CURRENT] = get_phase_current(phase_b_entity)
    else:
        state[CONF_PHASE_B_CURRENT] = 0
    
    phase_c_entity = hub_entry.data.get(CONF_PHASE_C_CURRENT_ENTITY_ID)
    if phase_c_entity and phase_c_entity != 'None':
        state[CONF_PHASE_C_CURRENT] = get_phase_current(phase_c_entity)
    else:
        state[CONF_PHASE_C_CURRENT] = 0
    
    # Charger-level current readings
    state[CONF_EVSE_CURRENT_IMPORT] = get_sensor_data(hass, charger_entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID))
    state[CONF_EVSE_CURRENT_OFFERED] = get_sensor_data(hass, charger_entry.data.get(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID))
    
    # Max import power from hub
    state[CONF_MAX_IMPORT_POWER] = get_sensor_data(hass, hub_entry.data.get(CONF_MAX_IMPORT_POWER_ENTITY_ID))
    
    # Charger-level limits
    state[CONF_EVSE_MINIMUM_CHARGE_CURRENT] = charger_entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
    state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT] = charger_entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)
    
    # Get min/max current from charger number entities
    charger_entity_id = charger_entry.data.get(CONF_ENTITY_ID)
    state[CONF_MIN_CURRENT] = get_sensor_data(hass, f"number.{charger_entity_id}_min_current")
    state[CONF_MAX_CURRENT] = get_sensor_data(hass, f"number.{charger_entity_id}_max_current")
    
    # Hub-level settings
    state[CONF_EXCESS_EXPORT_THRESHOLD] = hub_entry.data.get(CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD)
    
    # Battery values from hub
    hub_entity_id = hub_entry.data.get(CONF_ENTITY_ID)
    
    battery_soc_entity_id = hub_entry.data.get(CONF_BATTERY_SOC_ENTITY_ID)
    if battery_soc_entity_id and battery_soc_entity_id != 'None':
        state["battery_soc"] = get_sensor_data(hass, battery_soc_entity_id)
    else:
        state["battery_soc"] = None

    battery_power_entity_id = hub_entry.data.get(CONF_BATTERY_POWER_ENTITY_ID)
    if battery_power_entity_id and battery_power_entity_id != 'None':
        state["battery_power"] = get_sensor_data(hass, battery_power_entity_id)
    else:
        state["battery_power"] = None

    # Battery SOC target from hub number entity
    battery_soc_target_entity = f"number.{hub_entity_id}_home_battery_soc_target"
    state["battery_soc_target"] = get_sensor_data(hass, battery_soc_target_entity)
    _LOGGER.info(f"Battery SOC target: entity={battery_soc_target_entity}, value={state['battery_soc_target']}")
    if state["battery_soc_target"] is None:
        _LOGGER.warning(f"Battery SOC target entity {battery_soc_target_entity} not found, using default 80%")
        state["battery_soc_target"] = 80  # Default to 80% if not found
    
    # Battery SOC min from hub number entity
    battery_soc_min_entity = f"number.{hub_entity_id}_home_battery_soc_min"
    state["battery_soc_min"] = get_sensor_data(hass, battery_soc_min_entity)
    _LOGGER.info(f"Battery SOC min: entity={battery_soc_min_entity}, value={state['battery_soc_min']}")
    if state["battery_soc_min"] is None:
        _LOGGER.warning(f"Battery SOC min entity {battery_soc_min_entity} not found or unavailable, using default {DEFAULT_BATTERY_SOC_MIN}")
        state["battery_soc_min"] = DEFAULT_BATTERY_SOC_MIN
    
    # Battery SOC hysteresis from hub config
    state["battery_soc_hysteresis"] = hub_entry.data.get(CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS)

    state[CONF_BATTERY_MAX_CHARGE_POWER] = hub_entry.data.get(CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER)
    state[CONF_BATTERY_MAX_DISCHARGE_POWER] = hub_entry.data.get(CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER)

    # Power buffer from hub number entity
    power_buffer_entity = f"number.{hub_entity_id}_power_buffer"
    power_buffer_value = get_sensor_data(hass, power_buffer_entity)
    state[CONF_POWER_BUFFER] = power_buffer_value if power_buffer_value is not None else 0

    # Allow grid charging from hub switch
    allow_grid_entity = f"switch.{hub_entity_id}_allow_grid_charging"
    switch_state = get_sensor_data(hass, allow_grid_entity)
    state["allow_grid_charging"] = switch_state == "on" if switch_state else True
    
    return state


def get_charge_context_values(sensor, state):
    """Build ChargeContext from state dictionary."""
    min_current = state[CONF_MIN_CURRENT] if state[CONF_MIN_CURRENT] is not None else state[CONF_EVSE_MINIMUM_CHARGE_CURRENT]
    max_current = state[CONF_MAX_CURRENT] if state[CONF_MAX_CURRENT] is not None else state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]
    phases, calc_used = determine_phases(sensor, state)
    voltage = state[CONF_PHASE_VOLTAGE] if state[CONF_PHASE_VOLTAGE] is not None and is_number(state[CONF_PHASE_VOLTAGE]) else DEFAULT_PHASE_VOLTAGE
    
    phase_a_current = state[CONF_PHASE_A_CURRENT] if state[CONF_PHASE_A_CURRENT] is not None and is_number(state[CONF_PHASE_A_CURRENT]) else 0
    phase_b_current = state[CONF_PHASE_B_CURRENT] if state[CONF_PHASE_B_CURRENT] is not None and is_number(state[CONF_PHASE_B_CURRENT]) else 0
    phase_c_current = state[CONF_PHASE_C_CURRENT] if state[CONF_PHASE_C_CURRENT] is not None and is_number(state[CONF_PHASE_C_CURRENT]) else 0
    
    total_export_current = (
        max(-phase_a_current, 0) +
        max(-phase_b_current, 0) +
        max(-phase_c_current, 0)
    )
    total_export_power = total_export_current * voltage
    
    if state[CONF_INVERT_PHASES]:
        phase_a_current, phase_b_current, phase_c_current = -phase_a_current, -phase_b_current, -phase_c_current
    
    grid_phase_a_current = phase_a_current
    grid_phase_b_current = phase_b_current
    grid_phase_c_current = phase_c_current
    
    phase_a_import_current = max(grid_phase_a_current, 0)
    phase_b_import_current = max(grid_phase_b_current, 0)
    phase_c_import_current = max(grid_phase_c_current, 0)
    phase_a_export_current = max(-grid_phase_a_current, 0)
    phase_b_export_current = max(-grid_phase_b_current, 0)
    phase_c_export_current = max(-grid_phase_c_current, 0)
    total_import_current = phase_a_import_current + phase_b_import_current + phase_c_import_current
    
    evse_current = state[CONF_EVSE_CURRENT_IMPORT]
    if evse_current is None or not is_number(evse_current):
        evse_current = 0
    evse_current_per_phase = evse_current
    
    # Battery values
    battery_soc = state["battery_soc"]
    battery_power = state["battery_power"]
    battery_soc_target = state.get("battery_soc_target")
    battery_soc_min = state.get("battery_soc_min", DEFAULT_BATTERY_SOC_MIN)
    battery_soc_hysteresis = state.get("battery_soc_hysteresis", DEFAULT_BATTERY_SOC_HYSTERESIS)
    battery_max_charge_power = state.get(CONF_BATTERY_MAX_CHARGE_POWER)
    battery_max_discharge_power = state.get(CONF_BATTERY_MAX_DISCHARGE_POWER)
    allow_grid_charging = state.get("allow_grid_charging", True)
    
    return ChargeContext(
        state=state,
        phases=phases,
        voltage=voltage,
        total_import_current=total_import_current,
        grid_phase_a_current=grid_phase_a_current,
        grid_phase_b_current=grid_phase_b_current,
        grid_phase_c_current=grid_phase_c_current,
        phase_a_export_current=phase_a_export_current,
        phase_b_export_current=phase_b_export_current,
        phase_c_export_current=phase_c_export_current,
        evse_current_per_phase=evse_current_per_phase,
        max_evse_available=0,
        min_current=min_current,
        max_current=max_current,
        total_export_power=total_export_power,
        battery_soc=battery_soc,
        battery_power=battery_power,
        battery_soc_target=battery_soc_target,
        battery_soc_min=battery_soc_min,
        battery_soc_hysteresis=battery_soc_hysteresis,
        battery_max_charge_power=battery_max_charge_power,
        battery_max_discharge_power=battery_max_discharge_power,
        allow_grid_charging=allow_grid_charging,
    )
