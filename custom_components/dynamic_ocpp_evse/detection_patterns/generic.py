"""Generic catch-all detection patterns.

These are tried last, after all brand-specific patterns.
They use broad naming conventions common across many inverter brands.
"""

INVERTER_OUTPUT = [
    {
        "name": "Generic - inverter phase power/current",
        "patterns": {
            "phase_a": r'sensor\..*inverter.*(?:power|current).*(?:phase_?a|l1|_1).*',
            "phase_b": r'sensor\..*inverter.*(?:power|current).*(?:phase_?b|l2|_2).*',
            "phase_c": r'sensor\..*inverter.*(?:power|current).*(?:phase_?c|l3|_3).*',
        },
    },
]

BATTERY_SOC = [
    {"name": "Generic", "pattern": r'sensor\..*battery_soc$'},
    {"name": "Generic (level)", "pattern": r'sensor\..*battery_level$'},
    {"name": "Generic (broad)", "pattern": r'sensor\..*battery.*(?:soc|state_of_charge).*'},
]

BATTERY_POWER = [
    {"name": "Generic", "pattern": r'sensor\..*battery_power$'},
    {"name": "Generic (broad)", "pattern": r'sensor\..*battery.*power.*'},
]

SOLAR_PRODUCTION = [
    {"name": "Generic (pv_power)", "pattern": r'sensor\..*_pv_power$'},
    {"name": "Generic (solar)", "pattern": r'sensor\..*solar.*(?:production|power|generation).*'},
    {"name": "Generic (pv generation)", "pattern": r'sensor\..*pv.*(?:generation|production).*'},
]
