"""Smart-plug constants — switch entity, power rating, modes."""

from .common import (
    OPERATING_MODE_CONTINUOUS,
    OPERATING_MODE_SOLAR_ONLY,
    OPERATING_MODE_EXCESS,
)

CONF_PLUG_SWITCH_ENTITY_ID = "plug_switch_entity_id"  # HA switch entity to control on/off
CONF_PLUG_POWER_RATING = "plug_power_rating"  # Fixed power draw in watts
CONF_PLUG_POWER_MONITOR_ENTITY_ID = "plug_power_monitor_entity_id"  # Optional power monitoring sensor
DEFAULT_PLUG_POWER_RATING = 2000

# Smart-plug operating modes
OPERATING_MODES_PLUG = [
    OPERATING_MODE_CONTINUOUS,
    OPERATING_MODE_SOLAR_ONLY,
    OPERATING_MODE_EXCESS,
]
DEFAULT_OPERATING_MODE_PLUG = OPERATING_MODE_CONTINUOUS
