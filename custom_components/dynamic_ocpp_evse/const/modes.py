"""Central operating-mode registry.

The per-device-type modules (evse.py / plug.py / hot_water_tank.py /
power_station.py) define each device type's modes - name, urgency priority,
icon and the engine behavior it competes with. This module maps a device type
to its modes and resolves a stored mode key back to its OperatingMode.
"""

from .common import (
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_POWER_STATION,
)
from .evse import OPERATING_MODES_EVSE, DEFAULT_OPERATING_MODE_EVSE
from .plug import OPERATING_MODES_PLUG, DEFAULT_OPERATING_MODE_PLUG
from .hot_water_tank import (
    OPERATING_MODES_HOT_WATER_TANK,
    DEFAULT_OPERATING_MODE_HOT_WATER_TANK,
)
from .power_station import (
    OPERATING_MODES_POWER_STATION,
    DEFAULT_OPERATING_MODE_POWER_STATION,
)

_MODES_BY_TYPE = {
    DEVICE_TYPE_EVSE: (OPERATING_MODES_EVSE, DEFAULT_OPERATING_MODE_EVSE),
    DEVICE_TYPE_PLUG: (OPERATING_MODES_PLUG, DEFAULT_OPERATING_MODE_PLUG),
    DEVICE_TYPE_HOT_WATER_TANK: (
        OPERATING_MODES_HOT_WATER_TANK,
        DEFAULT_OPERATING_MODE_HOT_WATER_TANK,
    ),
    DEVICE_TYPE_POWER_STATION: (
        OPERATING_MODES_POWER_STATION,
        DEFAULT_OPERATING_MODE_POWER_STATION,
    ),
}

# Every valid mode key across all device types (for service-call validation).
ALL_OPERATING_MODE_KEYS = sorted(
    {m.key for modes, _ in _MODES_BY_TYPE.values() for m in modes}
)


def modes_for(device_type):
    """``(modes, default mode)`` of a device type; an unknown type is an EVSE."""
    return _MODES_BY_TYPE.get(device_type, _MODES_BY_TYPE[DEVICE_TYPE_EVSE])


def resolve_operating_mode(device_type, key):
    """Return the OperatingMode for (device_type, stored key).

    Falls back to the device type's default mode if the key is unknown
    (e.g. a stale value left over from an older version).
    """
    modes, default = modes_for(device_type)
    for mode in modes:
        if mode.key == key:
            return mode
    return default
