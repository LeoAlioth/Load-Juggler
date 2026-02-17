"""Fronius inverter detection patterns.

Official HA Core integration via Fronius Solar API.
SmartMeter required for per-phase grid measurements.
GEN24 systems have battery support.
"""

GRID_CT = [
    {
        "name": "Fronius SmartMeter",
        "patterns": {
            "phase_a": r'sensor\..*_current_ac_phase_1$',
            "phase_b": r'sensor\..*_current_ac_phase_2$',
            "phase_c": r'sensor\..*_current_ac_phase_3$',
        },
        "unit": "A",
    },
]

INVERTER_OUTPUT = [
    {
        "name": "Fronius SmartMeter - real power",
        "patterns": {
            "phase_a": r'sensor\..*_power_real_phase_1$',
            "phase_b": r'sensor\..*_power_real_phase_2$',
            "phase_c": r'sensor\..*_power_real_phase_3$',
        },
    },
]

BATTERY_SOC = [
    {"name": "Fronius", "pattern": r'sensor\..*_state_of_charge$'},
]

SOLAR_PRODUCTION = [
    {"name": "Fronius", "pattern": r'sensor\..*_power_photovoltaics$'},
]
