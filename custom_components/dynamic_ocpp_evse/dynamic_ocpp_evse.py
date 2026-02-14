"""
Dynamic OCPP EVSE - Main calculation module.

This file provides a unified interface for EVSE calculations.
All core calculation logic has been refactored into the calculations/ directory.
"""

from .calculations import (
    SiteContext,
    ChargerContext,
    PhaseValues,
    calculate_all_charger_targets,
)
from .const import *


def calculate_available_current_for_hub(sensor):
    """
    Calculate available current for a hub.

    This is the main entry point for Home Assistant sensor updates.
    Reads HA entity states from hub config and builds a SiteContext.

    Args:
        sensor: The HA sensor object containing config, hub_entry, and hass

    Returns:
        dict with calculated values including:
            - CONF_AVAILABLE_CURRENT: Total available current (A)
            - CONF_PHASES: Number of phases
            - CONF_CHARING_MODE: Current charging mode
            - charger_targets: per-charger target currents
            - Other site/charger data
    """
    import logging
    _LOGGER = logging.getLogger(__name__)
    from .helpers import get_entry_value

    hass = sensor.hass
    hub_entry = sensor.hub_entry

    # --- Helper to read a float from an HA entity state ---
    def _read_entity(entity_id, default=0):
        """Read a numeric value from an HA entity."""
        if not entity_id:
            return default
        st = hass.states.get(entity_id)
        if st and st.state not in ('unknown', 'unavailable', None, ''):
            try:
                return float(st.state)
            except (ValueError, TypeError):
                return default
        return default

    # --- Read hub config values ---
    voltage = get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
    main_breaker_rating = get_entry_value(hub_entry, CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING)
    excess_threshold = get_entry_value(hub_entry, CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD)
    invert_phases = get_entry_value(hub_entry, CONF_INVERT_PHASES, False)

    # --- Read per-phase grid current from HA entities ---
    phase_a_entity = get_entry_value(hub_entry, CONF_PHASE_A_CURRENT_ENTITY_ID, None)
    phase_b_entity = get_entry_value(hub_entry, CONF_PHASE_B_CURRENT_ENTITY_ID, None)
    phase_c_entity = get_entry_value(hub_entry, CONF_PHASE_C_CURRENT_ENTITY_ID, None)

    # Read raw phase values - None if entity not configured (phase doesn't exist)
    raw_phase_a = _read_entity(phase_a_entity, 0) if phase_a_entity else None
    raw_phase_b = _read_entity(phase_b_entity, 0) if phase_b_entity else None
    raw_phase_c = _read_entity(phase_c_entity, 0) if phase_c_entity else None

    # Apply inversion if configured (only for existing phases)
    if invert_phases:
        if raw_phase_a is not None:
            raw_phase_a = -raw_phase_a
        if raw_phase_b is not None:
            raw_phase_b = -raw_phase_b
        if raw_phase_c is not None:
            raw_phase_c = -raw_phase_c

    # Split into consumption (import, positive) and export (surplus, positive)
    # Convention: raw > 0 means importing from grid, raw < 0 means exporting
    # None = phase doesn't physically exist
    phase_a_consumption = max(0, raw_phase_a) if raw_phase_a is not None else None
    phase_b_consumption = max(0, raw_phase_b) if raw_phase_b is not None else None
    phase_c_consumption = max(0, raw_phase_c) if raw_phase_c is not None else None

    phase_a_export_current = max(0, -raw_phase_a) if raw_phase_a is not None else None
    phase_b_export_current = max(0, -raw_phase_b) if raw_phase_b is not None else None
    phase_c_export_current = max(0, -raw_phase_c) if raw_phase_c is not None else None

    # Use PhaseValues for clean aggregation (sums non-None values only)
    consumption_pv = PhaseValues(phase_a_consumption, phase_b_consumption, phase_c_consumption)
    export_pv = PhaseValues(phase_a_export_current, phase_b_export_current, phase_c_export_current)

    total_export_current = export_pv.total
    total_export_power = total_export_current * voltage if voltage > 0 else 0

    # Solar production: use dedicated entity if configured, otherwise derive from grid meter
    solar_production_entity = get_entry_value(hub_entry, CONF_SOLAR_PRODUCTION_ENTITY_ID, None)
    if solar_production_entity:
        solar_production_total = _read_entity(solar_production_entity, 0)
    else:
        solar_production_total = (consumption_pv.total + export_pv.total) * voltage

    # --- Read battery data from HA entities ---
    battery_soc_entity = get_entry_value(hub_entry, CONF_BATTERY_SOC_ENTITY_ID, None)
    battery_power_entity = get_entry_value(hub_entry, CONF_BATTERY_POWER_ENTITY_ID, None)
    battery_soc_target_entity = get_entry_value(hub_entry, CONF_BATTERY_SOC_TARGET_ENTITY_ID, None)

    battery_soc = _read_entity(battery_soc_entity, None) if battery_soc_entity else None
    battery_power = _read_entity(battery_power_entity, None) if battery_power_entity else None
    battery_soc_min = get_entry_value(hub_entry, CONF_BATTERY_SOC_MIN, DEFAULT_BATTERY_SOC_MIN)
    battery_soc_target = _read_entity(battery_soc_target_entity, None) if battery_soc_target_entity else None
    battery_soc_hysteresis = get_entry_value(hub_entry, CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS)
    battery_max_charge_power = get_entry_value(hub_entry, CONF_BATTERY_MAX_CHARGE_POWER, None)
    battery_max_discharge_power = get_entry_value(hub_entry, CONF_BATTERY_MAX_DISCHARGE_POWER, None)

    # --- Read max grid import power from HA entity ---
    max_import_power_entity = get_entry_value(hub_entry, CONF_MAX_IMPORT_POWER_ENTITY_ID, None)
    max_grid_import_power = _read_entity(max_import_power_entity, None) if max_import_power_entity else None
    inverter_supports_asymmetric = False  # TODO: make configurable

    # --- Read charging and distribution mode from HA select entities ---
    hub_entity_id = hub_entry.data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")

    charging_mode_entity = f"select.{hub_entity_id}_charging_mode"
    charging_mode_state = hass.states.get(charging_mode_entity)
    charging_mode = charging_mode_state.state if charging_mode_state and charging_mode_state.state else "Standard"

    distribution_mode_entity = f"select.{hub_entity_id}_distribution_mode"
    distribution_mode_state = hass.states.get(distribution_mode_entity)
    distribution_mode = distribution_mode_state.state if distribution_mode_state and distribution_mode_state.state else "Priority"

    # --- Read allow_grid_charging switch and power_buffer number ---
    allow_grid_entity = f"switch.{hub_entity_id}_allow_grid_charging"
    allow_grid_state = hass.states.get(allow_grid_entity)
    allow_grid_charging = allow_grid_state.state != "off" if allow_grid_state else True

    power_buffer_entity = f"number.{hub_entity_id}_power_buffer"
    power_buffer = _read_entity(power_buffer_entity, 0)

    # Apply power buffer to reduce effective max grid import power
    if max_grid_import_power is not None and power_buffer > 0:
        max_grid_import_power = max(0, max_grid_import_power - power_buffer)

    _LOGGER.debug(
        "Hub state read: phases=%d, phase_a=%sA (entity=%s), phase_b=%sA, phase_c=%sA, "
        "export=%.1fA, battery_soc=%s, mode=%s, dist=%s",
        consumption_pv.active_count, raw_phase_a, phase_a_entity, raw_phase_b, raw_phase_c,
        total_export_current, battery_soc, charging_mode, distribution_mode
    )
    
    # Build site context
    site = SiteContext(
        voltage=voltage,
        main_breaker_rating=main_breaker_rating,
        grid_current=PhaseValues(raw_phase_a, raw_phase_b, raw_phase_c),
        consumption=PhaseValues(phase_a_consumption, phase_b_consumption, phase_c_consumption),
        export_current=PhaseValues(phase_a_export_current, phase_b_export_current, phase_c_export_current),
        solar_production_total=solar_production_total,
        battery_soc=float(battery_soc) if battery_soc is not None else None,
        battery_power=float(battery_power) if battery_power is not None else None,
        battery_soc_min=float(battery_soc_min) if battery_soc_min is not None else None,
        battery_soc_target=float(battery_soc_target) if battery_soc_target is not None else None,
        battery_soc_hysteresis=float(battery_soc_hysteresis) if battery_soc_hysteresis is not None else 5,
        battery_max_charge_power=float(battery_max_charge_power) if battery_max_charge_power is not None else None,
        battery_max_discharge_power=float(battery_max_discharge_power) if battery_max_discharge_power is not None else None,
        max_grid_import_power=float(max_grid_import_power) if max_grid_import_power is not None else None,
        inverter_supports_asymmetric=inverter_supports_asymmetric,
        excess_export_threshold=excess_threshold,
        allow_grid_charging=allow_grid_charging,
        power_buffer=power_buffer,
        distribution_mode=distribution_mode,
        charging_mode=charging_mode,
    )

    # Add chargers to site
    from . import get_chargers_for_hub
    hub_entry_id = hub_entry.entry_id if hasattr(hub_entry, 'entry_id') else hub_entry.data.get('hub_entry_id')

    if hasattr(sensor, '_charger_entries'):
        chargers = sensor._charger_entries
    else:
        chargers = get_chargers_for_hub(hass, hub_entry_id)
    
    for entry in chargers:
        device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        charger_entity_id = entry.data.get(CONF_ENTITY_ID, f"charger_{entry.entry_id}")
        priority = get_entry_value(entry, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY)

        if device_type == DEVICE_TYPE_PLUG:
            # Smart plug / relay — binary on/off device with fixed power rating
            power_rating = get_entry_value(entry, CONF_PLUG_POWER_RATING, DEFAULT_PLUG_POWER_RATING)
            connected_to_phase = get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A")
            phases = len(connected_to_phase)
            equivalent_current = power_rating / (voltage * phases) if voltage > 0 else 0

            # Determine connector status from plug switch state
            plug_switch_entity = entry.data.get(CONF_PLUG_SWITCH_ENTITY_ID)
            plug_switch_state = hass.states.get(plug_switch_entity) if plug_switch_entity else None

            # Check power monitor if available (more reliable than switch state)
            power_monitor_entity = get_entry_value(entry, CONF_PLUG_POWER_MONITOR_ENTITY_ID, None)
            if power_monitor_entity:
                power_draw = _read_entity(power_monitor_entity, 0)
                connector_status = "Charging" if power_draw > 10 else "Available"
            elif plug_switch_state:
                connector_status = "Charging" if plug_switch_state.state == "on" else "Available"
            else:
                connector_status = "Charging"  # Default to active if we can't determine

            charger = ChargerContext(
                charger_id=entry.entry_id,
                entity_id=charger_entity_id,
                min_current=equivalent_current,
                max_current=equivalent_current,
                phases=phases,
                priority=priority,
                active_phases_mask=connected_to_phase,
                connector_status=connector_status,
                device_type=DEVICE_TYPE_PLUG,
            )
        else:
            # OCPP EVSE — standard charger with current modulation
            min_current = get_entry_value(entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
            max_current = get_entry_value(entry, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)
            phases = int(get_entry_value(entry, CONF_PHASES, 3) or 3)

            # Read connector status from OCPP entity
            connector_status_entity = f"sensor.{charger_entity_id}_status_connector"
            connector_status_state = hass.states.get(connector_status_entity)
            connector_status = connector_status_state.state if connector_status_state else "Unknown"

            charger = ChargerContext(
                charger_id=entry.entry_id,
                entity_id=charger_entity_id,
                min_current=min_current,
                max_current=max_current,
                phases=phases,
                priority=priority,
                connector_status=connector_status,
            )

            # Get OCPP data for this charger
            evse_import = entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
            if evse_import:
                evse_state = hass.states.get(evse_import)
                if evse_state and evse_state.state not in ['unknown', 'unavailable', None]:
                    try:
                        charger.l1_current = float(evse_state.attributes.get('l1_current', 0) or 0)
                        charger.l2_current = float(evse_state.attributes.get('l2_current', 0) or 0)
                        charger.l3_current = float(evse_state.attributes.get('l3_current', 0) or 0)
                    except (ValueError, TypeError):
                        pass

        site.chargers.append(charger)
    
    # Calculate targets (includes distribution)
    calculate_all_charger_targets(site)

    # Build per-charger targets dict — these ARE the final allocations
    charger_targets = {c.charger_id: c.allocated_current for c in site.chargers}
    charger_available = {c.charger_id: c.available_current for c in site.chargers}

    # Total available current = sum of all charger targets
    total_available = sum(charger_targets.values())

    # Compute derived values for hub sensors
    # Grid headroom: how much more we can import per phase before hitting breaker
    grid_headroom_a = max(0, main_breaker_rating - phase_a_consumption) * voltage if phase_a_consumption is not None else 0
    grid_headroom_b = max(0, main_breaker_rating - phase_b_consumption) * voltage if phase_b_consumption is not None else 0
    grid_headroom_c = max(0, main_breaker_rating - phase_c_consumption) * voltage if phase_c_consumption is not None else 0
    site_grid_available_power = round(grid_headroom_a + grid_headroom_b + grid_headroom_c, 0)

    # Battery discharge power available for EV charging
    if (battery_soc is not None and battery_soc_min is not None
            and battery_soc >= battery_soc_min and battery_max_discharge_power):
        available_battery_power = round(float(battery_max_discharge_power), 0)
    else:
        available_battery_power = 0

    # Total EVSE power = sum of actual charger draws
    total_evse_power = round(
        sum((c.l1_current + c.l2_current + c.l3_current) * voltage for c in site.chargers), 0
    )

    # Build result dict
    result = {
        CONF_AVAILABLE_CURRENT: round(total_available, 1),
        CONF_PHASES: site.num_phases,
        CONF_CHARING_MODE: site.charging_mode,
        "calc_used": "calculate_all_charger_targets",

        # Site-level data for hub sensor
        "battery_soc": site.battery_soc,
        "battery_soc_min": site.battery_soc_min,
        "battery_soc_target": site.battery_soc_target,
        "battery_power": battery_power,
        "available_battery_power": available_battery_power,
        # Per-phase available current for EV = breaker headroom + solar surplus
        "site_available_current_phase_a": round(main_breaker_rating - raw_phase_a, 1) if raw_phase_a is not None else 0,
        "site_available_current_phase_b": round(main_breaker_rating - raw_phase_b, 1) if raw_phase_b is not None else 0,
        "site_available_current_phase_c": round(main_breaker_rating - raw_phase_c, 1) if raw_phase_c is not None else 0,
        # Total site available power = sum of per-phase available * voltage
        "total_site_available_power": round(
            (max(0, main_breaker_rating - raw_phase_a) * voltage if raw_phase_a is not None else 0)
            + (max(0, main_breaker_rating - raw_phase_b) * voltage if raw_phase_b is not None else 0)
            + (max(0, main_breaker_rating - raw_phase_c) * voltage if raw_phase_c is not None else 0),
            0,
        ),
        "net_site_consumption": round(consumption_pv.total * voltage, 0),
        "site_grid_available_power": site_grid_available_power,
        "site_battery_available_power": available_battery_power,
        "total_evse_power": total_evse_power,
        "solar_surplus_power": round(total_export_power, 0),
        "solar_surplus_current": round(total_export_current, 2),

        # Per-charger targets — these are the final allocations from the engine
        "charger_targets": charger_targets,
        "charger_available": charger_available,
        "distribution_mode": site.distribution_mode,
    }

    return result


__all__ = [
    "SiteContext",
    "ChargerContext",
    "PhaseValues",
    "calculate_all_charger_targets",
    "calculate_available_current_for_hub",
]