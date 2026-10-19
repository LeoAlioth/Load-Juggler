"""SG Ready heat pump constants (JUG-24) - two relays, timing limits.

The state machine is calculations/sg_ready.py; the actuation control/sg_ready.py.
"""

CONF_SG_RELAY_1_ENTITY_ID = "sg_relay_1_entity_id"  # closed alone = Block
CONF_SG_RELAY_2_ENTITY_ID = "sg_relay_2_entity_id"  # closed alone = Recommended; both = Force
CONF_SG_NOMINAL_POWER = "sg_nominal_power"  # W the pump adds above Normal in Recommended / Force
CONF_SG_POWER_ENTITY_ID = "sg_power_entity_id"  # optional: the pump's total draw
CONF_SG_MIN_STATE_TIME = "sg_min_state_time"  # minutes every state lasts at least
CONF_SG_MAX_BLOCK_TIME = "sg_max_block_time"  # minutes one Block lasts at most
CONF_SG_MAX_BLOCKS_PER_DAY = "sg_max_blocks_per_day"  # rolling 24 h
DEFAULT_SG_NOMINAL_POWER = 2000
DEFAULT_SG_MIN_STATE_TIME = 10
DEFAULT_SG_MAX_BLOCK_TIME = 120
DEFAULT_SG_MAX_BLOCKS_PER_DAY = 3

# Runtime keys in the load's dict
SG_RT_MEMORY = "_sg_memory"
SG_RT_STATUS = "sg_ready"  # the published decision, for the status sensor
SG_RT_NORMAL_DRAW = "sg_normal_draw_w"
# Seconds a commanded combination may differ from what the relays read before
# it counts as a fault (-> Normal).
SG_VERIFY_S = 60
