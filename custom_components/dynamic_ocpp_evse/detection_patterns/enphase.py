"""Enphase Envoy detection patterns.

Official HA Core integration via local Envoy gateway.
Per-phase sensors disabled by default — must be enabled in entity settings.
CT clamps required for consumption data.  Battery support for Encharge series.
"""

GRID_CT = [
    {
        "name": "Enphase Envoy - consumption per phase",
        "patterns": {
            "phase_a": r'sensor\.envoy.*_consumption_phase_a$',
            "phase_b": r'sensor\.envoy.*_consumption_phase_b$',
            "phase_c": r'sensor\.envoy.*_consumption_phase_c$',
        },
        "unit": "W",
    },
]

BATTERY_SOC = [
    {"name": "Enphase Encharge", "pattern": r'sensor\.encharge.*_soc$'},
]

BATTERY_POWER = [
    {"name": "Enphase Encharge", "pattern": r'sensor\.encharge.*_power$'},
]

SOLAR_PRODUCTION = [
    {"name": "Enphase Envoy", "pattern": r'sensor\.envoy.*_current_power_production$'},
]
