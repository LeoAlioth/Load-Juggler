#!/usr/bin/env python3
"""Debug script for mixed-phase charger scenario."""

import sys
import logging
from pathlib import Path

# Enable debug logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

# Import production code
sys.path.insert(0, str(Path(__file__).parent / "custom_components" / "dynamic_ocpp_evse" / "calculations"))

from models import ChargerContext, SiteContext
from target_calculator import calculate_all_charger_targets

# Build test scenario: 3ph-2c-solar-2ph-and-1ph-mixed-battery
site = SiteContext(
    voltage=230,
    main_breaker_rating=32,
    max_import_power=22080,
    num_phases=3,
    distribution_mode="priority",
    charging_mode="Solar",
    solar_production_total=6900,  # 30A total
    phase_a_consumption=1.0,
    phase_b_consumption=1.0,
    phase_c_consumption=1.0,  # 3A total
    battery_soc=85,
    battery_soc_min=20,
    battery_soc_target=80,  # SOC > target, battery can discharge
    battery_max_charge_power=4140,
    battery_max_discharge_power=4140,  # 18A
    inverter_supports_asymmetric=True,
    inverter_max_power_per_phase=8000,
    inverter_max_power=20000,
)

# Calculate initial export
solar_per_phase_amps = (6900 / 3) / 230
phase_a_export = max(0, solar_per_phase_amps - 1.0)
phase_b_export = max(0, solar_per_phase_amps - 1.0)
phase_c_export = max(0, solar_per_phase_amps - 1.0)
total_export_current = phase_a_export + phase_b_export + phase_c_export
total_export_power = total_export_current * 230

site.phase_a_export = phase_a_export
site.phase_b_export = phase_b_export
site.phase_c_export = phase_c_export
site.total_export_current = total_export_current
site.total_export_power = total_export_power

print(f"Site Configuration:")
print(f"  Solar: {6900}W = {6900/230:.1f}A total")
print(f"  Consumption: 3A total (1A per phase)")
print(f"  Export: {total_export_current:.1f}A ({total_export_power:.0f}W)")
print(f"  Battery SOC: {site.battery_soc}% (target: {site.battery_soc_target}%)")
print(f"  Battery discharge: 4140W = 18A")
print(f"  Expected solar available: 30A - 3A + 18A = 45A")
print()

# Create 2 chargers
chargers = [
    ChargerContext(
        charger_id="charger_1",
        entity_id="charger_1",
        min_current=6,
        max_current=16,
        phases=2,
        active_phases_mask="AB",
        priority=1,
    ),
    ChargerContext(
        charger_id="charger_2",
        entity_id="charger_2",
        min_current=6,
        max_current=16,
        phases=1,
        active_phases_mask="C",
        priority=2,
    ),
]

site.chargers = chargers

# Run calculation
calculate_all_charger_targets(site)

print(f"=== RESULTS ===")
for charger in chargers:
    print(f"{charger.entity_id}: {charger.target_current:.1f}A")

print()
print(f"Expected: C1=16A (2-phase AB), C2=13A (1-phase C)")
print(f"Actual:   C1={chargers[0].target_current:.1f}A, C2={chargers[1].target_current:.1f}A")
print()
print(f"Power accounting:")
print(f"  C1 draws: {chargers[0].target_current}A on phase A + {chargers[0].target_current}A on phase B = {chargers[0].target_current * 2}A total")
print(f"  C2 draws: {chargers[1].target_current}A on phase C")
print(f"  Total: {chargers[0].target_current * 2 + chargers[1].target_current}A (should be ≤ 45A)")
