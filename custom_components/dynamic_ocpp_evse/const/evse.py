"""EVSE (OCPP charger) constants - entities, OCPP, charge limits, modes."""

from .common import (
    BEHAVIOR_EXCESS,
    BEHAVIOR_FULL_POWER,
    BEHAVIOR_SOLAR_ONLY,
    BEHAVIOR_SOLAR_PRIORITY,
    EXCESS_URGENCY_TIER,
    OperatingMode,
)

# EVSE configuration keys
CONF_OCPP_DEVICE_ID = "ocpp_device_id"
CONF_EVSE_CURRENT_IMPORT_ENTITY_ID = "evse_current_import_entity_id"
CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID = "evse_current_import_l1_entity_id"
CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID = "evse_current_import_l2_entity_id"
CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID = "evse_current_import_l3_entity_id"
CONF_EVSE_CURRENT_OFFERED_ENTITY_ID = "evse_current_offered_entity_id"
CONF_EVSE_POWER_OFFERED_ENTITY_ID = "evse_power_offered_entity_id"
CONF_EVSE_POWER_IMPORT_ENTITY_ID = "evse_power_import_entity_id"
CONF_EVSE_MINIMUM_CHARGE_CURRENT = "evse_minimum_charge_current"  # defaults to 6
CONF_EVSE_MAXIMUM_CHARGE_CURRENT = "evse_maximum_charge_current"  # defaults to 16
CONF_MIN_CURRENT_ENTITY_ID = "min_current_entity_id"
CONF_MAX_CURRENT_ENTITY_ID = "max_current_entity_id"
CONF_OCPP_PROFILE_TIMEOUT = "ocpp_profile_timeout"
CONF_CHARGE_PAUSE_DURATION = "charge_pause_duration"
CONF_PAUSE_PROFILE_VALIDITY = "pause_profile_validity"  # minutes a pause's 0 A profile stays valid
CONF_STACK_LEVEL = "stack_level"

# OCPP charger L1/L2/L3 → site phase mapping
CONF_CHARGER_L1_PHASE = "charger_l1_phase"
CONF_CHARGER_L2_PHASE = "charger_l2_phase"
CONF_CHARGER_L3_PHASE = "charger_l3_phase"

# The ocpp integration's own domain - the device-registry identifier domain it
# stamps on every charge point ("ocpp", <charge point id>), the platform its
# entities carry, and the device selector's integration filter.
OCPP_INTEGRATION_DOMAIN = "ocpp"

# The charger_info device picker's form key. Flow-only, never stored: the HA
# device-registry UUID it hands back is resolved to CONF_OCPP_DEVICE_ID (the
# OCPP charge point id) and to the charger's sensor entities before the entry
# is written, because a UUID is not something an ocpp service can address.
FIELD_OCPP_DEVICE = "ocpp_device"

# OCPP integration entity suffixes for auto-discovery
OCPP_ENTITY_SUFFIX_CURRENT_IMPORT = "_current_import"
OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L1 = "_current_import_l1"
OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L2 = "_current_import_l2"
OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L3 = "_current_import_l3"
OCPP_ENTITY_SUFFIX_CURRENT_OFFERED = "_current_offered"
OCPP_ENTITY_SUFFIX_POWER_OFFERED = "_power_offered"
OCPP_ENTITY_SUFFIX_POWER_IMPORT = "_power_active_import"
# The connector status ("Status.Connector") - classified like the metrics above
# but never stored on an entry: it is resolved from the registries at setup.
OCPP_ENTITY_SUFFIX_STATUS_CONNECTOR = "_status_connector"

# Runtime keys in an EVSE's ``hass.data[DOMAIN]["loads"][entry_id]`` bucket.
#
# The limit the charger was last told, in amps, and the unit its profile was
# encoded in ("A" or "W") - written by control/ocpp.py only after
# set_charge_rate returned without raising, so it is what the charger actually
# holds, not what the engine wished for. Read by the engine's stuck-readout
# watch, which judges the charger's reported draw against it.
EVSE_RT_COMMANDED_LIMIT = "commanded_limit"
EVSE_RT_COMMANDED_RATE_UNIT = "commanded_rate_unit"
# The stuck-readout watch's state (engine/readout_watch.py), plus the display
# fields the builder adds for the load's Available Current attributes.
EVSE_RT_READOUT_WATCH = "readout_watch"
# Phase switching (calculations/phase_switch.py, docs/plans/2026-11-01/phase-switching.md):
# the phase count the engine decided (1 or 3), since when (monotonic) and why;
# whether the charger reports ConnectorSwitch3to1PhaseSupported (True / False,
# None until read) and when to ask again; the phase count its last profile
# carried (control/ocpp.py, after set_charge_rate returned).
EVSE_RT_PHASES = "phase_switch_phases"
EVSE_RT_PHASES_SINCE = "phase_switch_since"
EVSE_RT_PHASES_REASON = "phase_switch_reason"
# Since when (monotonic) the load on one phase could take the minimum plus the
# margin on all three legs; cleared the cycle it cannot.
EVSE_RT_PHASES_ABOVE_AT = "phase_switch_above_at"
EVSE_RT_PHASE_SUPPORTED = "phase_switch_supported"
EVSE_RT_PHASE_PROBE_AT = "phase_switch_probe_at"
EVSE_RT_COMMANDED_PHASES = "commanded_phases"

# Per-charger option: the current (A) the charger is held at while Dynamic Control
# is off. Not stored or EVSE_DC_OFF_MAX: its maximum current (the behaviour before
# the option); None (the field cleared): no profile at all - the charger keeps the
# last one until it runs out (the owner, 2026-11-02).
CONF_EVSE_DC_OFF_CURRENT = "dynamic_control_off_current"
EVSE_DC_OFF_MAX = "max"

# Phase switching is the charger's Phase Switching select (select.py), published
# into the runtime as EVSE_RT_PHASE_MODE: Auto (switch to one phase instead of a
# pause), Three phases (never switch), One phase (always on its L1). The stored
# option below was the Configure checkbox before 2.1.7; it only seeds the
# select's first value (on: Auto, off: Three phases).
CONF_PHASE_SWITCHING = "phase_switching"
DEFAULT_PHASE_SWITCHING = True
EVSE_RT_PHASE_MODE = "phase_mode"
PHASE_MODE_AUTO = "Auto"
PHASE_MODE_THREE = "Three phases"
PHASE_MODE_ONE = "One phase"
PHASE_MODES = [PHASE_MODE_AUTO, PHASE_MODE_THREE, PHASE_MODE_ONE]
# When the charger last went to one phase (ISO, UTC) and how many times it has:
# the Last Switch to One Phase sensor shows and restores them.
EVSE_RT_ONE_PHASE_AT = "one_phase_at"
EVSE_RT_ONE_PHASE_COUNT = "one_phase_count"
# Per-charger option: a charger that can switch is told its phase count
# (numberPhases) on every profile. Off: no profile carries it, as before 2.1.7,
# and phase switching cannot work (JUG-41: whether a 0 A with it is what faults
# the third site's go-e; the owner, 2026-11-05).
CONF_SEND_NUMBER_PHASES = "send_number_phases"
DEFAULT_SEND_NUMBER_PHASES = True
# The OCPP configuration key a charger answers "true" to when it can switch.
OCPP_KEY_PHASE_SWITCH = "ConnectorSwitch3to1PhaseSupported"
# Per-charger option: no phase change with a car connected sooner than this
# many minutes after the last one, in either direction (the owner, 2026-11-05:
# on 2026-11-05 the third site's go-e was switched to one phase and back within
# 2-8 s, three times in two minutes, because its own stop for the switch read
# as "not charging"). A fixed 10 min until 2026-11-10, then an option, 15 by
# default (the owner).
CONF_PHASE_SWITCH_INTERVAL = "phase_switch_interval"
DEFAULT_PHASE_SWITCH_INTERVAL = 15
# A charger that draws is paused (0 A, its old phase count) before its new
# phase count goes out: at least PHASE_SWITCH_PAUSE_S, until it draws under
# 1 A, but no longer than PHASE_SWITCH_PAUSE_MAX_S (a reading that never comes)
# (the owner, 2026-11-05; myenergi waits 90 s, the go-e pauses ~35 s itself).
PHASE_SWITCH_PAUSE_S = 60
PHASE_SWITCH_PAUSE_MAX_S = 180
# When the pause before a phase switch began (monotonic), None outside one.
EVSE_RT_PHASE_PAUSE_AT = "phase_switch_pause_at"
# Set by Reset OCPP: the load's next cycle drops any charge pause it is in (the
# owner, 2026-11-05: a reset clears pauses). A charger back from Faulted drops
# it too - its reboot lost the pause's 0 A, and re-sending it stopped the third
# site's go-e again 20 s after it came back.
EVSE_RT_CLEAR_PAUSE = "clear_pause"
# Per-charger option: three phases again only when they would give at least this
# much more power (W) than one phase gets now, all three legs at the minimum or
# more, without a break for PHASE_SWITCH_UP_HOLD_S. At the third site one phase
# at 20 A (4.6 kW) against three at 6 A (4.1 kW) under a 5.4 kW cap: 1 kW kept
# the switches where the old rule had them (the minimum plus 2 A on all three)
# while still going back when three give clearly more (the owner, 2026-11-11).
CONF_PHASE_SWITCH_UP_MARGIN = "phase_switch_up_margin"
DEFAULT_PHASE_SWITCH_UP_MARGIN = 1000
PHASE_SWITCH_UP_HOLD_S = 300
# How often an unanswered capability read is tried again (s).
PHASE_SWITCH_PROBE_RETRY_S = 600

# EVSE default values
DEFAULT_MIN_CHARGE_CURRENT = 6
DEFAULT_MAX_CHARGE_CURRENT = 16
DEFAULT_OCPP_PROFILE_TIMEOUT = 120
DEFAULT_CHARGE_PAUSE_DURATION = 3  # minutes
DEFAULT_PAUSE_PROFILE_VALIDITY = 60  # minutes
DEFAULT_STACK_LEVEL = 3

# Charge rate unit configuration (per charger)
CONF_CHARGE_RATE_UNIT = "charge_rate_unit"
CHARGE_RATE_UNIT_AUTO = "auto"
CHARGE_RATE_UNIT_AMPS = "A"
CHARGE_RATE_UNIT_WATTS = "W"
DEFAULT_CHARGE_RATE_UNIT = CHARGE_RATE_UNIT_AUTO

# Profile validity mode configuration (per charger)
CONF_PROFILE_VALIDITY_MODE = "profile_validity_mode"
PROFILE_VALIDITY_MODE_RELATIVE = "relative"
PROFILE_VALIDITY_MODE_ABSOLUTE = "absolute"
DEFAULT_PROFILE_VALIDITY_MODE = PROFILE_VALIDITY_MODE_ABSOLUTE

# EVSE operating modes - priority is the distribution urgency tier (1-4).
EVSE_MODE_STANDARD = OperatingMode(
    key="Standard", label="Standard", priority=1, icon="mdi:flash",
    behavior=BEHAVIOR_FULL_POWER,
)
EVSE_MODE_SOLAR_PRIORITY = OperatingMode(
    key="Solar Priority", label="Solar Priority", priority=2, icon="mdi:leaf",
    behavior=BEHAVIOR_SOLAR_PRIORITY,
)
EVSE_MODE_SOLAR_ONLY = OperatingMode(
    key="Solar Only", label="Solar Only", priority=3, icon="mdi:solar-power",
    behavior=BEHAVIOR_SOLAR_ONLY,
)
EVSE_MODE_EXCESS = OperatingMode(
    key="Excess", label="Excess", priority=EXCESS_URGENCY_TIER, icon="mdi:solar-power-variant",
    behavior=BEHAVIOR_EXCESS,
)
# Hold: the limit goes to 0 (or the entity's own minimum when it cannot take 0)
# and the load claims nothing. An OCPP charger and a Variable Load have it; a
# power station shares the four modes above without it.
EVSE_MODE_HOLD = OperatingMode(
    key="Hold", label="Hold", priority=EXCESS_URGENCY_TIER, icon="mdi:pause-circle-outline",
    behavior=BEHAVIOR_EXCESS,
)
OPERATING_MODES_EVSE = [
    EVSE_MODE_STANDARD,
    EVSE_MODE_SOLAR_PRIORITY,
    EVSE_MODE_SOLAR_ONLY,
    EVSE_MODE_EXCESS,
]
OPERATING_MODES_EVSE_HOLD = [*OPERATING_MODES_EVSE, EVSE_MODE_HOLD]
DEFAULT_OPERATING_MODE_EVSE = EVSE_MODE_STANDARD
