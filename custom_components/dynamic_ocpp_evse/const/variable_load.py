"""Variable Load constants (JUG-30) - a modulating load driven through an entity.

Anything that takes a current or power setpoint through a ``number`` /
``input_number`` (a Modbus charger, a heater, a battery charge setpoint). To
the allocation it is a modulating load like an EVSE: the main fuse, circuit
groups and the power budget apply through the same site allocation. Without a
setpoint entity it only publishes its allowance (output-only mode).

The pure arithmetic is calculations/variable_load.py; the writes
control/variable_load.py.
"""

from .common import EXCESS_URGENCY_TIER, BEHAVIOR_EXCESS, OperatingMode
from .evse import OPERATING_MODES_EVSE, EVSE_MODE_STANDARD

CONF_VL_SETPOINT_ENTITY_ID = "vl_setpoint_entity_id"  # number / input_number; none = output only
CONF_VL_SETPOINT_UNIT = "vl_setpoint_unit"            # "A" (per leg) or "W" (total)
CONF_VL_MIN = "vl_min"                                # in the setpoint unit
CONF_VL_MAX = "vl_max"
CONF_VL_BIAS = "vl_bias"                              # added to every setpoint > 0, setpoint unit
CONF_VL_RESEND_INTERVAL = "vl_resend_interval"        # s; 0 = write only on a change
CONF_VL_CURRENT_L1_ENTITY_ID = "vl_current_l1_entity_id"
CONF_VL_CURRENT_L2_ENTITY_ID = "vl_current_l2_entity_id"
CONF_VL_CURRENT_L3_ENTITY_ID = "vl_current_l3_entity_id"
CONF_VL_POWER_ENTITY_ID = "vl_power_entity_id"
CONF_VL_CONNECTED_ENTITY_ID = "vl_connected_entity_id"  # binary_sensor: off = no demand
CONF_VL_CHARGING_ENTITY_ID = "vl_charging_entity_id"    # binary_sensor: off = not drawing
CONF_VL_PHASE_MULTI_ABOVE = "vl_phase_multi_above"      # W
CONF_VL_PHASE_SINGLE_BELOW = "vl_phase_single_below"    # W
CONF_VL_PHASE_MIN_INTERVAL = "vl_phase_min_interval"    # minutes between advice changes
CONF_VL_FALLBACK_SETPOINT = "vl_fallback_setpoint"      # written when the entry unloads; unset = nothing

VL_UNIT_AMPS = "A"
VL_UNIT_WATTS = "W"
DEFAULT_VL_SETPOINT_UNIT = VL_UNIT_AMPS
DEFAULT_VL_MIN = 6
DEFAULT_VL_MAX = 16
DEFAULT_VL_BIAS = 0
DEFAULT_VL_RESEND_INTERVAL = 0
DEFAULT_VL_PHASE_MULTI_ABOVE = 4000
DEFAULT_VL_PHASE_SINGLE_BELOW = 2000
DEFAULT_VL_PHASE_MIN_INTERVAL = 10

# Runtime keys in the load's hass.data[DOMAIN]["loads"][entry_id] bucket.
VL_RT_ALLOWED_A = "vl_allowed_a"        # per-leg amps last handed out (the draw booked without readings)
VL_RT_LEGS_IN_USE = "vl_legs_in_use"    # legs in use by the per-leg readings (remembered while idle)
VL_RT_SIZED_COUNT = "vl_sized_count"    # how many legs the allocation sized it on last cycle
VL_RT_LEGS_GREW = "vl_legs_grew"        # set by the builder, taken by the processor
VL_RT_OUTPUTS = "vl_outputs"            # what the allowance sensors publish
VL_RT_ADVICE = "vl_phase_advice"        # (advice, since) memory of the phase advice
VL_RT_LAST_WRITE = "vl_last_write"      # monotonic time of the last setpoint write

# The EVSE's four modes plus Hold: the setpoint goes to 0 (or the entity's own
# minimum when it cannot take 0) and the load claims nothing.
VL_MODE_HOLD = OperatingMode(
    key="Hold", label="Hold", priority=EXCESS_URGENCY_TIER, icon="mdi:pause-circle-outline",
    behavior=BEHAVIOR_EXCESS,
)
OPERATING_MODES_VARIABLE_LOAD = [*OPERATING_MODES_EVSE, VL_MODE_HOLD]
DEFAULT_OPERATING_MODE_VARIABLE_LOAD = EVSE_MODE_STANDARD
