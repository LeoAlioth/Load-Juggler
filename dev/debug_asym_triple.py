#!/usr/bin/env python3
"""Debug script for asymmetric triple mixed chargers test."""

import sys
import logging
from pathlib import Path

# Enable debug logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

# Import production code
sys.path.insert(0, str(Path(__file__).parent / "custom_components" / "dynamic_ocpp_evse" / "calculations"))

from models import ChargerContext, SiteContext
from target_calculator import calculate_all_charger_targets

# Build test scenario: 3ph-3c-solar-asym-triple-mixed
site = SiteContext(
    voltage=230,
    main_breaker_rating=32,
    max_import_power=22080,
    num_phases=3,
    distribution_mode="priority",
    charging_mode="Solar",
    solar_production_total=14950,  # 65A total
    phase_a_consumption=2.0,
    phase_b_consumption=1.5,
    phase_c_consumption=1.5,
    battery_soc=85,
    battery_soc_min=20,
    battery_soc_target=80,  # SOC > target, battery can discharge
    battery_max_charge_power=5060,  # 22A
    battery_max_discharge_power=5060,  # 22A
    inverter_supports_asymmetric=True,
    inverter_max_power_per_phase=8050,  # 35A
    inverter_max_power=20000,  # 87A
)

# Calculate values
solar_total_amps = 14950 / 230
battery_discharge_amps = 5060 / 230
total_consumption = 2.0 + 1.5 + 1.5
available_current = solar_total_amps - total_consumption + battery_discharge_amps
inverter_max_amps = 20000 / 230

print(f"Site Configuration:")
print(f"  Solar: 14950W = {solar_total_amps:.1f}A total")
print(f"  Consumption: {total_consumption}A (2A + 1.5A + 1.5A)")
print(f"  Battery discharge: 5060W = {battery_discharge_amps:.1f}A")
print(f"  Net available: {solar_total_amps:.1f}A - {total_consumption}A + {battery_discharge_amps:.1f}A = {available_current:.1f}A")
print(f"  Inverter max: 20000W = {inverter_max_amps:.1f}A")
print(f"  Inverter per-phase max: 8050W = {8050/230:.1f}A")
print(f"  Limited by inverter: min({available_current:.1f}, {inverter_max_amps:.1f}) = {min(available_current, inverter_max_amps):.1f}A")
print()

# Calculate solar export
solar_per_phase_a = (14950 / 3) / 230
solar_per_phase_b = (14950 / 3) / 230
solar_per_phase_c = (14950 / 3) / 230
phase_a_export = max(0, solar_per_phase_a - 2.0)
phase_b_export = max(0, solar_per_phase_b - 1.5)
phase_c_export = max(0, solar_per_phase_c - 1.5)
total_export_current = phase_a_export + phase_b_export + phase_c_export
total_export_power = total_export_current * 230

site.phase_a_export = phase_a_export
site.phase_b_export = phase_b_export
site.phase_c_export = phase_c_export
site.total_export_current = total_export_current
site.total_export_power = total_export_power

# Create 3 chargers - NOTE: 1-phase chargers without connected_to_phase default to phase 'A'
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
        phases=1,
        active_phases_mask="A",  # Default when not specified
        priority=2,
    ),
    ChargerContext(
        charger_id="charger_3",
        entity_id="charger_3",
        min_current=6,
        max_current=16,
        phases=1,
        active_phases_mask="A",  # Default when not specified
        priority=3,
    ),
]

site.chargers = chargers

# Run calculation
calculate_all_charger_targets(site)

print(f"=== RESULTS ===")
for charger in chargers:
    print(f"{charger.entity_id}: {charger.target_current:.1f}A (phases={charger.phases})")

print()
print(f"Expected: C1=16A, C2=11A, C3=6A")
print(f"Actual:   C1={chargers[0].target_current:.1f}A, C2={chargers[1].target_current:.1f}A, C3={chargers[2].target_current:.1f}A")
print()
print(f"Power accounting:")
total_used = chargers[0].target_current * 3 + chargers[1].target_current * 1 + chargers[2].target_current * 1
print(f"  Total available: {min(available_current, inverter_max_amps):.1f}A")
print(f"  C1 uses: {chargers[0].target_current * 3:.1f}A (3-phase)")
print(f"  C2 uses: {chargers[1].target_current * 1:.1f}A (1-phase)")
print(f"  C3 uses: {chargers[2].target_current * 1:.1f}A (1-phase)")
print(f"  Total consumed: {total_used:.1f}A")
print()
print("NOTE: This test has verified=False, so expected values may be incorrect")
