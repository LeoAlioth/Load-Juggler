"""
Target Calculator - Centralized calculation of charging targets for all loads.

Clear architecture:
0. Refresh SiteContext (done externally)
1. Calculate absolute site limits (per-phase, prevents breaker trips)
2. Calculate solar available
3. Calculate excess available
4. Compute per-load ceilings based on operating mode
5. Distribute power among loads (dual-pool: physical + solar tracking)
6. Enforce circuit group limits (post-distribution capping)
"""

import logging

from .models import (
    INACTIVE_STATUSES,
    SiteContext,
    LoadContext,
    PhaseConstraints,
    CircuitGroup,
)
from ..const import (
    BEHAVIOR_FULL_POWER,
    BEHAVIOR_SOLAR_PRIORITY,
    BEHAVIOR_SOLAR_ONLY,
    BEHAVIOR_EXCESS,
    BEHAVIOR_BINARY_ABOVE_MIN,
    BEHAVIOR_BINARY_ABOVE_TARGET,
    BEHAVIOR_BINARY_EXCESS,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_PLUG,
)

_LOGGER = logging.getLogger(__name__)

# Behaviors whose fill-up is bounded by a surplus pool, grouped by the pool that
# bounds them. Used by the shared-mode round to cap each source's group against
# its own pool. Binary behaviors are deliberately absent — see
# _scale_source_increments.
_SOLAR_BOUND_BEHAVIORS = frozenset({BEHAVIOR_SOLAR_PRIORITY, BEHAVIOR_SOLAR_ONLY})
_EXCESS_BOUND_BEHAVIORS = frozenset({BEHAVIOR_EXCESS})


def _measured_draw(load: LoadContext) -> float:
    """The load's real per-phase draw — the max across its occupied phases."""
    return max(load.l1_current, load.l2_current, load.l3_current)


def _pool_deduction(load: LoadContext, fallback: float) -> float:
    """The current a load removes from the shared pools — its footprint.

    Premise: pools are reduced by the load's real draw, not by the permit
    reserved for it. A plug or tank removes its measured draw — which the
    builder placed into l1/l2/l3 (the metered value, its set power when
    unmetered, or 0 when off) — regardless of the rating reserved for it.

    An EVSE is footprint-accounted only once its draw has *settled* — held
    steady for several cycles, meaning the car has reached a ceiling below
    what we offered. A 32 A EVSE feeding a car that holds at 16 A then frees
    the other 16 A to lower-priority loads. While the draw is still moving it
    is merely following our ramping permit (not a real ceiling), and an
    unmetered EVSE has no draw at all — both fall back to ``fallback``, the
    reserved current.
    """
    if load.device_type == DEVICE_TYPE_EVSE:
        if load.unmetered or not load.draw_settled:
            return fallback
        return _measured_draw(load)
    return _measured_draw(load)


def calculate_all_load_targets(site: SiteContext) -> None:
    """
    Calculate allocated and available current for all loads.

    Steps:
    0. Filter active loads (with cars connected)
    1. Calculate absolute site limits (physical pool: grid + inverter)
    2. Calculate solar available power (solar pool)
    3. Calculate excess available power
    4. Distribute power among active loads (dual-pool, per-load ceilings)
    5. Calculate available current for all loads

    Args:
        site: SiteContext containing all site and load data
    """
    # Step 0: Filter active vs inactive loads
    # SuspendedEVSE = the charger is throttling (our profile active), still active.
    # SuspendedEV idle timeout is handled in the HA layer (dynamic_ocpp_evse.py),
    # which overrides connector_status to "Finishing" after the grace period.
    # An EVSE receives power only with a car connected; a hot water tank only
    # while its thermostat is calling for heat (the HA layer reports connector
    # status "Available" when the climate's hvac_action is "idle"). Both are
    # inactive otherwise — they get 0 allocated, but still see an available
    # current so the HA layer can permit them to switch back on. A plug has no
    # connector and is always active: an off plug reports "Available", and
    # treating that as inactive would leave it stuck off forever.
    #
    # The membership itself lives in models.INACTIVE_STATUSES, because the
    # publisher asks the same question of a load whose power monitor cannot be
    # read (engine/hub_result.py) — without the plug carve-out, which is a
    # distribution rule rather than a statement about drawing power.
    all_loads = site.loads
    active_loads = [
        c for c in all_loads
        if c.device_type == DEVICE_TYPE_PLUG
        or c.connector_status not in INACTIVE_STATUSES
    ]
    inactive_loads = [
        c for c in all_loads
        if c.device_type != DEVICE_TYPE_PLUG
        and c.connector_status in INACTIVE_STATUSES
    ]

    _mode_summary = ", ".join(
        f"{c.entity_id}={c.operating_mode}" for c in active_loads
    ) if active_loads else "none"
    _LOGGER.debug(
        f"Calculating targets for {len(active_loads)}/{len(all_loads)} active loads - "
        f"Distribution: {site.distribution_mode} | Modes: {_mode_summary}"
    )

    # Steps 1-3: Calculate pools (always, even with no active loads)
    physical_pool = _calculate_site_limit(site)
    _LOGGER.debug(f"Step 1 - Physical pool (grid+inverter): {physical_pool}")

    solar_pool = _calculate_solar_surplus(site)
    _LOGGER.debug(f"Step 2 - Solar pool: {solar_pool}")

    excess_pool = _calculate_excess_available(site)
    _LOGGER.debug(f"Step 3 - Excess pool: {excess_pool}")

    # Step 4: Distribute power among active loads only.
    # site.loads is temporarily narrowed to the active set; the try/finally
    # guarantees it is restored even if _distribute_power raises, so downstream
    # steps (circuit groups, hub result) still see every load.
    if active_loads:
        site.loads = active_loads
        try:
            _distribute_power(site, physical_pool, solar_pool, excess_pool)
        finally:
            site.loads = all_loads

    # Set inactive loads to 0 allocated
    for load in inactive_loads:
        load.allocated_current = 0

    # Step 6: Enforce circuit group limits (post-distribution capping)
    if site.circuit_groups:
        _enforce_circuit_groups(site)

    # Step 5: Calculate available current for all loads (the permit ceiling)
    _set_available_current_for_loads(
        all_loads, active_loads, inactive_loads,
        physical_pool, solar_pool, excess_pool, site,
    )

    # Step 7: Translate allocated_current to the real footprint — the measured
    # draw (or set power) the load removes from the pools, not the rating
    # reserved for it. available_current (the permit) was already captured by
    # _set_available_current_for_loads above. A ramping or unmetered EVSE
    # has no trustworthy draw; _pool_deduction leaves it at the signalled
    # current.
    for load in active_loads:
        if load.allocated_current > 0:
            load.allocated_current = round(
                _pool_deduction(load, load.allocated_current), 1
            )

    for load in all_loads:
        _draw = load.l1_current + load.l2_current + load.l3_current
        _LOGGER.debug(
            f"Final -- {load.entity_id} [{load.operating_mode}]: "
            f"allocated={load.allocated_current:.1f}A "
            f"available={load.available_current:.1f}A | "
            f"draw={_draw:.1f}A (L1:{load.l1_current:.1f} L2:{load.l2_current:.1f} L3:{load.l3_current:.1f})"
        )


def _set_available_current_for_loads(
    all_loads: list,
    active_loads: list,
    inactive_loads: list,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
    site: SiteContext,
) -> None:
    """
    Set available_current — the permit ceiling — for every load.

    available_current is what the device *could* draw: the pool headroom
    capped by the device's hardware rating. It is informational, computed
    per-device, and may sum to more than the pool.

    - EVSE: the current it was signalled (its allocated_current).
    - Plug / tank the engine powered: the pool headroom left after
      higher-priority loads' real footprints, capped by the hardware rating.
      0 when the engine did not power it.
    - Inactive load: what it could get from the leftover capacity.

    Pools are reduced by each active load's footprint (real draw), per the
    allocated-current premise.
    """
    remaining = physical_pool.copy()
    solar_rem = solar_pool.copy()
    excess_rem = excess_pool.copy()

    # Active loads, in distribution order.
    for load in _sort_loads(active_loads):
        mask = load.active_phases_mask
        if load.device_type == DEVICE_TYPE_EVSE:
            # EVSE: available_current is the signalled current.
            load.available_current = round(load.allocated_current, 1)
        elif mask and load.allocated_current > 0:
            # Plug / tank the engine powered: pool headroom, capped by the
            # device's hardware rating.
            cap = load.rated_current or load.max_current
            load.available_current = round(
                max(0, min(remaining.get_available(mask), cap)), 1
            )
        else:
            load.available_current = 0
        # Reduce the pools by this load's real footprint before the next.
        footprint = _pool_deduction(load, load.allocated_current)
        if footprint > 0 and mask:
            remaining = remaining.deduct(footprint, mask)
            solar_rem, excess_rem = _deduct_from_sources(
                footprint, mask, solar_rem, excess_rem
            )

    # Inactive loads: what they could get from the leftover capacity.
    for load in inactive_loads:
        mask = load.active_phases_mask
        if not mask:
            load.available_current = 0
            continue
        phys_avail = remaining.get_available(mask)
        src_max = _source_limit(load, site, solar_rem, excess_rem, base=0)
        available = min(phys_avail, src_max)
        if available >= load.min_current:
            load.available_current = round(min(load.max_current, available), 1)
        else:
            load.available_current = 0


def _enforce_circuit_groups(site: SiteContext) -> None:
    """Enforce circuit group breaker limits (post-distribution capping).

    For each group, builds a PhaseConstraints pool from the group's current limit
    and walks members in priority order (highest urgency+priority first).
    Higher-priority loads keep their allocation; lower-priority loads get capped.
    """
    load_by_id = {c.load_id: c for c in site.loads}

    for group in site.circuit_groups:
        members = [load_by_id[mid] for mid in group.member_ids if mid in load_by_id]
        if not members:
            continue

        # Build group budget — per-phase limit on every phase the group's
        # members occupy. The group breaker limit is a property of the group's
        # wiring, independent of which site phases happen to have CT metering.
        group_phases = set()
        for m in members:
            if m.active_phases_mask:
                group_phases.update(m.active_phases_mask)
        limit = group.current_limit
        a = limit if "A" in group_phases else 0
        b = limit if "B" in group_phases else 0
        c = limit if "C" in group_phases else 0
        group_pool = PhaseConstraints.from_per_phase(a, b, c)

        # Walk members in priority order (highest urgency+priority first → keeps allocation)
        sorted_members = _sort_loads(members)

        capped_any = False
        for load in sorted_members:
            mask = load.active_phases_mask
            if not mask or load.allocated_current == 0:
                continue

            avail = group_pool.get_available(mask)
            original = load.allocated_current
            capped = min(original, avail)

            if capped < load.min_current:
                capped = 0

            if capped != original:
                capped_any = True
                _LOGGER.debug(
                    "Circuit group '%s': %s capped %.1fA → %.1fA (group limit %.0fA)",
                    group.name, load.entity_id, original, capped, group.current_limit,
                )

            load.allocated_current = round(capped, 1)
            if capped > 0:
                group_pool = group_pool.deduct(capped, mask)

        if not capped_any:
            _LOGGER.debug("Circuit group '%s': all members within %.0fA limit", group.name, group.current_limit)


def _calculate_grid_limit(site: SiteContext) -> PhaseConstraints:
    """
    Calculate grid power limit based on main breaker rating and consumption.

    Grid power is per-phase and CANNOT be reallocated between phases.
    """
    # Off-grid: there is no grid feed, so the main breaker rating must not be
    # turned into phantom headroom (consumption reads 0 without grid CTs).
    # All power comes through the inverter pool.
    if site.is_off_grid:
        return PhaseConstraints.zeros()

    # Power buffer (W) is a safety margin kept unused on the grid. Spread it
    # across the phases as an extra per-phase deduction so it is honored on the
    # main-breaker limit even when no max_grid_import_power is configured.
    buffer_per_phase = 0.0
    if site.power_buffer and site.power_buffer > 0:
        buffer_per_phase = (site.power_buffer / site.voltage) / (site.num_phases or 1)

    # Calculate per-phase limits (only for phases that physically exist)
    phase_a_limit = max(0, site.main_breaker_rating - site.consumption.a - buffer_per_phase) if site.consumption.a is not None else 0
    phase_b_limit = max(0, site.main_breaker_rating - site.consumption.b - buffer_per_phase) if site.consumption.b is not None else 0
    phase_c_limit = max(0, site.main_breaker_rating - site.consumption.c - buffer_per_phase) if site.consumption.c is not None else 0

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
    # The power buffer is already subtracted from max_grid_import_power upstream
    # (in run_hub_calculation) and from the per-phase breaker limits above.
    # Applied as a cap on combination fields (ABC, AB, AC, BC) — NOT by scaling
    # per-phase limits, which would be overly conservative for multi-phase loads.
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
    hh_a, hh_b, hh_c = _get_household_per_phase(site)
    if site.inverter_supports_asymmetric:
        phase_a = min(total_pool, max(0, max_per_phase - hh_a)) if site.consumption.a is not None else 0
        phase_b = min(total_pool, max(0, max_per_phase - hh_b)) if site.consumption.b is not None else 0
        phase_c = min(total_pool, max(0, max_per_phase - hh_c)) if site.consumption.c is not None else 0
        return PhaseConstraints.from_pool(phase_a, phase_b, phase_c, total_pool)
    else:
        # Same per-phase capacity rule as the asymmetric branch: the inverter
        # phase already serving the household can only hand the remainder to
        # loads.
        per_phase = total_pool / site.num_phases
        phase_a = min(per_phase, max(0, max_per_phase - hh_a)) if site.consumption.a is not None else 0
        phase_b = min(per_phase, max(0, max_per_phase - hh_b)) if site.consumption.b is not None else 0
        phase_c = min(per_phase, max(0, max_per_phase - hh_c)) if site.consumption.c is not None else 0
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
        if site.is_off_grid:
            # Off-grid the household is invisible to the (nonexistent) grid
            # CTs, yet the same inverter must keep serving it — only the
            # capacity left after the household can go to managed loads.
            household = sum(_get_household_per_phase(site))
            max_total_current = max(0, max_total_current - household)
        constraints.ABC = min(constraints.ABC, max_total_current)
        constraints = constraints.normalize()

    return constraints


def _calculate_site_limit(site: SiteContext) -> PhaseConstraints:
    """
    Step 1: Calculate absolute site power limit (prevents breaker trips).

    Returns PhaseConstraints for ALL phase combinations (Multi-Phase Constraint Principle).

    Always includes grid + inverter (solar + battery when SOC >= min).
    Mode-specific limits are handled by per-load ceilings, not by reducing
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
    #    (solar power absorbed by battery is available if load draws instead)
    # 2. Battery DISCHARGE potential when SOC > target — add remaining capacity.
    #    (self-consumption keeps battery idle unless there's demand, but the
    #    load CAN create that demand, making the discharge available)
    #
    # Inverter headroom constraint on discharge:
    #    Battery discharge goes through the inverter. If solar already maxes out
    #    the inverter, there's no room for additional battery discharge.
    #    base_pool (export + charge_back) ≈ solar - household.
    #    estimated_solar ≈ base_pool + household.
    #    Discharge headroom = inverter_max - estimated_solar.
    charge_back = 0
    discharge_potential = 0
    discharge_drain = 0

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
        # At/below target the battery is NOT surplus. A discharge here is
        # covering a load deficit, which props the grid CT up and inflates
        # the export the surplus is derived from — strip it back out so
        # Solar loads cannot quietly drain the battery.
        elif site.battery_power > 0:
            discharge_drain = site.battery_power / site.voltage

    # Limit discharge by inverter headroom: additional discharge only fits in
    # the capacity the inverter is not already using for solar plus the
    # discharge in flight.
    if site.inverter_max_power and discharge_potential > 0:
        inverter_max_current = site.inverter_max_power / site.voltage
        actual_discharge = max(0, site.battery_power or 0) / site.voltage
        if (
            site.household_consumption_total is not None
            or site.inverter_output_per_phase is not None
        ):
            # Accurate: solar_production_total comes from a dedicated solar
            # entity or was derived from the inverter output sensors, so the
            # inverter's current output is simply solar + in-flight discharge.
            estimated_output = (
                site.solar_production_total / site.voltage + actual_discharge
            )
        else:
            # Estimate from CT readings (derived mode). When the site is
            # exporting, the export already CONTAINS the battery discharge
            # (export = solar + discharge − household), so adding the
            # discharge would double-count it. When the battery is only
            # covering local load the CT reads ~0 and the discharge is
            # invisible. The inverter's output is at least the larger of the
            # two views.
            export_total = site.export_current.total if site.export_current else 0
            base_pool = export_total + charge_back
            household = site.consumption.total or 0
            estimated_output = max(base_pool + household, actual_discharge)
        inverter_headroom = max(0, inverter_max_current - estimated_output)
        discharge_potential = min(discharge_potential, inverter_headroom)

    battery_adjustment_total = charge_back + discharge_potential - discharge_drain

    battery_adjustment_per_phase = battery_adjustment_total / (
        site.export_current.active_count or site.consumption.active_count or 1
    ) if battery_adjustment_total else 0

    max_per_phase = site.inverter_max_power_per_phase / site.voltage if site.inverter_max_power_per_phase else float('inf')

    if site.inverter_supports_asymmetric:
        total_pool = (site.export_current.total if site.export_current else 0) + battery_adjustment_total
        constraints = _build_inverter_constraints(site, total_pool)
    else:
        # Symmetric: per-phase export + battery adjustment = per-phase surplus,
        # capped by the per-phase inverter capacity left after the household
        # (mirrors _build_inverter_constraints).
        hh_a, hh_b, hh_c = _get_household_per_phase(site)
        cap_a = max(0, max_per_phase - hh_a)
        cap_b = max(0, max_per_phase - hh_b)
        cap_c = max(0, max_per_phase - hh_c)
        phase_a_available = min((site.export_current.a or 0) + battery_adjustment_per_phase, cap_a) if site.export_current.a is not None else 0
        phase_b_available = min((site.export_current.b or 0) + battery_adjustment_per_phase, cap_b) if site.export_current.b is not None else 0
        phase_c_available = min((site.export_current.c or 0) + battery_adjustment_per_phase, cap_c) if site.export_current.c is not None else 0
        constraints = PhaseConstraints.from_per_phase(phase_a_available, phase_b_available, phase_c_available)

    # Apply total inverter limit if configured, accounting for household.
    # Cap combination fields (not per-phase) — same principle as grid limit.
    if site.inverter_max_power:
        max_total = site.inverter_max_power / site.voltage
        household = sum(_get_household_per_phase(site))
        max_for_loads = max(0, max_total - household)
        constraints.ABC = min(constraints.ABC, max_for_loads)
        constraints = constraints.normalize()

    _LOGGER.debug(f"Solar available constraints ({'asymmetric' if site.inverter_supports_asymmetric else 'symmetric'}): {constraints}")

    return constraints


def _charge_allowance(site: SiteContext) -> float:
    """The rate the site's battery is PERMITTED to take, as a sink allowance.

    0 when no battery is configured or the one configured is at/above its full
    SOC — a full battery draws nothing, so leaving its rating in an allowance
    would make the sum unreachable exactly when the site is dumping the most
    energy. Otherwise ``battery_max_charge_power``, which the engine has already
    narrowed to whatever our own charge control is enforcing (see
    ``excess_margin`` and ``engine/fleet.charge_power_total``).
    """
    battery_present = site.battery_power is not None or site.battery_soc is not None
    battery_full = (
        site.battery_soc is not None
        and site.battery_soc_full is not None
        and site.battery_soc >= site.battery_soc_full
    )
    if not battery_present or battery_full:
        return 0.0
    return site.battery_max_charge_power or 0


def _reconstruct_placement(site: SiteContext):
    """The load-off reconstruction: ``(export_w, battery_restored_w)``.

    Every figure the Excess verdict decides on is read as the site would read it
    *with our own managed loads off* — that is what makes the number stable
    enough to decide with, since a load that is running must not suppress the
    verdict that engaged it. This is the part of that reconstruction that
    depends on the grid readings, split out because ``excess_margin`` is no
    longer its only consumer: the forecast's charge-limit trim steers on the
    same reconstructed export (see ``engine/hub_result._compute_forecast_advice``).

    Off-grid there are no readings at all, so nothing is reconstructed from
    them: export is 0 and the managed draws are handed back wholesale.
    ``_apply_feedback_loop`` returns early there (the grid readings are
    synthetic zeros that never contained the draws), so without that a load's
    own consumption would come straight out of the battery's charge rate and
    suppress the very margin that engaged it — the verdict would chatter every
    cycle. Adding it back makes each load a probe: drawing power makes a
    curtailing inverter ramp up, and the margin settles at the site's *true*
    surplus, which is otherwise invisible off-grid.

    Grid-tied, ``_apply_feedback_loop`` has already taken the draws off the grid
    readings, which is the load-off state for every watt the inverter served by
    exporting less. What it cannot see is the watt served by CHARGING THE
    BATTERY LESS on a site whose phases are unbalanced: the battery's rate falls
    site-wide while the draw is subtracted from one phase, and on a phase that
    still reads net import the subtraction is clamped at zero instead of showing
    up as export. The margin then dropped the moment the load engaged — the
    on/off cycling of #41.

    So finish the reconstruction the same way the site would: give the freed
    power back to the battery, up to the headroom it actually has, and restore
    the per-phase demand that charging represents. Whatever the battery cannot
    take stays where the feedback loop put it, on the export side. A saturated
    (or full, or absent) battery has no headroom, so nothing moves and this is
    exactly the plain gross reading — and a battery sitting on an enforced
    charge limit is saturated in precisely that sense, which is why narrowing
    the allowance to the enforced rate cannot make the verdict move when a load
    starts: the load's draw was taken off the grid readings, the battery has no
    room to be handed it back, so it stays on the export side and the margin is
    unchanged.

    The export term is GROSS and clamped per phase: an export limit is physical
    and contractual per exported flow, so a site pushing 30 A out on two phases
    while pulling 10 A in on the third is exporting 30 A, not 20 A. Import on
    one phase never buys export headroom on another.

    It is also the PHYSICAL export — every watt at the meter, whatever produced
    it. The Excess verdict wants only the SOLAR share and nets the battery's
    discharge off this figure itself (see ``excess_margin``); the charge-limit
    trim wants the physical number, because the meter is the plant it steers,
    and it simply stops integrating while the battery discharges (see
    ``engine/hub_result._advance_export_trim``). Two consumers, one
    reconstruction, and the mode-dependent part stays with the consumer that
    cares.

    Pure function — unit-testable.
    """
    # battery_power is positive discharging, negative charging.
    charge_power = max(0.0, -(site.battery_power or 0))
    managed_draw = (
        sum(sum(c.get_site_phase_draw()) for c in site.loads) * site.voltage
    )

    if site.is_off_grid:
        return 0.0, managed_draw

    headroom = (
        max(0.0, _charge_allowance(site) - charge_power)
        if (site.battery_power or 0) <= 0
        else 0.0  # discharging: the freed power stops the discharge first
    )
    battery_restored = min(managed_draw, headroom)
    # Charging is symmetric across the phases that exist, so the restored
    # demand lands per phase — which is why it can cancel export on one
    # phase without touching the import on another. Gross, clamped per
    # phase, then summed: the export semantics never change.
    per_phase = (
        battery_restored / site.export_current.active_count / site.voltage
        if battery_restored and site.export_current.active_count
        else 0.0
    )
    export = 0.0
    for exp, cons in (
        (site.export_current.a, site.consumption.a),
        (site.export_current.b, site.consumption.b),
        (site.export_current.c, site.consumption.c),
    ):
        if exp is None:
            continue
        export += max(0.0, exp - (cons or 0) - per_phase)
    return export * site.voltage, battery_restored


def reconstructed_export_power(site: SiteContext) -> float:
    """Export in watts as the site would read it with our managed loads off.

    The steering signal for the forecast's charge-limit trim, and the same
    number the Excess verdict places against its allowance — see
    ``_reconstruct_placement`` for why it is not simply the CT reading. Its
    load-invariance is the property the trim needs: an engaged Excess load
    drawing kilowatts must not look like an export shortfall, or the trim would
    be steering on our own loads instead of on the site's standing error.

    Pure function — unit-testable.
    """
    export, _ = _reconstruct_placement(site)
    return export


def excess_load_draw_power(site: SiteContext) -> float:
    """Watts drawn right now by the loads in an Excess operating mode.

    The measured draw, phase-mapped, which is the same figure the reconstruction
    above credits back to the site. Above the battery's destination this is what
    the battery yields to: the Excess loads get the surplus first and the
    battery only absorbs what they cannot (see
    ``calculations.recommended_charge_limit``).

    Pure function — unit-testable.
    """
    return (
        sum(
            sum(c.get_site_phase_draw())
            for c in site.loads
            if c.mode_behavior in (BEHAVIOR_EXCESS, BEHAVIOR_BINARY_EXCESS)
        )
        * site.voltage
    )


def excess_margin(site: SiteContext, hysteresis: float = 0.0) -> float:
    """Watts by which the site is over the point where Excess mode triggers.

    Excess means the site can no longer place its own production anywhere else:
    the grid export allowance is used up AND the battery is taking all it can.
    Both sinks are summed, so one number decides Excess for every load —

        margin = (SOLAR export + battery charge power + our own managed draws)
               - (export allowance + battery charge allowance - hysteresis)

    The export term is GROSS and clamped per phase: an export limit is physical
    and contractual per exported flow, so a site pushing 30 A out on two phases
    while pulling 10 A in on the third is exporting 30 A, not 20 A. Import on one
    phase never buys export headroom on another.

    And it is SOLAR-ONLY: the battery's discharge is netted off it, so only the
    site's own production can trigger Excess. Stored energy on its way out of the
    meter is not surplus — it is yesterday's surplus being sold, and an Excess
    load engaging on it would be charging a car from the house battery. See the
    term itself for the conservation identity that makes one subtraction cover
    every inverter work mode.

    Every figure is read as the site would read it *with our own loads off* —
    that is what makes the number stable enough to decide with: a load that is
    running must not suppress the verdict that engaged it. Grid-tied, the
    feedback loop has already taken the draws off the grid readings, and the
    managed-draw term finishes the job by handing the freed power back to the
    battery's charge headroom (see the term itself); off-grid, where there are
    no readings at all, it is added wholesale.

    — where ``margin >= 0`` means Excess is on, and the value *is* the excess
    pool in watts. Callers need nothing else; the breakdown goes to the debug log.

    A sink contributes its allowance only while it can actually absorb:

    - **No grid** (off-grid site): export allowance is 0 — nothing can leave.
    - **No battery configured**, or **battery at/above its full SOC**: charge
      allowance is 0. A full battery draws no charge power, so leaving its rating
      in the allowance would make the sum unreachable exactly when the site is
      dumping the most energy.
    - **A battery being held below its rating**: the allowance is the rate it is
      PERMITTED to take, which is what ``site.battery_max_charge_power`` carries.
      Same principle as the full battery, one step short of it: while our own
      charge control holds an inverter's register at, say, 6.5 kW of a 10 kW
      rating — the PV clipping forecast reserving room for the afternoon — the
      missing 3.5 kW is not a place this site can put production either, and
      counting it would make the sum unreachable for the whole clipping window,
      which is precisely when the surplus Excess loads exist to soak up appears.
      Only actual enforcement narrows it; a battery merely *advised* a lower rate
      still charges at its rating. The engine assembles the number
      (``engine/fleet.charge_power_total``) — this stays one figure in watts.

    Zero counts as on, because it is the saturated case — export sitting at the
    allowance *and* the battery pulling its maximum charge rate is precisely
    "nothing more can be absorbed".

    ``hysteresis`` widens the band once Excess is engaged so a load doesn't
    chatter at the trigger point. It shrinks the allowance rather than shifting
    the margin, and the allowance is clamped at zero — otherwise a site with no
    allowance at all would report a pool larger than the power it actually has.

    A site with no allowance therefore sits exactly at 0: off-grid with a full
    battery. That is correct rather than degenerate — a full battery cannot take
    another watt, and an off-grid inverter in that state is curtailing. The loads
    that read the plain verdict do run there: the hot water tank's boost setpoint,
    a plug on its near-full trigger, and a modulating Excess load at its minimum
    current (a margin of 0 is a pool of 0, and the minimum is a floor while the
    verdict holds — see _source_limit). It self-corrects rather than self-limits:
    if production cannot cover them, the battery discharges, SOC falls below full,
    its charge allowance returns and the verdict clears.

    The reconstruction itself — the export the site would read with our loads
    off, and the share of their freed power the battery would take — lives in
    ``_reconstruct_placement``, because the forecast's charge-limit trim steers
    on the same figures. The solar-only subtraction stays HERE rather than there:
    the trim steers the meter, so it wants the physical export and handles a
    discharging battery by not integrating at all.

    Pure function — unit-testable.
    """
    # battery_power is positive discharging, negative charging.
    charge_power = max(0.0, -(site.battery_power or 0))
    managed_draw = (
        sum(sum(c.get_site_phase_draw()) for c in site.loads) * site.voltage
    )

    export_allowance = 0.0 if site.is_off_grid else (site.excess_export_threshold or 0)
    # The rate the battery is PERMITTED to take, not its nameplate rating — the
    # engine narrows this scalar to whatever our charge control is actually
    # enforcing (see the docstring). Everything else is unchanged by that: a
    # narrower allowance is a smaller headroom in exactly the same way a
    # partly-charged battery is, so the draw add-back keeps cancelling.
    charge_allowance = _charge_allowance(site)
    export, battery_restored = _reconstruct_placement(site)

    # SOLAR-ONLY EXPORT. Only the site's own production can be surplus; stored
    # energy leaving the meter never is. The subtraction is exactly power
    # conservation, which is why ONE term covers every inverter work mode:
    #
    #     export - battery_discharge == production - consumption
    #
    # so subtracting the discharge from the export reading yields the export the
    # site's PV alone accounts for. The three cases fall out of it:
    #
    # * battery serving the HOUSE — its discharge is consumed, not exported, so
    #   the site is not exporting it and there is nothing here to take away;
    # * battery SELLING to the grid (Deye "Selling First", a slot with sell
    #   semantics, a scheduled sell-down) — subtracted in full, so a pack
    #   emptying itself into the meter can never trigger Excess;
    # * real PV surplus — a charging or idle battery subtracts nothing, so every
    #   figure on a Zero-Export-to-CT site is byte-identical to before.
    #
    # Where it lands, and why the per-phase semantics survive: at the site-level
    # AGGREGATION POINT, on the watts ``_reconstruct_placement`` returns, never
    # on the phase figures. Those are gross and clamped per phase because an
    # export limit is per exported flow; battery power is a SITE quantity (one
    # pack behind one inverter, no per-phase reading exists), so it can only be
    # netted against the site total — the same shape as ``battery_restored``,
    # which is likewise a site figure. Subtracting it phase by phase would let
    # one phase's import cancel another's export, the exact semantics the gross
    # clamp exists to prevent. On an unbalanced site the gross sum can exceed
    # the net export, so part of a house-served discharge is still netted off;
    # that errs on the side of reading LESS solar export, which is the safe
    # direction for a verdict that must fire only on production.
    #
    # ``site.battery_power`` is positive discharging, negative charging — the
    # raw sensor value, uninverted, summed across the fleet
    # (``engine/readers`` → ``fleet.battery_power_total``), so the clamp below
    # keeps a charging pack out of this term entirely. The charge-allowance side
    # is untouched: a discharging battery still absorbs nothing.
    discharge = max(0.0, site.battery_power or 0)
    solar_export = max(0.0, export - discharge)

    allowance = max(0.0, export_allowance + charge_allowance - hysteresis)
    absorbed = solar_export + charge_power + battery_restored
    margin = absorbed - allowance

    _LOGGER.debug(
        "Excess margin %+.0fW: placing %.0fW (solar export %.0fW of %.0fW metered"
        " less %.0fW battery discharge + battery charge %.0fW"
        " + freed to battery %.0fW of %.0fW managed draw) vs allowance %.0fW"
        " (export %.0fW + battery %.0fW - hysteresis %.0fW)",
        margin,
        absorbed,
        solar_export,
        export,
        discharge,
        charge_power,
        battery_restored,
        managed_draw,
        allowance,
        export_allowance,
        charge_allowance,
        hysteresis,
    )
    return margin


def _excess_verdict(site: SiteContext) -> bool:
    """Is Excess engaged this cycle? The plain verdict, no pool arithmetic.

    Same test ``_calculate_excess_available()`` gates the pool on, and the same
    one the engine's latch has already settled: by the time the calculator runs,
    ``site.excess_hysteresis`` is the widened band while engaged and 0 while not,
    so reading the margin with it reproduces the latch's answer exactly. Kept
    separate because the pool is not the verdict — a margin of 0 IS Excess (the
    saturated case) and yet buys a pool of 0 amps.
    """
    return excess_margin(site, site.excess_hysteresis) >= 0


def _calculate_excess_available(site: SiteContext) -> PhaseConstraints:
    """
    Step 3: Calculate excess available power.

    Returns PhaseConstraints for ALL phase combinations.
    Excess mode only charges once the site has run out of places to put its own
    production — see excess_margin() for what that means.

    For ASYMMETRIC inverters: Excess power can be allocated to any phase.
    For SYMMETRIC inverters: Excess power is divided per-phase.
    """
    margin = excess_margin(site, site.excess_hysteresis)

    if margin >= 0:
        total_available = margin / site.voltage if site.voltage > 0 else 0
        constraints = _build_inverter_constraints(site, total_available)
        _LOGGER.debug(
            f"Excess constraints ({'asymmetric' if site.inverter_supports_asymmetric else 'symmetric'}): {constraints}"
        )
        return constraints

    return PhaseConstraints.zeros()


def _below_soc_target(site: SiteContext) -> bool:
    """Check if battery SOC is below target."""
    return (site.battery_soc is not None and site.battery_soc_target is not None
            and site.battery_soc < site.battery_soc_target)


def _rank(load: LoadContext) -> tuple[int, int]:
    """Distribution rank — the same key _sort_loads() serves loads in."""
    return (load.mode_priority, load.priority)


def _load_power(load: LoadContext, site: SiteContext) -> float:
    """Watts this load draws while running: its permit on every phase it spans.

    ``max_current`` is per-phase, and for a binary load it IS the load's rating
    (min == max == rating / (voltage × phases)), so this recovers the plate
    rating exactly whatever the phase count.
    """
    phases = len(load.active_phases_mask or "A")
    return load.max_current * phases * site.voltage


def _inverter_covers_load(load: LoadContext, site: SiteContext) -> bool:
    """Is there room under the inverter's RATING to source this load's draw?

    The SOC-gated binary modes hand out a permit on the strength of stored
    energy alone. That says nothing about the path: while the inverters are
    already putting out everything they are rated for, one more binary load
    cannot be served from the battery at all — its power comes from the grid
    (or, off-grid, pushes the inverters past their plate rating). This gate is
    the second half of the dual gate: SOC says there IS energy, this says the
    inverter can still deliver it. No rating configured (None/0) or no output
    reading → unlimited, the pre-gate behavior.

    **Evaluated with the load off** (issue #41's discipline — a gate a load's
    own draw can flip is a gate that suppresses itself). The load-off output is
    the current output minus the draws that would go away if this load, and
    everything it outranks, were shed:

        freed   = max(0, shed_draw − net_grid)      (net_grid: + import, − export)
        covered = rating − (output − freed) >= load's own rated power

    Two subtleties are why ``freed`` is not simply the shed draw:

    * **Grid import caps the add-back.** A draw the site is IMPORTING for is
      not part of what the inverters are delivering, so shedding it frees no
      inverter capacity. Without this cap, a load whose power comes from the
      grid while the inverters sit at their rating would credit itself with its
      own draw, the gate could never fail once the load was on, and issue #17
      would survive for every load that was already running when saturation
      arrived. When the site is EXPORTING the same term goes the other way and
      credits the export: that output is already on the AC bus and the load can
      have it by displacing it, no extra inverter capacity needed.
    * **Only outranked draws count.** Loads served BEFORE this one keep their
      share of the output (the distributor will not take it back), while loads
      this one outranks would be shed in its favour — so their draw is capacity
      this load may claim. Without this a running low-priority load would lock
      a higher-priority one out of a saturated inverter, undoing preemption.

    This gate is about the inverter's RATING only. Whether the energy exists at
    all stays the SOC gate's and the source pools' business.
    """
    rating = site.inverter_max_power
    output = site.inverter_output_total
    if not rating or output is None:
        return True

    shed_current = sum(
        sum(c.get_site_phase_draw())
        for c in site.loads
        if c is load or _rank(c) > _rank(load)
    )
    # Signed on purpose: importing eats into the add-back, exporting adds to it.
    net_grid = site.net_grid_power or 0.0
    freed = max(0.0, shed_current * site.voltage - net_grid)
    headroom = rating - (output - freed)
    needed = _load_power(load, site)
    covered = headroom >= needed
    if not covered:
        _LOGGER.debug(
            "Inverter coverage denied for %s: needs %.0fW, load-off headroom "
            "%.0fW (rating %.0fW − output %.0fW + freed %.0fW)",
            load.entity_id,
            needed,
            headroom,
            rating,
            output,
            freed,
        )
    return covered


def _source_limit(
    load: LoadContext,
    site: SiteContext,
    solar: PhaseConstraints,
    excess: PhaseConstraints,
    base: float = 0,
) -> float:
    """Compute source-limited maximum allocation for a load.

    Returns the maximum per-phase current this load may receive based on its
    mode behavior and available energy sources. Physical pool limits are applied
    separately by the caller. Switches purely on ``load.mode_behavior`` — the
    operating mode and device type never enter here.

    Args:
        base: Current already reserved in pass 1 (accounts for prior deductions
              from source pools so the ceiling includes the pass-1 allocation).
    """
    mask = load.active_phases_mask
    behavior = load.mode_behavior

    # Binary smart-plug behaviors — on/off, never grid. With a battery the
    # battery is the stored-solar buffer, and each mode drains it only to a
    # progressively higher SOC floor; with no battery they fall back to a
    # live-surplus rule.
    #
    # Every SOC-derived permit below is a DUAL gate: stored energy (SOC) AND a
    # path for it (_inverter_covers_load). SOC alone would hand out a permit the
    # inverter has to fill from the grid whenever it is already saturated
    # (ISSUES #17). The flow-derived permits need no such gate — an export-driven
    # verdict is already proof the power is on the AC bus.

    # Solar Priority: run while the battery is above its minimum SOC.
    if behavior == BEHAVIOR_BINARY_ABOVE_MIN:
        if site.battery_soc is not None:
            soc_min = site.battery_soc_min or 0
            if site.battery_soc > soc_min and _inverter_covers_load(load, site):
                return load.max_current
            return 0
        behavior = BEHAVIOR_SOLAR_ONLY

    # Solar Only: run while the battery is above its target SOC (only the
    # above-target band counts as stored surplus).
    if behavior == BEHAVIOR_BINARY_ABOVE_TARGET:
        if site.battery_soc is not None:
            if site.battery_soc_target is None:
                return 0
            if (
                site.battery_soc > site.battery_soc_target
                and _inverter_covers_load(load, site)
            ):
                return load.max_current
            return 0
        behavior = BEHAVIOR_SOLAR_ONLY

    # Excess: run while the battery is near-full, OR whenever the site is
    # exporting — export can reach the threshold before the battery fills
    # (battery charge-rate limited). With no battery it is purely
    # export-driven.
    #
    # Only the near-full shortcut is SOC-derived, so only it takes the inverter
    # gate: "the battery cannot absorb any more" is not evidence that the
    # inverter can pass this load's draw, and a full battery next to a saturated
    # inverter is exactly the grid-draw case. A saturated inverter then falls
    # THROUGH to the export rule rather than answering 0 — a clipping inverter
    # can still be exporting, and a load that displaces export costs the
    # inverter no extra output.
    if behavior == BEHAVIOR_BINARY_EXCESS:
        if (
            site.battery_soc is not None
            and site.battery_soc_full is not None
            and site.battery_soc >= site.battery_soc_full
            and _inverter_covers_load(load, site)
        ):
            return load.max_current
        return load.max_current if excess.get_available(mask) > 0 else 0

    if behavior == BEHAVIOR_FULL_POWER:
        return load.max_current

    if behavior == BEHAVIOR_SOLAR_PRIORITY:
        if _below_soc_target(site):
            return load.min_current  # Grid-backed minimum only
        return max(load.min_current, base + solar.get_available(mask))

    if behavior == BEHAVIOR_SOLAR_ONLY:
        if _below_soc_target(site):
            return 0  # Battery needs to charge
        return base + solar.get_available(mask)

    if behavior == BEHAVIOR_EXCESS:
        # The verdict starts this load, the pool only sizes it. A modulating
        # load cannot run below its minimum, so while Excess is engaged the
        # minimum IS the floor — held there while the momentary pool is smaller
        # than it, and followed upward once the pool exceeds it. That is the
        # same start edge the binary Excess loads have always had: they engage
        # on threshold-hit even though their whole rating overshoots the pool.
        #
        # Gating the start on the pool instead leaves a modulating load stuck at
        # 0 forever on the site the pool is smallest at: with our charge control
        # tracking the export overshoot the standing margin sits AT the trigger
        # (a pool of 0 amps — saturated, which is Excess by definition), peaking
        # only between register writes. The pool is checked first because it is
        # free and, above zero, decides on its own: the pool exists only while
        # the verdict is on.
        #
        # Release is untouched — the latch's hysteresis on the reconstructed
        # margin (which adds this load's own draw back) is what lets go.
        e_avail = excess.get_available(mask)
        if e_avail <= 0 and not _excess_verdict(site):
            return 0
        return max(load.min_current, base + e_avail)

    return load.max_current


def _deduct_from_sources(
    current: float,
    mask: str,
    solar: PhaseConstraints,
    excess: PhaseConstraints,
) -> tuple[PhaseConstraints, PhaseConstraints]:
    """Deduct allocated current from source pools.

    ALL draws reduce both solar and excess pools because any power consumption
    reduces grid export, which reduces surplus available for other loads.
    """
    s_avail = solar.get_available(mask)
    if s_avail > 0:
        solar = solar.deduct(min(current, s_avail), mask)
    e_avail = excess.get_available(mask)
    if e_avail > 0:
        excess = excess.deduct(min(current, e_avail), mask)
    return solar, excess


def _sort_loads(loads: list[LoadContext]) -> list[LoadContext]:
    """Sort loads by (mode urgency tier, per-load priority) for distribution."""
    return sorted(
        loads,
        key=lambda c: (c.mode_priority, c.priority),
    )


def _distribute_power(
    site: SiteContext,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
) -> None:
    """
    Step 4: Distribute power among loads using source-aware pools.

    Three pools tracked simultaneously:
    - Physical pool: hard wire limits (grid + inverter). ALL allocations deduct.
    - Solar pool: surplus from renewables. ALL allocations deduct (any draw
      reduces export, shrinking the surplus available for other loads).
    - Excess pool: surplus above threshold. ALL allocations deduct.

    Mode determines SOURCE LIMIT (max a load may draw):
    - Standard/Continuous: physical pool only (any source)
    - Solar Priority: solar pool + grid minimum guarantee
    - Solar Only: solar pool only
    - Excess: excess pool + minimum guarantee while the verdict is on
    """
    if not site.loads:
        return

    _LOGGER.debug(f"Distribution — physical: {physical_pool}")
    _LOGGER.debug(f"Distribution — solar: {solar_pool}")
    _LOGGER.debug(f"Distribution — excess: {excess_pool}")

    for load in site.loads:
        _eff_ph = len(load.active_phases_mask) if load.active_phases_mask else 0
        _draw = load.l1_current + load.l2_current + load.l3_current
        _LOGGER.debug(
            f"  {load.entity_id}: mode={load.operating_mode} "
            f"mask={load.active_phases_mask}({_eff_ph}ph) "
            f"hw={load.phases}ph {load.min_current:.0f}-{load.max_current:.0f}A "
            f"prio={load.priority} [{load.connector_status}] draw={_draw:.1f}A"
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
    loads: list[LoadContext],
    site: SiteContext,
    physical: PhaseConstraints,
    solar: PhaseConstraints,
    excess: PhaseConstraints,
) -> tuple[dict[str, float], dict[str, float], PhaseConstraints, PhaseConstraints, PhaseConstraints]:
    """Pass 1: Reserve minimum current for all eligible loads.

    Source-aware: each mode checks its allowed energy sources.
    All allocations deduct from physical pool (wire limits apply to all).
    All allocations deduct from solar and excess pools (any draw reduces export).

    Returns (allocated dict, footprints dict, remaining physical, remaining
    solar, remaining excess). ``footprints`` is the real draw deducted here
    for each load — never more than the minimum reserved; a load that
    draws above its minimum has the surplus deducted in pass 2, where it
    fills. Pass 2 uses ``footprints`` to deduct only the *additional* real
    draw, so the pools end up reduced by each load's true footprint.
    """
    allocated = {}
    footprints = {}
    for load in loads:
        mask = load.active_phases_mask
        if not mask:
            allocated[load.entity_id] = 0
            footprints[load.entity_id] = 0
            continue

        # Source limit: is this mode allowed to charge at all?
        src_max = _source_limit(load, site, solar, excess, base=0)
        if src_max < load.min_current:
            allocated[load.entity_id] = 0
            footprints[load.entity_id] = 0
            continue

        # Physical pool must have room on the wire
        if physical.get_available(mask) < load.min_current:
            allocated[load.entity_id] = 0
            footprints[load.entity_id] = 0
            continue

        # Reserve minimum (the permit base). The pools are reduced by the
        # load's real footprint, but never more than this minimum — a
        # load drawing above its minimum has the surplus deducted in pass 2.
        allocated[load.entity_id] = load.min_current
        draw = min(
            _pool_deduction(load, load.min_current), load.min_current
        )
        footprints[load.entity_id] = draw
        physical = physical.deduct(draw, mask)
        solar, excess = _deduct_from_sources(draw, mask, solar, excess)

    return allocated, footprints, physical, solar, excess


def _distribute_per_phase_priority(
    site: SiteContext,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
) -> None:
    """
    PRIORITY mode: Pass 1 reserve minimums for all eligible loads,
    Pass 2 fill remainder by urgency+priority order.

    Source-aware: each load's fill-up is limited by its mode's source pool.
    All draws deduct from physical, solar, and excess pools.
    """
    sorted_loads = _sort_loads(site.loads)

    # Pass 1: Reserve minimums (source-aware)
    remaining = physical_pool.copy()
    solar_rem = solar_pool.copy()
    excess_rem = excess_pool.copy()
    allocated, footprints, remaining, solar_rem, excess_rem = _allocate_minimums(
        sorted_loads, site, remaining, solar_rem, excess_rem
    )

    for cid, alloc in allocated.items():
        _LOGGER.debug(f"  Pass 1: {cid} = {alloc:.1f}A")

    # Pass 2: Fill by priority order, source-limited
    for load in sorted_loads:
        base = allocated.get(load.entity_id, 0)
        mask = load.active_phases_mask
        if not mask or base == 0:
            load.allocated_current = round(base, 1)
            continue

        phys_avail = remaining.get_available(mask)
        src_max = _source_limit(load, site, solar_rem, excess_rem, base=base)
        effective_max = min(src_max, load.max_current)
        additional = max(0, min(effective_max - base, phys_avail))
        total = base + additional

        load.allocated_current = round(total, 1)
        # Deduct this load's real footprint, beyond what pass 1 already
        # took. A ramping load / plug consumes its full permit; a settled
        # EVSE drawing below its permit consumes only its measured draw,
        # leaving the gap for lower-priority loads.
        consumption = _pool_deduction(load, total)
        pool_delta = consumption - footprints.get(load.entity_id, 0)
        if pool_delta > 0:
            remaining = remaining.deduct(pool_delta, mask)
            solar_rem, excess_rem = _deduct_from_sources(
                pool_delta, mask, solar_rem, excess_rem
            )


def _scale_source_increments(
    batch: list[tuple[LoadContext, str, float]],
    behaviors: frozenset[str],
    pool: PhaseConstraints,
) -> list[tuple[LoadContext, str, float]]:
    """Cap one source's group of increments against that source's pool.

    Every increment in a shared-mode round is sized against the same pool
    snapshot, so each one fits on its own while their sum need not — two loads
    on one phase can each be offered the whole surplus. The binding limit is the
    pool on the most constrained mask among the group's loads; scale the group's
    increments down to it proportionally (to zero when nothing is left).

    Only the named behaviors are scaled. Grid-backed loads are untouched — their
    ceiling is the physical pool, not a surplus pool, and their draw still
    drains the surplus afterwards via _deduct_from_sources. Binary behaviors are
    excluded too: they are on/off loads whose whole permit is gated by SOC or
    the excess verdict in _source_limit, so a fractionally scaled increment
    would describe a state they cannot occupy.
    """
    members = [
        (mask, incr)
        for load, mask, incr in batch
        if load.mode_behavior in behaviors and incr > 0
    ]
    if not members:
        return batch

    total = sum(incr for _, incr in members)
    available = min(pool.get_available(mask) for mask, _ in members)
    if total <= available:
        return batch

    scale = max(0.0, available) / total
    return [
        (
            load,
            mask,
            incr * scale if load.mode_behavior in behaviors and incr > 0 else incr,
        )
        for load, mask, incr in batch
    ]


def _distribute_per_phase_shared(
    site: SiteContext,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
) -> None:
    """
    SHARED mode: Pass 1 reserve minimums for all eligible loads,
    Pass 2 split remainder equally among charging loads.

    Source-aware: each load's fill-up is limited by its mode's source pool.
    Equal split respects source ceilings — source-limited loads cap early
    and the remainder goes to others in subsequent rounds.
    """
    sorted_loads = _sort_loads(site.loads)

    # Pass 1: Reserve minimums (source-aware)
    remaining = physical_pool.copy()
    solar_rem = solar_pool.copy()
    excess_rem = excess_pool.copy()
    allocated, footprints, remaining, solar_rem, excess_rem = _allocate_minimums(
        sorted_loads, site, remaining, solar_rem, excess_rem
    )

    charging_loads = [c for c in sorted_loads if allocated.get(c.entity_id, 0) > 0]
    if not charging_loads:
        for load in site.loads:
            load.allocated_current = 0
        return

    # Track each load's cumulative pool consumption so the loop can deduct
    # only the *real* draw, not the permit increment. A settled EVSE drawing
    # below its permit never consumes more than its measured draw, so the
    # surplus permit doesn't drain the pool — equal-split then routes the
    # slack to other charging loads (the user's "free 9 A to the second
    # EVSE" case). Initialised from pass-1 footprints.
    consumed = dict(footprints)

    # Pass 2: Split remainder equally, respecting source limits.
    # Batch compute increments to avoid order-dependent solar depletion.
    while True:
        loads_wanting_more = []
        for c in charging_loads:
            src_max = _source_limit(c, site, solar_rem, excess_rem, base=allocated[c.entity_id])
            effective_max = min(c.max_current, src_max)
            if allocated[c.entity_id] >= effective_max:
                continue
            # A load whose own phases are physically exhausted cannot receive
            # anything, so it is not "wanting more" in any actionable sense.
            # Leaving it in would pin the equal-split share at 0 and freeze
            # every other load — including ones with headroom on other phases.
            if remaining.get_available(c.active_phases_mask) <= 0:
                continue
            loads_wanting_more.append(c)

        if not loads_wanting_more:
            break

        min_available = min(
            remaining.get_available(c.active_phases_mask) for c in loads_wanting_more
        )
        if min_available <= 0:
            break

        per_load_increment = min_available / len(loads_wanting_more)

        # Batch: compute all increments against current pool state
        batch = []
        for load in loads_wanting_more:
            mask = load.active_phases_mask
            src_max = _source_limit(load, site, solar_rem, excess_rem, base=allocated[load.entity_id])
            effective_max = min(load.max_current, src_max)
            additional = min(per_load_increment, effective_max - allocated[load.entity_id])
            additional = max(0, additional)
            batch.append((load, mask, additional))

        # Per-source overshoot: the increments above were all sized against the
        # same snapshot, so loads bound to one surplus pool can each fit and
        # still jointly exceed it. Cap each source's group against its own pool,
        # leaving loads bound to a different source (or to none) alone.
        batch = _scale_source_increments(batch, _SOLAR_BOUND_BEHAVIORS, solar_rem)
        batch = _scale_source_increments(batch, _EXCESS_BOUND_BEHAVIORS, excess_rem)

        # Apply all increments, deducting each load's real consumption
        # growth (not the permit increment) so a settled-and-under-drawing
        # EVSE leaves the unused gap in the pool for others.
        any_progress = False
        for load, mask, additional in batch:
            if additional > 0.001:
                allocated[load.entity_id] += additional
                new_cons = _pool_deduction(load, allocated[load.entity_id])
                pool_delta = new_cons - consumed.get(load.entity_id, 0)
                consumed[load.entity_id] = new_cons
                if pool_delta > 0:
                    remaining = remaining.deduct(pool_delta, mask)
                    solar_rem, excess_rem = _deduct_from_sources(
                        pool_delta, mask, solar_rem, excess_rem
                    )
                any_progress = True

        if not any_progress:
            break

    for load in charging_loads:
        load.allocated_current = round(allocated[load.entity_id], 1)

    for load in site.loads:
        if load not in charging_loads:
            load.allocated_current = 0


def _distribute_per_phase_strict(
    site: SiteContext,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
) -> None:
    """
    STRICT mode: Give first load up to max (or source limit), then next, etc.
    Sorted by (urgency, priority). No minimum reservation — sequential greedy.
    """
    remaining = physical_pool.copy()
    solar_rem = solar_pool.copy()
    excess_rem = excess_pool.copy()
    sorted_loads = _sort_loads(site.loads)

    for load in sorted_loads:
        mask = load.active_phases_mask
        if not mask:
            load.allocated_current = 0
            continue

        src_max = _source_limit(load, site, solar_rem, excess_rem, base=0)
        phys_avail = remaining.get_available(mask)
        allocation = round(min(load.max_current, src_max, phys_avail), 1)

        if allocation < load.min_current:
            load.allocated_current = 0
            continue

        load.allocated_current = allocation
        draw = _pool_deduction(load, allocation)
        remaining = remaining.deduct(draw, mask)
        solar_rem, excess_rem = _deduct_from_sources(
            draw, mask, solar_rem, excess_rem
        )


def _distribute_per_phase_optimized(
    site: SiteContext,
    physical_pool: PhaseConstraints,
    solar_pool: PhaseConstraints,
    excess_pool: PhaseConstraints,
) -> None:
    """
    OPTIMIZED mode: Reduce higher priority loads to allow lower priority
    to charge at minimum. Sorted by (urgency, priority). Source-aware.
    """
    remaining = physical_pool.copy()
    solar_rem = solar_pool.copy()
    excess_rem = excess_pool.copy()
    sorted_loads = _sort_loads(site.loads)

    for i, load in enumerate(sorted_loads):
        mask = load.active_phases_mask
        if not mask:
            load.allocated_current = 0
            continue

        src_max = _source_limit(load, site, solar_rem, excess_rem, base=0)
        if src_max < load.min_current:
            load.allocated_current = 0
            continue

        phys_avail = remaining.get_available(mask)
        wanted = min(load.max_current, src_max, phys_avail)

        # Check if we should reduce to help next load
        if i < len(sorted_loads) - 1:
            next_load = sorted_loads[i + 1]
            next_mask = next_load.active_phases_mask
            if next_mask:
                # Pre-check: does next load have source potential before our draw?
                pre_src = _source_limit(next_load, site, solar_rem, excess_rem, base=0)
                if pre_src >= next_load.min_current:
                    # Simulate full deduction (physical + sources)
                    temp_remaining = remaining.deduct(wanted, mask)
                    temp_solar, temp_excess = _deduct_from_sources(
                        wanted, mask, solar_rem, excess_rem
                    )
                    next_phys = temp_remaining.get_available(next_mask)
                    next_src = _source_limit(
                        next_load, site, temp_solar, temp_excess, base=0
                    )
                    next_effective = min(next_phys, next_src)
                    if next_effective < next_load.min_current:
                        reduction_needed = next_load.min_current - next_effective
                        can_reduce = max(0, wanted - load.min_current)
                        wanted -= min(reduction_needed, can_reduce)

        if wanted < load.min_current:
            load.allocated_current = 0
            continue

        load.allocated_current = round(wanted, 1)
        draw = _pool_deduction(load, load.allocated_current)
        remaining = remaining.deduct(draw, mask)
        solar_rem, excess_rem = _deduct_from_sources(
            draw, mask, solar_rem, excess_rem
        )


