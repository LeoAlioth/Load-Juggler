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

from .models import SiteContext, ChargerContext, PhaseConstraints
from ..const import (
    CHARGING_MODE_STANDARD,
    CHARGING_MODE_ECO,
    CHARGING_MODE_SOLAR,
    CHARGING_MODE_EXCESS,
)

_LOGGER = logging.getLogger(__name__)


def calculate_all_charger_targets(site: SiteContext) -> None:
    """
    Calculate allocated and available current for all chargers.

    Steps:
    0. Filter active chargers (with cars connected)
    1. Calculate absolute site limits (per-phase physical constraints)
    2. Calculate solar available power
    3. Calculate excess available power
    4. Determine target power based on charging mode
    5. Distribute power among active chargers
    6. Calculate available current for all chargers

    Args:
        site: SiteContext containing all site and charger data
    """
    # Step 0: Filter active vs inactive chargers
    all_chargers = site.chargers
    active_chargers = [c for c in all_chargers
                       if c.connector_status not in ["Available", "Unknown", "Unavailable"]]
    inactive_chargers = [c for c in all_chargers if c not in active_chargers]

    _LOGGER.debug(
        f"Calculating targets for {len(active_chargers)}/{len(all_chargers)} active chargers - "
        f"Mode: {site.charging_mode}, Distribution: {site.distribution_mode}"
    )

    # Steps 1-4: Calculate constraints (always, even with no active chargers)
    site_limit_constraints = _calculate_site_limit(site)
    _LOGGER.debug(f"Step 1 - Site limit constraints: {site_limit_constraints}")

    solar_constraints = _calculate_solar_available(site)
    _LOGGER.debug(f"Step 2 - Solar available constraints: {solar_constraints}")

    excess_constraints = _calculate_excess_available(site)
    _LOGGER.debug(f"Step 3 - Excess available constraints: {excess_constraints}")

    target_constraints = _determine_target_power(
        site, site_limit_constraints, solar_constraints, excess_constraints
    )
    _LOGGER.debug(f"Step 4 - Target power ({site.charging_mode}) constraints: {target_constraints}")

    # Step 5: Distribute power among active chargers only
    if active_chargers:
        site.chargers = active_chargers
        _distribute_power(site, target_constraints)
        site.chargers = all_chargers

    # Set inactive chargers to 0 allocated
    for charger in inactive_chargers:
        charger.allocated_current = 0

    # Step 6: Calculate available current for all chargers
    _calculate_available_current(all_chargers, active_chargers, inactive_chargers, target_constraints)

    for charger in all_chargers:
        _LOGGER.debug(
            f"Final - {charger.entity_id}: allocated={charger.allocated_current:.1f}A, "
            f"available={charger.available_current:.1f}A"
        )


def _calculate_available_current(
    all_chargers: list,
    active_chargers: list,
    inactive_chargers: list,
    target_constraints: PhaseConstraints,
) -> None:
    """
    Calculate available current for all chargers.

    - Active chargers: available = allocated (they're getting what's available)
    - Inactive chargers: available = what they could get from remaining capacity
      after active chargers are deducted. Each idle charger independently sees
      the same remaining pool (hypothetical, no deduction between idle chargers).
    """
    # Active chargers: available = allocated
    for charger in active_chargers:
        charger.available_current = charger.allocated_current

    # Calculate remaining constraints after active allocations
    remaining = target_constraints.copy()
    for charger in active_chargers:
        if charger.allocated_current > 0 and charger.active_phases_mask:
            remaining = remaining.deduct(charger.allocated_current, charger.active_phases_mask)

    # Inactive chargers: each independently sees remaining pool
    for charger in inactive_chargers:
        mask = charger.active_phases_mask
        if not mask:
            charger.available_current = 0
            continue
        available = remaining.get_available(mask)
        if available >= charger.min_current:
            charger.available_current = min(charger.max_current, available)
        else:
            charger.available_current = 0


def _calculate_grid_limit(site: SiteContext) -> PhaseConstraints:
    """
    Calculate grid power limit based on main breaker rating and consumption.

    Grid power is per-phase and CANNOT be reallocated between phases.
    """
    # Calculate per-phase limits
    phase_a_limit = max(0, site.main_breaker_rating - site.consumption.a)
    phase_b_limit = max(0, site.main_breaker_rating - site.consumption.b)
    phase_c_limit = max(0, site.main_breaker_rating - site.consumption.c)

    # If grid charging not allowed (and has battery), limited to export only
    if not site.allow_grid_charging and site.battery_soc is not None:
        phase_a_limit = min(phase_a_limit, site.export_current.a)
        phase_b_limit = min(phase_b_limit, site.export_current.b)
        phase_c_limit = min(phase_c_limit, site.export_current.c)

    # Calculate total available (sum of all phases)
    total_limit = phase_a_limit + phase_b_limit + phase_c_limit

    # Apply max grid import power limit (if configured)
    # This is a total (all-phase) constraint from the grid operator / smart meter.
    # Power buffer has already been subtracted before reaching SiteContext.
    if site.max_grid_import_power is not None:
        total_consumption = site.consumption.total
        max_import_current = site.max_grid_import_power / site.voltage
        available_for_evs = max(0, max_import_current - total_consumption)
        if total_limit > available_for_evs and total_limit > 0:
            scale = available_for_evs / total_limit
            phase_a_limit *= scale
            phase_b_limit *= scale
            phase_c_limit *= scale
            total_limit = available_for_evs

    return PhaseConstraints.from_per_phase(phase_a_limit, phase_b_limit, phase_c_limit)


def _calculate_inverter_limit(site: SiteContext) -> PhaseConstraints:
    """
    Calculate inverter power limit (solar + battery for Standard mode).

    Returns PhaseConstraints for ALL phase combinations.
    Solar and battery share the same inverter, so per-phase and total inverter limits
    apply to their combined output.

    Battery can discharge when SOC >= battery_soc_min.

    For ASYMMETRIC inverters: Solar+battery power can be allocated to any phase.
    For SYMMETRIC inverters: Solar+battery power is fixed per-phase.
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
        return PhaseConstraints.zeros()

    # Determine per-phase limit (inverter constraint)
    if site.inverter_max_power_per_phase:
        max_per_phase = site.inverter_max_power_per_phase / site.voltage
    else:
        max_per_phase = float('inf')

    if site.inverter_supports_asymmetric:
        # ASYMMETRIC: Inverter power can be allocated to any phase
        phase_a_limit = min(total_inverter_current, max(0, max_per_phase - site.consumption.a))
        phase_b_limit = min(total_inverter_current, max(0, max_per_phase - site.consumption.b))
        phase_c_limit = min(total_inverter_current, max(0, max_per_phase - site.consumption.c))

        constraints = PhaseConstraints.from_pool(phase_a_limit, phase_b_limit, phase_c_limit, total_inverter_current)
    else:
        # SYMMETRIC: Inverter power is fixed per-phase
        inverter_per_phase = total_inverter_current / num_active_phases

        phase_a_available = min(inverter_per_phase, max_per_phase)
        phase_b_available = min(inverter_per_phase, max_per_phase)
        phase_c_available = min(inverter_per_phase, max_per_phase)

        constraints = PhaseConstraints.from_per_phase(phase_a_available, phase_b_available, phase_c_available)

    # Apply total inverter power limit if configured
    if site.inverter_max_power:
        max_total_current = site.inverter_max_power / site.voltage
        if constraints.ABC > max_total_current:
            constraints = constraints.scale(max_total_current / constraints.ABC)

    return constraints


def _calculate_site_limit(site: SiteContext) -> PhaseConstraints:
    """
    Step 1: Calculate absolute site power limit (prevents breaker trips).

    Returns PhaseConstraints for ALL phase combinations (Multi-Phase Constraint Principle).

    For Standard mode: Includes grid + inverter (solar + battery when SOC >= min)
    For other modes: Only includes grid (solar/battery handled separately)
    """
    grid_constraints = _calculate_grid_limit(site)

    if site.charging_mode == CHARGING_MODE_STANDARD:
        inverter_constraints = _calculate_inverter_limit(site)
        constraints = grid_constraints + inverter_constraints

        _LOGGER.debug(f"Site limit (Standard): grid={grid_constraints.ABC:.1f}A + "
                     f"inverter={inverter_constraints.ABC:.1f}A = "
                     f"total={constraints.ABC:.1f}A")
    else:
        constraints = grid_constraints
        _LOGGER.debug(f"Site limit (grid only): {constraints.ABC:.1f}A")

    return constraints


def _calculate_solar_available(site: SiteContext) -> PhaseConstraints:
    """
    Step 2: Calculate solar available power.

    Returns PhaseConstraints for ALL phase combinations.

    Considers:
    - Solar export per phase after consumption
    - Battery charging (if SOC < target)
    - Battery discharge (if SOC > target)
    - Inverter limits (per-phase and total)

    For ASYMMETRIC inverters: Solar/battery power is a flexible pool.
    For SYMMETRIC inverters: Solar/battery power is fixed per-phase.
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
        solar_available = max(0, inverter_output - site.consumption.total)

        # Per-phase constraints: limited by (inverter per-phase max - consumption on that phase)
        phase_a_limit = min(solar_available, max(0, max_per_phase - site.consumption.a))
        phase_b_limit = min(solar_available, max(0, max_per_phase - site.consumption.b))
        phase_c_limit = min(solar_available, max(0, max_per_phase - site.consumption.c))

        constraints = PhaseConstraints.from_pool(phase_a_limit, phase_b_limit, phase_c_limit, solar_available)
    else:
        # SYMMETRIC: Solar/battery power is fixed per-phase
        # Calculate solar per phase (evenly distributed)
        solar_per_phase_current = (site.solar_production_total / num_active_phases) / site.voltage if site.solar_production_total else 0

        # Subtract consumption per phase
        phase_a_available = max(0, solar_per_phase_current - site.consumption.a)
        phase_b_available = max(0, solar_per_phase_current - site.consumption.b)
        phase_c_available = max(0, solar_per_phase_current - site.consumption.c)

        # Handle battery charging/discharging (distributed per phase)
        if site.battery_soc is not None:
            if site.battery_soc < site.battery_soc_target:
                # Battery charges first - reduce available per phase
                if site.battery_max_charge_power:
                    battery_charge_current = site.battery_max_charge_power / site.voltage
                    battery_current_per_phase = battery_charge_current / num_active_phases
                    phase_a_available = max(0, phase_a_available - battery_current_per_phase)
                    phase_b_available = max(0, phase_b_available - battery_current_per_phase)
                    phase_c_available = max(0, phase_c_available - battery_current_per_phase)

            elif site.battery_soc > site.battery_soc_target:
                # Battery can discharge - add to available per phase
                if site.battery_max_discharge_power:
                    battery_discharge_current = site.battery_max_discharge_power / site.voltage
                    battery_current_per_phase = battery_discharge_current / num_active_phases
                    phase_a_available += battery_current_per_phase
                    phase_b_available += battery_current_per_phase
                    phase_c_available += battery_current_per_phase

        # Apply per-phase inverter limits
        phase_a_available = min(phase_a_available, max_per_phase)
        phase_b_available = min(phase_b_available, max_per_phase)
        phase_c_available = min(phase_c_available, max_per_phase)

        # Apply total inverter limit if configured
        constraints = PhaseConstraints.from_per_phase(phase_a_available, phase_b_available, phase_c_available)

        if site.inverter_max_power:
            max_total = site.inverter_max_power / site.voltage
            if constraints.ABC > max_total:
                constraints = constraints.scale(max_total / constraints.ABC)

    _LOGGER.debug(f"Solar available constraints ({'asymmetric' if site.inverter_supports_asymmetric else 'symmetric'}): {constraints}")

    return constraints


def _calculate_excess_available(site: SiteContext) -> PhaseConstraints:
    """
    Step 3: Calculate excess available power.

    Returns PhaseConstraints for ALL phase combinations.
    Excess mode only charges when export exceeds threshold.

    For ASYMMETRIC inverters: Excess power can be allocated to any phase.
    For SYMMETRIC inverters: Excess power is divided per-phase.
    """
    if site.total_export_power > site.excess_export_threshold:
        available_power = site.total_export_power - site.excess_export_threshold
        total_available = available_power / site.voltage if site.voltage > 0 else 0

        if site.inverter_max_power_per_phase:
            max_per_phase = site.inverter_max_power_per_phase / site.voltage
        else:
            max_per_phase = float('inf')

        if site.inverter_supports_asymmetric:
            phase_a_limit = min(total_available, max(0, max_per_phase - site.consumption.a))
            phase_b_limit = min(total_available, max(0, max_per_phase - site.consumption.b))
            phase_c_limit = min(total_available, max(0, max_per_phase - site.consumption.c))

            constraints = PhaseConstraints.from_pool(phase_a_limit, phase_b_limit, phase_c_limit, total_available)
        else:
            num_active_phases = site.num_phases if site.num_phases > 0 else 1
            per_phase_available = total_available / num_active_phases

            phase_a = min(per_phase_available, max_per_phase)
            phase_b = min(per_phase_available, max_per_phase)
            phase_c = min(per_phase_available, max_per_phase)

            constraints = PhaseConstraints.from_per_phase(phase_a, phase_b, phase_c)

        _LOGGER.debug(f"Excess available constraints ({'asymmetric' if site.inverter_supports_asymmetric else 'symmetric'}): {constraints}")
        return constraints

    return PhaseConstraints.zeros()


def _determine_target_power(
    site: SiteContext,
    site_limit_constraints: PhaseConstraints,
    solar_constraints: PhaseConstraints,
    excess_constraints: PhaseConstraints,
) -> PhaseConstraints:
    """
    Step 4: Determine target power based on charging mode.

    Returns PhaseConstraints for ALL phase combinations.
    """
    mode = site.charging_mode

    if mode == CHARGING_MODE_STANDARD:
        return site_limit_constraints

    elif mode == CHARGING_MODE_ECO:
        # Battery below minimum - protect battery (no charging)
        if site.battery_soc is not None and site.battery_soc < site.battery_soc_min:
            return PhaseConstraints.zeros()

        # Calculate sum of minimum charge rates
        sum_minimums_total = sum(c.min_current * c.phases for c in site.chargers)
        num_active_phases = site.num_phases if site.num_phases > 0 else 1
        sum_minimums_per_phase = sum_minimums_total / num_active_phases

        minimums = PhaseConstraints.from_per_phase(
            sum_minimums_per_phase, sum_minimums_per_phase, sum_minimums_per_phase
        )

        # Battery between min and target - charge at minimum only
        if site.battery_soc is not None and site.battery_soc < site.battery_soc_target:
            return minimums.element_min(site_limit_constraints)
        else:
            # Battery >= target or no battery - use max of solar and minimums, capped at site limit
            target = solar_constraints.element_max(minimums)
            return target.element_min(site_limit_constraints)

    elif mode == CHARGING_MODE_SOLAR:
        if site.battery_soc is not None and site.battery_soc < site.battery_soc_target:
            return PhaseConstraints.zeros()
        return solar_constraints

    elif mode == CHARGING_MODE_EXCESS:
        return excess_constraints

    else:
        _LOGGER.warning(f"Unknown charging mode '{mode}', using site limit")
        return site_limit_constraints


def _distribute_power(site: SiteContext, target_constraints: PhaseConstraints) -> None:
    """
    Step 5: Distribute target power among chargers.

    Uses PhaseConstraints (Multi-Phase Constraint Principle).
    Dispatches to mode-specific distribution function.
    """
    if len(site.chargers) == 0:
        return

    _LOGGER.debug(f"Distribution constraints: {target_constraints}")

    for charger in site.chargers:
        _LOGGER.debug(f"Charger {charger.entity_id}: mask={charger.active_phases_mask}, phases={charger.phases}")

    mode = site.distribution_mode.lower() if site.distribution_mode else "priority"
    
    if mode == "priority":
        _distribute_per_phase_priority(site, target_constraints)
    elif mode == "shared":
        _distribute_per_phase_shared(site, target_constraints)
    elif mode == "strict":
        _distribute_per_phase_strict(site, target_constraints)
    elif mode == "optimized":
        _distribute_per_phase_optimized(site, target_constraints)
    else:
        _LOGGER.warning(f"Unknown distribution mode '{mode}', using priority")
        _distribute_per_phase_priority(site, target_constraints)


def _distribute_per_phase_priority(site: SiteContext, constraints: PhaseConstraints) -> None:
    """
    PRIORITY mode: Pass 1 allocate minimum by priority, Pass 2 give remainder by priority.
    """
    chargers_by_priority = sorted(site.chargers, key=lambda c: c.priority)
    remaining = constraints.copy()
    allocated = {}

    # Pass 1: Allocate minimum to all chargers (by priority)
    for charger in chargers_by_priority:
        mask = charger.active_phases_mask
        if not mask:
            _LOGGER.error(f"Charger {charger.entity_id} has no phase mask!")
            allocated[charger.entity_id] = 0
            continue

        charger_available = remaining.get_available(mask)

        if charger_available >= charger.min_current:
            allocated[charger.entity_id] = charger.min_current
            remaining = remaining.deduct(charger.min_current, mask)
        else:
            allocated[charger.entity_id] = 0

    # Pass 2: Allocate remainder by priority
    for charger in chargers_by_priority:
        if allocated.get(charger.entity_id, 0) == 0:
            charger.allocated_current = 0
            continue

        mask = charger.active_phases_mask
        if not mask:
            charger.allocated_current = allocated[charger.entity_id]
            continue

        charger_available = remaining.get_available(mask)
        wanted_additional = charger.max_current - allocated[charger.entity_id]
        additional = min(wanted_additional, charger_available)
        charger.allocated_current = allocated[charger.entity_id] + additional
        remaining = remaining.deduct(additional, mask)


def _distribute_per_phase_shared(site: SiteContext, constraints: PhaseConstraints) -> None:
    """
    SHARED mode: Pass 1 allocate minimum to all, Pass 2 split remainder equally.
    """
    remaining = constraints.copy()
    allocated = {}

    # Pass 1: Allocate minimum to all
    for charger in site.chargers:
        mask = charger.active_phases_mask
        if not mask:
            _LOGGER.error(f"Charger {charger.entity_id} has no phase mask!")
            allocated[charger.entity_id] = 0
            continue

        charger_available = remaining.get_available(mask)

        if charger_available >= charger.min_current:
            allocated[charger.entity_id] = charger.min_current
            remaining = remaining.deduct(charger.min_current, mask)
        else:
            allocated[charger.entity_id] = 0

    charging_chargers = [c for c in site.chargers if allocated.get(c.entity_id, 0) > 0]
    if not charging_chargers:
        for charger in site.chargers:
            charger.allocated_current = 0
        return

    # Pass 2: Split remainder equally among charging chargers
    while True:
        chargers_wanting_more = [c for c in charging_chargers if allocated[c.entity_id] < c.max_current]
        if not chargers_wanting_more:
            break

        min_available = min(remaining.get_available(c.active_phases_mask) for c in chargers_wanting_more)
        if min_available <= 0:
            break

        per_charger_increment = min_available / len(chargers_wanting_more)

        for charger in chargers_wanting_more:
            mask = charger.active_phases_mask
            additional = min(per_charger_increment, charger.max_current - allocated[charger.entity_id])
            allocated[charger.entity_id] += additional
            remaining = remaining.deduct(additional, mask)

    for charger in charging_chargers:
        charger.allocated_current = allocated[charger.entity_id]

    for charger in site.chargers:
        if charger not in charging_chargers:
            charger.allocated_current = 0


def _distribute_per_phase_strict(site: SiteContext, constraints: PhaseConstraints) -> None:
    """
    STRICT mode: Give first charger up to max, then next, etc. (by priority).
    """
    remaining = constraints.copy()
    chargers_by_priority = sorted(site.chargers, key=lambda c: c.priority)

    for charger in chargers_by_priority:
        mask = charger.active_phases_mask
        if not mask:
            _LOGGER.error(f"Charger {charger.entity_id} has no phase mask!")
            charger.allocated_current = 0
            continue

        charger_available = remaining.get_available(mask)
        charger.allocated_current = min(charger.max_current, charger_available)
        remaining = remaining.deduct(charger.allocated_current, mask)


def _distribute_per_phase_optimized(site: SiteContext, constraints: PhaseConstraints) -> None:
    """
    OPTIMIZED mode: Reduce higher priority chargers to allow lower priority to charge at minimum.
    """
    remaining = constraints.copy()
    chargers_by_priority = sorted(site.chargers, key=lambda c: c.priority)

    for i, charger in enumerate(chargers_by_priority):
        mask = charger.active_phases_mask
        if not mask:
            _LOGGER.error(f"Charger {charger.entity_id} has no phase mask!")
            charger.allocated_current = 0
            continue

        charger_available = remaining.get_available(mask)
        wanted = min(charger.max_current, charger_available)

        # Check if we should reduce to help next charger
        if i < len(chargers_by_priority) - 1:
            next_charger = chargers_by_priority[i + 1]
            next_mask = next_charger.active_phases_mask
            if next_mask:
                temp_remaining = remaining.deduct(wanted, mask)
                next_available = temp_remaining.get_available(next_mask)

                if next_available < next_charger.min_current:
                    reduction_needed = next_charger.min_current - next_available
                    can_reduce = max(0, wanted - charger.min_current)
                    wanted -= min(reduction_needed, can_reduce)

        charger.allocated_current = wanted
        remaining = remaining.deduct(wanted, mask)


