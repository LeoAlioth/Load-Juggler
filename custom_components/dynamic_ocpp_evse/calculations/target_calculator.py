"""
Target Calculator - Centralized calculation of charging targets for all chargers.

Clear architecture:
0. Refresh SiteContext (done externally)
1. Calculate absolute site limits (per-phase, prevents breaker trips)
2. Calculate solar available
3. Calculate excess available
4. Compute per-charger ceilings based on operating mode
5. Distribute power among chargers (dual-pool: physical + solar tracking)
"""

import logging

from .models import SiteContext, LoadContext, PhaseConstraints
from ..const import (
    OPERATING_MODE_STANDARD,
    OPERATING_MODE_CONTINUOUS,
    OPERATING_MODE_SOLAR_PRIORITY,
    OPERATING_MODE_SOLAR_ONLY,
    OPERATING_MODE_EXCESS,
    MODE_URGENCY,
)

_LOGGER = logging.getLogger(__name__)


def calculate_all_charger_targets(site: SiteContext) -> None:
    """
    Calculate allocated and available current for all chargers.

    Steps:
    0. Filter active chargers (with cars connected)
    1. Calculate absolute site limits (physical pool: grid + inverter)
    2. Calculate solar available power (solar pool)
    3. Calculate excess available power
    4. Distribute power among active chargers (dual-pool, per-charger ceilings)
    5. Calculate available current for all chargers

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
        f"Distribution: {site.distribution_mode}"
    )

    # Steps 1-3: Calculate pools (always, even with no active chargers)
    physical_pool = _calculate_site_limit(site)
    _LOGGER.debug(f"Step 1 - Physical pool (grid+inverter): {physical_pool}")

    solar_pool = _calculate_solar_surplus(site)
    _LOGGER.debug(f"Step 2 - Solar pool: {solar_pool}")

    excess_pool = _calculate_excess_available(site)
    _LOGGER.debug(f"Step 3 - Excess pool: {excess_pool}")

    # Step 4: Distribute power among active chargers only
    if active_chargers:
        site.chargers = active_chargers
        _distribute_power(site, physical_pool, solar_pool, excess_pool)
        site.chargers = all_chargers

    # Set inactive chargers to 0 allocated
    for charger in inactive_chargers:
        charger.allocated_current = 0

    # Step 5: Calculate available current for all chargers (mode-aware)
    _set_available_current_for_chargers(
        all_chargers, active_chargers, inactive_chargers,
        physical_pool, solar_pool, excess_pool, site,
    )

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
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
    site: SiteContext,
) -> None:
    """
    Calculate available current for all chargers (mode-aware).

    - Active chargers: available = allocated (they're getting what's available)
    - Inactive chargers: available = what they could get from remaining capacity
      after active chargers are deducted, capped by their mode's source limit.
      Each idle charger independently sees the same remaining pools.
    """
    # Active chargers: available = allocated (already rounded)
    for charger in active_chargers:
        charger.available_current = round(charger.allocated_current, 1)

    # Calculate remaining pools after active allocations
    remaining = physical_pool.copy()
    solar_rem = solar_pool.copy()
    excess_rem = excess_pool.copy()
    for charger in active_chargers:
        if charger.allocated_current > 0 and charger.active_phases_mask:
            remaining = remaining.deduct(charger.allocated_current, charger.active_phases_mask)
            solar_rem, excess_rem = _deduct_from_sources(
                charger.allocated_current, charger.active_phases_mask,
                solar_rem, excess_rem,
            )

    # Inactive chargers: mode-aware available (each independently sees remaining pools)
    for charger in inactive_chargers:
        mask = charger.active_phases_mask
        if not mask:
            charger.available_current = 0
            continue
        phys_avail = remaining.get_available(mask)
        src_max = _source_limit(charger, site, solar_rem, excess_rem, base=0)
        available = min(phys_avail, src_max)
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

    In derived mode (solar from grid CT): solar_production_total includes battery
    charge redirect (added by feedback loop). Only REMAINING discharge capacity
    is added here to avoid double-counting.

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
            # Derived mode: solar_production_total already includes battery charge
            # redirect (charge power added back in feedback loop). Only add the
            # remaining discharge capacity to avoid double-counting.
            # battery_power: positive=discharging, negative=charging
            actual_discharge = max(0, site.battery_power) / site.voltage
            max_discharge = site.battery_max_discharge_power / site.voltage
            battery_current = max(0, max_discharge - actual_discharge)
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

    Always includes grid + inverter (solar + battery when SOC >= min).
    Mode-specific limits are handled by per-charger ceilings, not by reducing
    the physical pool.
    """
    grid_constraints = _calculate_grid_limit(site)
    inverter_constraints = _calculate_inverter_limit(site)
    constraints = grid_constraints + inverter_constraints

    _LOGGER.debug(f"Site limit: grid={grid_constraints.ABC:.1f}A + "
                 f"inverter={inverter_constraints.ABC:.1f}A = "
                 f"total={constraints.ABC:.1f}A")

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


def _below_soc_target(site: SiteContext) -> bool:
    """Check if battery SOC is below target."""
    return (site.battery_soc is not None and site.battery_soc_target is not None
            and site.battery_soc < site.battery_soc_target)


def _source_limit(
    charger: LoadContext,
    site: SiteContext,
    solar: PhaseConstraints,
    excess: PhaseConstraints,
    base: float = 0,
) -> float:
    """Compute source-limited maximum allocation for a charger.

    Returns the maximum per-phase current this charger may receive based on its
    operating mode and available energy sources. Physical pool limits are applied
    separately by the caller.

    Args:
        base: Current already reserved in pass 1 (accounts for prior deductions
              from source pools so the ceiling includes the pass-1 allocation).
    """
    mask = charger.active_phases_mask
    mode = charger.operating_mode

    if mode in (OPERATING_MODE_STANDARD, OPERATING_MODE_CONTINUOUS):
        return charger.max_current

    if mode == OPERATING_MODE_SOLAR_PRIORITY:
        if _below_soc_target(site):
            return charger.min_current  # Grid-backed minimum only
        return max(charger.min_current, base + solar.get_available(mask))

    if mode == OPERATING_MODE_SOLAR_ONLY:
        if _below_soc_target(site):
            return 0  # Battery needs to charge
        return base + solar.get_available(mask)

    if mode == OPERATING_MODE_EXCESS:
        e_avail = excess.get_available(mask)
        if e_avail <= 0:
            return 0
        # Trigger: once excess exists, guarantee at least min_current
        return max(charger.min_current, base + e_avail)

    return charger.max_current


def _deduct_from_sources(
    current: float,
    mask: str,
    solar: PhaseConstraints,
    excess: PhaseConstraints,
) -> tuple[PhaseConstraints, PhaseConstraints]:
    """Deduct allocated current from source pools.

    ALL draws reduce both solar and excess pools because any power consumption
    reduces grid export, which reduces surplus available for other chargers.
    """
    s_avail = solar.get_available(mask)
    if s_avail > 0:
        solar = solar.deduct(min(current, s_avail), mask)
    e_avail = excess.get_available(mask)
    if e_avail > 0:
        excess = excess.deduct(min(current, e_avail), mask)
    return solar, excess


def _sort_chargers(chargers: list[LoadContext]) -> list[LoadContext]:
    """Sort chargers by (mode_urgency, priority) for distribution order."""
    return sorted(
        chargers,
        key=lambda c: (MODE_URGENCY.get(c.operating_mode, 0), c.priority),
    )


def _distribute_power(
    site: SiteContext,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
) -> None:
    """
    Step 4: Distribute power among chargers using source-aware pools.

    Three pools tracked simultaneously:
    - Physical pool: hard wire limits (grid + inverter). ALL allocations deduct.
    - Solar pool: surplus from renewables. ALL allocations deduct (any draw
      reduces export, shrinking the surplus available for other chargers).
    - Excess pool: surplus above threshold. ALL allocations deduct.

    Mode determines SOURCE LIMIT (max a charger may draw):
    - Standard/Continuous: physical pool only (any source)
    - Solar Priority: solar pool + grid minimum guarantee
    - Solar Only: solar pool only
    - Excess: excess pool + minimum guarantee when excess > 0
    """
    if not site.chargers:
        return

    _LOGGER.debug(f"Distribution — physical: {physical_pool}")
    _LOGGER.debug(f"Distribution — solar: {solar_pool}")
    _LOGGER.debug(f"Distribution — excess: {excess_pool}")

    for charger in site.chargers:
        _eff_ph = len(charger.active_phases_mask) if charger.active_phases_mask else 0
        _draw = charger.l1_current + charger.l2_current + charger.l3_current
        _LOGGER.debug(
            f"  {charger.entity_id}: mode={charger.operating_mode} "
            f"mask={charger.active_phases_mask}({_eff_ph}ph) "
            f"hw={charger.phases}ph {charger.min_current:.0f}-{charger.max_current:.0f}A "
            f"prio={charger.priority} [{charger.connector_status}] draw={_draw:.1f}A"
        )

    mode = site.distribution_mode.lower() if site.distribution_mode else "priority"

    if "priority" in mode:
        _distribute_per_phase_priority(site, physical_pool, solar_pool, excess_pool)
    elif "shared" in mode:
        _distribute_per_phase_shared(site, physical_pool, solar_pool, excess_pool)
    elif "strict" in mode:
        _distribute_per_phase_strict(site, physical_pool, solar_pool, excess_pool)
    elif "optimized" in mode:
        _distribute_per_phase_optimized(site, physical_pool, solar_pool, excess_pool)
    else:
        _LOGGER.warning(f"Unknown distribution mode '{mode}', using priority")
        _distribute_per_phase_priority(site, physical_pool, solar_pool, excess_pool)


def _allocate_minimums(
    chargers: list[LoadContext],
    site: SiteContext,
    physical: PhaseConstraints,
    solar: PhaseConstraints,
    excess: PhaseConstraints,
) -> tuple[dict[str, float], PhaseConstraints, PhaseConstraints, PhaseConstraints]:
    """Pass 1: Reserve minimum current for all eligible chargers.

    Source-aware: each mode checks its allowed energy sources.
    All allocations deduct from physical pool (wire limits apply to all).
    All allocations deduct from solar and excess pools (any draw reduces export).

    Returns (allocated dict, remaining physical, remaining solar, remaining excess).
    """
    allocated = {}
    for charger in chargers:
        mask = charger.active_phases_mask
        if not mask:
            allocated[charger.entity_id] = 0
            continue

        # Source limit: is this mode allowed to charge at all?
        src_max = _source_limit(charger, site, solar, excess, base=0)
        if src_max < charger.min_current:
            allocated[charger.entity_id] = 0
            continue

        # Physical pool must have room on the wire
        if physical.get_available(mask) < charger.min_current:
            allocated[charger.entity_id] = 0
            continue

        # Reserve minimum
        allocated[charger.entity_id] = charger.min_current
        physical = physical.deduct(charger.min_current, mask)
        solar, excess = _deduct_from_sources(charger.min_current, mask, solar, excess)

    return allocated, physical, solar, excess


def _distribute_per_phase_priority(
    site: SiteContext,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
) -> None:
    """
    PRIORITY mode: Pass 1 reserve minimums for all eligible chargers,
    Pass 2 fill remainder by urgency+priority order.

    Source-aware: each charger's fill-up is limited by its mode's source pool.
    All draws deduct from physical, solar, and excess pools.
    """
    sorted_chargers = _sort_chargers(site.chargers)

    # Pass 1: Reserve minimums (source-aware)
    remaining = physical_pool.copy()
    solar_rem = solar_pool.copy()
    excess_rem = excess_pool.copy()
    allocated, remaining, solar_rem, excess_rem = _allocate_minimums(
        sorted_chargers, site, remaining, solar_rem, excess_rem
    )

    for cid, alloc in allocated.items():
        _LOGGER.debug(f"  Pass 1: {cid} = {alloc:.1f}A")

    # Pass 2: Fill by priority order, source-limited
    for charger in sorted_chargers:
        base = allocated.get(charger.entity_id, 0)
        mask = charger.active_phases_mask
        if not mask or base == 0:
            charger.allocated_current = round(base, 1)
            continue

        phys_avail = remaining.get_available(mask)
        src_max = _source_limit(charger, site, solar_rem, excess_rem, base=base)
        effective_max = min(src_max, charger.max_current)
        additional = max(0, min(effective_max - base, phys_avail))
        total = base + additional

        charger.allocated_current = round(total, 1)
        if additional > 0:
            remaining = remaining.deduct(additional, mask)
            solar_rem, excess_rem = _deduct_from_sources(
                additional, mask, solar_rem, excess_rem
            )


def _distribute_per_phase_shared(
    site: SiteContext,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
) -> None:
    """
    SHARED mode: Pass 1 reserve minimums for all eligible chargers,
    Pass 2 split remainder equally among charging chargers.

    Source-aware: each charger's fill-up is limited by its mode's source pool.
    Equal split respects source ceilings — source-limited chargers cap early
    and the remainder goes to others in subsequent rounds.
    """
    sorted_chargers = _sort_chargers(site.chargers)

    # Pass 1: Reserve minimums (source-aware)
    remaining = physical_pool.copy()
    solar_rem = solar_pool.copy()
    excess_rem = excess_pool.copy()
    allocated, remaining, solar_rem, excess_rem = _allocate_minimums(
        sorted_chargers, site, remaining, solar_rem, excess_rem
    )

    charging_chargers = [c for c in sorted_chargers if allocated.get(c.entity_id, 0) > 0]
    if not charging_chargers:
        for charger in site.chargers:
            charger.allocated_current = 0
        return

    # Pass 2: Split remainder equally, respecting source limits.
    # Batch compute increments to avoid order-dependent solar depletion.
    while True:
        chargers_wanting_more = []
        for c in charging_chargers:
            src_max = _source_limit(c, site, solar_rem, excess_rem, base=allocated[c.entity_id])
            effective_max = min(c.max_current, src_max)
            if allocated[c.entity_id] < effective_max:
                chargers_wanting_more.append(c)

        if not chargers_wanting_more:
            break

        min_available = min(
            remaining.get_available(c.active_phases_mask) for c in chargers_wanting_more
        )
        if min_available <= 0:
            break

        per_charger_increment = min_available / len(chargers_wanting_more)

        # Batch: compute all increments against current pool state
        batch = []
        for charger in chargers_wanting_more:
            mask = charger.active_phases_mask
            src_max = _source_limit(charger, site, solar_rem, excess_rem, base=allocated[charger.entity_id])
            effective_max = min(charger.max_current, src_max)
            additional = min(per_charger_increment, effective_max - allocated[charger.entity_id])
            additional = max(0, additional)
            batch.append((charger, mask, additional))

        # Check total solar consumption doesn't exceed available.
        # For source-limited chargers sharing the same phases, scale down if needed.
        total_increment = sum(incr for _, _, incr in batch if incr > 0)
        if total_increment > 0:
            min_solar = min(solar_rem.get_available(c.active_phases_mask) for c in chargers_wanting_more)
            if total_increment > min_solar > 0:
                scale = min_solar / total_increment
                batch = [(c, m, incr * scale) for c, m, incr in batch]

        # Apply all increments
        any_progress = False
        for charger, mask, additional in batch:
            if additional > 0.001:
                allocated[charger.entity_id] += additional
                remaining = remaining.deduct(additional, mask)
                solar_rem, excess_rem = _deduct_from_sources(
                    additional, mask, solar_rem, excess_rem
                )
                any_progress = True

        if not any_progress:
            break

    for charger in charging_chargers:
        charger.allocated_current = round(allocated[charger.entity_id], 1)

    for charger in site.chargers:
        if charger not in charging_chargers:
            charger.allocated_current = 0


def _distribute_per_phase_strict(
    site: SiteContext,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
) -> None:
    """
    STRICT mode: Give first charger up to max (or source limit), then next, etc.
    Sorted by (urgency, priority). No minimum reservation — sequential greedy.
    """
    remaining = physical_pool.copy()
    solar_rem = solar_pool.copy()
    excess_rem = excess_pool.copy()
    sorted_chargers = _sort_chargers(site.chargers)

    for charger in sorted_chargers:
        mask = charger.active_phases_mask
        if not mask:
            charger.allocated_current = 0
            continue

        src_max = _source_limit(charger, site, solar_rem, excess_rem, base=0)
        phys_avail = remaining.get_available(mask)
        allocation = round(min(charger.max_current, src_max, phys_avail), 1)

        if allocation < charger.min_current:
            charger.allocated_current = 0
            continue

        charger.allocated_current = allocation
        remaining = remaining.deduct(allocation, mask)
        solar_rem, excess_rem = _deduct_from_sources(
            allocation, mask, solar_rem, excess_rem
        )


def _distribute_per_phase_optimized(
    site: SiteContext,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
) -> None:
    """
    OPTIMIZED mode: Reduce higher priority chargers to allow lower priority
    to charge at minimum. Sorted by (urgency, priority). Source-aware.
    """
    remaining = physical_pool.copy()
    solar_rem = solar_pool.copy()
    excess_rem = excess_pool.copy()
    sorted_chargers = _sort_chargers(site.chargers)

    for i, charger in enumerate(sorted_chargers):
        mask = charger.active_phases_mask
        if not mask:
            charger.allocated_current = 0
            continue

        src_max = _source_limit(charger, site, solar_rem, excess_rem, base=0)
        if src_max < charger.min_current:
            charger.allocated_current = 0
            continue

        phys_avail = remaining.get_available(mask)
        wanted = min(charger.max_current, src_max, phys_avail)

        # Check if we should reduce to help next charger
        if i < len(sorted_chargers) - 1:
            next_charger = sorted_chargers[i + 1]
            next_mask = next_charger.active_phases_mask
            if next_mask:
                # Pre-check: does next charger have source potential before our draw?
                pre_src = _source_limit(next_charger, site, solar_rem, excess_rem, base=0)
                if pre_src >= next_charger.min_current:
                    # Simulate full deduction (physical + sources)
                    temp_remaining = remaining.deduct(wanted, mask)
                    temp_solar, temp_excess = _deduct_from_sources(
                        wanted, mask, solar_rem, excess_rem
                    )
                    next_phys = temp_remaining.get_available(next_mask)
                    next_src = _source_limit(
                        next_charger, site, temp_solar, temp_excess, base=0
                    )
                    next_effective = min(next_phys, next_src)
                    if next_effective < next_charger.min_current:
                        reduction_needed = next_charger.min_current - next_effective
                        can_reduce = max(0, wanted - charger.min_current)
                        wanted -= min(reduction_needed, can_reduce)

        if wanted < charger.min_current:
            charger.allocated_current = 0
            continue

        charger.allocated_current = round(wanted, 1)
        remaining = remaining.deduct(charger.allocated_current, mask)
        solar_rem, excess_rem = _deduct_from_sources(
            charger.allocated_current, mask, solar_rem, excess_rem
        )


