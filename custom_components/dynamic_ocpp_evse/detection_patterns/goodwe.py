"""GoodWe inverter detection patterns.

Custom integration (mletenay/home-assistant-goodwe-inverter).
ET/EH hybrid families have best support.
No per-phase grid data available.
"""

# GoodWe uses common naming (_battery_soc, _battery_power, _pv_power)
# that is handled by generic patterns.  Only brand-prefixed patterns here.

SOLAR_PRODUCTION = [
    {"name": "GoodWe", "pattern": r'sensor\..*goodwe.*_pv_power$'},
]
