"""
Target Calculator - Centralized calculation of charging targets for all chargers.

Clear architecture:
0. Refresh SiteContext (done externally)
1. Calculate absolute site limits (per-phase, prevents breaker trips)
2. Calculate solar available
3. Calculate excess available
4. Determine target power based on charging mode
5. Distribute power among chargers
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
    Calculate target current for all chargers using clear step-by-step approach.
    
    Steps:
    1. Calculate absolute site limits (per-phase physical constraints)
    2. Calculate solar available power
    3. Calculate excess available power
    4. Determine target power based on charging mode
    5. Distribute power among chargers
    
    Args:
        site: SiteContext containing all site and charger data
    """
    _LOGGER.debug(
        f"Calculating targets for {len(site.chargers)} chargers - "
        f"Mode: {site.charging_mode}, Distribution: {site.distribution_mode}"
    )
    
    # Step 1: Calculate absolute site limits (physical constraints)
    site_limit = _calculate_site_limit(site)
    _LOGGER.debug(f"Step 1 - Site limit: {site_limit:.1f}A")
    
    # Step 2: Calculate solar available
    solar_available = _calculate_solar_available(site)
    _LOGGER.debug(f"Step 2 - Solar available: {solar_available:.1f}A")
    
    # Step 3: Calculate excess available
    excess_available = _calculate_excess_available(site)
    _LOGGER.debug(f"Step 3 - Excess available: {excess_available:.1f}A")
    
    # Step 4: Determine target power based on mode
    target_power = _determine_target_power(site, site_limit, solar_available, excess_available)
    _LOGGER.debug(f"Step 4 - Target power ({site.charging_mode}): {target_power:.1f}A")
    
    # Step 5: Distribute power among chargers
    _distribute_power(site, target_power)
    
    for charger in site.chargers:
        _LOGGER.debug(f"Final - {charger.entity_id}: {charger.target_current:.1f}A")


def _calculate_site_limit(site: SiteContext) -> float:
    """
    Step 1: Calculate absolute site power limit (prevents breaker trips).
    
    Returns total available current considering:
    - Main breaker rating
    - Current consumption
    - Grid charging permission (with battery)
    
    Returns:
        Total current (A) available without tripping breakers
    """
    # Single-phase calculation
    if site.num_phases == 1:
        # Available = breaker rating - current consumption
        available = site.main_breaker_rating - site.phase_a_consumption
        
        # If grid charging not allowed (and has battery), can only use export
        if not site.allow_grid_charging and site.battery_soc is not None:
            available = site.total_export_current
        
        return max(0, available)
    
    # Three-phase: Calculate per-phase, use minimum
    phase_a_limit = site.main_breaker_rating - site.phase_a_consumption
    phase_b_limit = site.main_breaker_rating - site.phase_b_consumption
    phase_c_limit = site.main_breaker_rating - site.phase_c_consumption
    
    # If grid charging not allowed (and has battery), limited to export only
    if not site.allow_grid_charging and site.battery_soc is not None:
        phase_a_limit = min(phase_a_limit, site.phase_a_export)
        phase_b_limit = min(phase_b_limit, site.phase_b_export)
        phase_c_limit = min(phase_c_limit, site.phase_c_export)
    
    # For now, return total (will handle per-phase in distribution)
    # TODO: Track per-phase limits through all steps
    total_limit = max(0, phase_a_limit + phase_b_limit + phase_c_limit)
    
    return total_limit


def _calculate_solar_available(site: SiteContext) -> float:
    """
    Step 2: Calculate solar available power.
    
    Considers:
    - Total solar export
    - Battery charging (if SOC < target)
    - Battery discharge (if SOC > target, mode-dependent)
    
    Note: Solar power is equally distributed across phases.
    Battery power can be freely distributed.
    
    Returns:
        Total current (A) available from solar (and battery if applicable)
    """
    # Start with total export
    available = site.total_export_current
    
    # No battery - just return export
    if site.battery_soc is None:
        return available
    
    # Battery below target: Battery charges first
    if site.battery_soc < site.battery_soc_target:
        battery_charge_current = site.battery_max_charge_power / site.voltage if site.battery_max_charge_power else 0
        available = max(0, available - battery_charge_current)
    
    # Battery above target: Can discharge (for Solar/Eco modes)
    # Note: Standard mode battery discharge is handled separately
    elif site.battery_soc > site.battery_soc_target:
        if site.battery_max_discharge_power:
            battery_discharge_current = site.battery_max_discharge_power / site.voltage
            available += battery_discharge_current
    
    return available


def _calculate_excess_available(site: SiteContext) -> float:
    """
    Step 3: Calculate excess available power.
    
    Excess mode only charges when export exceeds threshold.
    
    Returns:
        Current (A) available above excess threshold
    """
    # Check if export exceeds threshold
    if site.total_export_power > site.excess_export_threshold:
        available_power = site.total_export_power - site.excess_export_threshold
        available_current = available_power / site.voltage if site.voltage > 0 else 0
        return available_current
    
    return 0


def _determine_target_power(site: SiteContext, site_limit: float, solar_available: float, excess_available: float) -> float:
    """
    Step 4: Determine target power based on charging mode.
    
    Args:
        site: SiteContext
        site_limit: From Step 1 (absolute physical limit)
        solar_available: From Step 2
        excess_available: From Step 3
    
    Returns:
        Target current (A) to distribute among chargers
    """
    mode = site.charging_mode
    
    if mode == CHARGING_MODE_STANDARD:
        # Standard: Use site limit (essentially unlimited up to breaker)
        # Standard mode can use battery discharge if SOC > min
        target = site_limit
        
        if site.battery_soc is not None and site.battery_soc > site.battery_soc_min:
            if site.battery_max_discharge_power:
                battery_discharge = site.battery_max_discharge_power / site.voltage
                target += battery_discharge
        
        return target
    
    elif mode == CHARGING_MODE_ECO:
        # Eco: Use max of (solar_available, sum_of_minimums)
        # But respect battery protection
        
        # Calculate sum of minimum charge rates
        sum_minimums = sum(c.min_current * c.phases for c in site.chargers)
        
        # Battery below minimum - protect battery
        if site.battery_soc is not None and site.battery_soc < site.battery_soc_min:
            return 0
        
        # Battery above target - can use discharge (already in solar_available)
        # Use the larger of solar or sum of minimums
        target = max(solar_available, sum_minimums)
        
        # Cap at site limit
        return min(target, site_limit)
    
    elif mode == CHARGING_MODE_SOLAR:
        # Solar: Use only solar_available (includes battery discharge if SOC > target)
        return solar_available
    
    elif mode == CHARGING_MODE_EXCESS:
        # Excess: Use only excess_available
        return excess_available
    
    else:
        _LOGGER.warning(f"Unknown charging mode '{mode}', using site limit")
        return site_limit


def _distribute_power(site: SiteContext, target_power: float) -> None:
    """
    Step 5: Distribute target power among chargers.
    
    Uses distribution_mode to allocate power:
    - priority: Two-pass (min first, then by priority)
    - shared: Two-pass (min first, then equal split)
    - strict: One-pass sequential
    - optimized: One-pass with smart reduction
    
    Args:
        site: SiteContext with chargers
        target_power: Total current (A) to distribute
    """
    if len(site.chargers) == 0:
        return
    
    # Single charger - simple case
    if len(site.chargers) == 1:
        charger = site.chargers[0]
        available_per_phase = target_power / charger.phases if charger.phases > 0 else 0
        
        if available_per_phase >= charger.min_current:
            charger.target_current = min(available_per_phase, charger.max_current)
        else:
            charger.target_current = 0
        
        return
    
    # Multiple chargers - use distribution mode
    # Update site.total_export_current temporarily for distribution functions
    original_export = site.total_export_current
    site.total_export_current = target_power
    
    mode = site.distribution_mode.lower() if site.distribution_mode else "priority"
    
    if mode == "shared":
        _distribute_shared(site)
    elif mode == "priority":
        _distribute_priority(site)
    elif mode == "strict":
        _distribute_strict(site)
    elif mode == "optimized":
        _distribute_optimized(site)
    else:
        _LOGGER.warning(f"Unknown distribution mode '{mode}', using priority")
        _distribute_priority(site)
    
    # Restore original value
    site.total_export_current = original_export


def _distribute_shared(site: SiteContext) -> None:
    """
    SHARED mode: Two-pass approach.
    Pass 1: Get everyone to min_current
    Pass 2: Split remainder equally among all charging chargers
    """
    chargers = sorted(site.chargers, key=lambda c: c.priority)
    total_available = site.total_export_current
    
    # Initialize all chargers wanting to charge
    for charger in chargers:
        charger.target_current = charger.max_current
    
    charging_chargers = [c for c in chargers if c.target_current > 0]
    if not charging_chargers:
        return
    
    # Pass 1: Allocate min_current to all
    allocated = {}
    remaining = total_available
    
    for charger in charging_chargers:
        min_needed = charger.min_current * charger.phases
        if remaining >= min_needed:
            allocated[charger.entity_id] = min_needed / charger.phases
            remaining -= min_needed
        else:
            allocated[charger.entity_id] = 0
            charger.target_current = 0
    
    charging_chargers = [c for c in charging_chargers if allocated.get(c.entity_id, 0) > 0]
    if not charging_chargers:
        return
    
    # Pass 2: Split remainder equally
    if remaining > 0:
        per_charger_total = remaining / len(charging_chargers)
        for charger in charging_chargers:
            per_phase = per_charger_total / charger.phases
            additional = min(per_phase, charger.max_current - allocated[charger.entity_id])
            charger.target_current = allocated[charger.entity_id] + additional


def _distribute_priority(site: SiteContext) -> None:
    """
    PRIORITY mode: Two-pass approach.
    Pass 1: Get everyone to min_current (by priority)
    Pass 2: Give remainder to highest priority first
    """
    chargers = sorted(site.chargers, key=lambda c: c.priority)
    total_available = site.total_export_current
    
    # Initialize all wanting to charge
    for charger in chargers:
        charger.target_current = charger.max_current
    
    charging_chargers = [c for c in chargers if c.target_current > 0]
    if not charging_chargers:
        return
    
    # Pass 1: Allocate min_current
    allocated = {}
    remaining = total_available
    
    for charger in charging_chargers:
        min_needed = charger.min_current * charger.phases
        if remaining >= min_needed:
            allocated[charger.entity_id] = min_needed / charger.phases
            remaining -= min_needed
        else:
            allocated[charger.entity_id] = 0
            charger.target_current = 0
    
    charging_chargers = [c for c in charging_chargers if allocated.get(c.entity_id, 0) > 0]
    if not charging_chargers:
        return
    
    # Pass 2: Give remainder by priority
    for charger in charging_chargers:
        if remaining <= 0:
            charger.target_current = allocated[charger.entity_id]
            continue
        
        wanted_additional = (charger.max_current - allocated[charger.entity_id]) * charger.phases
        additional_total = min(wanted_additional, remaining)
        additional_per_phase = additional_total / charger.phases
        
        charger.target_current = allocated[charger.entity_id] + additional_per_phase
        remaining -= additional_total


def _distribute_strict(site: SiteContext) -> None:
    """
    STRICT mode: One-pass sequential.
    Give first charger up to max available, then next, etc.
    """
    chargers = sorted(site.chargers, key=lambda c: c.priority)
    remaining = site.total_export_current
    
    for charger in chargers:
        charger.target_current = charger.max_current
        
        needed = charger.max_current * charger.phases
        if remaining >= needed:
            # Give full amount
            remaining -= needed
        elif remaining > 0:
            # Give what's left
            charger.target_current = remaining / charger.phases
            remaining = 0
        else:
            # No power left
            charger.target_current = 0


def _distribute_optimized(site: SiteContext) -> None:
    """
    OPTIMIZED mode: One-pass smart allocation.
    Reduce higher priority chargers to allow lower priority to charge at minimum.
    """
    chargers = sorted(site.chargers, key=lambda c: c.priority)
    total_available = site.total_export_current
    remaining = total_available
    
    # Initialize all wanting max
    for charger in chargers:
        charger.target_current = charger.max_current
    
    allocated = []
    
    for i, charger in enumerate(chargers):
        wanted_total = charger.max_current * charger.phases
        wanted = min(wanted_total, remaining)
        
        # Check if there are more chargers after this one
        if i < len(chargers) - 1:
            next_charger = chargers[i + 1]
            leftover_after_wanted = remaining - wanted
            next_min_needed = next_charger.min_current * next_charger.phases
            
            # If leftover < next charger's minimum
            if leftover_after_wanted < next_min_needed:
                # Reduce current charger but keep it >= min
                charger_min_needed = charger.min_current * charger.phases
                can_reduce = max(0, wanted - charger_min_needed)
                reduction_needed = next_min_needed - leftover_after_wanted
                reduction = min(reduction_needed, can_reduce)
                
                wanted = wanted - reduction
        
        allocated.append(wanted / charger.phases)
        remaining -= wanted
    
    # Apply allocated values
    for charger, alloc in zip(chargers, allocated):
        charger.target_current = alloc
