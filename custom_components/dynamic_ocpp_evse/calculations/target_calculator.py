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
                       if c.connector_status not in [
                           "Available", "Unknown", "Unavailable",
                           "Finishing", "Faulted",
                       ]]
    inactive_chargers = [c for c in all_chargers if c not in active_chargers]

    _LOGGER.debug(
        f"Calculating targets for {len(active_chargers)}/{len(all_chargers)} active chargers - "
        f"Mode: {site.charging_mode}, Distribution: {site.distribution_mode}"
    )

    # Steps 1-4: Calculate constraints (always, even with no active chargers)
    site_limit_constraints = _calculate_site_limit(site)
    _LOGGER.debug(f"Step 1 - Site limit constraints: {site_limit_constraints}")

    solar_constraints = _calculate_solar_surplus(site)
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
    _set_available_current_for_chargers(all_chargers, active_chargers, inactive_chargers, target_constraints)

    for charger in all_chargers:
        _draw = charger.l1_current + charger.l2_current + charger.l3_current
        _LOGGER.debug(
            f"Final -- {charger.entity_id}: allocated={charger.allocated_current:.1f}A "
            f"available={charger.available_current:.1f}A | "
            f"draw={_draw:.1f}A (L1:{charger.l1_current:.1f} L2:{charger.l2_current:.1f} L3:{charger.l3_current:.1f})"
        )


def _set_available_current_for_chargers(
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
    # Active chargers: available = allocated (already rounded)
    for charger in active_chargers:
        charger.available_current = round(charger.allocated_current, 1)

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
            charger.available_current = round(min(charger.max_current, available), 1)
        else:
            charger.available_current = 0


def _calculate_grid_limit(site: SiteContext) -> PhaseConstraints:
    """
    Calculate grid power limit based on main breaker rating and consumption.

    Grid power is per-phase and CANNOT be reallocated between phases.
    """
    # Calculate per-phase limits (only for phases that physically exist)
    phase_a_limit = max(0, site.main_breaker_rating - site.consumption.a) if site.consumption.a is not None else 0
    phase_b_limit = max(0, site.main_breaker_rating - site.consumption.b) if site.consumption.b is not None else 0
    phase_c_limit = max(0, site.main_breaker_rating - site.consumption.c) if site.consumption.c is not None else 0

    # If grid charging not allowed (and has battery), limited to export only
    if not site.allow_grid_charging and site.battery_soc is not None:
        if site.export_current.a is not None:
            phase_a_limit = min(phase_a_limit, site.export_current.a)
        if site.export_current.b is not None:
            phase_b_limit = min(phase_b_limit, site.export_current.b)
        if site.export_current.c is not None:
            phase_c_limit = min(phase_c_limit, site.export_current.c)

    constraints = PhaseConstraints.from_per_phase(phase_a_limit, phase_b_limit, phase_c_limit)

    # Apply max grid import power limit (if configured)
    # This is a total (all-phase) constraint from the grid operator / smart meter.
    # Power buffer has already been subtracted before reaching SiteContext.
    # Applied as a cap on combination fields (ABC, AB, AC, BC) — NOT by scaling
    # per-phase limits, which would be overly conservative for multi-phase chargers.
    if site.max_grid_import_power is not None:
        total_consumption = site.consumption.total
        max_import_current = site.max_grid_import_power / site.voltage
        available_for_evs = max(0, max_import_current - total_consumption)
        constraints.ABC = min(constraints.ABC, available_for_evs)
        constraints = constraints.normalize()

    return constraints


def _get_household_per_phase(site: SiteContext) -> tuple[float, float, float]:
    """Get per-phase household consumption in Amps using best available data.

    Data hierarchy (best → worst):
    1. Per-phase household_consumption (from per-phase inverter output entities) — exact
    2. household_consumption_total (from single solar entity) — uniform estimate
    3. consumption from grid CT — visible only when site is importing, 0 when self-consuming
    """
    if site.household_consumption is not None:
        return (
            site.household_consumption.a or 0,
            site.household_consumption.b or 0,
            site.household_consumption.c or 0,
        )
    if site.household_consumption_total is not None:
        uniform = (site.household_consumption_total / site.voltage) / (site.num_phases or 1)
        return (
            uniform if site.consumption.a is not None else 0,
            uniform if site.consumption.b is not None else 0,
            uniform if site.consumption.c is not None else 0,
        )
    return (
        site.consumption.a or 0,
        site.consumption.b or 0,
        site.consumption.c or 0,
    )


def _build_inverter_constraints(site: SiteContext, total_pool: float) -> PhaseConstraints:
    """Build PhaseConstraints for inverter-limited power (solar/battery/excess).

    For ASYMMETRIC inverters: power is a flexible pool, per-phase capped by
    inverter_max_power_per_phase minus household.
    For SYMMETRIC inverters: power is fixed per-phase (total_pool / num_phases),
    capped by inverter_max_power_per_phase.
    """
    max_per_phase = site.inverter_max_power_per_phase / site.voltage if site.inverter_max_power_per_phase else float('inf')
    if site.inverter_supports_asymmetric:
        hh_a, hh_b, hh_c = _get_household_per_phase(site)
        phase_a = min(total_pool, max(0, max_per_phase - hh_a)) if site.consumption.a is not None else 0
        phase_b = min(total_pool, max(0, max_per_phase - hh_b)) if site.consumption.b is not None else 0
        phase_c = min(total_pool, max(0, max_per_phase - hh_c)) if site.consumption.c is not None else 0
        return PhaseConstraints.from_pool(phase_a, phase_b, phase_c, total_pool)
    else:
        per_phase = total_pool / site.num_phases
        phase_a = min(per_phase, max_per_phase) if site.consumption.a is not None else 0
        phase_b = min(per_phase, max_per_phase) if site.consumption.b is not None else 0
        phase_c = min(per_phase, max_per_phase) if site.consumption.c is not None else 0
        return PhaseConstraints.from_per_phase(phase_a, phase_b, phase_c)


def _calculate_inverter_limit(site: SiteContext) -> PhaseConstraints:
    """
    Calculate inverter power limit (solar + battery for Standard mode).

    Returns PhaseConstraints for ALL phase combinations.
    Solar and battery share the same inverter, so per-phase and total inverter limits
    apply to their combined output.

    Battery discharge is added when SOC >= battery_soc_min.

    In derived mode (solar from grid CT): solar_current already reflects battery
    effects in the grid readings. Adds REMAINING discharge capacity to avoid
    double-counting.

    With dedicated solar entity: solar_current is the raw inverter output.
    battery_power may not be available or embedded, so use full max_discharge.

    For ASYMMETRIC inverters: Solar+battery power can be allocated to any phase.
    For SYMMETRIC inverters: Solar+battery power is fixed per-phase.
    """
    # Calculate solar current
    solar_current = site.solar_production_total / site.voltage if site.solar_production_total else 0

    # Calculate battery discharge current (if available)
    battery_current = 0
    if (site.battery_soc is not None and
        site.battery_soc >= (site.battery_soc_min or 0) and
        site.battery_max_discharge_power):
        if site.solar_is_derived and site.battery_power is not None:
            # Derived mode: solar_current includes actual battery effect.
            # Add only the REMAINING discharge capacity.
            # battery_power: positive=discharging, negative=charging
            actual_effect = site.battery_power / site.voltage  # positive if already discharging
            max_discharge = site.battery_max_discharge_power / site.voltage
            battery_current = max(0, max_discharge - actual_effect)
        elif not site.solar_is_derived:
            # Dedicated solar entity: solar_current is raw inverter output,
            # battery effect not embedded. Use full max discharge.
            battery_current = site.battery_max_discharge_power / site.voltage

    # Total inverter output (solar + battery)
    total_inverter_current = solar_current + battery_current

    if total_inverter_current == 0:
        return PhaseConstraints.zeros()

    constraints = _build_inverter_constraints(site, total_inverter_current)

    # Apply total inverter power limit if configured.
    # Cap combination fields (not per-phase) — same principle as grid limit.
    if site.inverter_max_power:
        max_total_current = site.inverter_max_power / site.voltage
        constraints.ABC = min(constraints.ABC, max_total_current)
        constraints = constraints.normalize()

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


def _calculate_solar_surplus(site: SiteContext) -> PhaseConstraints:
    """
    Step 2: Calculate solar available power.

    Returns PhaseConstraints for ALL phase combinations.

    Export current IS the measured surplus per phase (derived from grid CT).
    If battery_power data is available and battery is charging, add it back
    to surplus — self-consumption hides this solar power from the grid CT.

    For ASYMMETRIC inverters: Solar/battery power is a flexible pool.
    For SYMMETRIC inverters: Solar/battery power is fixed per-phase.
    """
    # Export current IS the solar surplus per phase.
    # No consumption subtraction needed (export is already net).
    #
    # Battery awareness (self-consumption systems):
    # 1. Battery CHARGE hides surplus from export — add it back.
    #    (solar power absorbed by battery is available if charger draws instead)
    # 2. Battery DISCHARGE potential when SOC > target — add remaining capacity.
    #    (self-consumption keeps battery idle unless there's demand, but the
    #    charger CAN create that demand, making the discharge available)
    #
    # Inverter headroom constraint on discharge:
    #    Battery discharge goes through the inverter. If solar already maxes out
    #    the inverter, there's no room for additional battery discharge.
    #    base_pool (export + charge_back) ≈ solar - household.
    #    estimated_solar ≈ base_pool + household.
    #    Discharge headroom = inverter_max - estimated_solar.
    charge_back = 0
    discharge_potential = 0

    if site.battery_power is not None:
        # Charge absorption: battery_power < 0 = charging
        if site.battery_power < 0:
            charge_back = abs(site.battery_power) / site.voltage
        # Discharge potential: unused discharge capacity when SOC > target
        if (site.battery_soc is not None and site.battery_soc_target is not None and
                site.battery_soc > site.battery_soc_target and
                site.battery_max_discharge_power):
            actual_discharge = max(0, site.battery_power) / site.voltage
            max_discharge = site.battery_max_discharge_power / site.voltage
            discharge_potential = max(0, max_discharge - actual_discharge)

    # Limit discharge by inverter headroom
    if site.inverter_max_power and discharge_potential > 0:
        inverter_max_current = site.inverter_max_power / site.voltage
        if site.household_consumption_total is not None:
            # Accurate: solar entity provides true household consumption
            estimated_solar = site.solar_production_total / site.voltage
        else:
            # Estimate from CT readings (derived mode)
            export_total = site.export_current.total if site.export_current else 0
            base_pool = export_total + charge_back
            household = site.consumption.total or 0
            estimated_solar = base_pool + household
        inverter_headroom = max(0, inverter_max_current - estimated_solar)
        discharge_potential = min(discharge_potential, inverter_headroom)

    battery_adjustment_total = charge_back + discharge_potential

    battery_adjustment_per_phase = battery_adjustment_total / (
        site.export_current.active_count or site.consumption.active_count or 1
    ) if battery_adjustment_total else 0

    max_per_phase = site.inverter_max_power_per_phase / site.voltage if site.inverter_max_power_per_phase else float('inf')

    if site.inverter_supports_asymmetric:
        total_pool = (site.export_current.total if site.export_current else 0) + battery_adjustment_total
        constraints = _build_inverter_constraints(site, total_pool)
    else:
        # Symmetric: per-phase export + battery adjustment = per-phase surplus
        phase_a_available = min((site.export_current.a or 0) + battery_adjustment_per_phase, max_per_phase) if site.export_current.a is not None else 0
        phase_b_available = min((site.export_current.b or 0) + battery_adjustment_per_phase, max_per_phase) if site.export_current.b is not None else 0
        phase_c_available = min((site.export_current.c or 0) + battery_adjustment_per_phase, max_per_phase) if site.export_current.c is not None else 0
        constraints = PhaseConstraints.from_per_phase(phase_a_available, phase_b_available, phase_c_available)

    # Apply total inverter limit if configured, accounting for household.
    # Cap combination fields (not per-phase) — same principle as grid limit.
    if site.inverter_max_power:
        max_total = site.inverter_max_power / site.voltage
        household = sum(_get_household_per_phase(site))
        max_for_chargers = max(0, max_total - household)
        constraints.ABC = min(constraints.ABC, max_for_chargers)
        constraints = constraints.normalize()

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

        constraints = _build_inverter_constraints(site, total_available)

        _LOGGER.debug(f"Excess available constraints ({'asymmetric' if site.inverter_supports_asymmetric else 'symmetric'}): {constraints}")
        return constraints

    return PhaseConstraints.zeros()


def _calculate_active_minimums(site: SiteContext) -> PhaseConstraints:
    """Calculate PhaseConstraints for the sum of minimum charge rates of active chargers."""
    active = [c for c in site.chargers
              if c.connector_status not in (
                  "Available", "Unknown", "Unavailable",
                  "Finishing", "Faulted",
              )]
    sum_minimums_total = sum(c.min_current * c.phases for c in active)
    sum_minimums_per_phase = sum_minimums_total / site.num_phases
    return PhaseConstraints.from_per_phase(
        sum_minimums_per_phase if site.consumption.a is not None else 0,
        sum_minimums_per_phase if site.consumption.b is not None else 0,
        sum_minimums_per_phase if site.consumption.c is not None else 0,
    )


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
        minimums = _calculate_active_minimums(site)

        # Battery between min and target - charge at minimum only
        if site.battery_soc is not None and site.battery_soc_target is not None and site.battery_soc < site.battery_soc_target:
            return minimums.element_min(site_limit_constraints)
        else:
            # Battery >= target or no battery - use max of solar and minimums, capped at site limit
            target = solar_constraints.element_max(minimums)
            return target.element_min(site_limit_constraints)

    elif mode == CHARGING_MODE_SOLAR:
        if site.battery_soc is not None and site.battery_soc_target is not None and site.battery_soc < site.battery_soc_target:
            return PhaseConstraints.zeros()
        return solar_constraints

    elif mode == CHARGING_MODE_EXCESS:
        # If there's any excess over threshold, guarantee at least min_current
        # to avoid wasting export when inverter would otherwise throttle
        if excess_constraints.ABC > 0:
            minimums = _calculate_active_minimums(site)
            target = excess_constraints.element_max(minimums)
            return target.element_min(site_limit_constraints)
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
        _eff_ph = len(charger.active_phases_mask) if charger.active_phases_mask else 0
        _draw = charger.l1_current + charger.l2_current + charger.l3_current
        _LOGGER.debug(
            f"  {charger.entity_id}: mask={charger.active_phases_mask}({_eff_ph}ph) "
            f"hw={charger.phases}ph {charger.min_current:.0f}-{charger.max_current:.0f}A "
            f"prio={charger.priority} [{charger.connector_status}] draw={_draw:.1f}A"
        )

    mode = site.distribution_mode.lower() if site.distribution_mode else "priority"

    if "priority" in mode:
        _distribute_per_phase_priority(site, target_constraints)
    elif "shared" in mode:
        _distribute_per_phase_shared(site, target_constraints)
    elif "strict" in mode:
        _distribute_per_phase_strict(site, target_constraints)
    elif "optimized" in mode:
        _distribute_per_phase_optimized(site, target_constraints)
    else:
        _LOGGER.warning(f"Unknown distribution mode '{mode}', using priority")
        _distribute_per_phase_priority(site, target_constraints)


def _allocate_minimums(
    chargers: list[ChargerContext], remaining: PhaseConstraints
) -> tuple[dict[str, float], PhaseConstraints]:
    """Allocate minimum current to chargers that fit. Returns (allocated dict, remaining constraints)."""
    allocated = {}
    for charger in chargers:
        mask = charger.active_phases_mask
        if not mask:
            _LOGGER.error(f"Charger {charger.entity_id} has no phase mask!")
            allocated[charger.entity_id] = 0
            continue

        if remaining.get_available(mask) >= charger.min_current:
            allocated[charger.entity_id] = charger.min_current
            remaining = remaining.deduct(charger.min_current, mask)
        else:
            allocated[charger.entity_id] = 0

    return allocated, remaining


def _distribute_per_phase_priority(site: SiteContext, constraints: PhaseConstraints) -> None:
    """
    PRIORITY mode: Pass 1 allocate minimum by priority, Pass 2 give remainder by priority.
    """
    chargers_by_priority = sorted(site.chargers, key=lambda c: c.priority)
    allocated, remaining = _allocate_minimums(chargers_by_priority, constraints.copy())

    # Pass 2: Allocate remainder by priority
    for charger in chargers_by_priority:
        if allocated.get(charger.entity_id, 0) == 0:
            charger.allocated_current = 0
            continue

        mask = charger.active_phases_mask
        if not mask:
            charger.allocated_current = round(allocated[charger.entity_id], 1)
            continue

        charger_available = remaining.get_available(mask)
        wanted_additional = charger.max_current - allocated[charger.entity_id]
        additional = min(wanted_additional, charger_available)
        charger.allocated_current = round(allocated[charger.entity_id] + additional, 1)
        remaining = remaining.deduct(additional, mask)


def _distribute_per_phase_shared(site: SiteContext, constraints: PhaseConstraints) -> None:
    """
    SHARED mode: Pass 1 allocate minimum to all, Pass 2 split remainder equally.
    """
    allocated, remaining = _allocate_minimums(site.chargers, constraints.copy())

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
        charger.allocated_current = round(allocated[charger.entity_id], 1)

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
        charger.allocated_current = round(min(charger.max_current, charger_available), 1)
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

        charger.allocated_current = round(wanted, 1)
        remaining = remaining.deduct(charger.allocated_current, mask)


