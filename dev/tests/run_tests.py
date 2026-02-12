#!/usr/bin/env python3
"""
Oscillation test runner for EVSE distribution.
Uses ACTUAL production code - no duplicates!
"""

import sys
import yaml
from pathlib import Path
from datetime import datetime

# Import REAL production code (pure Python, no HA dependencies)
sys.path.insert(0, str(Path(__file__).parents[2] / "custom_components" / "dynamic_ocpp_evse" / "calculations"))

from models import ChargerContext, SiteContext
from target_calculator import calculate_all_charger_targets


def apply_charging_feedback(site, initial_solar, initial_consumption_per_phase):
    """
    Simulate feedback: chargers drawing power reduces export.

    Args:
        site: SiteContext with chargers that have target_current set
        initial_solar: Total solar production (W)
        initial_consumption_per_phase: [phase_a, phase_b, phase_c] consumption (A)
    """
    voltage = site.voltage
    num_phases = site.num_phases if site.num_phases > 0 else 1

    # Solar per phase (evenly distributed across active phases)
    if num_phases == 1:
        # Single-phase: all solar goes to phase A
        solar_per_phase_a = initial_solar / voltage if voltage > 0 else 0
        solar_per_phase_b = 0
        solar_per_phase_c = 0
    else:
        # Three-phase: solar evenly distributed
        solar_per_phase = initial_solar / num_phases / voltage if voltage > 0 else 0
        solar_per_phase_a = solar_per_phase
        solar_per_phase_b = solar_per_phase
        solar_per_phase_c = solar_per_phase

    # Start with initial consumption
    phase_a_load = initial_consumption_per_phase[0]
    phase_b_load = initial_consumption_per_phase[1]
    phase_c_load = initial_consumption_per_phase[2]

    # Add charger loads based on their actual phase connections
    for charger in site.chargers:
        if charger.phases == 1:
            # Single-phase: add to specific phase based on active_phases_mask
            if charger.active_phases_mask:
                if 'A' in charger.active_phases_mask and 'B' not in charger.active_phases_mask and 'C' not in charger.active_phases_mask:
                    phase_a_load += charger.target_current
                elif 'B' in charger.active_phases_mask and 'A' not in charger.active_phases_mask and 'C' not in charger.active_phases_mask:
                    phase_b_load += charger.target_current
                elif 'C' in charger.active_phases_mask and 'A' not in charger.active_phases_mask and 'B' not in charger.active_phases_mask:
                    phase_c_load += charger.target_current
            else:
                # No mask - default to phase A
                phase_a_load += charger.target_current

        elif charger.phases == 2:
            # Two-phase: add to appropriate two phases
            if charger.active_phases_mask:
                if 'A' in charger.active_phases_mask and 'B' in charger.active_phases_mask:
                    phase_a_load += charger.target_current
                    phase_b_load += charger.target_current
                elif 'A' in charger.active_phases_mask and 'C' in charger.active_phases_mask:
                    phase_a_load += charger.target_current
                    phase_c_load += charger.target_current
                elif 'B' in charger.active_phases_mask and 'C' in charger.active_phases_mask:
                    phase_b_load += charger.target_current
                    phase_c_load += charger.target_current
            else:
                # No mask - default to AB
                phase_a_load += charger.target_current
                phase_b_load += charger.target_current

        elif charger.phases == 3:
            # Three-phase: add to all phases
            phase_a_load += charger.target_current
            phase_b_load += charger.target_current
            phase_c_load += charger.target_current

    # Calculate new export per phase (solar - total_load)
    site.phase_a_export = max(0, solar_per_phase_a - phase_a_load)
    site.phase_b_export = max(0, solar_per_phase_b - phase_b_load)
    site.phase_c_export = max(0, solar_per_phase_c - phase_c_load)

    # Update totals
    site.total_export_current = site.phase_a_export + site.phase_b_export + site.phase_c_export
    site.total_export_power = site.total_export_current * voltage


def detect_oscillation(history, max_variation=0.5):
    """
    Detect if charger targets are oscillating.
    
    Args:
        history: List of iteration data
        max_variation: Maximum allowed variation after stabilization
        
    Returns:
        (is_stable, analysis)
    """
    if len(history) < 3:
        return True, "Not enough iterations"
    
    # Check each charger
    for charger_id in history[0]['chargers'].keys():
        values = [h['chargers'][charger_id] for h in history]
        
        # Check last 3 iterations for stability
        last_three = values[-3:]
        variation = max(last_three) - min(last_three)
        
        if variation > max_variation:
            return False, f"{charger_id} unstable: variation={variation:.2f}A"
        
        # Check for ping-pong pattern (up-down-up or down-up-down)
        if len(values) >= 4:
            diffs = [values[i+1] - values[i] for i in range(len(values)-1)]
            # Check if signs alternate
            sign_changes = sum(1 for i in range(len(diffs)-1) if diffs[i] * diffs[i+1] < 0)
            if sign_changes >= len(diffs) - 1:  # All signs different
                return False, f"{charger_id} oscillating: ping-pong pattern detected"
    
    return True, "Stable"


def load_scenarios(yaml_file):
    """Load test scenarios from YAML file."""
    with open(yaml_file, 'r') as f:
        data = yaml.safe_load(f)
    return data['scenarios']


def build_site_from_scenario(scenario):
    """Build SiteContext from scenario dict."""
    site_data = scenario['site']
    voltage = site_data.get('voltage', 230)
    
    # Solar production + per-phase consumption format
    solar_total = site_data.get('solar_production', 0)
    phase_a_cons = site_data.get('phase_a_consumption', 0)
    phase_b_cons = site_data.get('phase_b_consumption', 0)
    phase_c_cons = site_data.get('phase_c_consumption', 0)
    num_phases = site_data.get('num_phases', 3)
    
    # Calculate initial export per phase (solar evenly distributed)
    if num_phases == 1:
        # Single-phase: all solar goes to phase A
        solar_per_phase_amps = solar_total / voltage if voltage > 0 else 0
        phase_a_export = max(0, solar_per_phase_amps - phase_a_cons)
        phase_b_export = 0
        phase_c_export = 0
    else:
        # Multi-phase: solar evenly distributed across active phases
        solar_per_phase_amps = (solar_total / num_phases) / voltage if voltage > 0 else 0
        phase_a_export = max(0, solar_per_phase_amps - phase_a_cons)
        phase_b_export = max(0, solar_per_phase_amps - phase_b_cons)
        phase_c_export = max(0, solar_per_phase_amps - phase_c_cons)
    
    total_export_current = phase_a_export + phase_b_export + phase_c_export
    total_export_power = total_export_current * voltage
    
    site = SiteContext(
        voltage=voltage,
        num_phases=num_phases,
        main_breaker_rating=site_data.get('main_breaker_rating', 63),
        phase_a_consumption=phase_a_cons,
        phase_b_consumption=phase_b_cons,
        phase_c_consumption=phase_c_cons,
        phase_a_export=phase_a_export,
        phase_b_export=phase_b_export,
        phase_c_export=phase_c_export,
        solar_production_total=solar_total,
        total_export_current=total_export_current,
        total_export_power=total_export_power,
        battery_soc=site_data.get('battery_soc'),
        battery_soc_min=site_data.get('battery_soc_min', 20),
        battery_soc_target=site_data.get('battery_soc_target', 80),
        excess_export_threshold=site_data.get('excess_export_threshold', 13000),
        battery_max_charge_power=site_data.get('battery_max_charge_power', 5000),
        battery_max_discharge_power=site_data.get('battery_max_discharge_power', 5000),
        max_import_power=site_data.get('max_import_power'),
        distribution_mode=site_data.get('distribution_mode', 'priority'),
        inverter_max_power=site_data.get('inverter_max_power'),
        inverter_max_power_per_phase=site_data.get('inverter_max_power_per_phase'),
        inverter_supports_asymmetric=site_data.get('inverter_supports_asymmetric', False),
    )
    
    # Set site-level charging mode (first charger's mode, or from site if specified)
    if 'charging_mode' in site_data:
        site.charging_mode = site_data['charging_mode']
    else:
        # nofity user that test scenario should specify charging_mode at site level for clarity
        print(f"⚠️  Scenario '{scenario['name']}' should specify 'charging_mode' at site level.")
    
    # Build chargers
    for idx, charger_data in enumerate(scenario['chargers']):
        phases = charger_data.get("phases", 1)
        
        # Set active_phases_mask based on charger configuration
        active_phases_mask = charger_data.get("active_phases_mask")
        if not active_phases_mask:
            # Check for connected_to_phase first (works for 1-phase and 2-phase)
            connected_to_phase = charger_data.get('connected_to_phase')
            if connected_to_phase:
                active_phases_mask = connected_to_phase
            elif phases == 3:
                active_phases_mask = "ABC"  # 3-phase chargers on all phases
            elif phases == 2:
                active_phases_mask = "AB"  # 2-phase default (rare, when not specified)
            # else: phases == 1 without connected_to_phase remains None
            # Note: ChargerContext.__post_init__() will default single-phase to 'A'
        
        # For identification in charger_id
        connected_phase = charger_data.get('connected_to_phase') if phases == 1 else None
        
        charger = ChargerContext(
            charger_id=f"charger_{idx}",
            entity_id=charger_data.get("entity_id", f"charger_{idx}"),
            min_current=charger_data.get("min_current", 6),
            max_current=charger_data.get("max_current", 16),
            phases=charger_data.get("phases", 1),
            priority=charger_data.get("priority", idx),
            # New fields for phase tracking and connector status
            car_phases=charger_data.get("car_phases"),  # None = default to phases
            active_phases_mask=active_phases_mask,  # Set from connected_to_phase or YAML
            connector_status=charger_data.get("connector_status", "Charging"),  # Default to active
            l1_current=charger_data.get("l1_current", 0),
            l2_current=charger_data.get("l2_current", 0),
            l3_current=charger_data.get("l3_current", 0),
        )
        # Store connected_to_phase in charger_id for single-phase (test only, for identification)
        if charger.phases == 1 and connected_phase:
            charger.charger_id = f"{charger.charger_id}_phase_{connected_phase}"
        site.chargers.append(charger)
    
    return site


def run_scenario_with_iterations(scenario, verbose=False):
    """
    Run scenario with multiple iterations to detect oscillation.
    
    Returns:
        (passed, errors, history)
    """
    iterations = scenario.get('iterations', 1)
    site_data = scenario['site']
    
    # Store initial conditions for feedback
    initial_solar = site_data.get('solar_production', 0)
    initial_consumption = [
        site_data.get('phase_a_consumption', 0),
        site_data.get('phase_b_consumption', 0),
        site_data.get('phase_c_consumption', 0),
    ]
    
    history = []
    
    for i in range(iterations):
        # Build/rebuild site
        site = build_site_from_scenario(scenario)
        
        # If not first iteration, use updated export from feedback
        if i > 0 and initial_solar > 0:
            prev = history[-1]
            site.phase_a_export = prev['export_per_phase'][0]
            site.phase_b_export = prev['export_per_phase'][1]
            site.phase_c_export = prev['export_per_phase'][2]
            site.total_export_current = sum(prev['export_per_phase'])
            site.total_export_power = site.total_export_current * site.voltage
        
        # Calculate targets
        calculate_all_charger_targets(site)
        
        # Record iteration
        history.append({
            'iteration': i,
            'chargers': {c.entity_id: c.target_current for c in site.chargers},
            'export_per_phase': [site.phase_a_export, site.phase_b_export, site.phase_c_export],
            'total_export': site.total_export_current,
        })
        
        # Apply feedback for next iteration
        if i < iterations - 1 and initial_solar > 0:
            apply_charging_feedback(site, initial_solar, initial_consumption)
        
        if verbose:
            print(f"  Iteration {i+1}: {', '.join([f'{k}={v:.1f}A' for k,v in history[-1]['chargers'].items()])}")
    
    # Validate final result
    passed, errors = validate_results(scenario, site)
    
    # Check for oscillation if multiple iterations
    if iterations > 1:
        is_stable, analysis = detect_oscillation(history, scenario.get('max_variation', 0.5))
        if not is_stable:
            passed = False
            errors.append(f"Oscillation detected: {analysis}")
        elif verbose:
            errors.append(f"Stable: {analysis}")
    
    return passed, errors, history


def validate_results(scenario, site):
    """Validate test results against expected values."""
    expected = scenario['expected']
    passed = True
    errors = []
    
    for charger in site.chargers:
        entity_id = charger.entity_id
        if entity_id in expected:
            expected_target = expected[entity_id]['target']
            actual_target = charger.target_current
            
            # Allow 0.1A tolerance for floating point
            if abs(actual_target - expected_target) > 0.1:
                passed = False
                errors.append(
                    f"{entity_id}: expected {expected_target}A, got {actual_target:.1f}A"
                )
            else:
                errors.append(
                    f"{entity_id}: {actual_target:.1f}A"
                )
    
    return passed, errors


def run_tests(yaml_file='dev/tests/test_scenarios.yaml', verbose=False, filter_verified=None):
    """
    Run all test scenarios.
    
    Args:
        yaml_file: Path to YAML file with scenarios
        verbose: Print detailed output
        filter_verified: Filter scenarios by verified status
            - None or 'all': Run all scenarios (default)
            - 'verified': Run only human verified scenarios
            - 'unverified': Run only human unverified scenarios
    """
    all_scenarios = load_scenarios(yaml_file)
    
    # Filter scenarios based on verified field
    if filter_verified == 'verified':
        scenarios = [s for s in all_scenarios if s.get('human_verified', False)]
        filter_msg = " (VERIFIED ONLY)"
    elif filter_verified == 'unverified':
        scenarios = [s for s in all_scenarios if not s.get('human_verified', False)]
        filter_msg = " (UNVERIFIED ONLY)"
    else:
        scenarios = all_scenarios
        filter_msg = ""
    
    print(f"\n{'='*70}")
    print(f"TEST RUNNER: RUNNING {len(scenarios)} TEST SCENARIOS")
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
        iterations = scenario.get('iterations', 1)
        
        # Run with iterations if specified
        if iterations > 1:
            passed, errors, history = run_scenario_with_iterations(scenario, verbose=verbose)
            site = None  # Not needed for multi-iteration
        else:
            # Single iteration
            site = build_site_from_scenario(scenario)
            calculate_all_charger_targets(site)
            passed, errors = validate_results(scenario, site)
            history = None
        
        # Track verified/unverified status
        is_verified = scenario.get('human_verified', False)
        
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
            'site': site,
            'history': history,
            'iterations': iterations
        })
        
        # Print result
        if verbose or not passed:
            iter_info = f"({iterations} iterations)" if iterations > 1 else ""
            print(("UNVERIFIED " if not is_verified else "") + f"{status} {name} {iter_info}")
            for error in errors:
                print(error)
            print()
    
    # Summary with verified/unverified split
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
    """Run a single scenario by name."""
    scenarios = load_scenarios(yaml_file)
    
    for scenario in scenarios:
        if scenario['name'] == scenario_name:
            print(f"\n{'='*70}")
            print(f"Running: {scenario['name']}")
            print(f"Description: {scenario['description']}")
            print(f"{'='*70}\n")
            
            site = build_site_from_scenario(scenario)
            calculate_all_charger_targets(site)
            
            # Print site info
            print(f"Site Configuration:")
            print(f"  Export: {site.total_export_current:.1f}A ({site.total_export_power:.0f}W)")
            print(f"  Battery SOC: {site.battery_soc}%")
            print(f"  Voltage: {site.voltage}V")
            print(f"  Mode: {site.charging_mode}")
            print()
            
            # Print charger results
            print(f"Charger Configuration:")
            for charger in site.chargers:
                print(f"  {charger.entity_id} ({site.charging_mode} mode):")
                print(f"    Config: {charger.min_current}-{charger.max_current}A, {charger.phases}ph")
                print(f"    Target: {charger.target_current:.1f}A")
            print()
            
            # Validate
            passed, errors = validate_results(scenario, site)
            print("Validation:")
            for error in errors:
                print(error)
            print()
            
            return passed
    
    print(f"❌ Scenario '{scenario_name}' not found")
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
    
    # Check for --verified or --unverified flags
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
            # If arg is a directory, merge all scenario files and run them
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
                # Arg is a specific yaml file
                success = run_tests(yaml_file=str(p), verbose=True, filter_verified=filter_verified)
            else:
                print(f"❌ Path '{arg}' is not a file or directory")
                success = False
        else:
            # Treat arg as a scenario name; search in tests/scenarios first, then fallback to default file
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
                print(f"❌ Scenario '{arg}' not found in scenarios directory or files")
                success = False
    else:
        # No args: if tests/scenarios exists, run all files in it, otherwise run default behaviour
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
