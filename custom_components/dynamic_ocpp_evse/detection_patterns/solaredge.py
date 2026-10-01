"""SolarEdge inverter detection patterns.

Entities created by the official SolarEdge Modbus integration.
Meter entities use 'm' prefix, inverter entities use 'i' prefix.
"""

GRID_CT = [
    {
        "name": "SolarEdge",
        "patterns": {
            "phase_a": r'sensor\..*m.*ac_current_a.*',
            "phase_b": r'sensor\..*m.*ac_current_b.*',
            "phase_c": r'sensor\..*m.*ac_current_c.*',
        },
        "unit": "A",
    },
]
