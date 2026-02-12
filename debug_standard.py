#!/usr/bin/env python3
"""Debug script for Standard mode 3-charger failure."""

import sys
import logging
from pathlib import Path

# Enable debug logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

# Import production code
sys.path.insert(0, str(Path(__file__).parent / "custom_components" / "dynamic_ocpp_evse" / "calculations"))

from models import ChargerContext, SiteContext
from target_calculator import calculate_all_charger_targets

# Build test scenario: 3ph-3c-standard-prio-with-bat-normal
site = SiteContext(
    voltage=230,
    main_breaker_rating=25,
    max_import_power=17250,
    num_phases=3,
    distribution_mode="priority",
    charging_mode="Standard",
    solar_production_total=4140,  # 18A total
    phase_a_consumption=3.0,
    phase_b_consumption=3.0,
    phase_c_consumption=3.0,  # 9A total
    battery_soc=80,
    battery_soc_min=20,
    battery_soc_target=80,  # Battery at target!
    battery_max_charge_power=4140,
    battery_max_discharge_power=4140,
    inverter_supports_asymmetric=True,
    inverter_max_power_per_phase=6000,
    inverter_max_power=12000,
)

# Calculate initial export
solar_per_phase_amps = (4140 / 3) / 230
phase_a_export = max(0, solar_per_phase_amps - 3.0)
phase_b_export = max(0, solar_per_phase_amps - 3.0)
phase_c_export = max(0, solar_per_phase_amps - 3.0)
total_export_current = phase_a_export + phase_b_export + phase_c_export
total_export_power = total_export_current * 230

site.phase_a_export = phase_a_export
site.phase_b_export = phase_b_export
site.phase_c_export = phase_c_export
site.total_export_current = total_export_current
site.total_export_power = total_export_power

print(f"Site Configuration:")
print(f"  Solar: {4140}W = {4140/230:.1f}A total")
print(f"  Consumption: 9A total (3A per phase)")
print(f"  Export: {total_export_current:.1f}A ({total_export_power:.0f}W)")
print(f"  Battery SOC: {site.battery_soc}% (target: {site.battery_soc_target}%)")
print(f"  Main breaker: {site.main_breaker_rating}A per phase")
print(f"  Max import: {site.max_import_power}W = {site.max_import_power/230:.1f}A total")
print()

# Create 3 chargers
chargers = [
    ChargerContext(
        charger_id="charger_1",
        entity_id="charger_1",
        min_current=6,
        max_current=16,
        phases=3,
        priority=1,
    ),
    ChargerContext(
        charger_id="charger_2",
        entity_id="charger_2",
        min_current=6,
        max_current=16,
        phases=3,
        priority=2,
    ),
    ChargerContext(
        charger_id="charger_3",
        entity_id="charger_3",
        min_current=6,
        max_current=10,
        phases=3,
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
print(f"Expected: C1=16A, C2=12A, C3=6A")
print(f"Actual:   C1={chargers[0].target_current:.1f}A, C2={chargers[1].target_current:.1f}A, C3={chargers[2].target_current:.1f}A")
