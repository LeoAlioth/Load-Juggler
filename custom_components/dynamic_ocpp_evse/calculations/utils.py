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
    """Apply ramping logic to smooth current changes."""
    if not hasattr(sensor, '_last_ramp_value'):
        sensor._last_ramp_value = None
    if not hasattr(sensor, '_last_ramp_time'):
        sensor._last_ramp_time = None

    ramp_limit_up = 0.05   # Amps per second (ramp up)
    ramp_limit_down = 0.2  # Amps per second (ramp down, faster)
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
            if delta > 0:
                max_delta = ramp_limit_up * max(dt, 0.1)
            else:
                max_delta = ramp_limit_down * max(dt, 0.1)
            if abs(delta) > max_delta:
                ramped_value = sensor._last_ramp_value + max_delta * (1 if delta > 0 else -1)
                _LOGGER.debug(f"Ramping limited: {sensor._last_ramp_value} -> {ramped_value} (requested {state[conf_available_current]})")
            else:
                ramped_value = state[conf_available_current]
        sensor._last_ramp_value = ramped_value
        sensor._last_ramp_time = now
        state[conf_available_current] = ramped_value
