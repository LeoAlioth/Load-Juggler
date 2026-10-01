"""Huawei Solar / FusionSolar detection patterns.

Custom integration via Modbus TCP (wlcrs/huawei_solar).
Power meter required for per-phase grid data.
Battery support for LUNA2000 series.
"""

GRID_CT = [
    {
        "name": "Huawei - power meter",
        "patterns": {
            "phase_a": r'sensor\..*power_meter_phase_a_current$',
            "phase_b": r'sensor\..*power_meter_phase_b_current$',
            "phase_c": r'sensor\..*power_meter_phase_c_current$',
        },
        "unit": "A",
    },
]
