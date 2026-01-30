import datetime
import logging
from .const import *
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
    battery_soc_min: float = None
    battery_soc_hysteresis: float = None
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


def get_sensor_data(hass, sensor):
    """Get sensor data from Home Assistant."""
    _LOGGER.debug(f"Getting state for sensor: {sensor}")
    state = hass.states.get(sensor)
    if state is None:
        _LOGGER.warning(f"Failed to get state for sensor: {sensor}")
        return None
    _LOGGER.debug(f"Got state for sensor: {sensor}  -  {state} ({type(state.state)})")
    value = state.state
    if type(value) == str:
        if is_number(value):
            value = float(value)
            _LOGGER.debug(f"Sensor: {sensor}  -  {state} is ({type(value)})")
    return value


def get_sensor_attribute(hass, sensor, attribute):
    """Get sensor attribute from Home Assistant."""
    state = hass.states.get(sensor)
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
    """Apply ramping logic to smooth current changes."""
    if not hasattr(self, '_last_ramp_value'):
        self._last_ramp_value = None
    if not hasattr(self, '_last_ramp_time'):
        self._last_ramp_time = None

    ramp_limit_up = 0.05   # Amps per second (ramp up)
    ramp_limit_down = 0.2  # Amps per second (ramp down, faster)
    now = datetime.datetime.now()
    
    ramp_enabled = True
    if ramp_enabled:
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


# ==================== CHARGING MODE CALCULATIONS ====================

def _check_soc_threshold_with_hysteresis(sensor, threshold_name, battery_soc, threshold, hysteresis, is_above_check=True):
    """
    Check if SOC is above/below a threshold with hysteresis.
    
    Args:
        sensor: Sensor object to store state
        threshold_name: Name of threshold for state tracking (e.g., "min_soc", "target_soc")
        battery_soc: Current battery SOC
        threshold: The SOC threshold to check against
        hysteresis: Hysteresis percentage
        is_above_check: If True, returns True when SOC is above threshold. If False, returns True when below.
    
    Returns:
        bool: Whether the condition is met (with hysteresis applied)
    """
    state_attr = f"_soc_above_{threshold_name}"
    was_above = getattr(sensor, state_attr, None)
    
    if is_above_check:
        # Check if SOC is above threshold
        if was_above is None:
            # First check - use simple comparison
            is_above = battery_soc >= threshold
        elif was_above:
            # Was above - stay above until we drop below threshold - hysteresis
            is_above = battery_soc >= (threshold - hysteresis)
        else:
            # Was below - stay below until we rise above threshold
            is_above = battery_soc >= threshold
        
        setattr(sensor, state_attr, is_above)
        return is_above
    else:
        # Check if SOC is below threshold (inverse logic)
        if was_above is None:
            is_below = battery_soc < threshold
        elif not was_above:  # was_below
            # Was below - stay below until we rise above threshold + hysteresis
            is_below = battery_soc < (threshold + hysteresis)
        else:
            # Was above - stay above until we drop below threshold
            is_below = battery_soc < threshold
        
        setattr(sensor, state_attr, not is_below)
        return is_below


def _calculate_base_target_evse(context: ChargeContext, available_battery_current: float, allow_grid_import: bool = True):
    """Calculate base target EVSE current from available power sources."""
    state = context.state
    max_import_power = state[CONF_MAX_IMPORT_POWER]
    max_import_current = max_import_power / context.voltage
    
    if not context.allow_grid_charging or not allow_grid_import:
        remaining_available_import_current = 0
    else:
        remaining_available_import_current = max_import_current - context.total_import_current

    remaining_available_current_phase_a = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_a_current
    remaining_available_current_phase_b = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_b_current
    remaining_available_current_phase_c = state[CONF_MAIN_BREAKER_RATING] - context.grid_phase_c_current

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
        target_evse = state.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
    
    return target_evse


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
    above_min_soc = _check_soc_threshold_with_hysteresis(
        sensor, "standard_min", battery_soc, battery_soc_min, hysteresis, is_above_check=True
    )
    
    if not above_min_soc:
        _LOGGER.debug(f"Standard mode: Battery SOC {battery_soc}% below minimum {battery_soc_min}% - no charging")
        return 0
    
    # Above min_soc - full battery discharge available
    available_battery_power = max(0, battery_max_discharge_power)
    available_battery_current = available_battery_power / context.voltage if context.voltage else 0
    
    target_evse = _calculate_base_target_evse(context, available_battery_current, allow_grid_import=True)
    
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
    battery_soc_target = context.battery_soc_target if context.battery_soc_target is not None else 50
    hysteresis = context.battery_soc_hysteresis if context.battery_soc_hysteresis is not None else DEFAULT_BATTERY_SOC_HYSTERESIS
    battery_power = context.battery_power if context.battery_power is not None else 0
    
    # Check if we're above target SOC (with hysteresis)
    above_target_soc = _check_soc_threshold_with_hysteresis(
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
    
    target_evse = _calculate_base_target_evse(context, available_battery_current, allow_grid_import=False)
    
    _LOGGER.debug(f"Solar mode: SOC {battery_soc}% >= target {battery_soc_target}%, solar rate {target_evse}A")
    return max(target_evse, 0)


def calculate_eco_mode(sensor, context: ChargeContext):
    """
    Calculate target current for Eco mode.
    
    Eco mode behavior with battery:
    - Below min_soc (with hysteresis): No charging
    - Between min_soc and target_soc: Minimum rate charging (no battery discharge)
    - At target_soc: Solar production rate
    - Above target_soc: Full speed (like Standard mode)
    """
    battery_soc = context.battery_soc if context.battery_soc is not None else 100
    battery_soc_min = context.battery_soc_min if context.battery_soc_min is not None else DEFAULT_BATTERY_SOC_MIN
    battery_soc_target = context.battery_soc_target if context.battery_soc_target is not None else 50
    hysteresis = context.battery_soc_hysteresis if context.battery_soc_hysteresis is not None else DEFAULT_BATTERY_SOC_HYSTERESIS
    battery_power = context.battery_power if context.battery_power is not None else 0
    battery_max_discharge_power = context.battery_max_discharge_power if context.battery_max_discharge_power is not None else 0
    
    # Check if we're above minimum SOC (with hysteresis)
    above_min_soc = _check_soc_threshold_with_hysteresis(
        sensor, "eco_min", battery_soc, battery_soc_min, hysteresis, is_above_check=True
    )
    
    if not above_min_soc:
        _LOGGER.debug(f"Eco mode: Battery SOC {battery_soc}% below minimum {battery_soc_min}% - no charging")
        return 0
    
    # Check if we're above target SOC (with hysteresis)
    above_target_soc = _check_soc_threshold_with_hysteresis(
        sensor, "eco_target", battery_soc, battery_soc_target, hysteresis, is_above_check=True
    )
    
    if above_target_soc:
        # Above target_soc - full speed (like Standard mode)
        available_battery_power = max(0, battery_max_discharge_power)
        available_battery_current = available_battery_power / context.voltage if context.voltage else 0
        target_evse = _calculate_base_target_evse(context, available_battery_current, allow_grid_import=True)
        # Clamp to max_evse_available
        target_evse = min(target_evse, context.max_evse_available, context.max_current)
        _LOGGER.debug(f"Eco mode: SOC {battery_soc}% > target {battery_soc_target}%, full speed {target_evse}A")
        return target_evse
    
    # Between min_soc and target_soc
    # Check if at target_soc (battery is charging = has solar)
    battery_is_charging = battery_power < 0
    at_target_with_solar = battery_soc >= battery_soc_target and battery_is_charging
    
    if at_target_with_solar:
        # At target with solar - charge at solar rate
        available_battery_current = max(0, -battery_power / context.voltage) if context.voltage else 0
        target_evse = _calculate_base_target_evse(context, available_battery_current, allow_grid_import=False)
        target_evse = max(target_evse, context.min_current)
        # Clamp to max_evse_available
        target_evse = min(target_evse, context.max_evse_available, context.max_current)
        _LOGGER.debug(f"Eco mode: SOC {battery_soc}% at target with solar, rate {target_evse}A")
        return target_evse
    
    # Between min_soc and target_soc without solar - minimum rate only (clamped to available)
    target_evse = min(context.min_current, context.max_evse_available, context.max_current)
    _LOGGER.debug(f"Eco mode: SOC {battery_soc}% between min {battery_soc_min}% and target {battery_soc_target}% - min rate {target_evse}A (clamped from {context.min_current}A)")
    return target_evse


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
    battery_soc_target = context.battery_soc_target if context.battery_soc_target is not None else 50
    hysteresis = context.battery_soc_hysteresis if context.battery_soc_hysteresis is not None else DEFAULT_BATTERY_SOC_HYSTERESIS
    battery_power = context.battery_power if context.battery_power is not None else 0
    
    # Check if we're above minimum SOC (with hysteresis)
    above_min_soc = _check_soc_threshold_with_hysteresis(
        sensor, "excess_min", battery_soc, battery_soc_min, hysteresis, is_above_check=True
    )
    
    if not above_min_soc:
        _LOGGER.debug(f"Excess mode: Battery SOC {battery_soc}% below minimum {battery_soc_min}% - no charging")
        return 0
    
    # If battery is nearly full (>= 98%), act like solar mode - match production
    if battery_soc >= 98:
        available_battery_current = max(0, -battery_power / voltage) if voltage else 0
        target_evse = _calculate_base_target_evse(context, available_battery_current, allow_grid_import=False)
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
    _LOGGER.debug(f"Excess mode: SOC {battery_soc}%, export {total_export_power}W, target {target_evse}A")
    return target_evse


# ==================== STATE GATHERING FUNCTIONS ====================

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

    # Battery SOC minimum from hub number entity
    battery_soc_min_entity = f"number.{hub_entity_id}_home_battery_soc_min"
    state["battery_soc_min"] = get_sensor_data(hass, battery_soc_min_entity)
    if state["battery_soc_min"] is None:
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


# ==================== MAIN CALCULATION FUNCTION ====================

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
    apply_ramping(sensor, state, target_evse, charge_context.min_current)
    
    if state[CONF_AVAILABLE_CURRENT] < state[CONF_EVSE_MINIMUM_CHARGE_CURRENT]:
        state[CONF_AVAILABLE_CURRENT] = 0
    if state[CONF_AVAILABLE_CURRENT] > state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]:
        state[CONF_AVAILABLE_CURRENT] = state[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]

    # Calculate available battery power for EV charging
    battery_soc = charge_context.battery_soc if charge_context.battery_soc is not None else 0
    battery_soc_min = charge_context.battery_soc_min if charge_context.battery_soc_min is not None else DEFAULT_BATTERY_SOC_MIN
    battery_soc_target = charge_context.battery_soc_target if charge_context.battery_soc_target is not None else 0
    battery_power = charge_context.battery_power if charge_context.battery_power is not None else 0
    battery_max_discharge_power = charge_context.battery_max_discharge_power if charge_context.battery_max_discharge_power is not None else 0
    
    # Available battery power depends on SOC level
    if battery_soc >= battery_soc_min:
        if battery_soc > battery_soc_target:
            # Above target - full discharge available
            available_battery_power = battery_max_discharge_power
        else:
            # Between min and target - only current discharge (no additional)
            available_battery_power = max(0, -battery_power)  # Negative power = charging, so negate
    else:
        # Below min - no battery power for EV
        available_battery_power = 0
    
    _LOGGER.info(f"Hub calculation: battery_soc={charge_context.battery_soc}, battery_soc_min={charge_context.battery_soc_min}, target_evse_standard={target_evse_standard}")
    
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
