#!/usr/bin/env python3
"""Debug script for Excess mode failure."""

import sys
import logging
from pathlib import Path

# Enable debug logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

# Import production code
sys.path.insert(0, str(Path(__file__).parent / "custom_components" / "dynamic_ocpp_evse" / "calculations"))

from models import ChargerContext, SiteContext
from target_calculator import calculate_all_charger_targets

# Build test scenario: 3ph-1c-excess-prio-with-bat-above-threshold
site = SiteContext(
    voltage=230,
    main_breaker_rating=25,
    max_import_power=17250,
    num_phases=3,
    distribution_mode="priority",
    charging_mode="Excess",
    solar_production_total=11840,  # 11.84kW
    phase_a_consumption=0,
    phase_b_consumption=0,
    phase_c_consumption=0,
    excess_export_threshold=10000,  # 10kW
    battery_soc=97,
    battery_soc_min=20,
    battery_soc_target=80,
    battery_max_charge_power=4140,
    battery_max_discharge_power=4140,
    inverter_supports_asymmetric=True,
    inverter_max_power_per_phase=6000,
    inverter_max_power=12000,
)

# Calculate initial export
solar_per_phase_amps = (11840 / 3) / 230
phase_a_export = max(0, solar_per_phase_amps - 0)
phase_b_export = max(0, solar_per_phase_amps - 0)
phase_c_export = max(0, solar_per_phase_amps - 0)
total_export_current = phase_a_export + phase_b_export + phase_c_export
total_export_power = total_export_current * 230

site.phase_a_export = phase_a_export
site.phase_b_export = phase_b_export
site.phase_c_export = phase_c_export
site.total_export_current = total_export_current
site.total_export_power = total_export_power

print(f"Site export: {total_export_current:.1f}A ({total_export_power:.0f}W)")
print(f"Threshold: {site.excess_export_threshold}W")
print(f"Exceeds threshold: {total_export_power > site.excess_export_threshold}")

# Create 1-phase charger
charger = ChargerContext(
    charger_id="charger_1",
    entity_id="charger_1",
    min_current=6,
    max_current=16,
    phases=1,
    priority=1,
)

print(f"\nCharger phase mask: {charger.active_phases_mask}")
print(f"Charger connector status: {charger.connector_status}")

site.chargers = [charger]

# Run calculation
calculate_all_charger_targets(site)

print(f"\n=== RESULT ===")
print(f"Charger target: {charger.target_current:.1f}A")
print(f"Expected: 8.0A")
print(f"Status: {'PASS' if abs(charger.target_current - 8.0) < 0.1 else 'FAIL'}")
