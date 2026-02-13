"""
Dynamic OCPP EVSE - Main calculation module.

This file provides a unified interface for EVSE calculations.
All core calculation logic has been refactored into the calculations/ directory.
"""

from .calculations import (
    SiteContext,
    ChargerContext,
    calculate_all_charger_targets,
)

# Import utilities
from .calculations.utils import is_number

# Import context helpers
from .calculations.context import determine_phases


def calculate_available_current_for_hub(sensor):
    """
    Calculate available current for a hub (legacy wrapper).
    
    This is the main entry point for Home Assistant sensor updates.
    Uses the new SiteContext-based calculation system.
    
    Args:
        sensor: The HA sensor object containing config and state
        
    Returns:
        dict with calculated values including:
            - CONF_AVAILABLE_CURRENT: Total available current (A)
            - CONF_PHASES: Number of phases
            - CONF_CHARING_MODE: Current charging mode
            - Other site/charger data
    """
    from .helpers import get_entry_value
    from .const import (
        CONF_MAIN_BREAKER_RATING,
        CONF_PHASE_VOLTAGE,
        CONF_EXCESS_EXPORT_THRESHOLD,
        DEFAULT_MAIN_BREAKER_RATING,
        DEFAULT_PHASE_VOLTAGE,
        DEFAULT_EXCESS_EXPORT_THRESHOLD,
    )
    
    # Get hub config
    hub_entry = sensor.hub_entry
    
    # Build SiteContext from sensor state and hub config
    state = getattr(sensor, 'state', {}) or {}
    
    voltage = get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
    main_breaker_rating = get_entry_value(hub_entry, CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING)
    excess_threshold = get_entry_value(hub_entry, CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD)
    
    # Get grid currents from state
    grid_phase_a_current = float(state.get('grid_phase_a_current', 0) or 0)
    grid_phase_b_current = float(state.get('grid_phase_b_current', 0) or 0)
    grid_phase_c_current = float(state.get('grid_phase_c_current', 0) or 0)
    
    # Get consumption (home load before EV)
    phase_a_consumption = float(state.get('phase_a_consumption', 0) or 0)
    phase_b_consumption = float(state.get('phase_b_consumption', 0) or 0)
    phase_c_consumption = float(state.get('phase_c_consumption', 0) or 0)
    
    # Get export (solar surplus) - positive values
    phase_a_export_current = float(state.get('phase_a_export_current', 0) or 0)
    phase_b_export_current = float(state.get('phase_b_export_current', 0) or 0)
    phase_c_export_current = float(state.get('phase_c_export_current', 0) or 0)
    
    total_export_current = phase_a_export_current + phase_b_export_current + phase_c_export_current
    total_export_power = total_export_current * voltage if voltage > 0 else 0
    
    # Solar production total (for calculations)
    solar_production_total = float(state.get('solar_production_total', 0) or 0)
    
    # Battery data
    battery_soc = state.get('battery_soc')
    battery_soc_min = state.get('battery_soc_min')
    battery_soc_target = state.get('battery_soc_target')
    battery_max_charge_power = state.get('battery_max_charge_power')
    battery_max_discharge_power = state.get('battery_max_discharge_power')
    
    # Inverter data
    inverter_max_power = state.get('inverter_max_power')
    inverter_max_power_per_phase = state.get('inverter_max_power_per_phase')
    inverter_supports_asymmetric = bool(state.get('inverter_supports_asymmetric', False))
    
    # Distribution mode
    distribution_mode = state.get('distribution_mode', 'priority')
    charging_mode = state.get('charging_mode', 'Standard')
    
    # Build site context
    site = SiteContext(
        voltage=voltage,
        main_breaker_rating=main_breaker_rating,
        num_phases=int(state.get('num_phases', 3) or 3),
        grid_phase_a_current=grid_phase_a_current,
        grid_phase_b_current=grid_phase_b_current,
        grid_phase_c_current=grid_phase_c_current,
        phase_a_consumption=phase_a_consumption,
        phase_b_consumption=phase_b_consumption,
        phase_c_consumption=phase_c_consumption,
        phase_a_export=phase_a_export_current,
        phase_b_export=phase_b_export_current,
        phase_c_export=phase_c_export_current,
        solar_production_total=solar_production_total,
        total_export_current=total_export_current,
        total_export_power=total_export_power,
        battery_soc=float(battery_soc) if battery_soc is not None else None,
        battery_soc_min=float(battery_soc_min) if battery_soc_min is not None else None,
        battery_soc_target=float(battery_soc_target) if battery_soc_target is not None else None,
        battery_max_charge_power=float(battery_max_charge_power) if battery_max_charge_power is not None else None,
        battery_max_discharge_power=float(battery_max_discharge_power) if battery_max_discharge_power is not None else None,
        inverter_max_power=float(inverter_max_power) if inverter_max_power is not None else None,
        inverter_max_power_per_phase=float(inverter_max_power_per_phase) if inverter_max_power_per_phase is not None else None,
        inverter_supports_asymmetric=inverter_supports_asymmetric,
        excess_export_threshold=excess_threshold,
        distribution_mode=distribution_mode,
        charging_mode=charging_mode,
    )
    
    # Add chargers to site
    from . import get_chargers_for_hub
    hub_entry_id = hub_entry.entry_id if hasattr(hub_entry, 'entry_id') else hub_entry.data.get('hub_entry_id')
    
    if hasattr(sensor, '_charger_entries'):
        # Use pre-fetched charger entries
        chargers = sensor._charger_entries
    else:
        chargers = get_chargers_for_hub(sensor.hass, hub_entry_id)
    
    for entry in chargers:
        min_current = get_entry_value(entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
        max_current = get_entry_value(entry, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)
        priority = get_entry_value(entry, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY)
        
        # Get charger phases from entry or detect
        phases = int(get_entry_value(entry, CONF_PHASES, 3) or 3)
        
        # Create charger context
        charger = ChargerContext(
            charger_id=entry.entry_id,
            entity_id=entry.data.get(CONF_ENTITY_ID, f"charger_{entry.entry_id}"),
            min_current=min_current,
            max_current=max_current,
            phases=phases,
            priority=priority,
        )
        
        # Get OCPP data for this charger
        evse_import = entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
        if evse_import:
            evse_state = sensor.hass.states.get(evse_import)
            if evse_state and evse_state.state not in ['unknown', 'unavailable', None]:
                try:
                    current_value = float(evse_state.state)
                    charger.l1_current = float(evse_state.attributes.get('l1_current', 0) or 0)
                    charger.l2_current = float(evse_state.attributes.get('l2_current', 0) or 0)
                    charger.l3_current = float(evse_state.attributes.get('l3_current', 0) or 0)
                except (ValueError, TypeError):
                    pass
        
        site.chargers.append(charger)
    
    # Calculate targets
    calculate_all_charger_targets(site)
    
    # Calculate total available current for distribution
    # This is the sum of all phase currents after accounting for charger targets
    total_available = 0
    for phase in ['A', 'B', 'C']:
        if hasattr(site, f'phase_{phase.lower()}_export'):
            total_available += getattr(site, f'phase_{phase.lower()}_export')
    
    # Get distribution mode from hub
    distribution_mode_entity = f"select.{hub_entry.data.get(CONF_ENTITY_ID, 'dynamic_ocpp_evse')}_distribution_mode"
    distribution_mode_state = sensor.hass.states.get(distribution_mode_entity)
    if distribution_mode_state and distribution_mode_state.state:
        site.distribution_mode = distribution_mode_state.state
    
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
        "battery_power": state.get('battery_power'),
        "available_battery_power": 0,  # Will be calculated by helper if needed
        "site_available_current_phase_a": round(site.phase_a_export, 1),
        "site_available_current_phase_b": round(site.phase_b_export, 1),
        "site_available_current_phase_c": round(site.phase_c_export, 1),
        "total_site_available_power": round(total_available * voltage, 0) if voltage > 0 else 0,
        
        # Per-charger targets for distribution
        "charger_targets": {c.charger_id: c.target_current for c in site.chargers},
        "distribution_mode": site.distribution_mode,
    }
    
    return result


def calculate_charger_available_current(charger_target, max_current):
    """
    Calculate available current for a single charger.
    
    Args:
        charger_target: Target current from mode calculation
        max_current: Maximum configured current
        
    Returns:
        Available current (A)
    """
    return min(charger_target, max_current)


# Backward compatibility aliases
ChargeContext = ChargerContext
calculate_standard_mode = lambda site: site.charging_mode  # Placeholder for backward compat
calculate_eco_mode = lambda site: site.charging_mode      # Placeholder for backward compat
calculate_solar_mode = lambda site: site.charging_mode    # Placeholder for backward compat
calculate_excess_mode = lambda site: site.charging_mode   # Placeholder for backward compat

# Re-export everything from calculations package
__all__ = [
    "SiteContext",
    "ChargerContext",
    "calculate_all_charger_targets",
    "calculate_available_current_for_hub",
    "calculate_charger_available_current",
    "is_number",
    "determine_phases",
]