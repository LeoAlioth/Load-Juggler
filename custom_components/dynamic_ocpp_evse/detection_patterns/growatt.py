"""Growatt inverter detection patterns.

Multiple integration options: Server API or Local Modbus.
Entity naming depends heavily on which integration is used.
MOD TL3-XH series has better per-phase support.
"""

# Growatt uses common naming for battery (_battery_soc, _battery_power).
# Only the solar entity has a distinctive name.

SOLAR_PRODUCTION = [
    {"name": "Growatt", "pattern": r'sensor\..*_solar_total_power$'},
]
