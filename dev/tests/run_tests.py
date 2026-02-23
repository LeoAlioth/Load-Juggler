#!/usr/bin/env python3
"""
Multi-cycle simulation test runner for EVSE distribution.
Uses ACTUAL production code - no duplicates!

Every scenario runs a 30-cycle simulation:
  - Cycles 0-4:   Ramp-up (site values interpolate from 0 to target)
  - Cycles 5-24:  Warmup (full site values, ramp rate limiting on charger output)
  - Cycles 25-29: Stability check (verify convergence)
"""

import sys
import yaml
from pathlib import Path
from datetime import datetime

# Load calculation modules directly from files to avoid importing Home Assistant-dependent
# package __init__.py which imports 'homeassistant'.
import importlib.util
import types
import sys

repo_root = Path(__file__).parents[2]
_comp_dir = repo_root / "custom_components" / "dynamic_ocpp_evse"
_calc_dir = _comp_dir / "calculations"

# Build proper package hierarchy so relative imports in target_calculator.py work.
_PKG_ROOT = "custom_components"
_PKG_COMP = "custom_components.dynamic_ocpp_evse"
_PKG_CALC = "custom_components.dynamic_ocpp_evse.calculations"

# Create stub namespace packages
for _pkg_name in (_PKG_ROOT, _PKG_COMP, _PKG_CALC):
    if _pkg_name not in sys.modules:
        _pkg = types.ModuleType(_pkg_name)
        _pkg.__path__ = []  # make it a package
        _pkg.__package__ = _pkg_name
        sys.modules[_pkg_name] = _pkg


def _load_module_as(fqn, path):
    """Load a module with its fully-qualified name so relative imports resolve."""
    spec = importlib.util.spec_from_file_location(fqn, str(path))
    module = importlib.util.module_from_spec(spec)
    # Set __package__ to the parent package so `from .x` and `from ..x` work
    module.__package__ = fqn.rsplit(".", 1)[0] if "." in fqn else fqn
    sys.modules[fqn] = module
    spec.loader.exec_module(module)
    return module


# 1) Load const (needed by target_calculator's `from ..const import ...`)
_load_module_as(f"{_PKG_COMP}.const", _comp_dir / "const.py")

# 2) Load models and utils (no relative imports of their own)
_load_module_as(f"{_PKG_CALC}.models", _calc_dir / "models.py")
_load_module_as(f"{_PKG_CALC}.utils", _calc_dir / "utils.py")

# 3) Load target_calculator (has relative imports: .models, .utils, ..const)
_load_module_as(f"{_PKG_CALC}.target_calculator", _calc_dir / "target_calculator.py")

# Convenience aliases for the rest of this file
from custom_components.dynamic_ocpp_evse.calculations.models import LoadContext, SiteContext, PhaseValues
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import calculate_all_charger_targets
from custom_components.dynamic_ocpp_evse.calculations.utils import compute_household_per_phase

# ---------------------------------------------------------------------------
# Mode name migration (old YAML → new operating modes)
# ---------------------------------------------------------------------------
_MIGRATE_MODE_NAMES = {
    "Eco": "Solar Priority",
    "Solar": "Solar Only",
    # Standard and Excess are unchanged
}

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


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------

def scale_site_values(site, t):
    """Scale dynamic site values by factor t (0.0 to 1.0) for cold-start ramp-up.

    Scales household consumption and solar_production_total.
    Export is NOT scaled — it's computed from scratch each cycle by the CT sim.
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


def set_charger_phase_currents(charger, commanded_limit):
    """Set charger l1/l2/l3_current from commanded limit based on phase mapping.

    Uses the charger's L1/L2/L3 → site phase mapping (l1_phase, l2_phase, l3_phase)
    to determine which OCPP phases are active. L1 is always used; L2/L3 depend on
    the charger's active_phases_mask containing the corresponding site phases.
    """
    charger.l1_current = 0
    charger.l2_current = 0
    charger.l3_current = 0
    if commanded_limit <= 0:
        return
    mask = (charger.active_phases_mask or "").upper()
    # L1 is always active if its mapped site phase is in the mask
    if charger.l1_phase in mask:
        charger.l1_current = commanded_limit
    if charger.l2_phase in mask:
        charger.l2_current = commanded_limit
    if charger.l3_phase in mask:
        charger.l3_current = commanded_limit


BATTERY_FULL_SOC = 97  # Battery considered full above this SOC


def simulate_grid_ct(site, household, charger_l1, charger_l2, charger_l3):
    """Compute grid CT readings using self-consumption battery model.

    Physical model:
    1. Raw demand per phase = household - solar + charger_draw
    2. Battery responds to minimize grid flow (self-consumption):
       - Deficit (raw > 0): discharges min(deficit, max_discharge) if SOC > min_soc
       - Surplus (raw < 0): charges min(surplus, max_charge) if SOC < 97%
    3. Grid CT = raw demand + battery effect

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

    raw_a = _raw(household.a, charger_l1)
    raw_b = _raw(household.b, charger_l2)
    raw_c = _raw(household.c, charger_l3)

    # Total raw demand across active phases
    total_raw = sum(v for v in [raw_a, raw_b, raw_c] if v is not None)

    # Self-consumption battery: buffer to minimize grid flow
    battery_per_phase = 0.0
    if site.battery_soc is not None:
        if total_raw > 0 and site.battery_soc > (site.battery_soc_min or 0):
            # Deficit: battery discharges to cover it
            max_discharge = (site.battery_max_discharge_power or 0) / site.voltage
            # Inverter output cap: battery discharge goes through the inverter.
            # If solar already uses all inverter capacity, battery can't discharge.
            if site.inverter_max_power:
                inverter_max_current = site.inverter_max_power / site.voltage
                inverter_headroom = max(0, inverter_max_current - solar_total)
                max_discharge = min(max_discharge, inverter_headroom)
            discharge = min(total_raw, max_discharge)
            battery_per_phase = -(discharge / num_phases)
        elif total_raw < 0 and site.battery_soc < BATTERY_FULL_SOC:
            # Surplus: battery charges from it
            max_charge = (site.battery_max_charge_power or 0) / site.voltage
            charge = min(abs(total_raw), max_charge)
            battery_per_phase = charge / num_phases

    # Grid CT = raw + battery effect
    def _ct(raw_val):
        if raw_val is None:
            return None, None, None
        net = raw_val + battery_per_phase
        return net, max(0.0, net), max(0.0, -net)

    ct_a_net, ct_a_cons, ct_a_exp = _ct(raw_a)
    ct_b_net, ct_b_cons, ct_b_exp = _ct(raw_b)
    ct_c_net, ct_c_cons, ct_c_exp = _ct(raw_c)

    site.consumption = PhaseValues(ct_a_cons, ct_b_cons, ct_c_cons)
    site.export_current = PhaseValues(ct_a_exp, ct_b_exp, ct_c_exp)

    # Set battery_power for engine battery awareness in derived mode.
    # Convention: positive = discharging, negative = charging.
    # battery_per_phase: positive = charging (adds demand), negative = discharging.
    if site.battery_soc is not None:
        site.battery_power = -battery_per_phase * site.voltage * num_phases

    # Update per-phase inverter output to reflect actual physical state.
    # Parallel: inverter output = solar per phase (inverter only carries solar)
    # Series: inverter output = household + charger draws per phase (all loads go through inverter)
    if site.inverter_output_per_phase is not None:
        if site.wiring_topology == 'parallel':
            site.inverter_output_per_phase = PhaseValues(
                solar_per_phase if household.a is not None else None,
                solar_per_phase if household.b is not None else None,
                solar_per_phase if household.c is not None else None,
            )
        else:
            # Series: everything downstream goes through inverter
            site.inverter_output_per_phase = PhaseValues(
                ((household.a or 0) + charger_l1) if household.a is not None else None,
                ((household.b or 0) + charger_l2) if household.b is not None else None,
                ((household.c or 0) + charger_l3) if household.c is not None else None,
            )

    return ct_a_net, ct_b_net, ct_c_net, solar_per_phase, battery_per_phase


def apply_feedback_adjustment(site):
    """Replicate dynamic_ocpp_evse.py feedback loop.

    Subtracts charger draws from grid CT readings (mapped to site phases via
    get_site_phase_draw()) to recover the true household consumption/export
    before charger was drawing.
    In derived mode, recalculates solar_production_total from adjusted export.
    In dedicated solar entity mode, computes household_consumption_total instead.
    """
    # Use phase mapping to get site-phase draws (A, B, C)
    total_phase_a = total_phase_b = total_phase_c = 0.0
    for c in site.chargers:
        a_draw, b_draw, c_draw = c.get_site_phase_draw()
        total_phase_a += a_draw
        total_phase_b += b_draw
        total_phase_c += c_draw

    total_l1 = total_phase_a
    total_l2 = total_phase_b
    total_l3 = total_phase_c

    if total_l1 > 0 or total_l2 > 0 or total_l3 > 0:
        def _adjust(cons, exp, draw):
            if cons is None:
                return None, None
            raw_grid = cons - (exp or 0)
            true_grid = raw_grid - draw
            return max(0.0, true_grid), max(0.0, -true_grid)

        adj_a_cons, adj_a_exp = _adjust(site.consumption.a, site.export_current.a, total_l1)
        adj_b_cons, adj_b_exp = _adjust(site.consumption.b, site.export_current.b, total_l2)
        adj_c_cons, adj_c_exp = _adjust(site.consumption.c, site.export_current.c, total_l3)

        site.consumption = PhaseValues(adj_a_cons, adj_b_cons, adj_c_cons)
        site.export_current = PhaseValues(adj_a_exp, adj_b_exp, adj_c_exp)

    # Derived mode: recalculate solar_production_total from adjusted export.
    # Battery charging absorbs solar power invisible to grid CT — add it back.
    if site.solar_is_derived:
        site.solar_production_total = site.export_current.total * site.voltage
        if site.battery_power is not None and site.battery_power < 0:
            site.solar_production_total += abs(site.battery_power)
    else:
        # Dedicated solar entity mode: compute household_consumption_total
        # via energy balance: household = solar + battery_power - export
        if site.solar_production_total > 0:
            export_power = site.export_current.total * site.voltage
            bp = float(site.battery_power) if site.battery_power is not None else 0
            site.household_consumption_total = max(0, site.solar_production_total + bp - export_power)

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

    for charger_id in tail[0]['commanded'].keys():
        values = [h['commanded'][charger_id] for h in tail]
        variation = max(values) - min(values)
        if variation > tolerance:
            return False, f"{charger_id} unstable: variation={variation:.2f}A over last {STABILITY_CYCLES} cycles"

    return True, "Stable"


# ---------------------------------------------------------------------------
# Scenario loading and building
# ---------------------------------------------------------------------------

def load_scenarios(yaml_file):
    """Load test scenarios from YAML file."""
    with open(yaml_file, 'r') as f:
        data = yaml.safe_load(f)
    return data['scenarios']


def build_site_from_scenario(scenario):
    """Build SiteContext from scenario dict.

    YAML values represent physical reality:
    - phase_X_consumption: household load on that phase (Amps)
    - solar_production: total solar production (Watts)
    - battery_*: battery state and limits

    The simulation loop converts these to grid CT values before feeding
    to the engine, matching the production data flow.
    """
    site_data = scenario['site']
    voltage = site_data.get('voltage', 230)

    # Per-phase household consumption (None = phase doesn't exist)
    solar_total = site_data.get('solar_production', 0)
    phase_a_cons = site_data.get('phase_a_consumption')
    phase_b_cons = site_data.get('phase_b_consumption')
    phase_c_cons = site_data.get('phase_c_consumption')

    # Export starts at zero — will be computed by CT simulation in the loop
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

    # Build chargers
    # Per-charger operating_mode; fallback to site-level charging_mode for migration
    site_mode = site_data.get('charging_mode')

    for idx, charger_data in enumerate(scenario['chargers']):
        device_type = charger_data.get("device_type", "evse")
        phases = charger_data.get("phases", 1)

        if device_type == "plug":
            power_rating = charger_data.get("power_rating", 2000)
            equiv_current = round(power_rating / (voltage * phases), 1)
            min_current = equiv_current
            max_current = equiv_current
        else:
            min_current = charger_data.get("min_current", 6)
            max_current = charger_data.get("max_current", 16)

        # Resolve operating mode: per-charger > site-level fallback > device default
        operating_mode = charger_data.get("operating_mode")
        if operating_mode is None and site_mode is not None:
            operating_mode = site_mode
        if operating_mode is None:
            operating_mode = "Continuous" if device_type == "plug" else "Standard"
        # Migrate old mode names
        operating_mode = _MIGRATE_MODE_NAMES.get(operating_mode, operating_mode)

        charger = LoadContext(
            charger_id=f"charger_{idx}",
            entity_id=charger_data.get("entity_id", f"charger_{idx}"),
            min_current=min_current,
            max_current=max_current,
            phases=phases,
            priority=charger_data.get("priority", idx),
            device_type=device_type,
            operating_mode=operating_mode,
            car_phases=charger_data.get("car_phases"),
            l1_phase=charger_data.get("l1_phase", "A"),
            l2_phase=charger_data.get("l2_phase", "B"),
            l3_phase=charger_data.get("l3_phase", "C"),
            connector_status=charger_data.get("connector_status",
                                              "Available" if charger_data.get("active") is False else "Charging"),
            l1_current=charger_data.get("l1_current", 0),
            l2_current=charger_data.get("l2_current", 0),
            l3_current=charger_data.get("l3_current", 0),
        )
        site.chargers.append(charger)

    return site


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------

def print_scenario_params(scenario):
    """Print scenario parameters for trace/verbose output."""
    site_data = scenario['site']
    chargers = scenario['chargers']

    # Site basics
    voltage = site_data.get('voltage', 230)
    breaker = site_data.get('main_breaker_rating', 63)
    dist = site_data.get('distribution_mode', 'priority')
    solar = site_data.get('solar_production', 0)
    max_import = site_data.get('max_import_power')

    # Phases from consumption
    cons_parts = []
    for ph, key in [('A', 'phase_a_consumption'), ('B', 'phase_b_consumption'), ('C', 'phase_c_consumption')]:
        val = site_data.get(key)
        if val is not None:
            cons_parts.append(f"{ph}={val}A")
    cons_str = '/'.join(cons_parts) if cons_parts else 'none'
    num_phases = len(cons_parts) or 1

    has_battery = site_data.get('battery_soc') is not None

    print(f"  Site: {voltage}V {breaker}A breaker {num_phases}ph | Solar {solar}W | Dist: {dist}")
    if max_import:
        print(f"        Max import: {max_import}W")
    print(f"  Consumption: {cons_str}")

    # Battery
    if has_battery:
        soc = site_data.get('battery_soc')
        soc_min = site_data.get('battery_soc_min', 20)
        soc_target = site_data.get('battery_soc_target', 80)
        charge = site_data.get('battery_max_charge_power', 5000)
        discharge = site_data.get('battery_max_discharge_power', 5000)
        print(f"  Battery: soc={soc}% min={soc_min}% target={soc_target}% | charge={charge}W discharge={discharge}W")

    # Inverter
    inv_max = site_data.get('inverter_max_power')
    inv_pp = site_data.get('inverter_max_power_per_phase')
    inv_asym = site_data.get('inverter_supports_asymmetric', False)
    if inv_max or inv_pp or inv_asym:
        parts = []
        if inv_max:
            parts.append(f"max={inv_max}W")
        if inv_pp:
            parts.append(f"per_phase={inv_pp}W")
        parts.append(f"asymmetric={inv_asym}")
        print(f"  Inverter: {' '.join(parts)}")

    # Excess threshold
    excess_thresh = site_data.get('excess_export_threshold')
    if excess_thresh:
        print(f"  Excess threshold: {excess_thresh}W")

    # Chargers
    site_mode = site_data.get('charging_mode')
    for ch in chargers:
        eid = ch.get('entity_id', '?')
        dev_type = ch.get('device_type', 'evse')
        phases = ch.get('phases', 1)
        priority = ch.get('priority', 0)
        status = ch.get('connector_status', 'Charging' if ch.get('active') is not False else 'Available')
        op_mode = ch.get('operating_mode', site_mode or ("Continuous" if dev_type == "plug" else "Standard"))
        # Phase mapping
        l1p = ch.get('l1_phase', 'A')
        l2p = ch.get('l2_phase', 'B')
        l3p = ch.get('l3_phase', 'C')

        # Derive mask the same way LoadContext.__post_init__ does
        if ch.get('active_phases_mask'):
            mask = ch['active_phases_mask']
        elif ch.get('connected_to_phase'):
            mask = ch['connected_to_phase']
        elif phases == 3:
            mask = "".join(sorted({l1p, l2p, l3p}))
        elif phases == 2:
            mask = "".join(sorted({l1p, l2p}))
        else:
            mask = l1p
        phase_map_str = ""
        if l1p != 'A' or l2p != 'B' or l3p != 'C':
            phase_map_str = f" map=L1→{l1p}/L2→{l2p}/L3→{l3p}"

        if dev_type == 'plug':
            power = ch.get('power_rating', 2000)
            print(f"  Charger {eid}: plug {power}W {phases}ph mask={mask} prio={priority} mode={op_mode}{phase_map_str} [{status}]")
        else:
            min_c = ch.get('min_current', 6)
            max_c = ch.get('max_current', 16)
            print(f"  Charger {eid}: {min_c}-{max_c}A {phases}ph mask={mask} prio={priority} mode={op_mode}{phase_map_str} [{status}]")

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
    Cycles 5-24:  Warmup with ramp rate limiting on charger output.
    Cycles 25-29: Stability check — engine targets and commanded limits
                  must converge.

    Returns (passed, errors, history).
    """
    if verbose:
        print_scenario_params(scenario)

    commanded_limits = {}  # entity_id -> current commanded limit
    history = []

    for cycle in range(TOTAL_CYCLES):
        # 1. Build site from YAML (household consumption, solar production, battery)
        site = build_site_from_scenario(scenario)

        # 2. Scale household + solar for cold-start ramp-up (cycles 0-4)
        if cycle < RAMP_UP_CYCLES:
            t = (cycle + 1) / RAMP_UP_CYCLES
            scale_site_values(site, t)

        # Save household consumption before CT simulation overwrites it
        household = PhaseValues(site.consumption.a, site.consumption.b, site.consumption.c)

        # 3. Set charger l1/l2/l3_current from previous commanded limits
        for charger in site.chargers:
            set_charger_phase_currents(charger, commanded_limits.get(charger.entity_id, 0))

        # 4. Compute grid CT values from physical inputs
        #    net = household - solar_per_phase + battery_per_phase + charger_draw
        #    Map charger L1/L2/L3 draws to site phases A/B/C via phase mapping
        charger_phase_a = charger_phase_b = charger_phase_c = 0.0
        for c in site.chargers:
            a_draw, b_draw, c_draw = c.get_site_phase_draw()
            charger_phase_a += a_draw
            charger_phase_b += b_draw
            charger_phase_c += c_draw
        ct_a_net, ct_b_net, ct_c_net, solar_pp, bat_pp = simulate_grid_ct(
            site, household, charger_phase_a, charger_phase_b, charger_phase_c)

        # 5. Apply feedback: subtract charger draws (replicates dynamic_ocpp_evse.py)
        apply_feedback_adjustment(site)

        # 7. Run calculation engine
        calculate_all_charger_targets(site)

        # 8. Apply ramp rate limiting to engine targets
        for charger in site.chargers:
            target = charger.allocated_current
            prev = commanded_limits.get(charger.entity_id, 0)
            commanded_limits[charger.entity_id] = apply_ramp_rate(prev, target)

        # 9. Record history
        history.append({
            'cycle': cycle,
            'engine_targets': {c.entity_id: c.allocated_current for c in site.chargers},
            'commanded': {c.entity_id: commanded_limits[c.entity_id] for c in site.chargers},
        })

        if verbose:
            parts = []
            for charger in site.chargers:
                eid = charger.entity_id
                parts.append(f"{eid}={commanded_limits[eid]:.1f}A(t={charger.allocated_current:.1f})")
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
                # Sum of charger draws per site phase (mapped from L1/L2/L3)
                ch_sum_str = f"ch_sum=({charger_phase_a:.1f}/{charger_phase_b:.1f}/{charger_phase_c:.1f})"
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

    return passed, errors, history


def validate_results(scenario, site):
    """Validate test results against expected values."""
    expected = scenario['expected']
    passed = True
    errors = []

    for charger in site.chargers:
        entity_id = charger.entity_id
        if entity_id in expected:
            expected_allocated = expected[entity_id]['allocated']
            actual_allocated = charger.allocated_current

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
                actual_available = charger.available_current
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


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_tests(yaml_file='dev/tests/test_scenarios.yaml', verbose=False, trace=False, filter_verified=None):
    """Run all test scenarios with 30-cycle simulation."""
    all_scenarios = load_scenarios(yaml_file)

    if filter_verified == 'verified':
        scenarios = [s for s in all_scenarios if s.get('human_verified', False)]
    elif filter_verified == 'unverified':
        scenarios = [s for s in all_scenarios if not s.get('human_verified', False)]
    else:
        scenarios = all_scenarios

    print(f"\n{'='*70}")
    print(f"TEST RUNNER: RUNNING {len(scenarios)} SCENARIOS ({TOTAL_CYCLES}-cycle simulation)")
    print(f"{'='*70}\n")

    passed_count = 0
    failed_count = 0
    verified_passed = 0
    verified_failed = 0
    unverified_passed = 0
    unverified_failed = 0
    results = []

    for scenario in scenarios:
        name = scenario['name']
        description = scenario['description']
        is_verified = scenario.get('human_verified', False)
        source_file = scenario.get('_source_file', '')

        if verbose:
            print(f"\n{'='*70}")
            if source_file:
                print(f"Running: [{source_file}] {name}")
            else:
                print(f"Running: {name}")
            print(f"Description: {description}")
            print(f"{'='*70}")

        passed, errors, history = run_scenario_simulation(scenario, verbose=verbose, trace=trace)

        if passed:
            passed_count += 1
            status = "PASS"
            if is_verified:
                verified_passed += 1
            else:
                unverified_passed += 1
        else:
            failed_count += 1
            status = "FAIL"
            if is_verified:
                verified_failed += 1
            else:
                unverified_failed += 1

        results.append({
            'name': name,
            'description': description,
            'status': status,
            'passed': passed,
            'errors': errors,
            'history': history,
        })

        prefix = "UNVERIFIED " if not is_verified else ""
        source_tag = f"[{source_file}] " if source_file else ""
        if verbose or not passed:
            print(f"{prefix}{status} {source_tag}{name}")
            for error in errors:
                print(f"  {error}")
            print()

    # Summary
    verified_total = verified_passed + verified_failed
    unverified_total = unverified_passed + unverified_failed

    print(f"\n{'='*70}")
    print(f"TEST SUMMARY")
    print(f"{'='*70}")
    print(f"Total:  {len(scenarios)}")
    print()
    print(f"Verified Scenarios:")
    print(f"  Passed: {verified_passed}")
    print(f"  Failed: {verified_failed}")
    print(f"  Total:  {verified_total}")
    print()
    print(f"Unverified Scenarios:")
    print(f"  Passed: {unverified_passed}")
    print(f"  Failed: {unverified_failed}")
    print(f"  Total:  {unverified_total}")
    print()
    print(f"Overall:")
    print(f"  Passed: {passed_count}")
    print(f"  Failed: {failed_count}")
    print(f"{'='*70}\n")

    if failed_count > 0:
        print("Failed scenarios:")
        for result in results:
            if not result['passed']:
                print(f"  - {result['name']}")
        print()

    return failed_count == 0


def run_single_scenario(scenario_name, yaml_file='dev/tests/test_scenarios.yaml', trace=False, source_file=''):
    """Run a single scenario by name with verbose simulation output."""
    scenarios = load_scenarios(yaml_file)

    for scenario in scenarios:
        if scenario['name'] == scenario_name:
            sf = scenario.get('_source_file', source_file)
            source_tag = f"[{sf}] " if sf else ""
            print(f"\n{'='*70}")
            print(f"Running: {source_tag}{scenario['name']}")
            print(f"Description: {scenario['description']}")
            print(f"{'='*70}\n")

            passed, errors, history = run_scenario_simulation(scenario, verbose=True, trace=trace)

            # Print final state summary
            last = history[-1]
            print(f"\nFinal state (cycle {last['cycle']}):")
            for eid in last['engine_targets']:
                print(f"  {eid}: engine={last['engine_targets'][eid]:.1f}A, "
                      f"commanded={last['commanded'][eid]:.1f}A")
            print()

            print("Validation:")
            for error in errors:
                print(f"  {error}")
            print()

            return passed

    print(f"Scenario '{scenario_name}' not found")
    return False


class TeeOutput:
    """Write to both console and log file."""
    def __init__(self, log_file):
        self.terminal = sys.stdout
        self.log = open(log_file, 'w', encoding='utf-8')
        # Reconfigure terminal for UTF-8 if possible (Windows cp1252 fix)
        if hasattr(self.terminal, 'reconfigure'):
            try:
                self.terminal.reconfigure(encoding='utf-8')
            except Exception:
                pass

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def close(self):
        self.log.close()


if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Redirect output to both console and log file
    log_file = Path(__file__).parent / "test_results.log"
    tee = TeeOutput(log_file)
    sys.stdout = tee

    # Print start timestamp
    start_time = datetime.now()
    print(f"Test run started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    def _merge_scenarios_from_dir(dir_path):
        """Merge all yaml scenarios from a directory into a single list."""
        combined = []
        p = Path(dir_path)
        files = sorted(p.rglob("*.yaml")) + sorted(p.rglob("*.yml"))
        for f in files:
            rel = f.relative_to(p)
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            for sc in data.get("scenarios", []):
                sc.setdefault("_source_file", str(rel))
                combined.append(sc)
        return combined

    # Parse flags from command line
    filter_verified = None
    trace = False
    args = sys.argv[1:]

    if '--verified' in args:
        filter_verified = 'verified'
        args.remove('--verified')
    elif '--unverified' in args:
        filter_verified = 'unverified'
        args.remove('--unverified')
    elif '--all' in args:
        filter_verified = None
        args.remove('--all')

    if '--trace' in args:
        trace = True
        args.remove('--trace')

    if len(args) > 0:
        arg = args[0]
        p = Path(arg)
        if p.exists():
            if p.is_dir():
                combined = _merge_scenarios_from_dir(p)
                tmp = Path(__file__).parent / "scenarios_combined_temp.yaml"
                with open(tmp, "w", encoding="utf-8") as fh:
                    yaml.safe_dump({"scenarios": combined}, fh)
                success = run_tests(yaml_file=str(tmp), verbose=True, trace=trace, filter_verified=filter_verified)
                try:
                    tmp.unlink()
                except Exception:
                    pass
            elif p.is_file():
                success = run_tests(yaml_file=str(p), verbose=True, trace=trace, filter_verified=filter_verified)
            else:
                print(f"Path '{arg}' is not a file or directory")
                success = False
        else:
            scenarios_dir = Path(__file__).parent / "scenarios"
            search_paths = []
            if scenarios_dir.exists():
                search_paths = list(sorted(scenarios_dir.rglob("*.yaml"))) + list(sorted(scenarios_dir.rglob("*.yml")))
            else:
                search_paths = [Path(__file__).parent / "test_scenarios.yaml"]

            found = False
            for f in search_paths:
                rel = f.relative_to(scenarios_dir) if scenarios_dir.exists() else f.name
                with open(f, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                for sc in data.get("scenarios", []):
                    if sc.get("name") == arg:
                        found = True
                        success = run_single_scenario(arg, yaml_file=str(f), trace=trace, source_file=str(rel))
                        break
                if found:
                    break
            if not found:
                print(f"Scenario '{arg}' not found in scenarios directory or files")
                success = False
    else:
        scenarios_dir = Path(__file__).parent / "scenarios"
        if scenarios_dir.exists():
            combined = _merge_scenarios_from_dir(scenarios_dir)
            tmp = Path(__file__).parent / "scenarios_combined_temp.yaml"
            with open(tmp, "w", encoding="utf-8") as fh:
                yaml.safe_dump({"scenarios": combined}, fh)
            success = run_tests(yaml_file=str(tmp), verbose=True, trace=trace, filter_verified=filter_verified)
            try:
                tmp.unlink()
            except Exception:
                pass
        else:
            success = run_tests(verbose=True, trace=trace, filter_verified=filter_verified)

    # Print end timestamp and duration
    end_time = datetime.now()
    duration = end_time - start_time
    print(f"\nTest run finished: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Duration: {duration.total_seconds():.2f} seconds")

    # Close log file
    tee.close()
    sys.stdout = tee.terminal

    sys.exit(0 if success else 1)
