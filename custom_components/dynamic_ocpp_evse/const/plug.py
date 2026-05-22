"""Smart-plug constants — switch entity, power rating, modes."""

from .common import OperatingMode

CONF_PLUG_SWITCH_ENTITY_ID = "plug_switch_entity_id"  # HA switch entity to control on/off
CONF_PLUG_POWER_RATING = "plug_power_rating"  # Set power — the load's draw, in watts
CONF_PLUG_MAX_CURRENT = "plug_max_current"  # Plug hardware current rating (A)
CONF_PLUG_POWER_MONITOR_ENTITY_ID = "plug_power_monitor_entity_id"  # Optional power monitoring sensor
DEFAULT_PLUG_POWER_RATING = 2000
DEFAULT_PLUG_MAX_CURRENT = 16

# Smart-plug operating modes — priority is the distribution urgency tier (1-4).
# A binary on/off load; each mode (bar Continuous) never uses the grid and
# drains the home battery only to a progressively higher floor:
#   Continuous     → battery to minimum, then grid, then stop
#   Solar Priority → battery to minimum, then stop  (no grid)
#   Solar Only     → battery to target,  then stop  (no grid)
#   Excess         → only when the battery is near-full or the site is exporting
PLUG_MODE_CONTINUOUS = OperatingMode(
    key="Continuous", label="Continuous", priority=1, icon="mdi:flash",
)
PLUG_MODE_SOLAR_PRIORITY = OperatingMode(
    key="Solar Priority", label="Solar Priority", priority=2, icon="mdi:leaf",
)
PLUG_MODE_SOLAR_ONLY = OperatingMode(
    key="Solar Only", label="Solar Only", priority=3, icon="mdi:solar-power",
)
PLUG_MODE_EXCESS = OperatingMode(
    key="Excess", label="Excess", priority=4, icon="mdi:solar-power-variant",
)
OPERATING_MODES_PLUG = [
    PLUG_MODE_CONTINUOUS,
    PLUG_MODE_SOLAR_PRIORITY,
    PLUG_MODE_SOLAR_ONLY,
    PLUG_MODE_EXCESS,
]
DEFAULT_OPERATING_MODE_PLUG = PLUG_MODE_CONTINUOUS
