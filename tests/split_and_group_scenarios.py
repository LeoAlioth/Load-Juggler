#!/usr/bin/env python3
"""
Reorganize test scenarios by phase (1ph/3ph) and battery presence.
Preserves comments and proper YAML formatting.
"""
import yaml
from pathlib import Path

SCENARIOS_DIR = Path('tests/scenarios')
OUT_1PH = SCENARIOS_DIR / 'test_scenarios_1ph.yaml'
OUT_1PH_BAT = SCENARIOS_DIR / 'test_scenarios_1ph_battery.yaml'
OUT_3PH = SCENARIOS_DIR / 'test_scenarios_3ph.yaml'
OUT_3PH_BAT = SCENARIOS_DIR / 'test_scenarios_3ph_battery.yaml'

def load_all_scenarios():
    """Load all scenarios from YAML files in scenarios directory."""
    all_scenarios = []
    files = sorted(SCENARIOS_DIR.glob('*.yaml')) + sorted(SCENARIOS_DIR.glob('*.yml'))
    
    for f in files:
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                data = yaml.safe_load(fh)
                if data and 'scenarios' in data:
                    all_scenarios.extend(data['scenarios'])
        except Exception as e:
            print(f"Warning: Could not load {f}: {e}")
    
    return all_scenarios

def classify_scenario(scenario):
    """Classify scenario by num_phases and battery presence."""
    site = scenario.get('site', {})
    num_phases = site.get('num_phases', 3)
    
    # Battery is present if battery_soc is defined and not null
    has_battery = 'battery_soc' in site and site.get('battery_soc') is not None
    
    return num_phases, has_battery

def write_grouped_file(path, scenarios):
    """Write scenarios to YAML file with proper formatting."""
    header = f"# Test scenarios for {path.stem}\n# Auto-grouped by phase and battery presence\n\n"
    
    data = {'scenarios': scenarios}
    
    with open(path, 'w', encoding='utf-8') as f:
        f.write(header)
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    
    print(f"Wrote {path.name}: {len(scenarios)} scenarios")

def main():
    # Load all scenarios
    all_scenarios = load_all_scenarios()
    print(f"Loaded {len(all_scenarios)} total scenarios")
    
    # Classify and group
    groups = {
        '1ph': [],
        '1ph_battery': [],
        '3ph': [],
        '3ph_battery': []
    }
    
    for scenario in all_scenarios:
        num_phases, has_battery = classify_scenario(scenario)
        
        if num_phases == 1:
            if has_battery:
                groups['1ph_battery'].append(scenario)
            else:
                groups['1ph'].append(scenario)
        else:  # num_phases == 3 or default
            if has_battery:
                groups['3ph_battery'].append(scenario)
            else:
                groups['3ph'].append(scenario)
    
    # Write grouped files
    write_grouped_file(OUT_1PH, groups['1ph'])
    write_grouped_file(OUT_1PH_BAT, groups['1ph_battery'])
    write_grouped_file(OUT_3PH, groups['3ph'])
    write_grouped_file(OUT_3PH_BAT, groups['3ph_battery'])
    
    print(f"\nTotal: {sum(len(g) for g in groups.values())} scenarios grouped")

if __name__ == '__main__':
    main()