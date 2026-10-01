"""
Multi-cycle simulation of the YAML scenarios, driven by test_scenarios.py.
Uses ACTUAL production code - no duplicates!

Every scenario runs a 30-cycle simulation:
  - Cycles 0-4:   Ramp-up (site values interpolate from 0 to target)
  - Cycles 5-24:  Warmup (full site values, ramp rate limiting on load output)
  - Cycles 25-29: Stability check (verify convergence)
"""

import yaml

from custom_components.dynamic_ocpp_evse.calculations.models import LoadContext, SiteContext, PhaseValues, CircuitGroup
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    calculate_all_load_targets,
    excess_margin,
)
from custom_components.dynamic_ocpp_evse.calculations.utils import (
    compute_household_per_phase,
    grid_without_managed_draws,
)
from custom_components.dynamic_ocpp_evse.const import (
    resolve_operating_mode,
    BEHAVIOR_EXCESS,
    BEHAVIOR_BINARY_EXCESS,
)
from custom_components.dynamic_ocpp_evse.const.hot_water_tank import (
    DEFAULT_TANK_AWAY_TEMPERATURE,
    TANK_MODE_FREEZE_PROTECTION,
    resolve_tank_mode_priority,
    tank_boost_is_opportunistic,
    DEFAULT_TANK_NORMAL_TEMPERATURE,
)
from custom_components.dynamic_ocpp_evse.const import DEFAULT_BATTERY_SOC_FULL, DEFAULT_PLUG_MAX_CURRENT

# ---------------------------------------------------------------------------
# Simulation constants
# ---------------------------------------------------------------------------
RAMP_UP_CYCLES = 5
WARMUP_CYCLES = 20
STABILITY_CYCLES = 5
TOTAL_CYCLES = RAMP_UP_CYCLES + WARMUP_CYCLES + STABILITY_CYCLES  # 30
UPDATE_FREQ = 15        # seconds per cycle
RAMP_UP_PER_CYCLE = 1.5   # 0.1 A/s * 15s
RAMP_DOWN_PER_CYCLE = 3.0  # 0.2 A/s * 15s

# EVSE draw-settle detection: the measured draw counts as "settled" once it
# has held within SETTLE_TOLERANCE for SETTLE_CYCLES consecutive cycles - the
# car has reached a ceiling rather than still tracking a ramping permit.
SETTLE_TOLERANCE = 0.5  # A
SETTLE_CYCLES = 3
# An EVSE only counts as settled-and-under-drawing - the case the footprint
# model frees to lower-priority loads - when its measured draw is also
# measurably below the permit we offered it last cycle. A car at util ≈ 1.0
# draws what it is offered; treating that as "capped" would let the engine
# repeatedly over-allocate and oscillate.
SETTLE_PERMIT_MARGIN = 1.0  # A


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------

def scale_site_values(site, t):
    """Scale dynamic site values by factor t (0.0 to 1.0) for cold-start ramp-up.

    Scales household consumption and solar_production_total.
    Export is NOT scaled - it's computed from scratch each cycle by the CT sim.
    Preserves None for non-existent phases.  Config values (voltage, breaker
    rating, battery SOC, etc.) are NOT scaled.
    """
    if t >= 1.0:
        return
    site.solar_production_total *= t
    site.consumption = PhaseValues(
        site.consumption.a * t if site.consumption.a is not None else None,
        site.consumption.b * t if site.consumption.b is not None else None,
        site.consumption.c * t if site.consumption.c is not None else None,
    )


def apply_ramp_rate(prev_limit, target):
    """Apply ramp rate limiting between consecutive cycles.

    Matches sensor.py behaviour: only ramp when both prev and target > 0
    (pause-to-resume is instant).
    """
    if prev_limit <= 0 or target <= 0:
        return target
    delta = target - prev_limit
    if delta > 0:
        return round(prev_limit + min(delta, RAMP_UP_PER_CYCLE), 1)
    else:
        return round(prev_limit + max(delta, -RAMP_DOWN_PER_CYCLE), 1)


def _fmt_phase(value):
    """Format a phase value for trace output."""
    return f"{value:.1f}A" if value is not None else "-"


def set_load_phase_currents(load, commanded_limit):
    """Set load l1/l2/l3_current from commanded limit based on phase mapping.

    Uses the load's L1/L2/L3 → site phase mapping (l1_phase, l2_phase, l3_phase)
    to determine which OCPP phases are active, bounded by how many legs the load
    actually HAS: a 1-phase load energises L1 only, whatever its l2/l3 mapping
    says. That bound matters because ``l2_phase`` defaults to "B" and
    ``l3_phase`` to "C" when a scenario does not name them, so a 1-phase load
    declared on phase B used to match on L2 as well and have its simulated draw
    DOUBLED onto that phase - the CT readings for every such load were wrong,
    and the physical-invariant check is what surfaced it (2026-09-07).
    """
    load.l1_current = 0
    load.l2_current = 0
    load.l3_current = 0
    if commanded_limit <= 0:
        return
    mask = (load.active_phases_mask or "").upper()
    legs = [(load.l1_phase, "l1_current")]
    if (load.phases or 1) >= 2:
        legs.append((load.l2_phase, "l2_current"))
    if (load.phases or 1) >= 3:
        legs.append((load.l3_phase, "l3_current"))
    for phase, attr in legs:
        if phase in mask:
            setattr(load, attr, commanded_limit)


def place_asymmetric_output(demand, total, cap):
    """Per-phase output of an ASYMMETRIC inverter putting out ``total`` A net.

    Anže, 2026-09-24: his asymmetric inverter "puts/pulls power on the phases in
    a way to always try to balance them on the grid". So it places its net
    output - and its charging draw - to bring every phase's grid current
    (``demand[i] - out[i]``, demand being household + managed loads) to one
    common level, each phase's output within +-``cap``; a phase the cap stops
    short of that level keeps what is left. The outputs sum to ``total``, so the
    site's net grid flow is the even spread's; only the split moves. A total
    no placement within the caps can carry falls back to the even spread.
    """
    # ponytail: the net total is held to the inverter's rating (the battery
    # model caps discharge at it); what a pull lets it push on top is bounded
    # only per phase. No scenario pulls today - sum the pushes against
    # inverter_max_power if one ever does.
    n = len(demand)
    if abs(total) > n * cap:
        return [total / n] * n

    def outputs(level):
        return [min(cap, max(-cap, d - level)) for d in demand]

    level = (sum(demand) - total) / n  # every phase equal, if the caps allow
    if all(abs(d - level) <= cap for d in demand):
        return outputs(level)
    # sum(outputs(level)) falls from n*cap at lo to -n*cap at hi: bisect onto total.
    lo, hi = min(demand) - cap, max(demand) + cap
    for _ in range(100):
        level = (lo + hi) / 2
        if sum(outputs(level)) > total:
            lo = level
        else:
            hi = level
    return outputs((lo + hi) / 2)


def simulate_grid_ct(site, household, load_l1, load_l2, load_l3):
    """Compute grid CT readings using self-consumption battery model.

    Physical model:
    1. Raw demand per phase = household - solar + load_draw
    2. Battery responds to minimize grid flow (self-consumption):
       - Deficit (raw > 0): discharges min(deficit, max_discharge) if SOC > min_soc
       - Surplus (raw < 0): charges min(surplus, max_charge) if SOC < 97%
    3. Grid CT = raw demand + battery effect - spread evenly over the phases
       for a symmetric inverter, placed to balance them for an asymmetric one
       (place_asymmetric_output)

    Positive net = importing, negative net = exporting.
    Decomposed for engine: consumption = max(0, net), export = max(0, -net).
    solar_production_total is NOT changed.

    Returns (ct_net_a, ct_net_b, ct_net_c, solar_per_phase, battery_per_phase).
    """
    num_phases = household.active_count or 1
    solar_per_phase = (site.solar_production_total / num_phases / site.voltage
                       if site.solar_production_total and site.voltage else 0.0)
    solar_total = solar_per_phase * num_phases  # Total solar current (Amps)

    # Raw demand per phase (without battery)
    def _raw(h, draw):
        return (h - solar_per_phase + draw) if h is not None else None

    raw_a = _raw(household.a, load_l1)
    raw_b = _raw(household.b, load_l2)
    raw_c = _raw(household.c, load_l3)

    # Total raw demand across active phases
    total_raw = sum(v for v in [raw_a, raw_b, raw_c] if v is not None)

    # Self-consumption battery: buffer to minimize grid flow
    battery_per_phase = 0.0
    if site.battery_soc is not None:
        if total_raw > 0 and (
            site.is_off_grid or site.battery_soc > (site.battery_soc_min or 0)
        ):
            # Deficit: battery discharges to cover it
            max_discharge = (site.battery_max_discharge_power or 0) / site.voltage
            # Inverter output cap: battery discharge goes through the inverter.
            # If solar already uses all inverter capacity, battery can't discharge.
            # Off-grid the battery covers the whole deficit regardless - the
            # inverter physically overloads past its rating (observed in the
            # field), which is exactly the state the engine must correct.
            # The same holds for the battery's own discharge rating and for
            # the engine's SOC floor: with no grid nothing else can cover the
            # deficit, so the pack does, and the battery power sensor reads
            # it. Capping it here left the modelled inverter output at the
            # site's demand while the battery flow stopped at its rating, so
            # the sim's energy balance broke and the missing watts turned up
            # as solar. Honouring the rating and the floor is the engine's
            # job; invariant C in check_physical_invariants holds it to it.
            if site.is_off_grid:
                max_discharge = float("inf")
            elif site.inverter_max_power:
                inverter_max_current = site.inverter_max_power / site.voltage
                inverter_headroom = max(0, inverter_max_current - solar_total)
                max_discharge = min(max_discharge, inverter_headroom)
            discharge = min(total_raw, max_discharge)
            battery_per_phase = -(discharge / num_phases)
        elif total_raw < 0 and site.battery_soc < (site.battery_soc_full or DEFAULT_BATTERY_SOC_FULL):
            # Surplus: battery charges from it
            max_charge = (site.battery_max_charge_power or 0) / site.voltage
            charge = min(abs(total_raw), max_charge)
            battery_per_phase = charge / num_phases

    # Grid CT = raw + battery effect: a symmetric inverter's solar and battery
    # flow land evenly on every phase.
    nets = [None if r is None else r + battery_per_phase for r in (raw_a, raw_b, raw_c)]
    if site.inverter_supports_asymmetric and num_phases > 1:
        # An asymmetric one places the same net output per phase so the grid
        # phases come out as equal as it can make them (place_asymmetric_output).
        # The site total is unchanged; only its split over the phases moves.
        demand = [
            None if h is None else h + draw
            for h, draw in ((household.a, load_l1), (household.b, load_l2), (household.c, load_l3))
        ]
        cap_w = site.inverter_max_power_per_phase or site.inverter_max_power
        outputs = iter(place_asymmetric_output(
            [d for d in demand if d is not None],
            solar_total - battery_per_phase * num_phases,
            cap_w / site.voltage if cap_w else float("inf"),
        ))
        nets = [None if d is None else d - next(outputs) for d in demand]

    def _ct(net):
        if net is None:
            return None, None, None
        return net, max(0.0, net), max(0.0, -net)

    ct_a_net, ct_a_cons, ct_a_exp = _ct(nets[0])
    ct_b_net, ct_b_cons, ct_b_exp = _ct(nets[1])
    ct_c_net, ct_c_cons, ct_c_exp = _ct(nets[2])

    site.consumption = PhaseValues(ct_a_cons, ct_b_cons, ct_c_cons)
    site.export_current = PhaseValues(ct_a_exp, ct_b_exp, ct_c_exp)

    # Off-grid: there are no grid CTs - production injects 0 A for phases that
    # have inverter output sensors, so the engine always sees zero grid flow.
    if site.is_off_grid:
        def _zero(h):
            return 0.0 if h is not None else None
        site.consumption = PhaseValues(_zero(household.a), _zero(household.b), _zero(household.c))
        site.export_current = PhaseValues(_zero(household.a), _zero(household.b), _zero(household.c))

    # Set battery_power for engine battery awareness in derived mode.
    # Convention: positive = discharging, negative = charging.
    # battery_per_phase: positive = charging (adds demand), negative = discharging.
    if site.battery_soc is not None:
        site.battery_power = -battery_per_phase * site.voltage * num_phases

    # Update per-phase inverter output to reflect actual physical state.
    # Parallel: inverter output = solar per phase (inverter only carries solar)
    # Series: inverter output = household + load draws per phase (all loads go through inverter)
    # Off-grid, either wiring: household + load draws - with no grid to carry
    # the rest, everything the site consumes comes out of the inverter
    # (production's _supply_per_phase reads the output the same way).
    if site.inverter_output_per_phase is not None:
        if site.wiring_topology == 'parallel' and not site.is_off_grid:
            site.inverter_output_per_phase = PhaseValues(
                solar_per_phase if household.a is not None else None,
                solar_per_phase if household.b is not None else None,
                solar_per_phase if household.c is not None else None,
            )
        else:
            # Series, or off-grid: everything downstream goes through inverter
            site.inverter_output_per_phase = PhaseValues(
                ((household.a or 0) + load_l1) if household.a is not None else None,
                ((household.b or 0) + load_l2) if household.b is not None else None,
                ((household.c or 0) + load_l3) if household.c is not None else None,
            )

    return ct_a_net, ct_b_net, ct_c_net, solar_per_phase, battery_per_phase


def simulate_inverter_output(site):
    """Fleet AC output in watts - the sim's stand-in for output_power_total().

    Mirrors engine/fleet.py's two tiers: the measured per-phase output when the
    scenario models output sensors, else the topology-aware estimate (solar plus
    the battery term - signed in series, discharge-only in parallel).

    Called BEFORE apply_feedback_adjustment() for the same reason production
    reads it before its feedback loop: afterwards the derived solar contains the
    managed draws and the estimate is inflated by them.
    """
    if site.inverter_output_per_phase is not None:
        return site.inverter_output_per_phase.total * site.voltage
    bp = site.battery_power or 0.0
    battery_term = bp if site.wiring_topology == 'series' else max(0.0, bp)
    return (site.solar_production_total or 0.0) + battery_term


def apply_feedback_adjustment(site):
    """Replicate dynamic_ocpp_evse.py feedback loop.

    Subtracts load draws from grid CT readings (mapped to site phases via
    get_site_phase_draw()) to recover the true household consumption/export
    before load was drawing.
    In derived mode, recalculates solar_production_total from adjusted export.
    In dedicated solar entity mode, computes household_consumption_total instead
    - and off-grid with no output sensors whenever the battery's power is known.
    """
    # Use phase mapping to get site-phase draws (A, B, C)
    total_phase_a = total_phase_b = total_phase_c = 0.0
    for c in site.loads:
        a_draw, b_draw, c_draw = c.get_site_phase_draw()
        total_phase_a += a_draw
        total_phase_b += b_draw
        total_phase_c += c_draw

    total_l1 = total_phase_a
    total_l2 = total_phase_b
    total_l3 = total_phase_c

    # Off-grid: the grid readings are synthetic zeros that never contained the
    # load draws - production's _apply_feedback_loop returns early without
    # adjusting them (subtracting would fabricate export).
    if not site.is_off_grid and (total_l1 > 0 or total_l2 > 0 or total_l3 > 0):
        # Same pure helper production's _apply_feedback_loop calls.
        site.consumption, site.export_current = grid_without_managed_draws(
            site.consumption,
            site.export_current,
            (total_l1, total_l2, total_l3),
        )

    # Derived mode: recalculate solar_production_total from adjusted export.
    # Battery charging absorbs solar power invisible to grid CT - add it back.
    if site.solar_is_derived:
        if site.inverter_output_per_phase is not None:
            # With output sensors, grid-tied as well as off-grid, production
            # derives solar from the inverter output (engine/fleet.member_solar),
            # never from the export - so must the harness. Until 2026-09-24 it
            # took the export-derived path on a grid-tied site, which is the
            # one configuration where the feedback loop's handed-back draw
            # reaches the inverter pool: it could not see the pool booking a
            # battery-fed car's own draw as spent.
            # Series, and off-grid on either wiring: inverter_output = solar +
            # battery_power, so solar = output - battery. Grid-tied parallel:
            # the inverter output IS solar.
            inv_watts = site.inverter_output_per_phase.total * site.voltage
            if site.wiring_topology == 'series' or site.is_off_grid:
                bp = site.battery_power if site.battery_power is not None else 0
                site.solar_production_total = max(0, inv_watts - bp)
            else:
                site.solar_production_total = max(0, inv_watts)
        else:
            site.solar_production_total = site.export_current.total * site.voltage
            if site.battery_power is not None and site.battery_power < 0:
                site.solar_production_total += abs(site.battery_power)

    # household_consumption_total via energy balance: household = solar +
    # battery_power - export. Off-grid no draw was put back onto the export
    # above, so solar + battery still carries the loads' own draw - take it
    # off here, as production's _apply_household_figures does. Off-grid with no
    # output sensors that balance is the whole supply, built whenever the
    # battery's power is known - at night (solar 0 W) and with no solar sensor
    # too; elsewhere only from a dedicated solar entity reading above 0.
    if site.is_off_grid and site.inverter_output_per_phase is None:
        build_total = site.battery_power is not None or site.battery_soc is None
    else:
        build_total = (
            not site.solar_is_derived and site.solar_production_total > 0
        )
    if build_total:
        export_power = site.export_current.total * site.voltage
        bp = float(site.battery_power) if site.battery_power is not None else 0
        managed_power = (
            (total_l1 + total_l2 + total_l3) * site.voltage
            if site.is_off_grid else 0.0
        )
        site.household_consumption_total = max(
            0, (site.solar_production_total or 0) + bp - export_power
            - managed_power
        )

    # Per-phase household from inverter output entities
    household = compute_household_per_phase(site, site.wiring_topology)
    if household is not None:
        site.household_consumption = household


def check_stability(history, tolerance=0.5):
    """Check that commanded limits are stable over the last STABILITY_CYCLES.

    Returns (is_stable, message).
    """
    if len(history) < STABILITY_CYCLES:
        return True, "Not enough cycles"

    tail = history[-STABILITY_CYCLES:]

    for load_id in tail[0]['commanded'].keys():
        values = [h['commanded'][load_id] for h in tail]
        variation = max(values) - min(values)
        if variation > tolerance:
            return False, f"{load_id} unstable: variation={variation:.2f}A over last {STABILITY_CYCLES} cycles"

    return True, "Stable"


# ---------------------------------------------------------------------------
# Scenario loading and building
# ---------------------------------------------------------------------------

def load_scenarios(yaml_file):
    """Load test scenarios from YAML file."""
    with open(yaml_file, 'r') as f:
        data = yaml.safe_load(f)
    return data['scenarios']


def build_site_from_scenario(scenario, excess_on=False):
    """Build SiteContext from scenario dict.

    YAML values represent physical reality:
    - phase_X_consumption: household load on that phase (Amps)
    - solar_production: total solar production (Watts)
    - battery_*: battery state and limits

    The simulation loop converts these to grid CT values before feeding
    to the engine, matching the production data flow.

    ``excess_on`` is LAST cycle's Excess verdict, and a tank needs it: the
    control layer writes the tank's setpoint from that verdict, and the builder
    reads the resulting label back one cycle stale to decide whether the tank
    is boosting. Without it every tank here was must-run and the boost path -
    the whole reason a tank competes as an Excess load - was unreachable.
    """
    site_data = scenario['site']
    voltage = site_data.get('voltage', 230)

    # Per-phase household consumption (None = phase doesn't exist)
    solar_total = site_data.get('solar_production', 0)
    phase_a_cons = site_data.get('phase_a_consumption')
    phase_b_cons = site_data.get('phase_b_consumption')
    phase_c_cons = site_data.get('phase_c_consumption')

    # Export starts at zero - will be computed by CT simulation in the loop
    phase_a_export = 0.0 if phase_a_cons is not None else None
    phase_b_export = 0.0 if phase_b_cons is not None else None
    phase_c_export = 0.0 if phase_c_cons is not None else None

    # Solar entity mode: solar_production_direct means user has a dedicated sensor
    solar_is_derived = not site_data.get('solar_production_direct', False)

    site = SiteContext(
        voltage=voltage,
        main_breaker_rating=site_data.get('main_breaker_rating', 63),
        consumption=PhaseValues(phase_a_cons, phase_b_cons, phase_c_cons),
        export_current=PhaseValues(phase_a_export, phase_b_export, phase_c_export),
        solar_production_total=solar_total,
        solar_is_derived=solar_is_derived,
        battery_soc=site_data.get('battery_soc'),
        battery_soc_min=site_data.get('battery_soc_min', 20),
        battery_soc_target=site_data.get('battery_soc_target', 80),
        battery_soc_full=site_data.get('battery_soc_full', DEFAULT_BATTERY_SOC_FULL),
        excess_export_threshold=site_data.get('excess_export_threshold', 13000),
        battery_max_charge_power=site_data.get('battery_max_charge_power', 5000),
        battery_max_discharge_power=site_data.get('battery_max_discharge_power', 5000),
        max_grid_import_power=site_data.get('max_import_power'),
        distribution_mode=site_data.get('distribution_mode', 'priority'),
        inverter_max_power=site_data.get('inverter_max_power'),
        inverter_max_power_per_phase=site_data.get('inverter_max_power_per_phase'),
        inverter_supports_asymmetric=site_data.get('inverter_supports_asymmetric', False),
        wiring_topology=site_data.get('wiring_topology', 'parallel'),
        allow_grid_charging=site_data.get('allow_grid_charging', True),
        is_off_grid=site_data.get('off_grid', False),
    )

    # Per-phase inverter output: explicit values or auto-derived from simulation
    inv_out_a = site_data.get('inverter_output_phase_a')
    inv_out_b = site_data.get('inverter_output_phase_b')
    inv_out_c = site_data.get('inverter_output_phase_c')
    inverter_output_sensors = site_data.get('inverter_output_sensors', False)

    if inv_out_a is not None:
        # Explicit per-phase values provided in YAML
        site.inverter_output_per_phase = PhaseValues(inv_out_a, inv_out_b, inv_out_c)
    elif inverter_output_sensors:
        # Auto-derive per-phase inverter output during simulation.
        # Auto-detect wiring topology: series for battery sites, parallel otherwise
        if 'wiring_topology' not in site_data:
            site.wiring_topology = 'series' if site.battery_soc is not None else 'parallel'
        # Initialize with zeros for active phases (simulate_grid_ct updates each cycle)
        site.inverter_output_per_phase = PhaseValues(
            0.0 if phase_a_cons is not None else None,
            0.0 if phase_b_cons is not None else None,
            0.0 if phase_c_cons is not None else None,
        )
    # No solar sensor and no output sensors: solar from the meter alone, as
    # production flags it (engine/fleet.solar_is_metered).
    site.solar_is_metered = solar_is_derived and site.inverter_output_per_phase is None

    # Build loads
    for idx, load_data in enumerate(scenario['loads']):
        device_type = load_data.get("device_type", "evse")
        phases = load_data.get("phases", 1)

        if device_type == "plug":
            power_rating = load_data.get("power_rating", 2000)
            equiv_current = round(power_rating / (voltage * phases), 1)
            min_current = equiv_current
            max_current = equiv_current
            # Plug hardware rating (A) - the cap for available_current,
            # separate from the set-power slider.
            rated_current = load_data.get("plug_max_current", DEFAULT_PLUG_MAX_CURRENT)
        elif device_type == "hot_water_tank":
            # Tank is a fixed-power binary load (like a plug): the heating
            # element draws its full rating or nothing.
            power_rating = load_data.get("power_rating", 2000)
            equiv_current = round(power_rating / (voltage * phases), 1)
            min_current = equiv_current
            max_current = equiv_current
            rated_current = equiv_current
        else:
            min_current = load_data.get("min_current", 6)
            max_current = load_data.get("max_current", 16)
            rated_current = max_current

        # The device type's OperatingMode (its default when the YAML names
        # none) → engine behavior + urgency
        _mode = resolve_operating_mode(device_type, load_data.get("operating_mode"))

        # Cold-tank promotion: a Solar Priority tank below its normal temperature
        # is bumped to the Normal urgency tier (behavior unchanged). Mirrors the
        # production builder in engine/hub_calculation.py.
        mode_priority = _mode.priority
        mode_behavior = _mode.behavior
        if device_type == "hot_water_tank":
            # The setpoint label, as control/hot_water_tank.py resolves it:
            # Freeze Protection and Normal both ride surplus up to the boost
            # setpoint, and every other mode keeps its own.
            current_temp = load_data.get("current_temperature")
            normal_temp = load_data.get(
                "normal_temperature", DEFAULT_TANK_NORMAL_TEMPERATURE
            )
            away_temp = load_data.get("away_temperature", DEFAULT_TANK_AWAY_TEMPERATURE)
            setpoint_label = None
            if _mode.key in (TANK_MODE_FREEZE_PROTECTION.key, "Normal"):
                setpoint_label = "boost" if excess_on else (
                    "away" if _mode.key == TANK_MODE_FREEZE_PROTECTION.key else "normal"
                )
            mode_priority, _ = resolve_tank_mode_priority(
                _mode.key,
                _mode.priority,
                current_temp,
                normal_temp,
                load_data.get("prioritize_below_normal", True),
                setpoint_label,
            )
            # ...and the behavior from the same label, mirroring
            # engine/load_builders.py: a tank heating past what its mode asks
            # for, on energy the site would otherwise dump, is opportunistic
            # and competes as an Excess load. Below its mode's own floor it
            # stays unconditional - that guard is what keeps frost protection
            # from ever being gated.
            if tank_boost_is_opportunistic(
                _mode.key,
                setpoint_label,
                current_temp,
                away_temp if _mode.key == TANK_MODE_FREEZE_PROTECTION.key else normal_temp,
            ):
                mode_behavior = BEHAVIOR_BINARY_EXCESS

        load = LoadContext(
            load_id=f"load_{idx}",
            entity_id=load_data.get("entity_id", f"load_{idx}"),
            min_current=min_current,
            max_current=max_current,
            phases=phases,
            priority=load_data.get("priority", idx),
            device_type=device_type,
            operating_mode=_mode.key,
            mode_behavior=mode_behavior,
            mode_priority=mode_priority,
            l1_phase=load_data.get("l1_phase", "A"),
            l2_phase=load_data.get("l2_phase", "B"),
            l3_phase=load_data.get("l3_phase", "C"),
            connector_status=load_data.get("connector_status",
                                              "Available" if load_data.get("active") is False else "Charging"),
            l1_current=load_data.get("l1_current", 0),
            l2_current=load_data.get("l2_current", 0),
            l3_current=load_data.get("l3_current", 0),
            unmetered=load_data.get("unmetered", False),
            # A scenario can hand a load back to the user, as the Dynamic
            # Control switch does: its draw becomes household and it competes
            # for nothing.
            dynamic_control=load_data.get("dynamic_control", True),
            rated_current=rated_current,
        )
        site.loads.append(load)

    # Build circuit groups
    load_id_by_entity = {c.entity_id: c.load_id for c in site.loads}
    for idx, group_data in enumerate(site_data.get('circuit_groups', [])):
        member_entities = group_data.get('members', [])
        member_ids = [load_id_by_entity[e] for e in member_entities if e in load_id_by_entity]
        group = CircuitGroup(
            group_id=f"group_{idx}",
            name=group_data.get('name', f"group_{idx}"),
            current_limit=group_data['current_limit'],
            member_ids=member_ids,
        )
        site.circuit_groups.append(group)

    return site


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------

def print_scenario_params(scenario):
    """Print the site and loads build_site_from_scenario makes of a scenario,
    for trace/verbose output."""
    site = build_site_from_scenario(scenario)
    cons = [(ph, val) for ph, val in zip("ABC", (
        site.consumption.a, site.consumption.b, site.consumption.c)) if val is not None]
    cons_str = '/'.join(f"{ph}={val}A" for ph, val in cons) or 'none'

    print(f"  Site: {site.voltage}V {site.main_breaker_rating}A breaker {len(cons) or 1}ph"
          f" | Solar {site.solar_production_total}W | Dist: {site.distribution_mode}")
    if site.max_grid_import_power:
        print(f"        Max import: {site.max_grid_import_power}W")
    print(f"  Consumption: {cons_str}")
    if site.battery_soc is not None:
        print(f"  Battery: soc={site.battery_soc}% min={site.battery_soc_min}% "
              f"target={site.battery_soc_target}% | charge={site.battery_max_charge_power}W "
              f"discharge={site.battery_max_discharge_power}W")
    if site.inverter_max_power or site.inverter_max_power_per_phase or site.inverter_supports_asymmetric:
        parts = []
        if site.inverter_max_power:
            parts.append(f"max={site.inverter_max_power}W")
        if site.inverter_max_power_per_phase:
            parts.append(f"per_phase={site.inverter_max_power_per_phase}W")
        parts.append(f"asymmetric={site.inverter_supports_asymmetric}")
        print(f"  Inverter: {' '.join(parts)}")
    print(f"  Excess threshold: {site.excess_export_threshold}W")

    for ch, load in zip(scenario['loads'], site.loads):
        phase_map_str = ""
        if (load.l1_phase, load.l2_phase, load.l3_phase) != ("A", "B", "C"):
            phase_map_str = f" map=L1→{load.l1_phase}/L2→{load.l2_phase}/L3→{load.l3_phase}"
        kind = {'hot_water_tank': 'tank'}.get(load.device_type, load.device_type)
        what = f"{kind} {load.min_current}-{load.max_current}A"
        if kind in ('plug', 'tank'):
            what = f"{kind} {ch.get('power_rating', 2000)}W"
        temp_str = ""
        if ch.get('current_temperature') is not None:
            temp_str = f" temp={ch['current_temperature']}°C tier={load.mode_priority}"
        print(f"  Load {load.entity_id}: {what} {load.phases}ph mask={load.active_phases_mask} "
              f"prio={load.priority} mode={load.operating_mode}{phase_map_str}{temp_str} "
              f"[{load.connector_status}]")

    # Expected
    expected = scenario.get('expected', {})
    exp_parts = []
    for eid, vals in expected.items():
        alloc = vals.get('allocated', '?')
        exp_parts.append(f"{eid}={alloc}A")
    if exp_parts:
        print(f"  Expected: {', '.join(exp_parts)}")
    print()


def run_scenario_simulation(scenario, verbose=False, trace=False):
    """Run 30-cycle simulation for a scenario.

    Cycles 0-4:   Site values ramp from 0 to target (cold start).
    Cycles 5-24:  Warmup with ramp rate limiting on load output.
    Cycles 25-29: Stability check - engine targets and commanded limits
                  must converge.

    Returns (passed, errors, history).
    """
    if verbose:
        print_scenario_params(scenario)

    commanded_limits = {}  # entity_id -> current commanded limit
    history = []

    # Excess latch state, the hub_runtime["_excess_on"] equivalent: the widened
    # release band only applies while Excess was already engaged last cycle.
    excess_on = False
    invariant_breaches = set()  # physical guards, deduplicated across cycles

    # Per-load draw-settle tracking: last measured draw and the count of
    # consecutive cycles it has held steady. Mirrors the HA layer's per-load
    # runtime state so the engine sees the same draw_settled flag.
    settle_last_draw = {}   # entity_id -> last cycle's measured draw
    settle_count = {}       # entity_id -> consecutive steady cycles
    last_permit = {}        # entity_id -> last cycle's available_current

    # Per-load utilization (0.0–1.0): the fraction of the commanded permit
    # the device actually draws. 1.0 (default) = draws its full permit; 0.0 =
    # switched on but idle. Models a car taking less than offered, or an
    # appliance behind a plug drawing nothing.
    #
    # draw_cap (optional, Amps): hard ceiling on the simulated draw - models a
    # car whose battery limits how much it can take regardless of what we
    # offer (e.g. a 16 A car on a 32 A EVSE). measured = min(commanded × util,
    # draw_cap). Defaults to no cap.
    utilization = {}
    draw_cap = {}
    for idx, cd in enumerate(scenario['loads']):
        eid = cd.get('entity_id', f"load_{idx}")
        utilization[eid] = cd.get('utilization', 1.0)
        draw_cap[eid] = cd.get('draw_cap')

    for cycle in range(TOTAL_CYCLES):
        # 1. Build site from YAML (household consumption, solar production, battery)
        # Last cycle's verdict, so a tank's boost setpoint (and therefore its
        # behavior) is resolved the way the control layer does it.
        site = build_site_from_scenario(scenario, excess_on=excess_on)

        # 2. Scale household + solar for cold-start ramp-up (cycles 0-4)
        if cycle < RAMP_UP_CYCLES:
            t = (cycle + 1) / RAMP_UP_CYCLES
            scale_site_values(site, t)

        # Save household consumption before CT simulation overwrites it, and
        # the PHYSICAL solar total before the feedback loop re-derives it - the
        # invariant check below needs the site as the sky made it, not as the
        # engine reconstructed it.
        household = PhaseValues(site.consumption.a, site.consumption.b, site.consumption.c)
        physical_solar_w = site.solar_production_total

        # 3. Set load l1/l2/l3_current from previous commanded limits,
        #    scaled by utilization - the device may draw less than its permit.
        #    Then update draw-settle tracking: a draw that has held steady for
        #    SETTLE_CYCLES is trusted as the EVSE's real footprint.
        for load in site.loads:
            cmd = commanded_limits.get(load.entity_id, 0)
            util = utilization.get(load.entity_id, 1.0)
            simulated = cmd * util
            cap = draw_cap.get(load.entity_id)
            if cap is not None:
                simulated = min(simulated, cap)
            set_load_phase_currents(load, simulated)

            eid = load.entity_id
            draw = max(load.l1_current, load.l2_current, load.l3_current)
            # Like the HA layer, the window runs only while Charging and opens
            # afresh when charging starts: a 0 A held while waiting or
            # suspended is not a settled draw.
            if load.connector_status != "Charging":
                settle_last_draw.pop(eid, None)
                settle_count[eid] = 0
                load.draw_settled = False
                continue
            prev = settle_last_draw.get(eid)
            if prev is not None and abs(draw - prev) <= SETTLE_TOLERANCE:
                settle_count[eid] = settle_count.get(eid, 0) + 1
            else:
                settle_count[eid] = 0
            settle_last_draw[eid] = draw
            steady = settle_count[eid] >= SETTLE_CYCLES
            under_permit = draw + SETTLE_PERMIT_MARGIN < last_permit.get(eid, 0)
            load.draw_settled = steady and under_permit

        # 4. Compute grid CT values from physical inputs
        #    net = household - solar_per_phase + battery_per_phase + load_draw
        #    Map load L1/L2/L3 draws to site phases A/B/C via phase mapping
        load_phase_a = load_phase_b = load_phase_c = 0.0
        for c in site.loads:
            a_draw, b_draw, c_draw = c.get_site_phase_draw()
            load_phase_a += a_draw
            load_phase_b += b_draw
            load_phase_c += c_draw
        ct_a_net, ct_b_net, ct_c_net, solar_pp, bat_pp = simulate_grid_ct(
            site, household, load_phase_a, load_phase_b, load_phase_c)
        # A battery whose power is not read (no sensor, or one past its
        # INPUT_STALE_TIMEOUT): it still does what the CT simulation says, the
        # engine just cannot see it - production's reader leaves None.
        if scenario['site'].get('battery_power_unread'):
            site.battery_power = None

        # 4b. Read-time figures the engine captures before its feedback loop and
        #     the calculator's inverter coverage gate reads: the raw meter and
        #     the fleet's AC output. Off grid there are no CTs at all, so
        #     grid_current stays unset (all-None → 0 W net), exactly as
        #     production leaves it when no phase entity is configured.
        if not site.is_off_grid:
            site.grid_current = PhaseValues(ct_a_net, ct_b_net, ct_c_net)
        site.net_grid_power = site.grid_current.total * site.voltage
        site.inverter_output_total = simulate_inverter_output(site)

        # 5. Apply feedback: subtract load draws (replicates dynamic_ocpp_evse.py)
        apply_feedback_adjustment(site)

        # 6. Excess trigger + hysteresis latch (replicates the same block in
        #    engine/hub_calculation.py - the calculator itself is stateless and
        #    just reads site.excess_hysteresis). Scenarios that leave
        #    `excess_hysteresis` unset get 0 and the latch is a no-op.
        hysteresis = scenario['site'].get('excess_hysteresis', 0)
        margin = excess_margin(site, hysteresis if excess_on else 0)
        excess_on = margin >= 0
        site.excess_hysteresis = hysteresis if excess_on else 0

        # 7. Run calculation engine
        calculate_all_load_targets(site)

        # 7b. Physical invariants - every scenario, every settled cycle. Only
        #     past the ramp-up: cycles 0-4 scale household and solar toward
        #     their real values, so the site is deliberately not yet the site
        #     the scenario describes and a transient breach there says nothing.
        #     The convergence the harness already tests is what makes the
        #     settled cycles the right place to assert physics.
        if cycle >= RAMP_UP_CYCLES:
            for breach in check_physical_invariants(site, household, physical_solar_w):
                invariant_breaches.add(f"cycle {cycle}: {breach}")

        # Remember this cycle's permit per load - the next cycle's settle
        # check uses it to tell "car capped below offer" from "car at offer".
        for c in site.loads:
            last_permit[c.entity_id] = c.available_current

        # 8. Set each load's commanded value for the next cycle.
        #    EVSE: ramp-limited toward the permit (available_current).
        #    Plug/tank: binary - its set power when the engine powers it
        #    (permit > 0), else 0; no ramp.
        for load in site.loads:
            if load.device_type == "plug":
                commanded_limits[load.entity_id] = (
                    load.max_current if load.available_current > 0 else 0
                )
            else:
                target = load.available_current
                prev = commanded_limits.get(load.entity_id, 0)
                commanded_limits[load.entity_id] = apply_ramp_rate(prev, target)

        # 9. Record history
        history.append({
            'cycle': cycle,
            'engine_targets': {c.entity_id: c.allocated_current for c in site.loads},
            'commanded': {c.entity_id: commanded_limits[c.entity_id] for c in site.loads},
        })

        if verbose:
            parts = []
            for load in site.loads:
                eid = load.entity_id
                parts.append(f"{eid}={commanded_limits[eid]:.1f}A(t={load.allocated_current:.1f})")
            line = f"  Cycle {cycle:2d}: {', '.join(parts)}"
            if trace:
                def _fmt_signed(v):
                    return f"{v:+.1f}" if v is not None else "-"
                # Grid CT: signed net per phase (positive=import, negative=export)
                grid_str = f"grid=({_fmt_signed(ct_a_net)}/{_fmt_signed(ct_b_net)}/{_fmt_signed(ct_c_net)})"
                # Inverter: per-phase current + solar/battery power in watts
                inv_a = f"{solar_pp:.1f}A" if household.a is not None else "-"
                inv_b = f"{solar_pp:.1f}A" if household.b is not None else "-"
                inv_c = f"{solar_pp:.1f}A" if household.c is not None else "-"
                inv_detail = f"solar={site.solar_production_total:.0f}W"
                if site.battery_soc is not None:
                    bat_watts = bat_pp * site.voltage * (household.active_count or 1)
                    inv_detail += f" bat={bat_watts:+.0f}W"
                inv_str = f"inverter=({inv_a}/{inv_b}/{inv_c} {inv_detail})"
                # Household load from YAML
                house_str = f"house=({_fmt_phase(household.a)}/{_fmt_phase(household.b)}/{_fmt_phase(household.c)})"
                # Sum of load draws per site phase (mapped from L1/L2/L3)
                ch_sum_str = f"ch_sum=({load_phase_a:.1f}/{load_phase_b:.1f}/{load_phase_c:.1f})"
                # Battery: per-phase current in (A/B/C) format + SOC
                bat_str = ""
                if site.battery_soc is not None:
                    ba = _fmt_signed(bat_pp) if household.a is not None else "-"
                    bb = _fmt_signed(bat_pp) if household.b is not None else "-"
                    bc = _fmt_signed(bat_pp) if household.c is not None else "-"
                    bat_str = f"bat=({ba}/{bb}/{bc} soc={site.battery_soc:.0f}%)"
                line += f"  | {grid_str} {inv_str} {house_str} {ch_sum_str}"
                if bat_str:
                    line += f" {bat_str}"
            print(line)

    # --- Validate engine targets from last cycle against expected values ---
    passed, errors = validate_results(scenario, site)

    # --- Check stability over last STABILITY_CYCLES ---
    is_stable, stability_msg = check_stability(history)
    if not is_stable:
        passed = False
        errors.append(f"Stability check failed: {stability_msg}")

    # --- Physical invariants: no scenario may breach them, whatever it tests ---
    #
    # A scenario may declare `known_invariant_breaches:` - a list of substrings,
    # each with the reason in its own comment. The bookkeeping is two-way on
    # purpose: an unlisted breach FAILS (so a new one cannot hide behind an old
    # one), and a listed substring that no longer occurs also FAILS (so the
    # exemption is deleted when the bug is fixed, instead of quietly outliving
    # it).
    known = scenario.get("known_invariant_breaches") or []
    unmatched = [
        b for b in sorted(invariant_breaches)
        if not any(k in b for k in known)
    ]
    stale = [k for k in known if not any(k in b for b in invariant_breaches)]
    if unmatched:
        passed = False
        for breach in unmatched:
            errors.append(f"Physical invariant: {breach}")
    if stale:
        passed = False
        for k in stale:
            errors.append(
                f"known_invariant_breaches lists '{k}' but nothing breached it "
                "- fixed? delete the entry"
            )

    return passed, errors, history


# Slack on the invariant checks below: float noise in the CT simulation, plus
# the register quantisation a real device applies. Well under the smallest
# decision the engine makes.
INVARIANT_TOLERANCE = 0.05  # A


def check_physical_invariants(site, household, physical_solar_w):
    """Physical guards on what the engine just decided - run on EVERY scenario.

    The scenarios' own ``expected`` blocks say what each site should allocate.
    These say what NO site may ever do, whatever it was written to test, and
    they are the half that catches a bug nobody thought to write a scenario
    for. Both are stated against the physical inputs the CT simulation was
    built from (household, solar, battery), not against the engine's own view
    of them - checking the engine against its own reconstruction would only
    prove it is self-consistent, which every bug in this class already was.

    **A - the breaker.** With the permits just issued actually drawn, no phase
    may exceed ``main_breaker_rating``.

    **B - a modulating Excess load may never cause import.** Recompute the
    site's position with those loads' permits removed: if removing them turns
    import into less import, the engine sized them on surplus that was not
    there. Restricted to ``BEHAVIOR_EXCESS`` on purpose - it is the one
    behaviour the engine SIZES against a surplus pool, so it has no excuse.
    Binary Excess loads are exempt by decision (Anže, 2026-09-07: a 2 kW
    element on a smaller surplus still boosts, because the overshoot costs
    export), and Solar Priority is exempt because it deliberately runs on a
    grid-backed minimum below the SOC target.

    **C - off-grid, our loads stay inside the battery and the inverter.** With
    no grid the pack covers every deficit (see ``simulate_grid_ct``), so an
    over-allocation shows up as the battery discharging past its rating, or
    below the SOC floor, and as the inverter delivering past its own. Measured
    against the site with every managed load off: our loads may take the pack
    up to its discharge rating while the SOC is at/above its minimum and not at
    all below it, and the inverter up to its rating - never past what the house
    alone already asks of either. With no pack at all nothing covers a deficit,
    and the sun is the whole supply: our loads may take the site up to it.

    **D - grid-tied, our loads stay inside the import allowance.** With the
    allocations drawn, the site's grid import (summed over the phases that
    import, the figure the engine budgets ``max_grid_import_power`` on) may not
    exceed the allowance - or, when the house alone already imports past it,
    what the house alone imports. With *Allow Grid Charging* off on a battery
    site the allowance is 0 W ("charging stops when it would require grid
    import", README). The breaker (A) is per phase and an Excess load's import
    (B) is only one behaviour; this is the site-wide limit every behaviour
    shares, and the one a pool that credits the inverter's output twice
    breaks.

    Returns a list of violation strings; empty means legal.
    """
    saved = [(c, c.l1_current, c.l2_current, c.l3_current) for c in site.loads]
    # The feedback loop re-derives solar_production_total on a derived-solar
    # site, so the value on `site` by now already has the loads' draws folded
    # in. Feeding that back into the CT simulation would count them twice and
    # report an import the site never had - restore the physical figure for the
    # duration of the check. (Missing this was worth ~2 A on a 460 W site.)
    saved_solar = site.solar_production_total
    site.solar_production_total = physical_solar_w

    def _phase_draws():
        a = b = c = 0.0
        for load in site.loads:
            pa, pb, pc = load.get_site_phase_draw()
            a += pa
            b += pb
            c += pc
        return a, b, c

    try:
        for load in site.loads:
            # ALLOCATED, not available: the permit is a per-load ceiling and two
            # loads sharing a phase are each offered more than the phase can
            # give them together. The allocation is the engine's actual
            # decision, and the only figure physics has to honour.
            set_load_phase_currents(load, load.allocated_current)
        drawn = _phase_draws()
        with_all_sim = simulate_grid_ct(site, household, *drawn)
        with_all = with_all_sim[:3]
        for load in site.loads:
            if load.mode_behavior == BEHAVIOR_EXCESS:
                set_load_phase_currents(load, 0)
        without_excess = simulate_grid_ct(site, household, *_phase_draws())[:3]
        for load in site.loads:
            set_load_phase_currents(load, 0)
        without_loads_sim = simulate_grid_ct(site, household, *_phase_draws())
    finally:
        for load, l1, l2, l3 in saved:
            load.l1_current, load.l2_current, load.l3_current = l1, l2, l3
        site.solar_production_total = saved_solar

    violations = []
    breaker = site.main_breaker_rating or 0
    for label, net, bare in zip("ABC", with_all, without_excess):
        if net is None:
            continue
        if breaker and net > breaker + INVARIANT_TOLERANCE:
            violations.append(
                f"phase {label}: {net:.2f} A drawn against a {breaker:.0f} A breaker"
            )
        extra = max(0.0, net) - max(0.0, bare or 0.0)
        if extra > INVARIANT_TOLERANCE:
            violations.append(
                f"phase {label}: modulating Excess loads add {extra:.2f} A of import"
            )
    if site.is_off_grid:
        violations.extend(
            _off_grid_violations(
                site, household, drawn, with_all_sim, without_loads_sim, physical_solar_w
            )
        )
    else:
        violations.extend(_import_violations(site, with_all_sim, without_loads_sim))
    return violations


def _import_violations(site, with_all_sim, without_loads_sim):
    """Invariant D of check_physical_invariants - see there."""
    violations = []
    if not site.allow_grid_charging and site.battery_soc is not None:
        allowance = 0.0
    elif site.max_grid_import_power is not None:
        allowance = site.max_grid_import_power / site.voltage
    else:
        return violations

    def _import(sim):
        return sum(max(0.0, net) for net in sim[:3] if net is not None)

    import_with = _import(with_all_sim)
    import_bare = _import(without_loads_sim)
    allowed = max(import_bare, allowance)
    if import_with > allowed + INVARIANT_TOLERANCE:
        violations.append(
            f"grid import {import_with:.2f} A against the {allowed:.2f} A "
            f"our loads may take it to ({(import_with - allowed) * site.voltage:.0f} W "
            f"over; allowance {allowance:.2f} A, house alone {import_bare:.2f} A)"
        )
    return violations


def _off_grid_violations(site, household, drawn, with_all_sim, without_loads_sim,
                         physical_solar_w):
    """Invariant C of check_physical_invariants - see there."""
    violations = []
    if site.battery_soc is None:
        # No pack: the sun is all there is, and simulate_grid_ct lets the
        # deficit vanish (no battery takes it), so it has to be checked here.
        sun = (physical_solar_w or 0) / site.voltage
        demand = household.total + sum(drawn)
        allowed_sun = max(household.total, sun)
        if demand > allowed_sun + INVARIANT_TOLERANCE:
            violations.append(
                f"site draws {demand:.2f} A against the {sun:.2f} A of sun, "
                f"with no battery to cover it ({(demand - allowed_sun) * site.voltage:.0f} W over)"
            )
    n = household.active_count or 1
    # battery_per_phase (the sim's 5th value) is negative while discharging.
    discharge_with = max(0.0, -with_all_sim[4]) * n
    discharge_bare = max(0.0, -without_loads_sim[4]) * n
    dischargeable = (
        site.battery_soc is not None
        and site.battery_soc >= (site.battery_soc_min or 0)
    )
    rating = (site.battery_max_discharge_power or 0) / site.voltage if dischargeable else 0.0
    allowed = max(discharge_bare, rating)
    if discharge_with > allowed + INVARIANT_TOLERANCE:
        violations.append(
            f"battery discharges {discharge_with:.2f} A against the {allowed:.2f} A "
            f"our loads may take it to (house alone {discharge_bare:.2f} A)"
        )
    if site.inverter_max_power:
        # Off-grid the inverter delivers everything the site draws.
        output_bare = household.total
        output_with = output_bare + sum(drawn)
        allowed_out = max(output_bare, site.inverter_max_power / site.voltage)
        if output_with > allowed_out + INVARIANT_TOLERANCE:
            violations.append(
                f"inverter delivers {output_with:.2f} A against its "
                f"{allowed_out:.2f} A rating (house alone {output_bare:.2f} A)"
            )
    # ...and each phase's leg what is on that phase, up to the leg's rating:
    # the configured per-phase one, or for a SYMMETRIC inverter a third of the
    # total - what symmetric means for its legs.
    leg = site.inverter_max_power_per_phase or (
        site.inverter_max_power / n
        if site.inverter_max_power and not site.inverter_supports_asymmetric
        else None
    )
    if leg:
        houses = (household.a, household.b, household.c)
        for label, house, draw in zip("ABC", houses, drawn):
            if house is None:
                continue
            allowed_leg = max(house, leg / site.voltage)
            if house + draw > allowed_leg + INVARIANT_TOLERANCE:
                violations.append(
                    f"phase {label}: inverter leg delivers {house + draw:.2f} A "
                    f"against its {allowed_leg:.2f} A rating "
                    f"({(house + draw - allowed_leg) * site.voltage:.0f} W over)"
                )
    return violations


def validate_results(scenario, site):
    """Validate test results against expected values."""
    expected = scenario['expected']
    passed = True
    errors = []

    for load in site.loads:
        entity_id = load.entity_id
        if entity_id in expected:
            expected_allocated = expected[entity_id]['allocated']
            actual_allocated = load.allocated_current

            if abs(actual_allocated - expected_allocated) > 0.1:
                passed = False
                errors.append(
                    f"{entity_id}: expected allocated={expected_allocated}A, got {actual_allocated:.1f}A"
                )
            else:
                errors.append(
                    f"{entity_id}: allocated={actual_allocated:.1f}A"
                )

            if 'available' in expected[entity_id]:
                expected_available = expected[entity_id]['available']
                actual_available = load.available_current
                if abs(actual_available - expected_available) > 0.1:
                    passed = False
                    errors.append(
                        f"{entity_id}: expected available={expected_available}A, got {actual_available:.1f}A"
                    )
                else:
                    errors.append(
                        f"{entity_id}: available={actual_available:.1f}A"
                    )

    return passed, errors
