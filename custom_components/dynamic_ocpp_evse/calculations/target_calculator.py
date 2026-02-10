"""
Target Calculator - Centralized calculation of charging targets for all chargers.

This module calculates charging targets for all chargers in a single pass,
preventing double-counting of shared resources like solar power and eliminating
race conditions between multiple chargers.
"""

import logging

# Try relative imports first (for HA), fall back to absolute (for tests)
try:
    from .models import SiteContext, ChargerContext
    from ..const import (
        CHARGING_MODE_STANDARD,
        CHARGING_MODE_ECO,
        CHARGING_MODE_SOLAR,
        CHARGING_MODE_EXCESS,
    )
except ImportError:
    from models import SiteContext, ChargerContext
    CHARGING_MODE_STANDARD = "Standard"
    CHARGING_MODE_ECO = "Eco"
    CHARGING_MODE_SOLAR = "Solar"
    CHARGING_MODE_EXCESS = "Excess"

_LOGGER = logging.getLogger(__name__)


def calculate_all_charger_targets(site: SiteContext) -> None:
    """
    Calculate target current for all chargers in a single pass.
    
    This prevents double-counting of shared resources (solar/export power)
    and ensures consistent calculations across all chargers.
    
    Modifies charger.target_current in place for each charger in site.chargers.
    
    Args:
        site: SiteContext containing all site and charger data
    """
    _LOGGER.debug(
        f"Calculating targets for {len(site.chargers)} chargers - "
        f"Export: {site.total_export_current:.1f}A ({site.total_export_power:.0f}W), "
        f"Battery SOC: {site.battery_soc}%"
    )
    
    for charger in site.chargers:
        # Calculate target based on mode
        if charger.charging_mode == CHARGING_MODE_STANDARD:
            # Standard: wants maximum available
            charger.target_current = charger.max_current
            
        elif charger.charging_mode == CHARGING_MODE_ECO:
            # Eco: battery-aware graduated charging
            charger.target_current = _calculate_eco_target(
                site, charger
            )
            
        elif charger.charging_mode == CHARGING_MODE_SOLAR:
            # Solar: only charge from available export
            charger.target_current = _calculate_solar_target(
                site, charger
            )
            
        elif charger.charging_mode == CHARGING_MODE_EXCESS:
            # Excess: threshold-based charging
            charger.target_current = _calculate_excess_target(
                site, charger
            )
            
        else:
            # Unknown mode - default to max
            _LOGGER.warning(f"Unknown charging mode '{charger.charging_mode}' for {charger.entity_id}, using max current")
            charger.target_current = charger.max_current
        
        _LOGGER.debug(f"Charger {charger.entity_id} ({charger.charging_mode}): target={charger.target_current:.1f}A")


def _calculate_eco_target(site: SiteContext, charger: ChargerContext) -> float:
    """
    Calculate Eco mode target for a charger.
    
    Eco mode logic:
    - If no battery OR battery above target: Use max(export, min_current)
    - If battery below min: Use 0 (protect battery)
    - If battery between min and target: Use min_current only
    """
    # No battery configured - use available export or minimum
    if site.battery_soc is None:
        available_per_phase = site.total_export_current / charger.phases if charger.phases > 0 else 0
        return max(charger.min_current, min(available_per_phase, charger.max_current))
    
    # Battery below minimum - don't charge
    if site.battery_soc < site.battery_soc_min:
        return 0
    
    # Battery above target - can use full speed
    if site.battery_soc >= site.battery_soc_target:
        return charger.max_current
    
    # Battery between min and target - use available export or minimum
    available_per_phase = site.total_export_current / charger.phases if charger.phases > 0 else 0
    return max(charger.min_current, min(available_per_phase, charger.max_current))


def _calculate_solar_target(site: SiteContext, charger: ChargerContext) -> float:
    """
    Calculate Solar mode target for a charger.
    
    Solar mode: Only charge from export power, must meet minimum.
    """
    available_per_phase = site.total_export_current / charger.phases if charger.phases > 0 else 0
    
    # Must have at least minimum current available
    if available_per_phase >= charger.min_current:
        return min(available_per_phase, charger.max_current)
    
    # Not enough solar - don't charge
    return 0


def _calculate_excess_target(site: SiteContext, charger: ChargerContext) -> float:
    """
    Calculate Excess mode target for a charger.
    
    Excess mode: Only charge when export exceeds threshold.
    With battery: Adjust threshold based on battery charge needs.
    """
    # Adjust threshold if battery needs charging
    effective_threshold = site.excess_export_threshold
    
    if site.battery_soc is not None and site.battery_soc < site.battery_soc_target:
        # Battery needs charging - increase threshold to give battery priority
        # Add battery max charge power to threshold
        if site.battery_max_charge_power:
            effective_threshold += site.battery_max_charge_power
    
    # Check if export exceeds threshold
    if site.total_export_power > effective_threshold:
        # Calculate available power above threshold
        available_power = site.total_export_power - effective_threshold
        
        # Convert to current per phase
        available_current = available_power / site.voltage / charger.phases if (site.voltage > 0 and charger.phases > 0) else 0
        
        # Must meet minimum
        if available_current >= charger.min_current:
            return min(available_current, charger.max_current)
    
    # Below threshold or not enough excess
    return 0
