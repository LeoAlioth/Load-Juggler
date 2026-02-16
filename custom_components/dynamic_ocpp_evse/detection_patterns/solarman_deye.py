"""Solarman / Deye inverter detection patterns.

Covers Deye, Sunsynk, and other inverters using the Solarman data logger.
Entity names depend on the YAML definition file used in the Solarman integration.
"""

GRID_CT = [
    {
        "name": "Solarman/Deye - external CTs",
        "patterns": {
            "phase_a": r'sensor\..*_external_ct1_current.*',
            "phase_b": r'sensor\..*_external_ct2_current.*',
            "phase_c": r'sensor\..*_external_ct3_current.*',
        },
        "unit": "A",
    },
    {
        "name": "Solarman/Deye - internal CTs",
        "patterns": {
            "phase_a": r'sensor\..*_internal_ct1_current.*',
            "phase_b": r'sensor\..*_internal_ct2_current.*',
            "phase_c": r'sensor\..*_internal_ct3_current.*',
        },
        "unit": "A",
    },
    {
        "name": "Solarman/Deye - grid power (individual phases)",
        "patterns": {
            "phase_a": r'sensor\..*grid_(?:1|l1|power_1|power_l1).*',
            "phase_b": r'sensor\..*grid_(?:2|l2|power_2|power_l2).*',
            "phase_c": r'sensor\..*grid_(?:3|l3|power_3|power_l3).*',
        },
        "unit": "W",
    },
]

INVERTER_OUTPUT = [
    {
        "name": "Solarman/Deye",
        "patterns": {
            "phase_a": r'sensor\..*(?:output|inverter)_(?:current_)?(?:l1|1|phase_?a).*',
            "phase_b": r'sensor\..*(?:output|inverter)_(?:current_)?(?:l2|2|phase_?b).*',
            "phase_c": r'sensor\..*(?:output|inverter)_(?:current_)?(?:l3|3|phase_?c).*',
        },
    },
]

BATTERY_SOC = [
    {"name": "Solarman/Deye", "pattern": r'sensor\..*_battery_capacity$'},
]

BATTERY_POWER = [
    {"name": "Solarman/Deye", "pattern": r'sensor\..*battery.*charge.*discharge.*power.*'},
]

SOLAR_PRODUCTION = [
    {"name": "Solarman/Deye", "pattern": r'sensor\..*_total_dc_power$'},
]
