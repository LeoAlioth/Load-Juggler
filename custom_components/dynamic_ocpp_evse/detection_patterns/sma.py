"""SMA inverter detection patterns.

Official HA Core integration (SMA WebConnect).
Energy Meter required for grid measurements.
Sunny Boy Storage required for battery data.
"""

BATTERY_SOC = [
    {"name": "SMA", "pattern": r'sensor\..*_battery_soc_total$'},
]

# SMA splits battery into separate charge/discharge entities —
# not compatible with our single battery_power field.  Users must
# configure battery power manually or use a template sensor.
