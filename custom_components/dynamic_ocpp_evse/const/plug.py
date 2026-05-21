"""Smart-plug constants — switch entity, power rating, modes."""

from .common import OperatingMode

CONF_PLUG_SWITCH_ENTITY_ID = "plug_switch_entity_id"  # HA switch entity to control on/off
CONF_PLUG_POWER_RATING = "plug_power_rating"  # Fixed power draw in watts
CONF_PLUG_POWER_MONITOR_ENTITY_ID = "plug_power_monitor_entity_id"  # Optional power monitoring sensor
DEFAULT_PLUG_POWER_RATING = 2000

# Smart-plug operating modes — priority is the distribution urgency tier (1-4).
PLUG_MODE_CONTINUOUS = OperatingMode(
    key="Continuous", label="Continuous", priority=1, icon="mdi:flash",
)
PLUG_MODE_SOLAR_ONLY = OperatingMode(
    key="Solar Only", label="Solar Only", priority=2, icon="mdi:solar-power",
)
PLUG_MODE_EXCESS = OperatingMode(
    key="Excess", label="Excess", priority=4, icon="mdi:solar-power-variant",
)
OPERATING_MODES_PLUG = [
    PLUG_MODE_CONTINUOUS,
    PLUG_MODE_SOLAR_ONLY,
    PLUG_MODE_EXCESS,
]
DEFAULT_OPERATING_MODE_PLUG = PLUG_MODE_CONTINUOUS
