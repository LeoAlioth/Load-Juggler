import datetime
import logging
from .const import *  # Make sure DOMAIN is defined in const.py
from dataclasses import dataclass
import inspect

_LOGGER = logging.getLogger(__name__)

@dataclass
class ChargeContext:
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
    battery_max_charge_power: float = None
    battery_max_discharge_power: float = None
    allow_grid_charging: bool = True
    allow_grid_charging_entity_id: str = None


def is_number(value):
    try:
        float(value)
        return True
    except ValueError:
        return False

def get_sensor_data(self, sensor):
    _LOGGER.debug(f"Getting state for sensor: {sensor}")
    state = self.hass.states.get(sensor)
    if state is None:
        _LOGGER.warning(f"Failed to get state for sensor: {sensor}: {inspect.stack()}")
        return None
    _LOGGER.debug(f"Got state for sensor: {sensor}  -  {state} ({type(state.state)})")
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
            if self._last_ramp_value is None or not is_number(self._last_ramp_value):
                ramped_value = state[CONF_EVSE_CURRENT_OFFERED] or state[CONF_AVAILABLE_CURRENT]
                self._last_ramp_value = ramped_value
            else:
                ramped_value = self._last_ramp_value if is_number(self._last_ramp_value) else 0
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
    
    # Total import current includes EVSE import current across all 3 phases
    # max import current is total for all phases, so we can directly compare
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

    # Only allow battery discharge if SOC > SOC target
    if battery_soc > battery_soc_target:
        available_battery_power = max(0, battery_max_discharge_power - battery_power)
    else:
        # Only allow battery to stop charging (i.e., don't discharge below target)
        available_battery_power = max(0, -battery_power)  # Only offset if battery is charging
    available_battery_current = (available_battery_power / context.voltage) if context.voltage else 0
    
    if context.phases == 1:
        max_evse_available = context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_import_current + context.phase_a_export_current + available_battery_current
        )
        _LOGGER.debug(f"Max EVSE available (1 phase): {max_evse_available}A")
        return max_evse_available
    elif context.phases == 2:
        max_evse_available =  context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + available_battery_current) / 2
        )
        _LOGGER.debug(f"Max EVSE available (2 phases): {max_evse_available}A")
        return max_evse_available
    elif context.phases == 3:
        max_evse_available =  context.evse_current_per_phase + min(
            remaining_available_current_phase_a,
            remaining_available_current_phase_b,
            remaining_available_current_phase_c,
            (remaining_available_import_current + context.phase_a_export_current + context.phase_b_export_current + context.phase_c_export_current + available_battery_current) / 3
        )
        _LOGGER.debug(f"Max EVSE available (3 phases): {max_evse_available}A")
        return max_evse_available
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

    # Finally just assume the safest case of 3 phases
    if phases == 0:
        phases = 3
        calc_used = f"3-{phases}"
    return phases, calc_used


# functions for calculating current for different charge modes

def calculate_standard_mode(context: ChargeContext):
    state = context.state
    max_import_power = state[CONF_MAX_IMPORT_POWER]
    max_import_current = max_import_power / context.voltage
    target_import_current = max_import_current
    # If grid charging is not allowed, set available import current to 0
    if not context.allow_grid_charging:
        remaining_available_import_current = 0
    else:
        remaining_available_import_current = target_import_current - context.total_import_current

    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_c_current

    # Battery discharge logic for standard mode
    battery_power = context.battery_power if context.battery_power is not None else 0
    battery_max_discharge_power = context.battery_max_discharge_power if context.battery_max_discharge_power is not None else 0
    battery_soc = context.battery_soc if context.battery_soc is not None else 100
    battery_soc_target = context.battery_soc_target if context.battery_soc_target is not None else 0

    if battery_soc > battery_soc_target:
        # Allow battery to discharge at max power
        available_battery_power = max(0, battery_max_discharge_power)
    else:
        # Only allow battery to stop charging (no discharge below target)
        available_battery_power = max(0, -battery_power)  # Only offset if battery is charging
    available_battery_current = available_battery_power / context.voltage if context.voltage else 0

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
        target_evse = state.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT)
    return target_evse

def calculate_solar_mode(context: ChargeContext, target_import_current=0):
    state = context.state
    # If grid charging is not allowed, set available import current to 0
    if not context.allow_grid_charging:
        remaining_available_import_current = 0
    else:
        remaining_available_import_current = target_import_current - context.total_import_current
    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_c_current

    # Battery discharge logic for solar mode
    battery_power = context.battery_power if context.battery_power is not None else 0
    battery_max_discharge_power = context.battery_max_discharge_power if context.battery_max_discharge_power is not None else 0
    battery_soc = context.battery_soc if context.battery_soc is not None else 0
    battery_soc_target = context.battery_soc_target if context.battery_soc_target is not None else 0

    if battery_soc > battery_soc_target:
        # Allow battery to discharge at max power
        available_battery_power = max(0, battery_max_discharge_power)
    else:
        # Only allow battery to stop charging (no discharge below target)
        available_battery_power = max(0, -battery_power)  # Only offset if battery is charging
    available_battery_current = available_battery_power / context.voltage if context.voltage else 0

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
        target_evse = state.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT)
    return max(target_evse, 0) # Ensure non-negative current

def calculate_eco_mode(context: ChargeContext):
    target_evse = calculate_solar_mode(context)
    target_evse = max(context.min_current, target_evse)
    return target_evse

def calculate_excess_mode(self, context: ChargeContext):
    state = context.state
    voltage = context.voltage
    total_export_power = context.total_export_power
    base_threshold = state.get(CONF_EXCESS_EXPORT_THRESHOLD, 13600)
    if context.battery_soc is not None and context.battery_soc < 100:
        battery_max_charge_power = state.get(CONF_BATTERY_MAX_CHARGE_POWER, 5000)
    else:
        battery_max_charge_power = 0
    # Add battery max charge power to the threshold
    threshold = base_threshold + (battery_max_charge_power if battery_max_charge_power else 0)
    now = datetime.datetime.now()
    if total_export_power > threshold:
        _LOGGER.info(f"Excess mode: total_export_power {total_export_power}W > threshold {threshold}W, starting charge")
        self._excess_charge_start_time = now
    keep_charging = False
    if getattr(self, '_excess_charge_start_time', None) is not None and \
       (now - self._excess_charge_start_time).total_seconds() < 15 * 60:
        if total_export_power + context.min_current * voltage > threshold:
            self._excess_charge_start_time = now
        keep_charging = True
    if keep_charging:
        export_available_current = (total_export_power - threshold) / voltage + context.evse_current_per_phase
        target_evse = max(context.min_current, export_available_current)
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
    
    # Get phase voltage for power-to-current conversion
    voltage = self.config_entry.data.get(CONF_PHASE_VOLTAGE, 230)
    
    # Helper function to convert power to current if needed
    def get_phase_current(entity_id):
        if not entity_id:
            return None
        value = get_sensor_data(self, entity_id)
        if value is None:
            return None
        
        # Check if this is a power sensor by looking at the entity's unit_of_measurement
        entity_state = self.hass.states.get(entity_id)
        if entity_state and entity_state.attributes.get('unit_of_measurement') == 'W':
            # Convert power to current: I = P / V
            return value / voltage if voltage > 0 else 0
        else:
            # Assume it's already current
            return value
    
    state[CONF_PHASE_A_CURRENT] = get_phase_current(self.config_entry.data.get(CONF_PHASE_A_CURRENT_ENTITY_ID))
    state[CONF_PHASE_B_CURRENT] = get_phase_current(self.config_entry.data.get(CONF_PHASE_B_CURRENT_ENTITY_ID))
    state[CONF_PHASE_C_CURRENT] = get_phase_current(self.config_entry.data.get(CONF_PHASE_C_CURRENT_ENTITY_ID))
    
    state[CONF_EVSE_CURRENT_IMPORT] = get_sensor_data(self, self.config_entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID))
    state[CONF_EVSE_CURRENT_OFFERED] = get_sensor_data(self, self.config_entry.data.get(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID))
    state[CONF_MAX_IMPORT_POWER] = get_sensor_data(self, self.config_entry.data.get(CONF_MAX_IMPORT_POWER_ENTITY_ID))
    state[CONF_PHASE_VOLTAGE] = voltage
    state[CONF_EVSE_MINIMUM_CHARGE_CURRENT] = self.config_entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, 6)
    state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT] = self.config_entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, 16)
    state[CONF_MIN_CURRENT] = get_sensor_data(self, self.config_entry.data.get(CONF_MIN_CURRENT_ENTITY_ID))
    state[CONF_MAX_CURRENT] = get_sensor_data(self, self.config_entry.data.get(CONF_MAX_CURRENT_ENTITY_ID))
    state[CONF_EXCESS_EXPORT_THRESHOLD] = self.config_entry.data.get(CONF_EXCESS_EXPORT_THRESHOLD, 13600)
    
    # Read battery values if entities are set
    battery_soc_entity_id = self.config_entry.data.get(CONF_BATTERY_SOC_ENTITY_ID)
    if battery_soc_entity_id != 'None':
        state["battery_soc"] = get_sensor_data(self, battery_soc_entity_id)
    else:
        state["battery_soc"] = None

    battery_power_entity_id = self.config_entry.data.get(CONF_BATTERY_POWER_ENTITY_ID)
    if battery_power_entity_id != 'None':
        state["battery_power"] = get_sensor_data(self, battery_power_entity_id)
    else:
        state["battery_power"] = None

    battery_soc_target_entity_id = self.config_entry.data.get(CONF_BATTERY_SOC_TARGET_ENTITY_ID)
    if battery_soc_target_entity_id != 'None':
        state["battery_soc_target"] = get_sensor_data(self, battery_soc_target_entity_id)
    else:
        state["battery_soc_target"] = None

    state[CONF_BATTERY_MAX_CHARGE_POWER] = self.config_entry.data.get(CONF_BATTERY_MAX_CHARGE_POWER, 5000)
    state[CONF_BATTERY_MAX_DISCHARGE_POWER] = self.config_entry.data.get(CONF_BATTERY_MAX_DISCHARGE_POWER, 5000)

    # Retrieve the allow grid charging switch state using the constant
    switch_state = get_sensor_data(self, self.config_entry.data.get(CONF_ALLOW_GRID_CHARGING_ENTITY_ID))
    state["allow_grid_charging"] = switch_state == "on" if switch_state else True  # Default to True
    return state

def get_charge_context_values(self, state):
    min_current = state[CONF_MIN_CURRENT] if state[CONF_MIN_CURRENT] is not None else state[CONF_EVSE_MINIMUM_CHARGE_CURRENT]
    max_current = state[CONF_MAX_CURRENT] if state[CONF_MAX_CURRENT] is not None else state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]
    phases, calc_used = determine_phases(self, state)
    voltage = state[CONF_PHASE_VOLTAGE] if state[CONF_PHASE_VOLTAGE] is not None and is_number(state[CONF_PHASE_VOLTAGE]) else 230
    # Calculate total export current (sum of negative phase currents)
    total_export_current = (
        max(-state[CONF_PHASE_A_CURRENT], 0) +
        max(-state[CONF_PHASE_B_CURRENT], 0) +
        max(-state[CONF_PHASE_C_CURRENT], 0)
    )
    total_export_power = total_export_current * voltage
    if state[CONF_INVERT_PHASES]:
        state[CONF_PHASE_A_CURRENT], state[CONF_PHASE_B_CURRENT], state[CONF_PHASE_C_CURRENT] = -state[CONF_PHASE_A_CURRENT], -state[CONF_PHASE_B_CURRENT], -state[CONF_PHASE_C_CURRENT]
    grid_phase_a_current = state[CONF_PHASE_A_CURRENT]
    grid_phase_b_current = state[CONF_PHASE_B_CURRENT]
    grid_phase_c_current = state[CONF_PHASE_C_CURRENT]
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
    # phases is always 1-3 at this point, so no need for additional checks
    evse_current_per_phase = evse_current
    # Battery values
    battery_soc = state["battery_soc"]
    battery_power = state["battery_power"]
    battery_soc_target = state.get("battery_soc_target")
    battery_max_charge_power = state.get(CONF_BATTERY_MAX_CHARGE_POWER)
    battery_max_discharge_power = state.get(CONF_BATTERY_MAX_DISCHARGE_POWER)
    allow_grid_charging = state.get("allow_grid_charging", True)
    allow_grid_charging_entity_id = state.get(CONF_ALLOW_GRID_CHARGING_ENTITY_ID)
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
        max_evse_available=0,  # will be set after calculation
        min_current=min_current,
        max_current=max_current,
        total_export_power=total_export_power,
        battery_soc=battery_soc,
        battery_power=battery_power,
        battery_soc_target=battery_soc_target,
        battery_max_charge_power=battery_max_charge_power,
        battery_max_discharge_power=battery_max_discharge_power,
        allow_grid_charging=allow_grid_charging,
        allow_grid_charging_entity_id=allow_grid_charging_entity_id,
    )

# Calculate the available current based on the configuration and sensor data - this is the main function called by the integration
# It gathers all necessary data, determines the number of phases, and calculates the available current based on the selected charging mode.
# It also applies ramping logic to smooth out changes in available current
# and ensures that the current is within the defined limits.
def calculate_available_current(self):
    _LOGGER.debug(" Logging in 10")
    _LOGGER.debug(" Logging in 9")
    _LOGGER.debug(" Logging in 8")
    _LOGGER.debug(" Logging in 7")
    _LOGGER.debug(" Logging in 6")
    _LOGGER.debug(" Logging in 5")
    _LOGGER.debug(" Logging in 4")
    _LOGGER.debug(" Logging in 3")
    _LOGGER.debug(" Logging in 2")
    _LOGGER.debug(" Logging in 1")

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
