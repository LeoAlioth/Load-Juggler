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
    0. Filter active chargers (with cars connected)
    1. Calculate absolute site limits (per-phase physical constraints)
    2. Calculate solar available power
    3. Calculate excess available power
    4. Determine target power based on charging mode
    5. Distribute power among chargers
    
    Args:
        site: SiteContext containing all site and charger data
    """
    # Step 0: Filter out chargers with no car connected
    active_chargers = [c for c in site.chargers 
                       if c.connector_status not in ["Available", "Unknown", "Unavailable"]]
    
    if len(active_chargers) == 0:
        _LOGGER.debug("No active chargers (all Available/Unknown/Unavailable)")
        # Set all chargers to 0
        for charger in site.chargers:
            charger.target_current = 0
        return
    
    # Temporarily replace site.chargers with active ones for calculation
    all_chargers = site.chargers
    site.chargers = active_chargers
    
    _LOGGER.debug(
        f"Calculating targets for {len(active_chargers)}/{len(all_chargers)} active chargers - "
        f"Mode: {site.charging_mode}, Distribution: {site.distribution_mode}"
    )
    
    # Step 1: Calculate absolute site limits (physical constraints)
    site_limit = _calculate_site_limit(site)
    _LOGGER.debug(f"Step 1 - Site limit: {site_limit:.1f}A")
    
    # Step 2: Calculate solar available (returns per-phase and total)
    solar_per_phase, solar_total = _calculate_solar_available(site)
    _LOGGER.debug(f"Step 2 - Solar available: per_phase={solar_per_phase}, total={solar_total:.1f}A")
    
    # Step 3: Calculate excess available
    excess_available = _calculate_excess_available(site)
    _LOGGER.debug(f"Step 3 - Excess available: {excess_available:.1f}A")
    
    # Step 4: Determine target power based on mode
    # For symmetric 3-phase systems, use minimum phase (per-phase semantics)
    # For asymmetric systems, use total (can balance across phases)
    if site.num_phases == 3 and not site.inverter_supports_asymmetric:
        # Symmetric: use limiting phase
        solar_for_mode = min(solar_per_phase)
    else:
        # Asymmetric or 1-phase: use total
        solar_for_mode = solar_total
    
    target_power = _determine_target_power(site, site_limit, solar_for_mode, excess_available)
    _LOGGER.debug(f"Step 4 - Target power ({site.charging_mode}): {target_power:.1f}A")
    
    # Step 5: Distribute power among chargers
    # Pass solar per-phase data for per-phase distribution
    _distribute_power(site, target_power, solar_per_phase, solar_total)
    
    for charger in site.chargers:
        _LOGGER.debug(f"Final - {charger.entity_id}: {charger.target_current:.1f}A")
    
    # Restore original chargers list and set inactive ones to 0
    site.chargers = all_chargers
    for charger in all_chargers:
        if charger not in active_chargers:
            charger.target_current = 0


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


def _calculate_solar_available(site: SiteContext) -> tuple:
    """
    Step 2: Calculate solar available power.
    
    Considers:
    - Total solar export (per-phase for 3-phase systems)
    - Battery charging (if SOC < target)
    - Battery discharge (if SOC > target, mode-dependent)
    - Inverter limits (both per-phase AND total)
    
    Returns:
        Tuple of (per_phase_limits, total_limit):
        - per_phase_limits: [phase_a, phase_b, phase_c] available current per phase
        - total_limit: Total available current across all phases
        
        For symmetric inverters: total = sum(per_phase)
        For asymmetric inverters: total may be less than sum(per_phase)
    """
    # Calculate available per phase for 3-phase systems
    if site.num_phases == 3:
        # Solar evenly distributed: each phase gets 1/3
        solar_per_phase = (site.solar_production_total / 3) / site.voltage if site.solar_production_total else 0
        
        # Calculate export per phase after consumption
        phase_a_avail = max(0, solar_per_phase - site.phase_a_consumption)
        phase_b_avail = max(0, solar_per_phase - site.phase_b_consumption)
        phase_c_avail = max(0, solar_per_phase - site.phase_c_consumption)
        
        # Handle battery charging/discharging
        if site.battery_soc is not None:
            if site.battery_soc < site.battery_soc_target:
                # Battery charges first - reduce available proportionally
                battery_charge_current = site.battery_max_charge_power / site.voltage if site.battery_max_charge_power else 0
                battery_per_phase = battery_charge_current / 3
                phase_a_avail = max(0, phase_a_avail - battery_per_phase)
                phase_b_avail = max(0, phase_b_avail - battery_per_phase)
                phase_c_avail = max(0, phase_c_avail - battery_per_phase)
            
            elif site.battery_soc > site.battery_soc_target:
                # Battery can discharge - add to each phase
                if site.battery_max_discharge_power:
                    battery_discharge_current = site.battery_max_discharge_power / site.voltage
                    battery_per_phase = battery_discharge_current / 3
                    phase_a_avail += battery_per_phase
                    phase_b_avail += battery_per_phase
                    phase_c_avail += battery_per_phase
        
        # Apply per-phase inverter limits if configured
        if site.inverter_max_power_per_phase:
            max_per_phase = site.inverter_max_power_per_phase / site.voltage
            phase_a_avail = min(phase_a_avail, max_per_phase)
            phase_b_avail = min(phase_b_avail, max_per_phase)
            phase_c_avail = min(phase_c_avail, max_per_phase)
        
        # Calculate total available (sum of phases before inverter total limit)
        total_available = phase_a_avail + phase_b_avail + phase_c_avail
        
        # Apply total inverter limit if configured
        if site.inverter_max_power:
            max_total = site.inverter_max_power / site.voltage
            total_available = min(total_available, max_total)
        
        # Return both per-phase and total
        per_phase = [phase_a_avail, phase_b_avail, phase_c_avail]
        
        _LOGGER.debug(
            f"3-phase solar: per-phase=[{phase_a_avail:.1f}, {phase_b_avail:.1f}, {phase_c_avail:.1f}], "
            f"total={total_available:.1f}A, asymmetric={site.inverter_supports_asymmetric}"
        )
        
        return (per_phase, total_available)
    
    # Single-phase system
    available = site.total_export_current
    
    # No battery - just return export
    if site.battery_soc is None:
        return ([available, 0, 0], available)
    
    # Battery below target: Battery charges first
    if site.battery_soc < site.battery_soc_target:
        battery_charge_current = site.battery_max_charge_power / site.voltage if site.battery_max_charge_power else 0
        available = max(0, available - battery_charge_current)
    
    # Battery above target: Can discharge
    elif site.battery_soc > site.battery_soc_target:
        if site.battery_max_discharge_power:
            battery_discharge_current = site.battery_max_discharge_power / site.voltage
            available += battery_discharge_current
    
    return ([available, 0, 0], available)


def _calculate_excess_available(site: SiteContext) -> float:
    """
    Step 3: Calculate excess available power.
    
    Excess mode only charges when export exceeds threshold.
    
    Returns:
        Current (A) available above excess threshold
        - With asymmetric inverter: TOTAL current
        - Without asymmetric: per-phase for 3ph, total for 1ph
    """
    # Check if export exceeds threshold
    if site.total_export_power > site.excess_export_threshold:
        available_power = site.total_export_power - site.excess_export_threshold
        available_current = available_power / site.voltage if site.voltage > 0 else 0
        
        # Match solar_available semantics
        if site.num_phases == 3 and not site.inverter_supports_asymmetric:
            # Symmetric 3-phase: return per-phase
            return available_current / 3
        
        # Asymmetric or 1-phase: return total
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
        # Standard: Use site limit + solar + battery
        # In Standard mode, everything available can be used
        target = site_limit
        
        # Add solar production (if any)
        if site.solar_production_total:
            solar_current = site.solar_production_total / site.voltage
            target += solar_current
        
        # Add battery discharge if SOC > min
        if site.battery_soc is not None and site.battery_soc > site.battery_soc_min:
            if site.battery_max_discharge_power:
                battery_discharge = site.battery_max_discharge_power / site.voltage
                target += battery_discharge
        
        # For 3-phase systems without asymmetric: divide by 3 (per-phase)
        # With asymmetric: return total (inverter can balance)
        if site.num_phases == 3 and not site.inverter_supports_asymmetric:
            return target / 3
        
        return target
    
    elif mode == CHARGING_MODE_ECO:
        # Eco: Charge at minimum to protect battery, can use more if battery is healthy
        # - Battery < min: No charging (protect battery)
        # - Battery between min and target: Charge at minimum only (gentle on battery)
        # - Battery >= target: Can use all solar available
        
        # Calculate sum of minimum charge rates
        # Must match solar_available semantics (total vs per-phase)
        if site.inverter_supports_asymmetric:
            # Asymmetric: solar_available is TOTAL, so use total minimums
            sum_minimums = sum(c.min_current * c.phases for c in site.chargers)
        elif site.num_phases == 3 and all(c.phases == 3 for c in site.chargers):
            # Symmetric 3-phase: solar_available is per-phase, so use per-phase minimums
            sum_minimums = sum(c.min_current for c in site.chargers)
        else:
            # 1-phase or mixed: use total
            sum_minimums = sum(c.min_current * c.phases for c in site.chargers)
        
        # Battery below minimum - protect battery (no charging)
        if site.battery_soc is not None and site.battery_soc < site.battery_soc_min:
            return 0
        
        # Battery between min and target - charge at minimum only (protect battery)
        if site.battery_soc is not None and site.battery_soc < site.battery_soc_target:
            target = sum_minimums
        else:
            # Battery >= target or no battery - can use all solar available
            target = max(solar_available, sum_minimums)
        
        # Cap at site limit
        return min(target, site_limit)
    
    elif mode == CHARGING_MODE_SOLAR:
        # Solar: Use only solar_available (includes battery discharge if SOC > target)
        # Battery priority: If battery below target, all solar goes to battery (EV gets 0)
        if site.battery_soc is not None and site.battery_soc < site.battery_soc_target:
            return 0
        return solar_available
    
    elif mode == CHARGING_MODE_EXCESS:
        # Excess: Use only excess_available
        return excess_available
    
    else:
        _LOGGER.warning(f"Unknown charging mode '{mode}', using site limit")
        return site_limit


def _get_phase_available_current(site: SiteContext) -> dict:
    """
    Helper: Get available current per phase for single-phase chargers.
    
    Returns:
        dict with keys 'A', 'B', 'C' containing available current per phase
    """
    if site.num_phases == 1:
        return {'A': site.phase_a_export, 'B': 0, 'C': 0}
    
    # For 3-phase, calculate available per phase
    return {
        'A': site.phase_a_export,
        'B': site.phase_b_export,
        'C': site.phase_c_export,
    }


def _distribute_power(site: SiteContext, target_power: float, solar_per_phase: list, solar_total: float) -> None:
    """
    Step 5: Distribute target power among chargers.
    
    Uses per-phase distribution for all cases.
    For chargers with explicit phase assignments, uses actual per-phase availability.
    For others, converts target_power to uniform per-phase representation.
    
    Args:
        site: SiteContext with chargers
        target_power: Current (A) to distribute (mode-calculated in Step 4)
        solar_per_phase: Per-phase available current [A, B, C] from Step 2
        solar_total: Total available current from Step 2
    """
    if len(site.chargers) == 0:
        return
    
    # Determine per-phase availability based on inverter type and mode
    # No distinctions between "explicit" and "default" - treat all the same
    if site.inverter_supports_asymmetric or site.num_phases == 1:
        # ASYMMETRIC or 1-PHASE: target_power is TOTAL, chargers can draw from full pool
        per_phase_available = [target_power, target_power, target_power]
        total_available = target_power
        _LOGGER.debug(f"Asymmetric/1ph: Using uniform pool {target_power}A per phase")
    elif site.charging_mode == "Solar":
        # SYMMETRIC 3-PHASE in Solar mode: Use actual per-phase solar values
        # This respects actual phase consumption differences
        per_phase_available = solar_per_phase
        total_available = solar_total
        _LOGGER.debug(f"Symmetric Solar: Using actual solar_per_phase {solar_per_phase}")
    else:
        # SYMMETRIC 3-PHASE in other modes: target_power is PER-PHASE (uniform)
        per_phase_available = [target_power, target_power, target_power]
        total_available = target_power * 3
        _LOGGER.debug(f"Symmetric non-Solar: Using uniform {target_power}A per phase")
    
    # Log what each charger is drawing from
    for charger in site.chargers:
        _LOGGER.debug(f"Charger {charger.entity_id}: mask={charger.active_phases_mask}")
    
    # Use per-phase distribution - it handles all cases uniformly
    _distribute_power_per_phase(site, per_phase_available, total_available, site.distribution_mode)


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
    
    ASYMMETRIC inverter: total_available is TOTAL current, chargers use (current × phases)
    SYMMETRIC (no asymmetric): total_available is per-phase for 3ph/all-3ph-chargers, else total
    """
    chargers = sorted(site.chargers, key=lambda c: c.priority)
    total_available = site.total_export_current
    
    # Determine distribution mode based on inverter capability
    if site.inverter_supports_asymmetric:
        # ASYMMETRIC: Work with total current pool (chargers use current × phases)
        is_per_phase = False
    else:
        # SYMMETRIC: For 3-phase systems, always work per-phase
        # (solar_available is per-phase for symmetric 3ph systems)
        is_per_phase = (site.num_phases == 3)
    
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
        if is_per_phase:
            # For per-phase: just need min_current per phase
            min_needed = charger.min_current
        else:
            # For total: need min_current * phases
            min_needed = charger.min_current * charger.phases
        
        if remaining >= min_needed:
            if is_per_phase:
                allocated[charger.entity_id] = min_needed
                remaining -= min_needed
            else:
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
        
        if is_per_phase:
            wanted_additional = charger.max_current - allocated[charger.entity_id]
            additional = min(wanted_additional, remaining)
            charger.target_current = allocated[charger.entity_id] + additional
            remaining -= additional
        else:
            wanted_additional = (charger.max_current - allocated[charger.entity_id]) * charger.phases
            additional_total = min(wanted_additional, remaining)
            additional_per_phase = additional_total / charger.phases
            charger.target_current = allocated[charger.entity_id] + additional_per_phase
            remaining -= additional_total


def _distribute_power_per_phase(site: SiteContext, solar_per_phase: list, solar_total: float, distribution_mode: str) -> None:
    """
    Universal per-phase distribution - handles ALL cases and ALL distribution modes.
    
    For ASYMMETRIC inverters: chargers can access total power pool
    For SYMMETRIC inverters: chargers limited to their specific phases
    
    Args:
        site: SiteContext with chargers
        solar_per_phase: Available current per phase [A, B, C] from mode calculation
        solar_total: Total available current from mode calculation
        distribution_mode: "priority", "shared", "strict", or "optimized"
    """
    mode = distribution_mode.lower() if distribution_mode else "priority"
    
    if mode == "priority":
        _distribute_per_phase_priority(site, solar_per_phase, solar_total)
    elif mode == "shared":
        _distribute_per_phase_shared(site, solar_per_phase, solar_total)
    elif mode == "strict":
        _distribute_per_phase_strict(site, solar_per_phase, solar_total)
    elif mode == "optimized":
        _distribute_per_phase_optimized(site, solar_per_phase, solar_total)
    else:
        _LOGGER.warning(f"Unknown distribution mode '{mode}', using priority")
        _distribute_per_phase_priority(site, solar_per_phase, solar_total)


def _distribute_per_phase_priority(site: SiteContext, solar_per_phase: list, solar_total: float) -> None:
    """
    PRIORITY mode in per-phase framework.
    Pass 1: Allocate minimum by priority
    Pass 2: Give remainder to highest priority first
    """
    # For asymmetric inverters, single-phase chargers can use total power pool
    # For symmetric, they're limited to their phase
    if site.inverter_supports_asymmetric:
        # Asymmetric: single-phase chargers draw from total pool
        phase_available = {
            'A': solar_total,  # Can access full pool
            'B': solar_total,
            'C': solar_total,
        }
    else:
        # Symmetric: use per-phase limits
        phase_available = {
            'A': solar_per_phase[0],
            'B': solar_per_phase[1],
            'C': solar_per_phase[2],
        }
    
    # Sort all chargers by priority (process across all phase groups)
    chargers_by_priority = sorted(site.chargers, key=lambda c: c.priority)
    
    # Track remaining capacity per phase as we allocate
    remaining_per_phase = phase_available.copy()
    
    # Two-pass allocation by priority
    allocated = {}
    
    # Pass 1: Allocate minimum to all chargers (by priority)
    for charger in chargers_by_priority:
        mask = charger.active_phases_mask
        # All chargers should have a mask set (via __post_init__)
        if not mask:
            _LOGGER.error(f"Charger {charger.entity_id} has no phase mask! This should not happen.")
            allocated[charger.entity_id] = 0
            continue
        
        # Determine limiting phase for this charger
        if mask == 'ABC':
            charger_available = min(remaining_per_phase['A'], remaining_per_phase['B'], remaining_per_phase['C'])
        elif len(mask) == 2:
            phases_used = list(mask)
            charger_available = min(remaining_per_phase[phases_used[0]], remaining_per_phase[phases_used[1]])
        elif len(mask) == 1:
            charger_available = remaining_per_phase[mask]
        else:
            _LOGGER.warning(f"Unknown phase mask '{mask}', skipping")
            allocated[charger.entity_id] = 0
            continue
        
        # Try to allocate minimum
        if charger_available >= charger.min_current:
            allocated[charger.entity_id] = charger.min_current
            # Deduct from all phases this charger uses
            for phase in mask:
                remaining_per_phase[phase] -= charger.min_current
        else:
            allocated[charger.entity_id] = 0
    
    # Pass 2: Allocate remainder by priority
    for charger in chargers_by_priority:
        if allocated.get(charger.entity_id, 0) == 0:
            charger.target_current = 0
            continue
        
        mask = charger.active_phases_mask
        if not mask:
            # 3-phase charger without explicit mask
            if charger.phases == 3:
                mask = 'ABC'
            else:
                charger.target_current = allocated[charger.entity_id]
                continue
        
        # Determine how much more this charger can get
        if mask == 'ABC':
            charger_available = min(remaining_per_phase['A'], remaining_per_phase['B'], remaining_per_phase['C'])
        elif len(mask) == 2:
            phases_used = list(mask)
            charger_available = min(remaining_per_phase[phases_used[0]], remaining_per_phase[phases_used[1]])
        elif len(mask) == 1:
            charger_available = remaining_per_phase[mask]
        else:
            charger.target_current = allocated[charger.entity_id]
            continue
        
        wanted_additional = charger.max_current - allocated[charger.entity_id]
        additional = min(wanted_additional, charger_available)
        charger.target_current = allocated[charger.entity_id] + additional
        
        # Deduct from all phases this charger uses
        for phase in mask:
            remaining_per_phase[phase] -= additional


def _distribute_per_phase_shared(site: SiteContext, solar_per_phase: list, solar_total: float) -> None:
    """
    SHARED mode in per-phase framework.
    Pass 1: Allocate minimum to all
    Pass 2: Split remainder equally among all charging chargers
    """
    # Set up phase available dict
    if site.inverter_supports_asymmetric:
        phase_available = {'A': solar_total, 'B': solar_total, 'C': solar_total}
    else:
        phase_available = {'A': solar_per_phase[0], 'B': solar_per_phase[1], 'C': solar_per_phase[2]}
    
    remaining_per_phase = phase_available.copy()
    allocated = {}
    
    # Pass 1: Allocate minimum to all
    for charger in site.chargers:
        mask = charger.active_phases_mask
        if not mask:
            allocated[charger.entity_id] = 0
            continue
        
        # Determine limiting phase
        if mask == 'ABC':
            charger_available = min(remaining_per_phase['A'], remaining_per_phase['B'], remaining_per_phase['C'])
        elif len(mask) == 2:
            phases_used = list(mask)
            charger_available = min(remaining_per_phase[phases_used[0]], remaining_per_phase[phases_used[1]])
        else:
            charger_available = remaining_per_phase[mask]
        
        # Allocate minimum
        if charger_available >= charger.min_current:
            allocated[charger.entity_id] = charger.min_current
            for phase in mask:
                remaining_per_phase[phase] -= charger.min_current
        else:
            allocated[charger.entity_id] = 0
    
    charging_chargers = [c for c in site.chargers if allocated.get(c.entity_id, 0) > 0]
    if not charging_chargers:
        for charger in site.chargers:
            charger.target_current = 0
        return
    
    # Pass 2: Split remainder equally
    # Calculate total remaining (minimum across all phases)
    total_remaining = min(remaining_per_phase['A'], remaining_per_phase['B'], remaining_per_phase['C'])
    
    if total_remaining > 0:
        per_charger = total_remaining / len(charging_chargers)
        for charger in charging_chargers:
            additional = min(per_charger, charger.max_current - allocated[charger.entity_id])
            charger.target_current = allocated[charger.entity_id] + additional
    else:
        for charger in charging_chargers:
            charger.target_current = allocated[charger.entity_id]
    
    # Set non-charging chargers to 0
    for charger in site.chargers:
        if charger not in charging_chargers:
            charger.target_current = 0


def _distribute_per_phase_strict(site: SiteContext, solar_per_phase: list, solar_total: float) -> None:
    """
    STRICT mode in per-phase framework.
    Give first charger up to max, then next, etc. (by priority)
    """
    if site.inverter_supports_asymmetric:
        phase_available = {'A': solar_total, 'B': solar_total, 'C': solar_total}
    else:
        phase_available = {'A': solar_per_phase[0], 'B': solar_per_phase[1], 'C': solar_per_phase[2]}
    
    remaining_per_phase = phase_available.copy()
    chargers_by_priority = sorted(site.chargers, key=lambda c: c.priority)
    
    for charger in chargers_by_priority:
        mask = charger.active_phases_mask
        if not mask:
            charger.target_current = 0
            continue
        
        # Determine limiting phase
        if mask == 'ABC':
            charger_available = min(remaining_per_phase['A'], remaining_per_phase['B'], remaining_per_phase['C'])
        elif len(mask) == 2:
            phases_used = list(mask)
            charger_available = min(remaining_per_phase[phases_used[0]], remaining_per_phase[phases_used[1]])
        else:
            charger_available = remaining_per_phase[mask]
        
        # Give up to max or what's available
        charger.target_current = min(charger.max_current, charger_available)
        
        # Deduct from all phases
        for phase in mask:
            remaining_per_phase[phase] -= charger.target_current


def _distribute_per_phase_optimized(site: SiteContext, solar_per_phase: list, solar_total: float) -> None:
    """
    OPTIMIZED mode in per-phase framework.
    Reduce higher priority chargers to allow lower priority to charge at minimum.
    """
    if site.inverter_supports_asymmetric:
        phase_available = {'A': solar_total, 'B': solar_total, 'C': solar_total}
    else:
        phase_available = {'A': solar_per_phase[0], 'B': solar_per_phase[1], 'C': solar_per_phase[2]}
    
    remaining_per_phase = phase_available.copy()
    chargers_by_priority = sorted(site.chargers, key=lambda c: c.priority)
    
    for i, charger in enumerate(chargers_by_priority):
        mask = charger.active_phases_mask
        if not mask:
            charger.target_current = 0
            continue
        
        # Determine limiting phase
        if mask == 'ABC':
            charger_available = min(remaining_per_phase['A'], remaining_per_phase['B'], remaining_per_phase['C'])
        elif len(mask) == 2:
            phases_used = list(mask)
            charger_available = min(remaining_per_phase[phases_used[0]], remaining_per_phase[phases_used[1]])
        else:
            charger_available = remaining_per_phase[mask]
        
        wanted = min(charger.max_current, charger_available)
        
        # Check if we should reduce to help next charger
        if i < len(chargers_by_priority) - 1:
            next_charger = chargers_by_priority[i + 1]
            next_mask = next_charger.active_phases_mask
            if next_mask:
                # Calculate what next charger would see
                temp_remaining = remaining_per_phase.copy()
                for phase in mask:
                    temp_remaining[phase] -= wanted
                
                if next_mask == 'ABC':
                    next_available = min(temp_remaining['A'], temp_remaining['B'], temp_remaining['C'])
                elif len(next_mask) == 2:
                    next_phases = list(next_mask)
                    next_available = min(temp_remaining[next_phases[0]], temp_remaining[next_phases[1]])
                else:
                    next_available = temp_remaining[next_mask]
                
                # If next can't charge at minimum, try to reduce current charger
                if next_available < next_charger.min_current:
                    reduction_needed = next_charger.min_current - next_available
                    can_reduce = max(0, wanted - charger.min_current)
                    reduction = min(reduction_needed, can_reduce)
                    wanted -= reduction
        
        charger.target_current = wanted
        
        # Deduct from all phases
        for phase in mask:
            remaining_per_phase[phase] -= wanted


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
