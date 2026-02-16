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

BATTERY_SOC = [
    {"name": "Huawei", "pattern": r'sensor\..*battery_state_of_capacity$'},
]

BATTERY_POWER = [
    {"name": "Huawei", "pattern": r'sensor\..*battery_charge_discharge_power$'},
]

SOLAR_PRODUCTION = [
    {"name": "Huawei", "pattern": r'sensor\..*inverter_input_power$'},
]

BATTERY_MAX_CHARGE_POWER = [
    {"name": "Huawei", "pattern": r'sensor\..*battery_maximum_charge_power$'},
]

BATTERY_MAX_DISCHARGE_POWER = [
    {"name": "Huawei", "pattern": r'sensor\..*battery_maximum_discharge_power$'},
]
