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

from .models import SiteContext, ChargerContext
from .utils import get_available_current, deduct_current
from ..const import (
    CHARGING_MODE_STANDARD,
    CHARGING_MODE_ECO,
    CHARGING_MODE_SOLAR,
    CHARGING_MODE_EXCESS,
)

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
    
    # Step 1: Calculate absolute site limits (physical constraints) - returns constraint dict
    site_limit_constraints = _calculate_site_limit(site)
    _LOGGER.debug(f"Step 1 - Site limit constraints: {site_limit_constraints}")
    
    # Step 2: Calculate solar available - returns constraint dict
    solar_constraints = _calculate_solar_available(site)
    _LOGGER.debug(f"Step 2 - Solar available constraints: {solar_constraints}")
    
    # Step 3: Calculate excess available - returns constraint dict
    excess_constraints = _calculate_excess_available(site)
    _LOGGER.debug(f"Step 3 - Excess available constraints: {excess_constraints}")
    
    # Step 4: Determine target power based on mode - returns constraint dict
    target_constraints = _determine_target_power(
        site,
        site_limit_constraints,
        solar_constraints,
        excess_constraints
    )
    _LOGGER.debug(f"Step 4 - Target power ({site.charging_mode}) constraints: {target_constraints}")
    
    # Step 5: Distribute power among chargers using constraint dict
    _distribute_power(site, target_constraints)
    
    for charger in site.chargers:
        _LOGGER.debug(f"Final - {charger.entity_id}: {charger.target_current:.1f}A")
    
    # Restore original chargers list and set inactive ones to 0
    site.chargers = all_chargers
    for charger in all_chargers:
        if charger not in active_chargers:
            charger.target_current = 0


def _sum_constraint_dicts(dict1: dict, dict2: dict) -> dict:
    """
    Sum two constraint dicts element-wise.

    Used to combine grid, solar, and battery limits.

    Args:
        dict1: First constraint dict
        dict2: Second constraint dict

    Returns:
        New constraint dict with summed values
    """
    return {
        'A': dict1['A'] + dict2['A'],
        'B': dict1['B'] + dict2['B'],
        'C': dict1['C'] + dict2['C'],
        'AB': dict1['AB'] + dict2['AB'],
        'AC': dict1['AC'] + dict2['AC'],
        'BC': dict1['BC'] + dict2['BC'],
        'ABC': dict1['ABC'] + dict2['ABC']
    }


def _calculate_grid_limit(site: SiteContext) -> dict:
    """
    Calculate grid power limit based on main breaker rating and consumption.

    Returns constraint dict for ALL phase combinations.
    Grid power is per-phase and CANNOT be reallocated between phases.

    Returns:
        Dict with keys 'A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC' containing available current
    """
    # Calculate per-phase limits
    phase_a_limit = max(0, site.main_breaker_rating - site.phase_a_consumption)
    phase_b_limit = max(0, site.main_breaker_rating - site.phase_b_consumption)
    phase_c_limit = max(0, site.main_breaker_rating - site.phase_c_consumption)

    # If grid charging not allowed (and has battery), limited to export only
    if not site.allow_grid_charging and site.battery_soc is not None:
        phase_a_limit = min(phase_a_limit, site.phase_a_export)
        phase_b_limit = min(phase_b_limit, site.phase_b_export)
        phase_c_limit = min(phase_c_limit, site.phase_c_export)

    # Calculate total available (sum of all phases)
    total_limit = phase_a_limit + phase_b_limit + phase_c_limit

    # Build constraint dict with all phase combinations
    constraints = {
        'A': phase_a_limit,
        'B': phase_b_limit,
        'C': phase_c_limit,
        'AB': phase_a_limit + phase_b_limit,
        'AC': phase_a_limit + phase_c_limit,
        'BC': phase_b_limit + phase_c_limit,
        'ABC': total_limit
    }

    return constraints


def _calculate_inverter_limit(site: SiteContext) -> dict:
    """
    Calculate inverter power limit (solar + battery for Standard mode).

    Returns constraint dict for ALL phase combinations.
    Solar and battery share the same inverter, so per-phase and total inverter limits
    apply to their combined output.

    Battery can discharge when SOC >= battery_soc_min.

    For ASYMMETRIC inverters: Solar+battery power can be allocated to any phase.
    For SYMMETRIC inverters: Solar+battery power is fixed per-phase.

    Returns:
        Dict with keys 'A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC' containing available current
    """
    num_active_phases = site.num_phases if site.num_phases > 0 else 1

    # Calculate solar current
    solar_current = site.solar_production_total / site.voltage if site.solar_production_total else 0

    # Calculate battery discharge current (if available)
    battery_current = 0
    if (site.battery_soc is not None and
        site.battery_soc >= site.battery_soc_min and
        site.battery_max_discharge_power):
        battery_current = site.battery_max_discharge_power / site.voltage

    # Total inverter output (solar + battery)
    total_inverter_current = solar_current + battery_current

    if total_inverter_current == 0:
        return {'A': 0, 'B': 0, 'C': 0, 'AB': 0, 'AC': 0, 'BC': 0, 'ABC': 0}

    # Determine per-phase limit (inverter constraint)
    if site.inverter_max_power_per_phase:
        max_per_phase = site.inverter_max_power_per_phase / site.voltage
    else:
        max_per_phase = float('inf')

    if site.inverter_supports_asymmetric:
        # ASYMMETRIC: Inverter power can be allocated to any phase
        # Per-phase limit must account for consumption on that phase
        phase_a_limit = min(total_inverter_current, max(0, max_per_phase - site.phase_a_consumption))
        phase_b_limit = min(total_inverter_current, max(0, max_per_phase - site.phase_b_consumption)) if site.num_phases > 1 else 0
        phase_c_limit = min(total_inverter_current, max(0, max_per_phase - site.phase_c_consumption)) if site.num_phases > 1 else 0

        constraints = {
            'A': phase_a_limit,
            'B': phase_b_limit,
            'C': phase_c_limit,
            'AB': total_inverter_current,
            'AC': total_inverter_current,
            'BC': total_inverter_current,
            'ABC': total_inverter_current
        }
    else:
        # SYMMETRIC: Inverter power is fixed per-phase
        inverter_per_phase = total_inverter_current / num_active_phases

        # Apply per-phase inverter limits
        phase_a_available = min(inverter_per_phase, max_per_phase)
        phase_b_available = min(inverter_per_phase, max_per_phase) if site.num_phases > 1 else 0
        phase_c_available = min(inverter_per_phase, max_per_phase) if site.num_phases > 1 else 0

        total_available = phase_a_available + phase_b_available + phase_c_available

        constraints = {
            'A': phase_a_available,
            'B': phase_b_available,
            'C': phase_c_available,
            'AB': phase_a_available + phase_b_available,
            'AC': phase_a_available + phase_c_available,
            'BC': phase_b_available + phase_c_available,
            'ABC': total_available
        }

    # Apply total inverter power limit if configured
    if site.inverter_max_power:
        max_total_current = site.inverter_max_power / site.voltage
        if constraints['ABC'] > max_total_current:
            # Scale down all constraints proportionally
            scale = max_total_current / constraints['ABC']
            constraints = {k: v * scale for k, v in constraints.items()}

    return constraints


def _calculate_site_limit(site: SiteContext) -> dict:
    """
    Step 1: Calculate absolute site power limit (prevents breaker trips).

    Returns constraint dict for ALL phase combinations (Multi-Phase Constraint Principle).

    For Standard mode: Includes grid + inverter (solar + battery when SOC >= min)
    For other modes: Only includes grid (solar/battery handled separately)

    Returns:
        Dict with keys 'A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC' containing available current
        for each phase combination.
    """
    # Always include grid limits
    grid_constraints = _calculate_grid_limit(site)

    # For Standard mode, include inverter (solar + battery)
    if site.charging_mode == CHARGING_MODE_STANDARD:
        inverter_constraints = _calculate_inverter_limit(site)

        # Sum grid + inverter
        constraints = _sum_constraint_dicts(grid_constraints, inverter_constraints)

        _LOGGER.debug(f"Site limit (Standard): grid={grid_constraints['ABC']:.1f}A + "
                     f"inverter={inverter_constraints['ABC']:.1f}A = "
                     f"total={constraints['ABC']:.1f}A")
    else:
        # Other modes handle solar/battery separately
        constraints = grid_constraints
        _LOGGER.debug(f"Site limit (grid only): {constraints['ABC']:.1f}A")

    return constraints


def _calculate_solar_available(site: SiteContext) -> dict:
    """
    Step 2: Calculate solar available power.

    Returns constraint dict for ALL phase combinations (Multi-Phase Constraint Principle).

    Considers:
    - Solar export per phase after consumption
    - Battery charging (if SOC < target)
    - Battery discharge (if SOC > target)
    - Inverter limits (per-phase and total)

    For ASYMMETRIC inverters: Solar/battery power is a flexible pool that can be allocated
    to any phase, so all phase constraints are set to the total available (limited by
    per-phase inverter max).

    For SYMMETRIC inverters: Solar/battery power is fixed per-phase.

    Returns:
        Dict with keys 'A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC' containing available current
    """
    num_active_phases = site.num_phases if site.num_phases > 0 else 1

    # Determine per-phase limit (inverter constraint)
    if site.inverter_max_power_per_phase:
        max_per_phase = site.inverter_max_power_per_phase / site.voltage
    else:
        max_per_phase = float('inf')

    # Build constraint dict based on inverter type
    if site.inverter_supports_asymmetric:
        # ASYMMETRIC: Solar/battery power is a flexible pool that can be allocated to any phase

        # Calculate total solar production
        solar_total_current = site.solar_production_total / site.voltage if site.solar_production_total else 0

        # Calculate total inverter output (solar + battery, respecting inverter limits)
        inverter_output = solar_total_current

        # Handle battery charging/discharging (affects inverter output)
        if site.battery_soc is not None:
            if site.battery_soc < site.battery_soc_target:
                # Battery charges first - reduce inverter output available for EV
                if site.battery_max_charge_power:
                    battery_charge_current = site.battery_max_charge_power / site.voltage
                    inverter_output = max(0, inverter_output - battery_charge_current)

            elif site.battery_soc > site.battery_soc_target:
                # Battery can discharge - add to inverter output
                if site.battery_max_discharge_power:
                    battery_discharge_current = site.battery_max_discharge_power / site.voltage
                    inverter_output += battery_discharge_current

        # Apply total inverter limit to OUTPUT (solar + battery combined)
        if site.inverter_max_power:
            max_total = site.inverter_max_power / site.voltage
            inverter_output = min(inverter_output, max_total)

        # NOW subtract total consumption to get what's available for charger
        total_consumption = site.phase_a_consumption
        if site.num_phases > 1:
            total_consumption += site.phase_b_consumption + site.phase_c_consumption

        solar_available = max(0, inverter_output - total_consumption)

        # Per-phase constraints: limited by (inverter per-phase max - consumption on that phase)
        # The inverter max is OUTPUT capacity, consumption must be covered first!
        phase_a_limit = min(solar_available, max(0, max_per_phase - site.phase_a_consumption))
        phase_b_limit = min(solar_available, max(0, max_per_phase - site.phase_b_consumption)) if site.num_phases > 1 else 0
        phase_c_limit = min(solar_available, max(0, max_per_phase - site.phase_c_consumption)) if site.num_phases > 1 else 0

        constraints = {
            'A': phase_a_limit,
            'B': phase_b_limit,
            'C': phase_c_limit,
            'AB': solar_available,
            'AC': solar_available,
            'BC': solar_available,
            'ABC': solar_available
        }
    else:
        # SYMMETRIC: Solar/battery power is fixed per-phase
        # Calculate solar per phase (evenly distributed)
        solar_per_phase_current = (site.solar_production_total / num_active_phases) / site.voltage if site.solar_production_total else 0

        # Subtract consumption per phase
        phase_a_available = max(0, solar_per_phase_current - site.phase_a_consumption)
        phase_b_available = max(0, solar_per_phase_current - site.phase_b_consumption) if site.num_phases > 1 else 0
        phase_c_available = max(0, solar_per_phase_current - site.phase_c_consumption) if site.num_phases > 1 else 0

        # Handle battery charging/discharging (distributed per phase)
        if site.battery_soc is not None:
            if site.battery_soc < site.battery_soc_target:
                # Battery charges first - reduce available per phase
                if site.battery_max_charge_power:
                    battery_charge_current = site.battery_max_charge_power / site.voltage
                    battery_current_per_phase = battery_charge_current / num_active_phases
                    phase_a_available = max(0, phase_a_available - battery_current_per_phase)
                    if site.num_phases > 1:
                        phase_b_available = max(0, phase_b_available - battery_current_per_phase)
                        phase_c_available = max(0, phase_c_available - battery_current_per_phase)

            elif site.battery_soc > site.battery_soc_target:
                # Battery can discharge - add to available per phase
                if site.battery_max_discharge_power:
                    battery_discharge_current = site.battery_max_discharge_power / site.voltage
                    battery_current_per_phase = battery_discharge_current / num_active_phases
                    phase_a_available += battery_current_per_phase
                    if site.num_phases > 1:
                        phase_b_available += battery_current_per_phase
                        phase_c_available += battery_current_per_phase

        # Apply per-phase inverter limits
        phase_a_available = min(phase_a_available, max_per_phase)
        if site.num_phases > 1:
            phase_b_available = min(phase_b_available, max_per_phase)
            phase_c_available = min(phase_c_available, max_per_phase)

        # Calculate total
        total_available = phase_a_available + phase_b_available + phase_c_available

        # Apply total inverter limit if configured
        if site.inverter_max_power:
            max_total = site.inverter_max_power / site.voltage
            if total_available > max_total:
                # Scale down all phases proportionally
                scale = max_total / total_available if total_available > 0 else 0
                phase_a_available *= scale
                phase_b_available *= scale
                phase_c_available *= scale
                total_available = max_total

        constraints = {
            'A': phase_a_available,
            'B': phase_b_available,
            'C': phase_c_available,
            'AB': min(phase_a_available + phase_b_available, total_available),
            'AC': min(phase_a_available + phase_c_available, total_available),
            'BC': min(phase_b_available + phase_c_available, total_available),
            'ABC': total_available
        }

    _LOGGER.debug(f"Solar available constraints ({'asymmetric' if site.inverter_supports_asymmetric else 'symmetric'}): {constraints}")

    return constraints


def _calculate_excess_available(site: SiteContext) -> dict:
    """
    Step 3: Calculate excess available power.

    Returns constraint dict for ALL phase combinations (Multi-Phase Constraint Principle).
    Excess mode only charges when export exceeds threshold.

    For ASYMMETRIC inverters: Excess power (solar) can be allocated to any phase,
    so all phase constraints are set to the total available (limited by per-phase inverter max).

    For SYMMETRIC inverters: Excess power is divided per-phase.

    Returns:
        Dict with keys 'A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC' containing available current
    """
    # Check if export exceeds threshold
    if site.total_export_power > site.excess_export_threshold:
        available_power = site.total_export_power - site.excess_export_threshold
        total_available = available_power / site.voltage if site.voltage > 0 else 0

        # Determine per-phase limit (inverter constraint)
        if site.inverter_max_power_per_phase:
            max_per_phase = site.inverter_max_power_per_phase / site.voltage
        else:
            max_per_phase = float('inf')

        # Build constraint dict based on inverter type
        if site.inverter_supports_asymmetric:
            # ASYMMETRIC: Excess power (solar) can be allocated to any phase
            # Per-phase limit must account for consumption on that phase
            phase_a_limit = min(total_available, max(0, max_per_phase - site.phase_a_consumption))
            phase_b_limit = min(total_available, max(0, max_per_phase - site.phase_b_consumption)) if site.num_phases > 1 else 0
            phase_c_limit = min(total_available, max(0, max_per_phase - site.phase_c_consumption)) if site.num_phases > 1 else 0

            constraints = {
                'A': phase_a_limit,
                'B': phase_b_limit,
                'C': phase_c_limit,
                'AB': total_available,
                'AC': total_available,
                'BC': total_available,
                'ABC': total_available
            }
        else:
            # SYMMETRIC: Excess power is divided per-phase
            num_active_phases = site.num_phases if site.num_phases > 0 else 1
            per_phase_available = total_available / num_active_phases

            phase_a = min(per_phase_available, max_per_phase)
            phase_b = min(per_phase_available, max_per_phase) if site.num_phases > 1 else 0
            phase_c = min(per_phase_available, max_per_phase) if site.num_phases > 1 else 0

            constraints = {
                'A': phase_a,
                'B': phase_b,
                'C': phase_c,
                'AB': min(phase_a + phase_b, total_available),
                'AC': min(phase_a + phase_c, total_available),
                'BC': min(phase_b + phase_c, total_available),
                'ABC': total_available
            }

        _LOGGER.debug(f"Excess available constraints ({'asymmetric' if site.inverter_supports_asymmetric else 'symmetric'}): {constraints}")
        return constraints

    # Below threshold - no power available
    return {'A': 0, 'B': 0, 'C': 0, 'AB': 0, 'AC': 0, 'BC': 0, 'ABC': 0}


def _determine_target_power(
    site: SiteContext,
    site_limit_constraints: dict,
    solar_constraints: dict,
    excess_constraints: dict
) -> dict:
    """
    Step 4: Determine target power based on charging mode.
    
    Returns constraint dict for ALL phase combinations (Multi-Phase Constraint Principle).
    
    Args:
        site: SiteContext
        site_limit_constraints: Site limit constraint dict from Step 1
        solar_constraints: Solar available constraint dict from Step 2
        excess_constraints: Excess available constraint dict from Step 3
    
    Returns:
        Dict with keys 'A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC' containing target current
    """
    mode = site.charging_mode
    
    if mode == CHARGING_MODE_STANDARD:
        # Standard: Use site limit (includes grid + solar + battery)
        return site_limit_constraints
    
    elif mode == CHARGING_MODE_ECO:
        # Eco: Charge at minimum to protect battery, can use more if battery is healthy
        
        # Battery below minimum - protect battery (no charging)
        if site.battery_soc is not None and site.battery_soc < site.battery_soc_min:
            return {'A': 0, 'B': 0, 'C': 0, 'AB': 0, 'AC': 0, 'BC': 0, 'ABC': 0}
        
        # Calculate sum of minimum charge rates
        sum_minimums_total = sum(c.min_current * c.phases for c in site.chargers)
        num_active_phases = site.num_phases if site.num_phases > 0 else 1
        sum_minimums_per_phase = sum_minimums_total / num_active_phases
        
        # Battery between min and target - charge at minimum only
        if site.battery_soc is not None and site.battery_soc < site.battery_soc_target:
            min_a = sum_minimums_per_phase
            min_b = sum_minimums_per_phase if site.num_phases > 1 else 0
            min_c = sum_minimums_per_phase if site.num_phases > 1 else 0
            
            # Cap at site limits
            return {
                'A': min(min_a, site_limit_constraints['A']),
                'B': min(min_b, site_limit_constraints['B']),
                'C': min(min_c, site_limit_constraints['C']),
                'AB': min(min_a + min_b, site_limit_constraints['AB']),
                'AC': min(min_a + min_c, site_limit_constraints['AC']),
                'BC': min(min_b + min_c, site_limit_constraints['BC']),
                'ABC': min(sum_minimums_total, site_limit_constraints['ABC'])
            }
        else:
            # Battery >= target or no battery - can use all solar available
            # Take maximum of solar and minimums for each constraint
            min_a = sum_minimums_per_phase
            min_b = sum_minimums_per_phase if site.num_phases > 1 else 0
            min_c = sum_minimums_per_phase if site.num_phases > 1 else 0
            
            target_constraints = {
                'A': max(solar_constraints['A'], min_a),
                'B': max(solar_constraints['B'], min_b),
                'C': max(solar_constraints['C'], min_c),
                'AB': max(solar_constraints['AB'], min_a + min_b),
                'AC': max(solar_constraints['AC'], min_a + min_c),
                'BC': max(solar_constraints['BC'], min_b + min_c),
                'ABC': max(solar_constraints['ABC'], sum_minimums_total)
            }
            
            # Cap at site limits
            return {
                'A': min(target_constraints['A'], site_limit_constraints['A']),
                'B': min(target_constraints['B'], site_limit_constraints['B']),
                'C': min(target_constraints['C'], site_limit_constraints['C']),
                'AB': min(target_constraints['AB'], site_limit_constraints['AB']),
                'AC': min(target_constraints['AC'], site_limit_constraints['AC']),
                'BC': min(target_constraints['BC'], site_limit_constraints['BC']),
                'ABC': min(target_constraints['ABC'], site_limit_constraints['ABC'])
            }
    
    elif mode == CHARGING_MODE_SOLAR:
        # Solar: Use only solar_available (includes battery discharge if SOC > target)
        # Battery priority: If battery below target, all solar goes to battery (EV gets 0)
        if site.battery_soc is not None and site.battery_soc < site.battery_soc_target:
            return {'A': 0, 'B': 0, 'C': 0, 'AB': 0, 'AC': 0, 'BC': 0, 'ABC': 0}
        return solar_constraints
    
    elif mode == CHARGING_MODE_EXCESS:
        # Excess: Use only excess_available
        return excess_constraints
    
    else:
        _LOGGER.warning(f"Unknown charging mode '{mode}', using site limit")
        return site_limit_constraints


def _get_phase_available_current(site: SiteContext) -> dict:
    """
    Helper: Get available current per phase.
    
    Uses uniform per-phase representation - no special cases.
    For 1-phase systems, phase_b_export and phase_c_export are 0.
    
    Returns:
        dict with keys 'A', 'B', 'C' containing available current per phase
    """
    # Always return all three phases - unused phases are 0
    return {
        'A': site.phase_a_export,
        'B': site.phase_b_export,
        'C': site.phase_c_export,
    }


def _distribute_power(site: SiteContext, target_constraints: dict) -> None:
    """
    Step 5: Distribute target power among chargers.
    
    Uses constraint dict for ALL phase combinations (Multi-Phase Constraint Principle).
    
    Args:
        site: SiteContext with chargers
        target_constraints: Constraint dict with keys 'A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC'
    """
    if len(site.chargers) == 0:
        return
    
    _LOGGER.debug(f"Distribution constraints: {target_constraints}")
    
    # Log what each charger is drawing from
    for charger in site.chargers:
        _LOGGER.debug(f"Charger {charger.entity_id}: mask={charger.active_phases_mask}, phases={charger.phases}")
    
    # Use universal distribution with constraint dict
    _distribute_power_per_phase(site, target_constraints, site.distribution_mode)


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


def _distribute_power_per_phase(site: SiteContext, constraints: dict, distribution_mode: str) -> None:
    """
    Universal per-phase distribution - handles ALL cases and ALL distribution modes.
    
    Uses constraint dict with all phase combinations (Multi-Phase Constraint Principle).
    
    Args:
        site: SiteContext with chargers
        constraints: Constraint dict with keys 'A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC'
        distribution_mode: "priority", "shared", "strict", or "optimized"
    """
    mode = distribution_mode.lower() if distribution_mode else "priority"
    
    if mode == "priority":
        _distribute_per_phase_priority(site, constraints)
    elif mode == "shared":
        _distribute_per_phase_shared(site, constraints)
    elif mode == "strict":
        _distribute_per_phase_strict(site, constraints)
    elif mode == "optimized":
        _distribute_per_phase_optimized(site, constraints)
    else:
        _LOGGER.warning(f"Unknown distribution mode '{mode}', using priority")
        _distribute_per_phase_priority(site, constraints)


def _distribute_per_phase_priority(site: SiteContext, constraints: dict) -> None:
    """
    PRIORITY mode using constraint dict with helper functions.
    Pass 1: Allocate minimum by priority
    Pass 2: Give remainder to highest priority first
    """
    # Sort all chargers by priority
    chargers_by_priority = sorted(site.chargers, key=lambda c: c.priority)
    
    # Track remaining capacity
    remaining_constraints = constraints.copy()
    allocated = {}
    
    # Pass 1: Allocate minimum to all chargers (by priority)
    for charger in chargers_by_priority:
        mask = charger.active_phases_mask
        if not mask:
            _LOGGER.error(f"Charger {charger.entity_id} has no phase mask!")
            allocated[charger.entity_id] = 0
            continue
        
        # Use helper to get available current for this charger
        charger_available = get_available_current(remaining_constraints, mask)
        
        # Try to allocate minimum
        if charger_available >= charger.min_current:
            allocated[charger.entity_id] = charger.min_current
            # Use helper to deduct from all affected phase combinations
            remaining_constraints = deduct_current(remaining_constraints, charger.min_current, mask)
        else:
            allocated[charger.entity_id] = 0
    
    # Pass 2: Allocate remainder by priority
    for charger in chargers_by_priority:
        if allocated.get(charger.entity_id, 0) == 0:
            charger.target_current = 0
            continue
        
        mask = charger.active_phases_mask
        if not mask:
            charger.target_current = allocated[charger.entity_id]
            continue
        
        # Use helper to get available current
        charger_available = get_available_current(remaining_constraints, mask)
        
        wanted_additional = charger.max_current - allocated[charger.entity_id]
        additional = min(wanted_additional, charger_available)
        charger.target_current = allocated[charger.entity_id] + additional
        
        # Use helper to deduct from all affected phase combinations
        remaining_constraints = deduct_current(remaining_constraints, additional, mask)


def _distribute_per_phase_shared(site: SiteContext, constraints: dict) -> None:
    """
    SHARED mode using constraint dict with helper functions.
    Pass 1: Allocate minimum to all
    Pass 2: Split remainder equally among all charging chargers
    """
    remaining_constraints = constraints.copy()
    allocated = {}
    
    # Pass 1: Allocate minimum to all
    for charger in site.chargers:
        mask = charger.active_phases_mask
        if not mask:
            _LOGGER.error(f"Charger {charger.entity_id} has no phase mask!")
            allocated[charger.entity_id] = 0
            continue
        
        # Use helper to get available current for this charger
        charger_available = get_available_current(remaining_constraints, mask)
        
        # Allocate minimum
        if charger_available >= charger.min_current:
            allocated[charger.entity_id] = charger.min_current
            # Use helper to deduct from all affected phase combinations
            remaining_constraints = deduct_current(remaining_constraints, charger.min_current, mask)
        else:
            allocated[charger.entity_id] = 0
    
    charging_chargers = [c for c in site.chargers if allocated.get(c.entity_id, 0) > 0]
    if not charging_chargers:
        for charger in site.chargers:
            charger.target_current = 0
        return
    
    # Pass 2: Split remainder equally among charging chargers
    # Continue distributing until no more power available or all chargers at max
    while True:
        # Calculate what each charger can still accept
        chargers_wanting_more = []
        for charger in charging_chargers:
            if allocated[charger.entity_id] < charger.max_current:
                chargers_wanting_more.append(charger)
        
        if not chargers_wanting_more:
            break
        
        # Find the minimum available across all chargers wanting more
        min_available = float('inf')
        for charger in chargers_wanting_more:
            mask = charger.active_phases_mask
            charger_available = get_available_current(remaining_constraints, mask)
            min_available = min(min_available, charger_available)
        
        if min_available <= 0:
            break
        
        # Split available equally among chargers wanting more
        per_charger_increment = min_available / len(chargers_wanting_more)
        
        for charger in chargers_wanting_more:
            mask = charger.active_phases_mask
            additional = min(per_charger_increment, charger.max_current - allocated[charger.entity_id])
            allocated[charger.entity_id] += additional
            remaining_constraints = deduct_current(remaining_constraints, additional, mask)
    
    # Apply allocated values
    for charger in charging_chargers:
        charger.target_current = allocated[charger.entity_id]
    
    # Set non-charging chargers to 0
    for charger in site.chargers:
        if charger not in charging_chargers:
            charger.target_current = 0


def _distribute_per_phase_strict(site: SiteContext, constraints: dict) -> None:
    """
    STRICT mode using constraint dict with helper functions.
    Give first charger up to max, then next, etc. (by priority)
    """
    remaining_constraints = constraints.copy()
    chargers_by_priority = sorted(site.chargers, key=lambda c: c.priority)
    
    for charger in chargers_by_priority:
        mask = charger.active_phases_mask
        if not mask:
            _LOGGER.error(f"Charger {charger.entity_id} has no phase mask!")
            charger.target_current = 0
            continue
        
        # Use helper to get available current for this charger
        charger_available = get_available_current(remaining_constraints, mask)
        
        # Give up to max or what's available
        charger.target_current = min(charger.max_current, charger_available)
        
        # Use helper to deduct from all affected phase combinations
        remaining_constraints = deduct_current(remaining_constraints, charger.target_current, mask)


def _distribute_per_phase_optimized(site: SiteContext, constraints: dict) -> None:
    """
    OPTIMIZED mode using constraint dict with helper functions.
    Reduce higher priority chargers to allow lower priority to charge at minimum.
    """
    remaining_constraints = constraints.copy()
    chargers_by_priority = sorted(site.chargers, key=lambda c: c.priority)
    
    for i, charger in enumerate(chargers_by_priority):
        mask = charger.active_phases_mask
        if not mask:
            _LOGGER.error(f"Charger {charger.entity_id} has no phase mask!")
            charger.target_current = 0
            continue
        
        # Use helper to get available current for this charger
        charger_available = get_available_current(remaining_constraints, mask)
        
        wanted = min(charger.max_current, charger_available)
        
        # Check if we should reduce to help next charger
        if i < len(chargers_by_priority) - 1:
            next_charger = chargers_by_priority[i + 1]
            next_mask = next_charger.active_phases_mask
            if next_mask:
                # Calculate what next charger would see after allocating 'wanted' to current charger
                temp_remaining = deduct_current(remaining_constraints, wanted, mask)
                next_available = get_available_current(temp_remaining, next_mask)
                
                # If next can't charge at minimum, try to reduce current charger
                if next_available < next_charger.min_current:
                    reduction_needed = next_charger.min_current - next_available
                    can_reduce = max(0, wanted - charger.min_current)
                    reduction = min(reduction_needed, can_reduce)
                    wanted -= reduction
        
        charger.target_current = wanted
        
        # Use helper to deduct from all affected phase combinations
        remaining_constraints = deduct_current(remaining_constraints, wanted, mask)


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
