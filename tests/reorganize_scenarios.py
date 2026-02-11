#!/usr/bin/env python3
"""
Reorganize test scenarios by phase and battery while preserving ALL comments.
Works with raw text to preserve formatting and comments.
"""
import re
from pathlib import Path

SCENARIOS_DIR = Path('tests/scenarios')

def extract_scenarios_from_file(filepath):
    """Extract individual scenario blocks with their comments from a file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Find the scenarios: line
    scenarios_match = re.search(r'^scenarios:\s*$', content, re.MULTILINE)
    if not scenarios_match:
        return []
    
    # Get everything after "scenarios:"
    after_scenarios = content[scenarios_match.end():]
    
    # Split by "- name:" but keep the delimiter
    parts = re.split(r'(\n\s*-\s*name:\s*)', after_scenarios)
    
    scenarios = []
    for i in range(1, len(parts), 2):
        if i + 1 < len(parts):
            scenario_text = parts[i] + parts[i + 1]
        else:
            scenario_text = parts[i]
        
        # Extract num_phases and battery_soc
        num_phases_match = re.search(r'num_phases:\s*(\d+)', scenario_text)
        battery_soc_match = re.search(r'battery_soc:\s*(null|\d+)', scenario_text)
        
        num_phases = int(num_phases_match.group(1)) if num_phases_match else 3
        has_battery = battery_soc_match and battery_soc_match.group(1) != 'null' if battery_soc_match else False
        
        scenarios.append({
            'text': scenario_text,
            'num_phases': num_phases,
            'has_battery': has_battery
        })
    
    return scenarios

def write_grouped_file(filepath, header_comment, scenarios_texts):
    """Write scenarios to file preserving all formatting."""
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(header_comment)
        f.write('\nscenarios:\n')
        for text in scenarios_texts:
            f.write(text)

# Main execution
all_scenarios = []

# Read from all existing files
for yaml_file in sorted(SCENARIOS_DIR.glob('*.yaml')):
    scenarios = extract_scenarios_from_file(yaml_file)
    all_scenarios.extend(scenarios)

print(f"Found {len(all_scenarios)} total scenarios")

# Group scenarios
groups = {
    '1ph': [],
    '1ph_battery': [],
    '3ph': [],
    '3ph_battery': []
}

for scenario in all_scenarios:
    if scenario['num_phases'] == 1:
        if scenario['has_battery']:
            groups['1ph_battery'].append(scenario['text'])
        else:
            groups['1ph'].append(scenario['text'])
    else:  # 3-phase
        if scenario['has_battery']:
            groups['3ph_battery'].append(scenario['text'])
        else:
            groups['3ph'].append(scenario['text'])

# Write grouped files
write_grouped_file(
    SCENARIOS_DIR / 'test_scenarios_1ph.yaml',
    '# Test scenarios: Single-phase sites WITHOUT battery\n# All comments and formatting preserved\n',
    groups['1ph']
)

write_grouped_file(
    SCENARIOS_DIR / 'test_scenarios_1ph_battery.yaml',
    '# Test scenarios: Single-phase sites WITH battery\n# All comments and formatting preserved\n',
    groups['1ph_battery']
)

write_grouped_file(
    SCENARIOS_DIR / 'test_scenarios_3ph.yaml',
    '# Test scenarios: Three-phase sites WITHOUT battery\n# All comments and formatting preserved\n',
    groups['3ph']
)

write_grouped_file(
    SCENARIOS_DIR / 'test_scenarios_3ph_battery.yaml',
    '# Test scenarios: Three-phase sites WITH battery\n# All comments and formatting preserved\n',
    groups['3ph_battery']
)

print(f"\nGrouped scenarios:")
print(f"  1ph (no battery): {len(groups['1ph'])}")
print(f"  1ph (with battery): {len(groups['1ph_battery'])}")
print(f"  3ph (no battery): {len(groups['3ph'])}")
print(f"  3ph (with battery): {len(groups['3ph_battery'])}")
print(f"\nTotal: {sum(len(g) for g in groups.values())}")