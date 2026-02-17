"""Fox ESS inverter detection patterns.

Custom Modbus integration (TonyM1958/HA-FoxESS-Modbus) or cloud API.
H1/H3/KH series inverters.  No per-phase grid data on most models.
"""

BATTERY_SOC = [
    {"name": "Fox ESS", "pattern": r'sensor\.foxess.*battery_soc$'},
    {"name": "Fox ESS (cloud)", "pattern": r'sensor\.fox_bat_?soc$'},
]

BATTERY_POWER = [
    {"name": "Fox ESS", "pattern": r'sensor\.foxess.*battery_power$'},
]

SOLAR_PRODUCTION = [
    {"name": "Fox ESS", "pattern": r'sensor\.foxess.*pv_power$'},
]
