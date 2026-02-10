#!/usr/bin/env python3
"""
Oscillation test runner for EVSE distribution.
Uses ACTUAL production code - no duplicates!
"""

import sys
import yaml
from pathlib import Path

# Import REAL production code (pure Python, no HA dependencies)
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "dynamic_ocpp_evse" / "calculations"))

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
    
    # Solar per phase (evenly distributed)
    solar_per_phase = initial_solar / 3 / voltage  # Convert to A per phase
    
    # Start with initial consumption
    phase_a_load = initial_consumption_per_phase[0]
    phase_b_load = initial_consumption_per_phase[1]
    phase_c_load = initial_consumption_per_phase[2]
    
    # Add charger loads
    for charger in site.chargers:
        # For simplicity, distribute all chargers evenly across phases
        # (Real system would track per-phase but this is good enough for oscillation testing)
        if charger.phases == 1:
            # Single-phase: add to one phase (assume phase A for now)
            phase_a_load += charger.target_current
        else:
            # 3-phase charger - add evenly to all phases
            phase_a_load += charger.target_current
            phase_b_load += charger.target_current
            phase_c_load += charger.target_current
    
    # Calculate new export per phase (solar - total_load)
    site.phase_a_export = max(0, solar_per_phase - phase_a_load)
    site.phase_b_export = max(0, solar_per_phase - phase_b_load)
    site.phase_c_export = max(0, solar_per_phase - phase_c_load)
    
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
    
    # Handle per-phase or total export
    if 'solar_production' in site_data:
        # New format: solar production + per-phase consumption
        solar_total = site_data.get('solar_production', 0)
        phase_a_cons = site_data.get('phase_a_consumption', 0)
        phase_b_cons = site_data.get('phase_b_consumption', 0)
        phase_c_cons = site_data.get('phase_c_consumption', 0)
        
        # Calculate initial export per phase (solar evenly distributed)
        solar_per_phase_amps = (solar_total / 3) / voltage
        phase_a_export = max(0, solar_per_phase_amps - phase_a_cons)
        phase_b_export = max(0, solar_per_phase_amps - phase_b_cons)
        phase_c_export = max(0, solar_per_phase_amps - phase_c_cons)
        
        total_export_current = phase_a_export + phase_b_export + phase_c_export
        total_export_power = total_export_current * voltage
        
        site = SiteContext(
            voltage=voltage,
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
        )
    else:
        # Legacy format: total_export_current
        site = SiteContext(
            voltage=voltage,
            total_export_current=site_data.get('total_export_current', 0),
            total_export_power=site_data.get('total_export_power', 0),
            battery_soc=site_data.get('battery_soc'),
            battery_soc_min=site_data.get('battery_soc_min', 20),
            battery_soc_target=site_data.get('battery_soc_target', 80),
            excess_export_threshold=site_data.get('excess_export_threshold', 13000),
            battery_max_charge_power=site_data.get('battery_max_charge_power', 5000),
        )
    
    # Build chargers
    for idx, charger_data in enumerate(scenario['chargers']):
        charger = ChargerContext(
            charger_id=f"charger_{idx}",
            entity_id=charger_data['entity_id'],
            min_current=charger_data['min_current'],
            max_current=charger_data['max_current'],
            phases=charger_data['phases'],
            charging_mode=charger_data.get('mode', 'Standard'),
            priority=charger_data.get('priority', 1),
        )
        # Store connected_to_phase in charger_id for single-phase (test only)
        if charger.phases == 1:
            phase = charger_data.get('connected_to_phase', 'A')
            charger.charger_id = f"{charger.charger_id}_phase_{phase}"
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
            errors.append(f"  ❌ Oscillation detected: {analysis}")
        elif verbose:
            errors.append(f"  ✅ Stable: {analysis}")
    
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
                    f"  ❌ {entity_id}: expected {expected_target}A, got {actual_target:.1f}A"
                )
            else:
                errors.append(
                    f"  ✅ {entity_id}: {actual_target:.1f}A"
                )
    
    return passed, errors


def run_tests(yaml_file='tests/test_scenarios.yaml', verbose=False):
    """Run all test scenarios."""
    scenarios = load_scenarios(yaml_file)
    
    print(f"\n{'='*70}")
    print(f"🧪 RUNNING {len(scenarios)} TEST SCENARIOS")
    print(f"{'='*70}\n")
    
    passed_count = 0
    failed_count = 0
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
            # Single iteration (legacy mode)
            site = build_site_from_scenario(scenario)
            calculate_all_charger_targets(site)
            passed, errors = validate_results(scenario, site)
            history = None
        
        if passed:
            passed_count += 1
            status = "✅ PASS"
        else:
            failed_count += 1
            status = "❌ FAIL"
        
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
            iter_info = f" ({iterations} iterations)" if iterations > 1 else ""
            print(f"{status} {name}{iter_info}")
            print(f"     {description}")
            for error in errors:
                print(error)
            print()
    
    # Summary
    print(f"\n{'='*70}")
    print(f"📊 TEST SUMMARY")
    print(f"{'='*70}")
    print(f"Total:  {len(scenarios)}")
    print(f"Passed: {passed_count} ✅")
    print(f"Failed: {failed_count} ❌")
    print(f"{'='*70}\n")
    
    if failed_count > 0:
        print("Failed scenarios:")
        for result in results:
            if not result['passed']:
                print(f"  - {result['name']}")
        print()
    
    return failed_count == 0


def run_single_scenario(scenario_name, yaml_file='tests/test_scenarios.yaml'):
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
            print()
            
            # Print charger results
            print(f"Charger Targets:")
            for charger in site.chargers:
                print(f"  {charger.entity_id} ({charger.charging_mode} mode):")
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


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        # Run specific scenario
        scenario_name = sys.argv[1]
        success = run_single_scenario(scenario_name)
    else:
        # Run all scenarios
        success = run_tests(verbose=True)
    
    sys.exit(0 if success else 1)
