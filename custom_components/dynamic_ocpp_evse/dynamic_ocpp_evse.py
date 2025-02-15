import datetime
import logging
from .const import *  # Make sure DOMAIN is defined in const.py

_LOGGER = logging.getLogger(__name__)

def is_number(value):
    try:
        float(value)
        return True
    except ValueError:
        return False

def get_sensor_data(self, sensor):
    state = self.hass.states.get(sensor)
    _LOGGER.debug(f"Getting state for sensor: {sensor}  -  {state}")
    if state is None:
        _LOGGER.error(f"Failed to get state for sensor: {sensor}")
        return None
    value = state.state
    if type(value) == str:
        if is_number(value):
            value = float(value)
    return value

def calculate_available_current(self):
    state = {}
    state[CONF_PHASES] = get_sensor_data(self, 'sensor.evbox_elvi_charging_phases')
    phases = state[CONF_PHASES]
    state[CONF_MAIN_BREAKER_RATING] = self.config_entry.data.get(CONF_MAIN_BREAKER_RATING)
    state[CONF_INVERT_PHASES] = self.config_entry.data.get(CONF_INVERT_PHASES)
    state[CONF_CHARING_MODE] = get_sensor_data(self, self.config_entry.data.get(CONF_CHARGIN_MODE_ENTITY_ID))
    state[CONF_PHASE_A_CURRENT] = get_sensor_data(self, self.config_entry.data.get(CONF_PHASE_A_CURRENT_ENTITY_ID))
    state[CONF_PHASE_B_CURRENT] = get_sensor_data(self, self.config_entry.data.get(CONF_PHASE_B_CURRENT_ENTITY_ID))
    state[CONF_PHASE_C_CURRENT] = get_sensor_data(self, self.config_entry.data.get(CONF_PHASE_C_CURRENT_ENTITY_ID))
    state[CONF_EVSE_CURRENT_IMPORT] = get_sensor_data(self, self.config_entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID))
    state[CONF_MAX_IMPORT_POWER] = get_sensor_data(self, self.config_entry.data.get(CONF_MAX_IMPORT_POWER_ENTITY_ID))
    voltage = 230

    for key, value in state.items():
        _LOGGER.debug(f"doe_values: {key} : {value} : {type(value)}")

    if state[CONF_INVERT_PHASES]:
        state[CONF_PHASE_A_CURRENT], state[CONF_PHASE_B_CURRENT], state[CONF_PHASE_C_CURRENT] = -state[CONF_PHASE_A_CURRENT], -state[CONF_PHASE_B_CURRENT], -state[CONF_PHASE_C_CURRENT]
        
    if state[CONF_CHARING_MODE] == 'Standard':
        target_import_power = state[CONF_MAX_IMPORT_POWER]
        target_import_current = target_import_power / voltage
    else:
        target_import_power = 0
        target_import_current = 0

    phase_a_current = state[CONF_PHASE_A_CURRENT]
    phase_b_current = state[CONF_PHASE_B_CURRENT]
    phase_c_current = state[CONF_PHASE_C_CURRENT]

    phase_a_import_current = max(phase_a_current, 0)
    phase_b_import_current = max(phase_b_current, 0)
    phase_c_import_current = max(phase_c_current, 0)

    phase_a_export_current = max(-phase_a_current, 0)
    phase_b_export_current = max(-phase_b_current, 0)
    phase_c_export_current = max(-phase_c_current, 0)
    
    total_import_current = phase_a_import_current + phase_b_import_current + phase_c_import_current

    evse_current = state[CONF_EVSE_CURRENT_IMPORT]
    evse_current_per_phase = evse_current / phases


    # calculate limits
    max_import_current = state[CONF_MAX_IMPORT_POWER] /voltage

    remaining_available_import_current = max_import_current - total_import_current
    
    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - phase_c_current

    if state[CONF_PHASES] == 1:
        max_evse_available = evse_current_per_phase + min(remaining_available_current_phase_a, remaining_available_import_current + phase_a_export_current)
        calc_used = "01"
    
    elif state[CONF_PHASES] == 2:
        max_evse_available = evse_current_per_phase + min(remaining_available_current_phase_a, remaining_available_current_phase_b, (remaining_available_import_current + phase_a_export_current + phase_b_export_current) / 2)
        calc_used = "02"
   
    elif state[CONF_PHASES] == 3:
        max_evse_available = evse_current_per_phase + min(remaining_available_current_phase_a, remaining_available_current_phase_b, remaining_available_current_phase_c, (remaining_available_import_current + phase_a_export_current + phase_b_export_current + phase_c_export_current) / 3)
        calc_used = "03"
    
    else:
        max_evse_available = self.config_entry.data.get(CONF_DEFAULT_CHARGE_CURRENT)
        calc_used = "04"
    
    # calculate target current
    if state[CONF_CHARING_MODE] == 'Standard':
        target_import_current = max_import_current
    else:
        target_import_current = 0

    remaining_available_import_current = target_import_current - total_import_current
    
    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - phase_c_current

    if state[CONF_PHASES] == 1:
        target_evse = evse_current_per_phase + min(remaining_available_current_phase_a, remaining_available_import_current + phase_a_export_current)
        calc_used = "01"
    
    elif state[CONF_PHASES] == 2:
        target_evse = evse_current_per_phase + min(remaining_available_current_phase_a, remaining_available_current_phase_b, (remaining_available_import_current + phase_a_export_current + phase_b_export_current) / 2)
        calc_used = "02"
   
    elif state[CONF_PHASES] == 3:
        target_evse = evse_current_per_phase + min(remaining_available_current_phase_a, remaining_available_current_phase_b, remaining_available_current_phase_c, (remaining_available_import_current + phase_a_export_current + phase_b_export_current + phase_c_export_current) / 3)
        calc_used = "03"
    
    else:
        target_evse = self.config_entry.data.get(CONF_DEFAULT_CHARGE_CURRENT)
        calc_used = "04"

    if state[CONF_CHARING_MODE] == 'Eco':
        target_evse = max(6, target_evse)




    state[CONF_AVAILABLE_CURRENT] = min(max_evse_available, target_evse)

    if state[CONF_AVAILABLE_CURRENT] < 6:
        state[CONF_AVAILABLE_CURRENT] = 0
    if state[CONF_AVAILABLE_CURRENT] > 16:
        state[CONF_AVAILABLE_CURRENT] = 16
    

    return {
        CONF_AVAILABLE_CURRENT: state[CONF_AVAILABLE_CURRENT],     # Current available based on input parameters
        CONF_PHASES: state[CONF_PHASES],                 # either a known or estimated number of phases used
        CONF_CHARING_MODE: state[CONF_CHARING_MODE],    # Charging mode selected by the user
        'calc_used': calc_used,
        'max_evse_available': max_evse_available,
    }


