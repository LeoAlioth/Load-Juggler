import datetime
import logging
from .const import *  # Make sure DOMAIN is defined in const.py
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

@dataclass
class ChargeContext:
    state: dict
    phases: int
    voltage: float
    total_import_current: float
    phase_a_current: float
    phase_b_current: float
    phase_c_current: float
    phase_a_export_current: float
    phase_b_export_current: float
    phase_c_export_current: float
    evse_current_per_phase: float
    max_evse_available: float
    min_current: float
    max_current: float


def is_number(value):
    try:
        float(value)
        return True
    except ValueError:
        return False

def get_sensor_data(self, sensor):
    _LOGGER.debug(f"Getting state for sensor: {sensor}")
    state = self.hass.states.get(sensor)
    _LOGGER.debug(f"Got state for sensor: {sensor}  -  {state} ({type(state.state)})")
    if state is None:
        _LOGGER.warning(f"Failed to get state for sensor: {sensor}")
        return None
    value = state.state
    if type(value) == str:
        if is_number(value):
            value = float(value)
            _LOGGER.debug(f"Sensor: {sensor}  -  {state} is ({type(value)})")
    return value

def get_sensor_attribute(self, sensor, attribute):
    state = self.hass.states.get(sensor)
    _LOGGER.debug(f"Getting attribute '{attribute}' for sensor: {sensor}  -  {state}")
    if state is None:
        _LOGGER.warning(f"Failed to get state for sensor: {sensor} when getting attribute '{attribute}'")
        return None
    value = state.attributes.get(attribute)
    if value is None:
        _LOGGER.warning(f"Failed to get attribute '{attribute}' for sensor: {sensor}")
        return None
    if type(value) == str:
        if is_number(value):
            value = float(value)
    return value

def apply_ramping(self, state, target_evse, min_current):
        # Store last available current and time
        if not hasattr(self, '_last_ramp_value'):
            self._last_ramp_value = None
        if not hasattr(self, '_last_ramp_time'):
            self._last_ramp_time = None

        ramp_limit_up = 0.05   # Amps per second (ramp up)
        ramp_limit_down = 0.2 # Amps per second (ramp down, faster)
        now = datetime.datetime.now()
        
        ramp_enabled = True
        if ramp_enabled:
            # Use last ramped value as base for ramping, fallback to EVSE current if None
            if self._last_ramp_value is None:
                ramped_value = state[CONF_EVSE_CURRENT_OFFERED] or state[CONF_AVAILABLE_CURRENT]
            else:
                ramped_value = self._last_ramp_value
                if ramped_value < min_current and target_evse > min_current:
                    ramped_value = min_current
                    self._last_ramp_value = ramped_value

            if self._last_ramp_value is not None and self._last_ramp_time is not None:
                dt = (now - self._last_ramp_time).total_seconds()
                delta = state[CONF_AVAILABLE_CURRENT] - self._last_ramp_value
                if delta > 0:
                    max_delta = ramp_limit_up * max(dt, 0.1)
                else:
                    max_delta = ramp_limit_down * max(dt, 0.1)
                if abs(delta) > max_delta:
                    ramped_value = self._last_ramp_value + max_delta * (1 if delta > 0 else -1)
                    _LOGGER.debug(f"Ramping limited: {self._last_ramp_value} -> {ramped_value} (requested {state[CONF_AVAILABLE_CURRENT]})")
                else:
                    ramped_value = state[CONF_AVAILABLE_CURRENT]
            self._last_ramp_value = ramped_value
            self._last_ramp_time = now
            state[CONF_AVAILABLE_CURRENT] = ramped_value

def calculate_max_evse_available(context: ChargeContext):
    state = context.state
    max_import_power = state[CONF_MAX_IMPORT_POWER]
    max_import_current = max_import_power / context.voltage

    remaining_available_import_current = max_import_current - context.total_import_current

    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - context.phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - context.phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - context.phase_c_current

    if context.phases == 1:
        return context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_import_current + context.phase_a_export_current
        )
    elif context.phases == 2:
        return context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current) / 2
        )
    elif context.phases == 3:
        return context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            remaining_available_current_phase_c,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + context.phase_c_export_current) / 3
        )
    else:
        return state.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT)

def determine_phases(self, state):
    phases = 0
    calc_used = ""
    
    if state[CONF_EVSE_CURRENT_OFFERED] is not None:
        evse_attributes = self.hass.states.get(self.config_entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)).attributes
        for attr, value in evse_attributes.items():
            if attr.startswith('L') and is_number(value) and float(value) > 1:
                phases += 1
        if phases > 0:
            calc_used = f"1-{phases}"

    # Fallback to the existing method if individual phase currents are not provided
    if phases == 0 and state[CONF_PHASES] is not None and is_number(state[CONF_PHASES]):
        phases = state[CONF_PHASES]
        calc_used = f"2-{phases}"

    if phases == 0 and (is_number(state[CONF_EVSE_CURRENT_IMPORT]) and state[CONF_EVSE_CURRENT_IMPORT] > 0) and (is_number(state[CONF_EVSE_CURRENT_OFFERED]) and state[CONF_EVSE_CURRENT_OFFERED] > 0):
        phases = min(max(round(state[CONF_EVSE_CURRENT_IMPORT] / state[CONF_EVSE_CURRENT_OFFERED], 0), 1), 3)
        calc_used = f"3-{phases}"
    # Finally just assume the safest case of 3 phases
    if phases == 0:
        phases = 3
        calc_used = f"4-{phases}"
    return phases, calc_used


# functions for calculating current for different charge modes

def calculate_standard_mode(context: ChargeContext):
    state = context.state
    max_import_power = state[CONF_MAX_IMPORT_POWER]
    max_import_current = max_import_power / context.voltage
    target_import_current = max_import_current
    remaining_available_import_current = target_import_current - context.total_import_current

    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - context.phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - context.phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - context.phase_c_current

    if context.phases == 1:
        target_evse = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_import_current + context.phase_a_export_current
        )
    elif context.phases == 2:
        target_evse = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current) / 2
        )
    elif context.phases == 3:
        target_evse = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            remaining_available_current_phase_c,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + context.phase_c_export_current) / 3
        )
    else:
        target_evse = state.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT)
    return target_evse

def calculate_solar_mode(context: ChargeContext, target_import_current=0):
    state = context.state
    remaining_available_import_current = target_import_current - context.total_import_current
    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - context.phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - context.phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - context.phase_c_current

    if context.phases == 1:
        target_evse = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_import_current + context.phase_a_export_current
        )
    elif context.phases == 2:
        target_evse = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current) / 2
        )
    elif context.phases == 3:
        target_evse = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            remaining_available_current_phase_c,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + context.phase_c_export_current) / 3
        )
    else:
        target_evse = state.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT)
    return target_evse

def calculate_eco_mode(context: ChargeContext):
    target_evse = calculate_solar_mode(context)
    target_evse = max(context.min_current, target_evse)
    return target_evse

def calculate_excess_mode(self, context: ChargeContext):
    state = context.state
    export_currents = [
        -context.phase_a_current if context.phase_a_current < 0 else 0,
        -context.phase_b_current if context.phase_b_current < 0 else 0,
        -context.phase_c_current if context.phase_c_current < 0 else 0
    ]
    max_export_current = max(export_currents)
    threshold = state[CONF_EXCESS_EXPORT_THRESHOLD]
    now = datetime.datetime.now()
    if max_export_current > threshold:
        _LOGGER.info(f"Excess mode: max_export_current {max_export_current} > threshold {threshold}, starting charge")
        self._excess_charge_start_time = now
    keep_charging = False
    if getattr(self, '_excess_charge_start_time', None) is not None and \
       (now - self._excess_charge_start_time).total_seconds() < 15 * 60:
        if max_export_current + context.min_current > threshold:
            self._excess_charge_start_time = now
        keep_charging = True
    if keep_charging:
        export_available = max_export_current - threshold + context.evse_current_per_phase
        target_evse = max(context.min_current, export_available)
    else:
        target_evse = 0
    target_evse = min(target_evse, context.max_current, context.max_evse_available)
    return target_evse

def get_state_config(self):
    state = {}
    try:
        state[CONF_PHASES] = get_sensor_attribute(self, "sensor." + self.config_entry.data.get(CONF_ENTITY_ID), CONF_PHASES)
    except:
        state[CONF_PHASES] = None
    state[CONF_MAIN_BREAKER_RATING] = self.config_entry.data.get(CONF_MAIN_BREAKER_RATING)
    state[CONF_INVERT_PHASES] = self.config_entry.data.get(CONF_INVERT_PHASES)
    state[CONF_CHARING_MODE] = get_sensor_data(self, self.config_entry.data.get(CONF_CHARGIN_MODE_ENTITY_ID))
    state[CONF_PHASE_A_CURRENT] = get_sensor_data(self, self.config_entry.data.get(CONF_PHASE_A_CURRENT_ENTITY_ID))
    state[CONF_PHASE_B_CURRENT] = get_sensor_data(self, self.config_entry.data.get(CONF_PHASE_B_CURRENT_ENTITY_ID))
    state[CONF_PHASE_C_CURRENT] = get_sensor_data(self, self.config_entry.data.get(CONF_PHASE_C_CURRENT_ENTITY_ID))
    state[CONF_EVSE_CURRENT_IMPORT] = get_sensor_data(self, self.config_entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID))
    state[CONF_EVSE_CURRENT_OFFERED] = get_sensor_data(self, self.config_entry.data.get(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID))
    state[CONF_MAX_IMPORT_POWER] = get_sensor_data(self, self.config_entry.data.get(CONF_MAX_IMPORT_POWER_ENTITY_ID))
    state[CONF_PHASE_VOLTAGE] = self.config_entry.data.get(CONF_PHASE_VOLTAGE)
    state[CONF_EVSE_MINIMUM_CHARGE_CURRENT] = self.config_entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, 6)
    state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT] = self.config_entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, 16)
    state[CONF_MIN_CURRENT] = get_sensor_data(self, self.config_entry.data.get(CONF_MIN_CURRENT_ENTITY_ID))
    state[CONF_MAX_CURRENT] = get_sensor_data(self, self.config_entry.data.get(CONF_MAX_CURRENT_ENTITY_ID))
    state[CONF_EXCESS_EXPORT_THRESHOLD] = self.config_entry.data.get(CONF_EXCESS_EXPORT_THRESHOLD, -19)
    return state

def get_charge_context_values(self, state):
    min_current = state[CONF_MIN_CURRENT] if state[CONF_MIN_CURRENT] is not None else state[CONF_EVSE_MINIMUM_CHARGE_CURRENT]
    max_current = state[CONF_MAX_CURRENT] if state[CONF_MAX_CURRENT] is not None else state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]
    phases, calc_used = determine_phases(self, state)
    voltage = 230
    if state[CONF_PHASE_VOLTAGE] is not None and is_number(state[CONF_PHASE_VOLTAGE]):
        voltage = state[CONF_PHASE_VOLTAGE]
    if state[CONF_INVERT_PHASES]:
        state[CONF_PHASE_A_CURRENT], state[CONF_PHASE_B_CURRENT], state[CONF_PHASE_C_CURRENT] = -state[CONF_PHASE_A_CURRENT], -state[CONF_PHASE_B_CURRENT], -state[CONF_PHASE_C_CURRENT]
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
    return ChargeContext(
        state=state,
        phases=phases,
        voltage=voltage,
        total_import_current=total_import_current,
        phase_a_current=phase_a_current,
        phase_b_current=phase_b_current,
        phase_c_current=phase_c_current,
        phase_a_export_current=phase_a_export_current,
        phase_b_export_current=phase_b_export_current,
        phase_c_export_current=phase_c_export_current,
        evse_current_per_phase=evse_current_per_phase,
        max_evse_available=0,  # will be set after calculation
        min_current=min_current,
        max_current=max_current
    )

# Calculate the available current based on the configuration and sensor data - this is the main function called by the integration
# It gathers all necessary data, determines the number of phases, and calculates the available current based on the selected charging mode.
# It also applies ramping logic to smooth out changes in available current
# and ensures that the current is within the defined limits.
def calculate_available_current(self):
    state = get_state_config(self)
    charge_context = get_charge_context_values(self, state)

    # Calculate max_evse_available using context
    max_evse_available = calculate_max_evse_available(charge_context)
    charge_context.max_evse_available = max_evse_available

    target_evse_standard = calculate_standard_mode(charge_context)
    target_evse_eco = calculate_eco_mode(charge_context)
    target_evse_solar = calculate_solar_mode(charge_context)
    target_evse_excess = calculate_excess_mode(self, charge_context)

    if state[CONF_CHARING_MODE] == 'Standard':
        target_evse = target_evse_standard
    elif state[CONF_CHARING_MODE] == 'Eco':
        target_evse = target_evse_eco
    elif state[CONF_CHARING_MODE] == 'Solar':
        target_evse = target_evse_solar
    elif state[CONF_CHARING_MODE] == 'Excess':
        target_evse = target_evse_excess

    # Clamp target_evse to CONF_MAX_CURRENT
    target_evse = min(target_evse, charge_context.max_current, max_evse_available)

    # Clamp to available
    state[CONF_AVAILABLE_CURRENT] = min(max_evse_available, target_evse)

    # --- Ramping logic ---
    apply_ramping(self, state, target_evse, charge_context.min_current)
    
    if state[CONF_AVAILABLE_CURRENT] < state[CONF_EVSE_MINIMUM_CHARGE_CURRENT]:
        state[CONF_AVAILABLE_CURRENT] = 0
    if state[CONF_AVAILABLE_CURRENT] > state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]:
        state[CONF_AVAILABLE_CURRENT] = state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]

    return {
        CONF_AVAILABLE_CURRENT: round(state[CONF_AVAILABLE_CURRENT], 1),
        CONF_PHASES: charge_context.phases,
        CONF_CHARING_MODE: state[CONF_CHARING_MODE],
        'calc_used': getattr(charge_context, 'calc_used', None),
        'max_evse_available': max_evse_available,
        'target_evse': target_evse,
        'target_evse_standard': target_evse_standard,
        'target_evse_eco': target_evse_eco,
        'target_evse_solar': target_evse_solar,
        'target_evse_excess': target_evse_excess,
        'excess_charge_start_time': getattr(self, '_excess_charge_start_time', None),
    }


