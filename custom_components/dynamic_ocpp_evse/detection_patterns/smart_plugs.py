"""Smart plug / smart load power monitoring detection patterns.

Covers Shelly, Sonoff, Tasmota, TP-Link Kasa, Tuya, and other WiFi smart plugs.
These patterns detect power monitoring sensors (typically in Watts).
"""

PLUG_POWER_MONITOR = [
    # Shelly plugs (shelly integration)
    {"name": "Shelly Plug", "pattern": r'sensor\.shelly.*plug.*power$'},
    {"name": "Shelly 1PM", "pattern": r'sensor\.shelly.*1pm.*power$'},
    {"name": "Shelly PM Mini", "pattern": r'sensor\.shelly.*pm.*mini.*power$'},
    # Sonoff plugs (eWeLink / SonoffLAN integration)
    {"name": "Sonoff Plug", "pattern": r'sensor\.sonoff.*(?:pow|plug|s[234]0).*power$'},
    # Tasmota plugs (tasmota integration)
    {"name": "Tasmota Power", "pattern": r'sensor\.tasmota.*power$'},
    # TP-Link Kasa plugs
    {"name": "TP-Link Kasa", "pattern": r'sensor\..*kasa.*(?:current_consumption|power)$'},
    # Tuya smart plugs
    {"name": "Tuya Plug", "pattern": r'sensor\..*tuya.*plug.*(?:power|current_consumption)$'},
    # Generic — match entity names with "plug" + "power" (broad fallback)
    {"name": "Generic (plug power)", "pattern": r'sensor\..*plug.*power$'},
]
