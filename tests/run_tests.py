#!/usr/bin/env python3
"""
Comprehensive test runner for EVSE distribution.
Loads test scenarios from test_scenarios.yaml and validates results.
"""

import yaml
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChargerContext:
    """Individual EVSE/charger state."""
    charger_id: str
    entity_id: str
    min_current: float
    max_current: float
    phases: int
    charging_mode: str = "Standard"
    priority: int = 1
    target_current: float = 0
    allocated_current: float = 0


@dataclass
class SiteContext:
    """Site-wide electrical system state."""
    voltage: float = 230
    total_export_current: float = 0
    total_export_power: float = 0
    battery_soc: float | None = None
    battery_soc_min: float = 20
    battery_soc_target: float = 80
    excess_export_threshold: float = 13000
    battery_max_charge_power: float = 5000
    chargers: list[ChargerContext] = field(default_factory=list)


def calculate_eco_target(site, charger):
    """Calculate Eco mode target."""
    if site.battery_soc is None:
        available_per_phase = site.total_export_current / charger.phases if charger.phases > 0 else 0
        return max(charger.min_current, min(available_per_phase, charger.max_current))
    
    if site.battery_soc < site.battery_soc_min:
        return 0
    
    if site.battery_soc >= site.battery_soc_target:
        return charger.max_current
    
    available_per_phase = site.total_export_current / charger.phases if charger.phases > 0 else 0
    return max(charger.min_current, min(available_per_phase, charger.max_current))


def calculate_solar_target(site, charger):
    """Calculate Solar mode target."""
    available_per_phase = site.total_export_current / charger.phases if charger.phases > 0 else 0
    
    if available_per_phase >= charger.min_current:
        return min(available_per_phase, charger.max_current)
    
    return 0


def calculate_excess_target(site, charger):
    """Calculate Excess mode target."""
    effective_threshold = site.excess_export_threshold
    
    if site.battery_soc is not None and site.battery_soc < site.battery_soc_target:
        if site.battery_max_charge_power:
            effective_threshold += site.battery_max_charge_power
    
    if site.total_export_power > effective_threshold:
        available_power = site.total_export_power - effective_threshold
        available_current = available_power / site.voltage / charger.phases if (site.voltage > 0 and charger.phases > 0) else 0
        
        if available_current >= charger.min_current:
            return min(available_current, charger.max_current)
    
    return 0


def calculate_all_charger_targets(site):
    """Calculate targets for all chargers."""
    for charger in site.chargers:
        if charger.charging_mode == "Standard":
            charger.target_current = charger.max_current
        elif charger.charging_mode == "Eco":
            charger.target_current = calculate_eco_target(site, charger)
        elif charger.charging_mode == "Solar":
            charger.target_current = calculate_solar_target(site, charger)
        elif charger.charging_mode == "Excess":
            charger.target_current = calculate_excess_target(site, charger)
        else:
            charger.target_current = charger.max_current


def load_scenarios(yaml_file):
    """Load test scenarios from YAML file."""
    with open(yaml_file, 'r') as f:
        data = yaml.safe_load(f)
    return data['scenarios']


def build_site_from_scenario(scenario):
    """Build SiteContext from scenario dict."""
    site_data = scenario['site']
    
    site = SiteContext(
        voltage=site_data.get('voltage', 230),
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
        site.chargers.append(charger)
    
    return site


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
        
        # Build site and run calculation
        site = build_site_from_scenario(scenario)
        calculate_all_charger_targets(site)
        
        # Validate
        passed, errors = validate_results(scenario, site)
        
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
            'site': site
        })
        
        # Print result
        if verbose or not passed:
            print(f"{status} {name}")
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
