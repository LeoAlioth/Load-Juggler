"""Generic catch-all detection patterns.

These are tried last, after all brand-specific patterns.
They use broad naming conventions common across many inverter brands.
"""

BATTERY_MAX_DISCHARGE_POWER = [
    {"name": "Generic", "pattern": r'(?:number|sensor)\..*(?:max.*discharge.*power|discharge.*power.*(?:limit|max))'},
]
