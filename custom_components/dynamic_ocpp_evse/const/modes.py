"""Central operating-mode registry.

The per-device-type modules (evse.py / plug.py / hot_water_tank.py) define each
device type's modes as user-facing catalogs — name, urgency priority, icon.
This module is the single place that bridges those modes to the engine: it
maps every mode to the BEHAVIOR_* it competes with, and resolves a stored mode
key back to its OperatingMode. Keeping the mapping here keeps the device
modules free of engine concepts.
"""

from .common import (
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_HOT_WATER_TANK,
    BEHAVIOR_FULL_POWER,
    BEHAVIOR_SOLAR_PRIORITY,
    BEHAVIOR_SOLAR_ONLY,
    BEHAVIOR_EXCESS,
    BEHAVIOR_BINARY_ABOVE_MIN,
    BEHAVIOR_BINARY_ABOVE_TARGET,
    BEHAVIOR_BINARY_EXCESS,
)
from .evse import (
    OPERATING_MODES_EVSE,
    DEFAULT_OPERATING_MODE_EVSE,
    EVSE_MODE_STANDARD,
    EVSE_MODE_SOLAR_PRIORITY,
    EVSE_MODE_SOLAR_ONLY,
    EVSE_MODE_EXCESS,
)
from .plug import (
    OPERATING_MODES_PLUG,
    DEFAULT_OPERATING_MODE_PLUG,
    PLUG_MODE_CONTINUOUS,
    PLUG_MODE_SOLAR_PRIORITY,
    PLUG_MODE_SOLAR_ONLY,
    PLUG_MODE_EXCESS,
)
from .hot_water_tank import (
    OPERATING_MODES_HOT_WATER_TANK,
    DEFAULT_OPERATING_MODE_HOT_WATER_TANK,
    TANK_MODE_FREEZE_PROTECTION,
    TANK_MODE_NORMAL,
    TANK_MODE_SOLAR_PRIORITY,
)

# The one place every operating mode is mapped to its engine behavior.
# Multiple modes — across device types — may map to the same behavior.
BEHAVIOR_BY_MODE = {
    EVSE_MODE_STANDARD: BEHAVIOR_FULL_POWER,
    EVSE_MODE_SOLAR_PRIORITY: BEHAVIOR_SOLAR_PRIORITY,
    EVSE_MODE_SOLAR_ONLY: BEHAVIOR_SOLAR_ONLY,
    EVSE_MODE_EXCESS: BEHAVIOR_EXCESS,
    PLUG_MODE_CONTINUOUS: BEHAVIOR_FULL_POWER,
    PLUG_MODE_SOLAR_PRIORITY: BEHAVIOR_BINARY_ABOVE_MIN,
    PLUG_MODE_SOLAR_ONLY: BEHAVIOR_BINARY_ABOVE_TARGET,
    PLUG_MODE_EXCESS: BEHAVIOR_BINARY_EXCESS,
    TANK_MODE_FREEZE_PROTECTION: BEHAVIOR_FULL_POWER,
    TANK_MODE_NORMAL: BEHAVIOR_FULL_POWER,
    TANK_MODE_SOLAR_PRIORITY: BEHAVIOR_SOLAR_PRIORITY,
}

_MODES_BY_TYPE = {
    DEVICE_TYPE_EVSE: (OPERATING_MODES_EVSE, DEFAULT_OPERATING_MODE_EVSE),
    DEVICE_TYPE_PLUG: (OPERATING_MODES_PLUG, DEFAULT_OPERATING_MODE_PLUG),
    DEVICE_TYPE_HOT_WATER_TANK: (
        OPERATING_MODES_HOT_WATER_TANK,
        DEFAULT_OPERATING_MODE_HOT_WATER_TANK,
    ),
}

# Every valid mode key across all device types (for service-call validation).
ALL_OPERATING_MODE_KEYS = sorted(
    {m.key for modes, _ in _MODES_BY_TYPE.values() for m in modes}
)


def resolve_operating_mode(device_type, key):
    """Return the OperatingMode for (device_type, stored key).

    Falls back to the device type's default mode if the key is unknown
    (e.g. a stale value left over from an older version).
    """
    modes, default = _MODES_BY_TYPE.get(
        device_type, _MODES_BY_TYPE[DEVICE_TYPE_EVSE]
    )
    for mode in modes:
        if mode.key == key:
            return mode
    return default


def behavior_for(mode):
    """Return the engine BEHAVIOR_* an OperatingMode competes with."""
    return BEHAVIOR_BY_MODE[mode]
