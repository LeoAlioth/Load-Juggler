"""Smart-plug constants - switch entity, power rating, modes."""

from .common import (
    BEHAVIOR_BINARY_ABOVE_MIN,
    BEHAVIOR_BINARY_ABOVE_TARGET,
    BEHAVIOR_BINARY_EXCESS,
    BEHAVIOR_FULL_POWER,
    EXCESS_URGENCY_TIER,
    OperatingMode,
)

CONF_PLUG_SWITCH_ENTITY_ID = "plug_switch_entity_id"  # HA switch entity to control on/off
CONF_PLUG_POWER_RATING = "plug_power_rating"  # Set power - the load's draw, in watts
CONF_PLUG_MAX_CURRENT = "plug_max_current"  # Plug hardware current rating (A)
CONF_PLUG_POWER_MONITOR_ENTITY_ID = "plug_power_monitor_entity_id"  # Optional power monitoring sensor
DEFAULT_PLUG_POWER_RATING = 2000
DEFAULT_PLUG_MAX_CURRENT = 16

# Restart if it stops drawing: a load behind the plug that cuts out on its own
# (an EVSE on over-temperature) leaves the relay on and the plug drawing
# nothing, and a turn_on to a relay already on does nothing. With this on and
# a power monitor configured, a permitted plug whose switch is on but which
# draws under PLUG_NO_DRAW_W for the "restart after" span is power-cycled
# (control/plug.py).
CONF_PLUG_RESTART_ON_NO_DRAW = "plug_restart_on_no_draw"
CONF_PLUG_RESTART_AFTER = "plug_restart_after"  # minutes of no draw
DEFAULT_PLUG_RESTART_ON_NO_DRAW = False
DEFAULT_PLUG_RESTART_AFTER = 10
# Finish a cycle before switching off: a load Load Juggler would switch off
# stays on until its monitor has read under this many W for this long (a
# washing machine mid-cycle). 0 W = off.
CONF_PLUG_FINISH_BELOW_W = "plug_finish_below_w"
CONF_PLUG_FINISH_FOR = "plug_finish_for"  # minutes
DEFAULT_PLUG_FINISH_BELOW_W = 0
DEFAULT_PLUG_FINISH_FOR = 5
PLUG_NO_DRAW_W = 10  # under this the load counts as not drawing
PLUG_RESTART_OFF_S = 30  # how long a restart holds the switch off
PLUG_RESTART_MIN_GAP_S = 30 * 60  # at most one restart per this span
PLUG_RESTART_MAX_TRIES = 3  # restarts in a row without a draw, then give up
PLUG_RESTART_RECOVERY_S = 5 * 60  # drawing this long resets the count

# Smart-plug operating modes - priority is the distribution urgency tier (1-4).
# A binary on/off load; each mode (bar Continuous) never uses the grid and
# drains the home battery only to a progressively higher floor:
#   Continuous     → battery to minimum, then grid, then stop
#   Solar Priority → battery to minimum, then stop  (no grid)
#   Solar Only     → battery to target,  then stop  (no grid)
#   Excess         → only when the battery is near-full or the site is exporting
PLUG_MODE_CONTINUOUS = OperatingMode(
    key="Continuous", label="Continuous", priority=1, icon="mdi:flash",
    behavior=BEHAVIOR_FULL_POWER,
)
PLUG_MODE_SOLAR_PRIORITY = OperatingMode(
    key="Solar Priority", label="Solar Priority", priority=2, icon="mdi:leaf",
    behavior=BEHAVIOR_BINARY_ABOVE_MIN,
)
PLUG_MODE_SOLAR_ONLY = OperatingMode(
    key="Solar Only", label="Solar Only", priority=3, icon="mdi:solar-power",
    behavior=BEHAVIOR_BINARY_ABOVE_TARGET,
)
PLUG_MODE_EXCESS = OperatingMode(
    key="Excess", label="Excess", priority=EXCESS_URGENCY_TIER, icon="mdi:solar-power-variant",
    behavior=BEHAVIOR_BINARY_EXCESS,
)
OPERATING_MODES_PLUG = [
    PLUG_MODE_CONTINUOUS,
    PLUG_MODE_SOLAR_PRIORITY,
    PLUG_MODE_SOLAR_ONLY,
    PLUG_MODE_EXCESS,
]
DEFAULT_OPERATING_MODE_PLUG = PLUG_MODE_CONTINUOUS
