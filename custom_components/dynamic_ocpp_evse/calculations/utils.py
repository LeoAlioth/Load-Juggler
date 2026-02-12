"""Utility functions for Dynamic OCPP EVSE calculations."""
import datetime
import logging

_LOGGER = logging.getLogger(__name__)


def get_available_current(constraints: dict, phase_mask: str) -> float:
    """
    Helper: Get available per-phase current for a charger based on its phase mask.

    Implements Multi-Phase Constraint Principle:
    - 1-phase on A: MIN(constraints['A'], constraints['ABC'])
    - 2-phase on AB: MIN(constraints['A'], constraints['B'], constraints['AB']/2, constraints['ABC']/2)
    - 3-phase on ABC: MIN(A, B, C, AB/2, AC/2, BC/2, ABC/3)

    CRITICAL: Must check ABC total constraint to prevent exceeding total available power
    when multiple chargers on different phases draw from the same power pool.

    Args:
        constraints: Constraint dict with keys 'A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC'
        phase_mask: Phase mask ('A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC')

    Returns:
        Per-phase current available for this charger (single float value)
    """
    if len(phase_mask) == 1:
        # Single-phase charger - limited by phase, 2-phase combinations, and total
        phase = phase_mask

        # Find all 2-phase combinations that include this phase
        two_phase_limits = []
        for combo in ['AB', 'AC', 'BC']:
            if phase in combo:
                two_phase_limits.append(constraints[combo])

        return min(
            constraints[phase],      # Single-phase limit
            *two_phase_limits,       # All 2-phase combinations including this phase
            constraints['ABC']       # Total limit
        )

    elif len(phase_mask) == 2:
        # Two-phase charger (e.g., 'AB', 'AC', 'BC')
        phase_a = phase_mask[0]
        phase_b = phase_mask[1]
        return min(
            constraints[phase_a],
            constraints[phase_b],
            constraints[phase_mask] / 2,  # Divide 2-phase constraint by 2
            constraints['ABC'] / 2  # Must not exceed total available (divided by 2 for per-phase)
        )

    elif phase_mask == 'ABC':
        # Three-phase charger
        return min(
            constraints['A'],
            constraints['B'],
            constraints['C'],
            constraints['AB'] / 2,  # Divide 2-phase constraints by 2
            constraints['AC'] / 2,
            constraints['BC'] / 2,
            constraints['ABC'] / 3  # Divide 3-phase constraint by 3
        )

    else:
        _LOGGER.warning(f"Unknown phase mask '{phase_mask}', returning 0")
        return 0


def deduct_current(constraints: dict, current: float, phase_mask: str) -> dict:
    """
    Helper: Deduct current from constraint dict for all affected phase combinations.

    Simplified approach:
    1. Subtract from each phase being used
    2. Subtract from affected 2-phase combinations
    3. Subtract from ABC total
    4. Apply cascading limits to ensure consistency

    Args:
        constraints: Constraint dict with keys 'A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC'
        current: Per-phase current being drawn by the charger
        phase_mask: Phase mask ('A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC')

    Returns:
        New constraint dict with current deducted from all relevant combinations

    Example:
        constraints = {A:10, B:10, C:10, AB:15, AC:15, BC:15, ABC:20}
        deduct_current(constraints, 6, 'AB')
        returns {A:4, B:4, C:10, AB:3, AC:9, BC:9, ABC:8}
    """
    new_constraints = constraints.copy()

    # Step 1: Deduct from individual phases
    for phase in phase_mask:
        new_constraints[phase] -= current

    # Step 2: Deduct from affected 2-phase combinations
    for combo in ['AB', 'AC', 'BC']:
        overlap = sum(1 for p in phase_mask if p in combo)
        if overlap > 0:
            new_constraints[combo] -= current * overlap

    # Step 3: Deduct total from ABC
    total_consumed = current * len(phase_mask)
    new_constraints['ABC'] -= total_consumed

    # Step 4: Apply cascading limits to ensure consistency
    return apply_constraint_limits(new_constraints)


def apply_constraint_limits(constraints: dict) -> dict:
    """
    Apply cascading limits to ensure constraint dict consistency.

    Enforces bidirectional physical constraints:
    1. Larger combinations limited by smaller: AB ≤ A+B, ABC ≤ A+B+C
    2. Smaller combinations limited by larger: A ≤ AB, A ≤ AC, A ≤ ABC

    This ensures that no constraint can be larger than what its components
    can provide, AND no component can be larger than any combination it's part of.

    Args:
        constraints: Constraint dict to normalize

    Returns:
        Normalized constraint dict with cascading limits applied
    """
    result = constraints.copy()

    # Apply constraints iteratively to ensure convergence
    # (changes in one level can affect others, so we iterate)
    for _ in range(2):  # Two iterations typically sufficient
        # DOWNWARD: Limit larger combinations by smaller components
        # Rule: AB ≤ A+B, AC ≤ A+C, BC ≤ B+C
        result['AB'] = min(result['AB'], result['A'] + result['B'], result['ABC'])
        result['AC'] = min(result['AC'], result['A'] + result['C'], result['ABC'])
        result['BC'] = min(result['BC'], result['B'] + result['C'], result['ABC'])

        # Rule: ABC ≤ A+B+C and ABC ≤ (any 2-phase combo + remaining phase)
        result['ABC'] = min(
            result['ABC'],
            result['A'] + result['B'] + result['C'],  # Sum of all single phases
            result['AB'] + result['C'],  # AB pair + remaining C
            result['AC'] + result['B'],  # AC pair + remaining B
            result['BC'] + result['A']   # BC pair + remaining A
        )

        # UPWARD: Limit smaller components by larger combinations they're part of
        # Rule: A ≤ AB, A ≤ AC, A ≤ ABC (and same for B, C)
        result['A'] = min(result['A'], result['AB'], result['AC'], result['ABC'])
        result['B'] = min(result['B'], result['AB'], result['BC'], result['ABC'])
        result['C'] = min(result['C'], result['AC'], result['BC'], result['ABC'])

    # Ensure all non-negative
    for key in result:
        result[key] = max(0, result[key])

    return result


def is_number(value):
    """Check if a value can be converted to a float."""
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def get_sensor_data(hass, sensor, attribute=None):
    """
    Get sensor data from Home Assistant.
    
    Args:
        hass: HomeAssistant instance
        sensor: Entity ID
        attribute: Optional attribute name to get instead of state
        
    Returns:
        Sensor state value or attribute value
    """
    _LOGGER.debug(f"Getting {'attribute ' + attribute if attribute else 'state'} for sensor: {sensor}")
    state = hass.states.get(sensor)
    if state is None:
        _LOGGER.warning(f"Failed to get state for sensor: {sensor}")
        return None
    
    # Get attribute if requested
    if attribute:
        value = state.attributes.get(attribute)
        if value is None:
            _LOGGER.debug(f"Attribute '{attribute}' not found for sensor: {sensor}")
            return None
    else:
        value = state.state
    
    _LOGGER.debug(f"Got {'attribute ' + attribute if attribute else 'state'} for sensor: {sensor}  -  {value} ({type(value)})")
    
    # Convert to number if possible
    if type(value) == str:
        if is_number(value):
            value = float(value)
            _LOGGER.debug(f"Sensor: {sensor}  -  converted to ({type(value)})")
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
    """
    Apply ramping logic to smooth current changes and prevent oscillations.
    
    Configuration changes (mode, min/max current) bypass ramping for instant response.
    Normal power fluctuations use ramping to prevent oscillations.
    """
    if not hasattr(sensor, '_last_ramp_value'):
        sensor._last_ramp_value = None
    if not hasattr(sensor, '_last_ramp_time'):
        sensor._last_ramp_time = None
    if not hasattr(sensor, '_last_charging_mode'):
        sensor._last_charging_mode = None
    if not hasattr(sensor, '_last_min_current'):
        sensor._last_min_current = None
    if not hasattr(sensor, '_last_max_current'):
        sensor._last_max_current = None

    # Ramping rates
    ramp_limit_up = 0.1    # Amps per second (ramp up) - slightly faster to reduce lag
    ramp_limit_down = 0.2  # Amps per second (ramp down, faster for safety)
    
    # Schmitt trigger hysteresis to prevent oscillations
    # Only start ramping when outside the hysteresis band around target
    hysteresis = 0.2  # Amps - hysteresis band in both directions
    
    now = datetime.datetime.now()

    
    # Check for configuration changes (skip ramping if config changed)
    from ..const import CONF_CHARING_MODE, CONF_MIN_CURRENT, CONF_MAX_CURRENT
    current_mode = state.get(CONF_CHARING_MODE)
    current_min = state.get(CONF_MIN_CURRENT)
    current_max = state.get(CONF_MAX_CURRENT)
    
    config_changed = False
    if (sensor._last_charging_mode is not None and sensor._last_charging_mode != current_mode):
        _LOGGER.info(f"Charging mode changed from {sensor._last_charging_mode} to {current_mode} - applying instantly")
        config_changed = True
    if (sensor._last_min_current is not None and sensor._last_min_current != current_min):
        _LOGGER.info(f"Min current changed from {sensor._last_min_current}A to {current_min}A - applying instantly")
        config_changed = True
    if (sensor._last_max_current is not None and sensor._last_max_current != current_max):
        _LOGGER.info(f"Max current changed from {sensor._last_max_current}A to {current_max}A - applying instantly")
        config_changed = True
    
    # Store current config for next comparison
    sensor._last_charging_mode = current_mode
    sensor._last_min_current = current_min
    sensor._last_max_current = current_max
    
    # If config changed, skip ramping and apply target immediately
    if config_changed:
        _LOGGER.debug(f"Config change detected - setting current to {state[conf_available_current]}A instantly (no ramping)")
        sensor._last_ramp_value = state[conf_available_current]
        sensor._last_ramp_time = now
        return
    
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
            target = state[conf_available_current]
            current = sensor._last_ramp_value
            
            # Schmitt trigger: Calculate thresholds
            lower_threshold = target - hysteresis  # Start ramping up if below this
            upper_threshold = target + hysteresis  # Start ramping down if above this
            
            if current < lower_threshold:
                # Below lower threshold: RAMP UP to target
                max_delta = ramp_limit_up * max(dt, 0.1)
                delta = target - current
                if delta > max_delta:
                    ramped_value = current + max_delta
                    _LOGGER.debug(f"Schmitt trigger: Ramping up {current:.2f}A -> {ramped_value:.2f}A (target {target}A, threshold {lower_threshold:.2f}A)")
                else:
                    ramped_value = target
                    _LOGGER.debug(f"Schmitt trigger: Reached target {target}A from below")
                    
            elif current > upper_threshold:
                # Above upper threshold: RAMP DOWN to target
                max_delta = ramp_limit_down * max(dt, 0.1)
                delta = current - target
                if delta > max_delta:
                    ramped_value = current - max_delta
                    _LOGGER.debug(f"Schmitt trigger: Ramping down {current:.2f}A -> {ramped_value:.2f}A (target {target}A, threshold {upper_threshold:.2f}A)")
                else:
                    ramped_value = target
                    _LOGGER.debug(f"Schmitt trigger: Reached target {target}A from above")
                    
            else:
                # Within hysteresis band: HOLD (no oscillation)
                ramped_value = current
                _LOGGER.debug(f"Schmitt trigger: Holding at {current:.2f}A (within {lower_threshold:.2f}A - {upper_threshold:.2f}A band, target {target}A)")
                    
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
    Calculate per-phase available current (A) and total site available power (W).
    
    Per-phase current shows what each phase can handle:
    - Phase X current (A) = min(breaker_rating - phase_X_current, import_headroom)
    
    Total site power is constrained by import headroom:
    - Total power (W) = import_headroom × voltage
    
    Example: 25A breakers, consumption 1/4/10A, 35A import limit, 230V
    - Import headroom: 35 - (1+4+10) = 20A
    - Phase A: min(25-1=24A, 20A) = 20A (can handle up to 20A)
    - Phase B: min(25-4=21A, 20A) = 20A (can handle up to 20A)
    - Phase C: min(25-10=15A, 20A) = 15A (can handle up to 15A)
    - Total power: 20A × 230V = 4,600W (limited by import headroom)
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
    
    # Constraint 2: Import power constraint (total headroom)
    max_import_current = max_import_power / voltage if voltage > 0 else 0
    import_headroom = max_import_current - context.total_import_current
    
    # Per phase available current (A) = min of breaker limit and total import headroom
    # This shows what each phase can independently handle
    context.site_available_current_phase_a = min(breaker_avail_a, import_headroom)
    context.site_available_current_phase_b = min(breaker_avail_b, import_headroom)
    context.site_available_current_phase_c = min(breaker_avail_c, import_headroom)
    
    # Grid available power (W) is constrained by import headroom
    context.site_grid_available_power = import_headroom * voltage
    
    # Total site available power (W) = grid + battery
    context.total_site_available_power = context.site_grid_available_power + context.site_battery_available_power
    
    _LOGGER.debug(
        f"Site available - Breakers: {breaker_rating}A, Import headroom: {import_headroom:.2f}A"
    )
    _LOGGER.debug(
        f"Per-phase current (A) - Phase A: {context.site_available_current_phase_a:.2f}A "
        f"(breaker avail: {breaker_avail_a:.2f}A), "
        f"Phase B: {context.site_available_current_phase_b:.2f}A "
        f"(breaker avail: {breaker_avail_b:.2f}A), "
        f"Phase C: {context.site_available_current_phase_c:.2f}A "
        f"(breaker avail: {breaker_avail_c:.2f}A)"
    )
    _LOGGER.debug(
        f"Site grid available: {context.site_grid_available_power:.0f}W, "
        f"Battery available: {context.site_battery_available_power:.0f}W, "
        f"Total available: {context.total_site_available_power:.0f}W"
    )
