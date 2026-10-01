"""Sungrow inverter detection patterns.

Custom Modbus integration (mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant).
SH series hybrid inverters.  Excellent per-phase grid support via meter.
"""

GRID_CT = [
    {
        "name": "Sungrow - meter current",
        "patterns": {
            "phase_a": r'sensor\..*meter_phase_a_current$',
            "phase_b": r'sensor\..*meter_phase_b_current$',
            "phase_c": r'sensor\..*meter_phase_c_current$',
        },
        "unit": "A",
    },
]
