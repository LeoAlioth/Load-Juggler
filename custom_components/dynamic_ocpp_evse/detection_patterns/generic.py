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
    {"name": "Generic (broad)", "pattern": r'sensor\..*battery.*(?:soc|state_of_charge).*'},
    # Note: battery_level$ excluded — matches phones/tablets/laptops.
]

BATTERY_POWER = [
    {"name": "Generic", "pattern": r'sensor\.(?!.*(?:phone|pixel|iphone|ipad|galaxy|oneplus|xiaomi|huawei_p|huawei_mate|samsung_|macbook|laptop|tablet|watch|ring)).*(?:_battery_power|battery_charge.*power)$'},
    # Negative lookahead excludes mobile devices (phones, tablets, laptops, watches).
    # Note: broad battery.*power excluded — matches non-energy devices.
]

BATTERY_MAX_CHARGE_POWER = [
    {"name": "Generic", "pattern": r'(?:number|sensor)\..*(?:max.*charge.*power|charge.*power.*(?:limit|max))'},
]

BATTERY_MAX_DISCHARGE_POWER = [
    {"name": "Generic", "pattern": r'(?:number|sensor)\..*(?:max.*discharge.*power|discharge.*power.*(?:limit|max))'},
]

SOLAR_PRODUCTION = [
    {"name": "Generic (pv_power)", "pattern": r'sensor\..*_pv_power$'},
    {"name": "Generic (solar)", "pattern": r'sensor\..*solar.*(?:production|power|generation).*'},
    {"name": "Generic (pv generation)", "pattern": r'sensor\..*pv.*(?:generation|production).*'},
]
