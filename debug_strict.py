#!/usr/bin/env python3
"""Debug script for strict distribution test."""

import sys
import logging
from pathlib import Path

# Enable debug logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

# Import production code
sys.path.insert(0, str(Path(__file__).parent / "custom_components" / "dynamic_ocpp_evse" / "calculations"))

from models import ChargerContext, SiteContext
from target_calculator import calculate_all_charger_targets

# Build test scenario: 3ph-3c-solar-strict-distribution
site = SiteContext(
    voltage=230,
    main_breaker_rating=32,
    max_import_power=22080,
    num_phases=3,
    distribution_mode="strict",
    charging_mode="Solar",
    solar_production_total=12000,  # 52.2A total
    phase_a_consumption=1.0,
    phase_b_consumption=1.0,
    phase_c_consumption=1.0,
    battery_soc=85,
    battery_soc_min=20,
    battery_soc_target=80,  # SOC > target, battery can discharge
    battery_max_charge_power=4000,
    battery_max_discharge_power=4000,  # 17.4A
    inverter_supports_asymmetric=True,
    inverter_max_power_per_phase=8000,
    inverter_max_power=20000,
)

# Calculate initial export
solar_total_amps = 12000 / 230
battery_discharge_amps = 4000 / 230
total_consumption = 3.0
available_current = solar_total_amps - total_consumption + battery_discharge_amps

print(f"Site Configuration:")
print(f"  Solar: 12000W = {solar_total_amps:.1f}A total")
print(f"  Consumption: 3A (1A per phase)")
print(f"  Battery discharge: 4000W = {battery_discharge_amps:.1f}A")
print(f"  Net available: {solar_total_amps:.1f}A - 3A + {battery_discharge_amps:.1f}A = {available_current:.1f}A")
print(f"  Inverter max: 20000W = {20000/230:.1f}A")
print(f"  Inverter per-phase max: 8000W = {8000/230:.1f}A")
print()

# Calculate solar export
solar_per_phase = solar_total_amps / 3
phase_a_export = max(0, solar_per_phase - 1.0)
phase_b_export = max(0, solar_per_phase - 1.0)
phase_c_export = max(0, solar_per_phase - 1.0)
total_export_current = phase_a_export + phase_b_export + phase_c_export
total_export_power = total_export_current * 230

site.phase_a_export = phase_a_export
site.phase_b_export = phase_b_export
site.phase_c_export = phase_c_export
site.total_export_current = total_export_current
site.total_export_power = total_export_power

# Create 3 chargers
chargers = [
    ChargerContext(
        charger_id="charger_1",
        entity_id="charger_1",
        min_current=6,
        max_current=16,
        phases=3,
        active_phases_mask="ABC",
        priority=1,
    ),
    ChargerContext(
        charger_id="charger_2",
        entity_id="charger_2",
        min_current=6,
        max_current=16,
        phases=3,
        active_phases_mask="ABC",
        priority=2,
    ),
    ChargerContext(
        charger_id="charger_3",
        entity_id="charger_3",
        min_current=6,
        max_current=10,
        phases=3,
        active_phases_mask="ABC",
        priority=3,
    ),
]

site.chargers = chargers

# Run calculation
calculate_all_charger_targets(site)

print(f"=== RESULTS ===")
for charger in chargers:
    print(f"{charger.entity_id}: {charger.target_current:.1f}A")

print()
print(f"Expected: C1=16A, C2=0A, C3=0A")
print(f"Actual:   C1={chargers[0].target_current:.1f}A, C2={chargers[1].target_current:.1f}A, C3={chargers[2].target_current:.1f}A")
print()
print(f"Power accounting:")
total_used = sum(c.target_current * c.phases for c in chargers)
print(f"  Total available: {available_current:.1f}A")
print(f"  C1 uses: {chargers[0].target_current * 3:.1f}A")
print(f"  Remaining after C1: {available_current - chargers[0].target_current * 3:.1f}A")
print(f"  C2 minimum needed: {chargers[1].min_current * 3}A (per-phase: {chargers[1].min_current}A)")
print(f"  Total consumed: {total_used:.1f}A")
