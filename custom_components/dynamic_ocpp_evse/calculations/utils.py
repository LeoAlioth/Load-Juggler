"""Utility functions for Dynamic OCPP EVSE calculations."""
import datetime
import logging

_LOGGER = logging.getLogger(__name__)


def is_number(value):
    """Check if a value can be converted to a float."""
    try:
        float(value)
        return True
    except (ValueError, TypeError):
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


def apply_ramping(sensor, state, target_evse, min_current, conf_evse_current_offered, conf_available_current):
    """Apply ramping logic to smooth current changes and prevent oscillations."""
    if not hasattr(sensor, '_last_ramp_value'):
        sensor._last_ramp_value = None
    if not hasattr(sensor, '_last_ramp_time'):
        sensor._last_ramp_time = None

    # Ramping rates
    ramp_limit_up = 0.1    # Amps per second (ramp up) - slightly faster to reduce lag
    ramp_limit_down = 0.2  # Amps per second (ramp down, faster for safety)
    
    # Deadband to prevent oscillations - don't change if target is within this range of current value
    deadband = 0.5  # Amps - changes smaller than this are ignored
    
    now = datetime.datetime.now()
    
    ramp_enabled = True
    if ramp_enabled:
        if sensor._last_ramp_value is None or not is_number(sensor._last_ramp_value):
            ramped_value = state[conf_evse_current_offered] or state[conf_available_current]
            sensor._last_ramp_value = ramped_value
        else:
            ramped_value = sensor._last_ramp_value if is_number(sensor._last_ramp_value) else 0
            if ramped_value < min_current and target_evse > min_current:
                ramped_value = min_current
                sensor._last_ramp_value = ramped_value

        if sensor._last_ramp_value is not None and sensor._last_ramp_time is not None:
            dt = (now - sensor._last_ramp_time).total_seconds()
            delta = state[conf_available_current] - sensor._last_ramp_value
            
            # Apply deadband - don't change if within deadband unless it's a significant change
            if abs(delta) < deadband:
                # Keep current value unchanged to prevent oscillations
                ramped_value = sensor._last_ramp_value
                _LOGGER.debug(f"Deadband applied: staying at {ramped_value}A (target {state[conf_available_current]}A, delta {delta}A)")
            elif delta > 0:
                max_delta = ramp_limit_up * max(dt, 0.1)
                if delta > max_delta:
                    ramped_value = sensor._last_ramp_value + max_delta
                    _LOGGER.debug(f"Ramping up: {sensor._last_ramp_value} -> {ramped_value} (requested {state[conf_available_current]})")
                else:
                    ramped_value = state[conf_available_current]
            else:
                max_delta = ramp_limit_down * max(dt, 0.1)
                if abs(delta) > max_delta:
                    ramped_value = sensor._last_ramp_value - max_delta
                    _LOGGER.debug(f"Ramping down: {sensor._last_ramp_value} -> {ramped_value} (requested {state[conf_available_current]})")
                else:
                    ramped_value = state[conf_available_current]
                    
        sensor._last_ramp_value = ramped_value
        sensor._last_ramp_time = now
        state[conf_available_current] = ramped_value


def calculate_site_battery_available_power(context):
    """
    Calculate battery available power for the whole site using three-state SOC logic.
    
    Three states:
    1. Below hysteresis zone (SOC < min_soc - hysteresis): No battery available (0W)
    2. Within hysteresis zone (min_soc - hysteresis <= SOC < min_soc): Only charging power available
    3. Above minimum (SOC >= min_soc): Full battery power available
    
    Returns:
        float: Available battery power in Watts
    """
    from ..const import DEFAULT_BATTERY_SOC_MIN, DEFAULT_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_MAX_POWER
    
    battery_soc = context.battery_soc if context.battery_soc is not None else 0
    battery_power = context.battery_power if context.battery_power is not None else 0
    battery_soc_min = context.battery_soc_min if context.battery_soc_min is not None else DEFAULT_BATTERY_SOC_MIN
    hysteresis = context.battery_soc_hysteresis if context.battery_soc_hysteresis is not None else DEFAULT_BATTERY_SOC_HYSTERESIS
    battery_max_discharge = context.battery_max_discharge_power if context.battery_max_discharge_power is not None else DEFAULT_BATTERY_MAX_POWER
    
    # Calculate hysteresis bounds
    lower_bound = battery_soc_min - hysteresis
    
    if battery_soc < lower_bound:
        # State 1: Below hysteresis - no battery available
        available_power = 0
        _LOGGER.debug(f"Battery SOC {battery_soc}% < {lower_bound}% (min-hysteresis): No battery power available")
        
    elif battery_soc < battery_soc_min:
        # State 2: Within hysteresis - only charging power available
        if battery_power < 0:  # Battery is charging (negative power)
            available_power = abs(battery_power)
            _LOGGER.debug(f"Battery SOC {battery_soc}% in hysteresis zone: Using charging power {available_power}W")
        else:  # Battery discharging or idle
            available_power = 0
            _LOGGER.debug(f"Battery SOC {battery_soc}% in hysteresis zone, not charging: No battery power available")
            
    else:  # battery_soc >= battery_soc_min
        # State 3: Above minimum - full power available
        if battery_power < 0:  # Charging (negative power)
            available_power = abs(battery_power)
            _LOGGER.debug(f"Battery SOC {battery_soc}% >= {battery_soc_min}%, charging: Available power {available_power}W")
        else:  # Discharging (positive power)
            # Current discharge + remaining capacity
            remaining_capacity = max(0, battery_max_discharge - battery_power)
            available_power = battery_power + remaining_capacity
            _LOGGER.debug(f"Battery SOC {battery_soc}% >= {battery_soc_min}%, discharging {battery_power}W: Available power {available_power}W")
    
    return available_power


def calculate_site_available_power(context):
    """
    Calculate per-phase and total site available current/power.
    
    This function calculates the available current on each phase based on:
    1. Main breaker rating per phase
    2. Maximum import power limit (distributed across active phases)
    
    The per-phase available current is the minimum of both constraints.
    Updates the context object with all calculated values.
    """
    from ..const import CONF_MAIN_BREAKER_RATING, CONF_MAX_IMPORT_POWER
    
    state = context.state
    voltage = context.voltage
    breaker_rating = state[CONF_MAIN_BREAKER_RATING]
    max_import_power = state[CONF_MAX_IMPORT_POWER]
    
    # Constraint 1: Breaker available current per phase
    breaker_avail_a = breaker_rating - context.grid_phase_a_current
    breaker_avail_b = breaker_rating - context.grid_phase_b_current
    breaker_avail_c = breaker_rating - context.grid_phase_c_current
    
    # Constraint 2: Import power constraint (distributed across active phases)
    max_import_current = max_import_power / voltage if voltage > 0 else 0
    import_headroom = max_import_current - context.total_import_current
    import_per_phase = import_headroom / context.phases if context.phases > 0 else 0
    
    # Per phase available = min of both constraints
    context.site_available_current_phase_a = min(breaker_avail_a, import_per_phase)
    context.site_available_current_phase_b = min(breaker_avail_b, import_per_phase)
    context.site_available_current_phase_c = min(breaker_avail_c, import_per_phase)
    
    # Convert to power
    context.site_available_power_phase_a = context.site_available_current_phase_a * voltage
    context.site_available_power_phase_b = context.site_available_current_phase_b * voltage
    context.site_available_power_phase_c = context.site_available_current_phase_c * voltage
    
    # Calculate totals
    context.total_site_available_current = (
        context.site_available_current_phase_a +
        context.site_available_current_phase_b +
        context.site_available_current_phase_c
    )
    context.total_site_available_power = context.total_site_available_current * voltage
    
    _LOGGER.debug(
        f"Site available current - Phase A: {context.site_available_current_phase_a:.2f}A, "
        f"Phase B: {context.site_available_current_phase_b:.2f}A, "
        f"Phase C: {context.site_available_current_phase_c:.2f}A, "
        f"Total: {context.total_site_available_current:.2f}A ({context.total_site_available_power:.0f}W)"
    )
