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
from custom_components.dynamic_ocpp_evse.calculations.models import ChargerContext, SiteContext, PhaseValues
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import calculate_all_charger_targets

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

    Scales consumption, export_current, and solar_production_total.
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
    site.export_current = PhaseValues(
        site.export_current.a * t if site.export_current.a is not None else None,
        site.export_current.b * t if site.export_current.b is not None else None,
        site.export_current.c * t if site.export_current.c is not None else None,
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
    """Build SiteContext from scenario dict."""
    site_data = scenario['site']
    voltage = site_data.get('voltage', 230)

    # Solar production + per-phase consumption (None = phase doesn't exist)
    solar_total = site_data.get('solar_production', 0)
    solar_production_direct = site_data.get('solar_production_direct', False)
    phase_a_cons = site_data.get('phase_a_consumption')
    phase_b_cons = site_data.get('phase_b_consumption')
    phase_c_cons = site_data.get('phase_c_consumption')

    # Infer num_phases from which consumption values are provided
    active_phases = sum(1 for v in [phase_a_cons, phase_b_cons, phase_c_cons] if v is not None)
    if active_phases == 0:
        active_phases = 1

    # Export current: use explicit per-phase values if provided, otherwise derive from solar
    if 'phase_a_export' in site_data or 'phase_b_export' in site_data or 'phase_c_export' in site_data:
        # Explicit export values (used with solar_production_direct to test independent solar reading)
        phase_a_export = site_data.get('phase_a_export') if phase_a_cons is not None else None
        phase_b_export = site_data.get('phase_b_export') if phase_b_cons is not None else None
        phase_c_export = site_data.get('phase_c_export') if phase_c_cons is not None else None
    else:
        # Derive export from solar (existing behavior)
        solar_per_phase_amps = (solar_total / active_phases) / voltage if voltage > 0 and solar_total > 0 else 0
        phase_a_export = max(0, solar_per_phase_amps - phase_a_cons) if phase_a_cons is not None else None
        phase_b_export = max(0, solar_per_phase_amps - phase_b_cons) if phase_b_cons is not None else None
        phase_c_export = max(0, solar_per_phase_amps - phase_c_cons) if phase_c_cons is not None else None

    site = SiteContext(
        voltage=voltage,
        main_breaker_rating=site_data.get('main_breaker_rating', 63),
        consumption=PhaseValues(phase_a_cons, phase_b_cons, phase_c_cons),
        export_current=PhaseValues(phase_a_export, phase_b_export, phase_c_export),
        solar_production_total=solar_total,
        solar_is_derived=site_data.get('solar_is_derived', False),
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
    )

    # Set site-level charging mode (first charger's mode, or from site if specified)
    if 'charging_mode' in site_data:
        site.charging_mode = site_data['charging_mode']
    else:
        print(f"  Scenario '{scenario['name']}' should specify 'charging_mode' at site level.")

    # Build chargers
    for idx, charger_data in enumerate(scenario['chargers']):
        phases = charger_data.get("phases", 1)

        # Set active_phases_mask based on charger configuration
        active_phases_mask = charger_data.get("active_phases_mask")
        if not active_phases_mask:
            connected_to_phase = charger_data.get('connected_to_phase')
            if connected_to_phase:
                active_phases_mask = connected_to_phase
            elif phases == 3:
                active_phases_mask = "ABC"
            elif phases == 2:
                active_phases_mask = "AB"

        connected_phase = charger_data.get('connected_to_phase') if phases == 1 else None

        device_type = charger_data.get("device_type", "evse")
        if device_type == "plug":
            power_rating = charger_data.get("power_rating", 2000)
            equiv_current = round(power_rating / (voltage * phases), 1)
            min_current = equiv_current
            max_current = equiv_current
        else:
            min_current = charger_data.get("min_current", 6)
            max_current = charger_data.get("max_current", 16)

        charger = ChargerContext(
            charger_id=f"charger_{idx}",
            entity_id=charger_data.get("entity_id", f"charger_{idx}"),
            min_current=min_current,
            max_current=max_current,
            phases=charger_data.get("phases", 1),
            priority=charger_data.get("priority", idx),
            device_type=device_type,
            car_phases=charger_data.get("car_phases"),
            active_phases_mask=active_phases_mask,
            connector_status=charger_data.get("connector_status",
                                              "Available" if charger_data.get("active") is False else "Charging"),
            l1_current=charger_data.get("l1_current", 0),
            l2_current=charger_data.get("l2_current", 0),
            l3_current=charger_data.get("l3_current", 0),
        )
        if charger.phases == 1 and connected_phase:
            charger.charger_id = f"{charger.charger_id}_phase_{connected_phase}"
        site.chargers.append(charger)

    return site


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------

def run_scenario_simulation(scenario, verbose=False):
    """Run 30-cycle simulation for a scenario.

    Cycles 0-4:   Site values ramp from 0 to target (cold start).
    Cycles 5-24:  Warmup with ramp rate limiting on charger output.
    Cycles 25-29: Stability check — engine targets and commanded limits
                  must converge.

    Returns (passed, errors, history).
    """
    commanded_limits = {}  # entity_id -> current commanded limit
    history = []

    for cycle in range(TOTAL_CYCLES):
        # 1. Build site with full target values from YAML
        site = build_site_from_scenario(scenario)

        # 2. Scale values for cold-start ramp-up (cycles 0-4)
        if cycle < RAMP_UP_CYCLES:
            t = (cycle + 1) / RAMP_UP_CYCLES
            scale_site_values(site, t)

        # 3. Run calculation engine
        calculate_all_charger_targets(site)

        # 4. Apply ramp rate limiting to engine targets
        for charger in site.chargers:
            target = charger.allocated_current
            prev = commanded_limits.get(charger.entity_id, 0)
            commanded_limits[charger.entity_id] = apply_ramp_rate(prev, target)

        # 5. Record history
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
            print(f"  Cycle {cycle:2d}: {', '.join(parts)}")

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

def run_tests(yaml_file='dev/tests/test_scenarios.yaml', verbose=False, filter_verified=None):
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

        passed, errors, history = run_scenario_simulation(scenario, verbose=verbose)

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

        if verbose or not passed:
            prefix = "UNVERIFIED " if not is_verified else ""
            print(f"{prefix}{status} {name}")
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


def run_single_scenario(scenario_name, yaml_file='dev/tests/test_scenarios.yaml'):
    """Run a single scenario by name with verbose simulation output."""
    scenarios = load_scenarios(yaml_file)

    for scenario in scenarios:
        if scenario['name'] == scenario_name:
            print(f"\n{'='*70}")
            print(f"Running: {scenario['name']}")
            print(f"Description: {scenario['description']}")
            print(f"{'='*70}\n")

            passed, errors, history = run_scenario_simulation(scenario, verbose=True)

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
        files = sorted(p.glob("*.yaml")) + sorted(p.glob("*.yml"))
        for f in files:
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            combined.extend(data.get("scenarios", []))
        return combined

    # Parse filter_verified from command line
    filter_verified = None
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

    if len(args) > 0:
        arg = args[0]
        p = Path(arg)
        if p.exists():
            if p.is_dir():
                combined = _merge_scenarios_from_dir(p)
                tmp = Path(__file__).parent / "scenarios_combined_temp.yaml"
                with open(tmp, "w", encoding="utf-8") as fh:
                    yaml.safe_dump({"scenarios": combined}, fh)
                success = run_tests(yaml_file=str(tmp), verbose=True, filter_verified=filter_verified)
                try:
                    tmp.unlink()
                except Exception:
                    pass
            elif p.is_file():
                success = run_tests(yaml_file=str(p), verbose=True, filter_verified=filter_verified)
            else:
                print(f"Path '{arg}' is not a file or directory")
                success = False
        else:
            scenarios_dir = Path(__file__).parent / "scenarios"
            search_paths = []
            if scenarios_dir.exists():
                search_paths = list(sorted(scenarios_dir.glob("*.yaml"))) + list(sorted(scenarios_dir.glob("*.yml")))
            else:
                search_paths = [Path(__file__).parent / "test_scenarios.yaml"]

            found = False
            for f in search_paths:
                with open(f, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                for sc in data.get("scenarios", []):
                    if sc.get("name") == arg:
                        found = True
                        success = run_single_scenario(arg, yaml_file=str(f))
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
            success = run_tests(yaml_file=str(tmp), verbose=True, filter_verified=filter_verified)
            try:
                tmp.unlink()
            except Exception:
                pass
        else:
            success = run_tests(verbose=True, filter_verified=filter_verified)

    # Print end timestamp and duration
    end_time = datetime.now()
    duration = end_time - start_time
    print(f"\nTest run finished: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Duration: {duration.total_seconds():.2f} seconds")

    # Close log file
    tee.close()
    sys.stdout = tee.terminal

    sys.exit(0 if success else 1)
