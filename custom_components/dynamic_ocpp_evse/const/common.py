"""Shared constants — used across the hub and every device type.

This is the leaf module of the ``const`` package: it imports nothing from its
siblings, so the per-device modules (evse / plug / hot_water_tank) can safely
import the shared ``OPERATING_MODE_*`` strings from here.
"""

DOMAIN = "dynamic_ocpp_evse"

# Entry types for hub/charger architecture
ENTRY_TYPE = "entry_type"
ENTRY_TYPE_HUB = "hub"
ENTRY_TYPE_CHARGER = "charger"
ENTRY_TYPE_GROUP = "group"

# configuration keys - common
CONF_NAME = "name"
CONF_ENTITY_ID = "entity_id"

# Device type (charger-level) — EVSE (OCPP), smart load, hot water tank, group
CONF_DEVICE_TYPE = "device_type"
DEVICE_TYPE_EVSE = "evse"
DEVICE_TYPE_PLUG = "plug"
DEVICE_TYPE_HOT_WATER_TANK = "hot_water_tank"
DEVICE_TYPE_GROUP = "group"

# Charger-specific configuration keys shared by every device type
CONF_HUB_ENTRY_ID = "hub_entry_id"
CONF_CHARGER_ID = "charger_id"
CONF_CHARGER_PRIORITY = "charger_priority"
CONF_CONNECTED_TO_PHASE = "connected_to_phase"  # Which phase(s) the device is wired to
CONF_UPDATE_FREQUENCY = "update_frequency"

# sensor attributes
CONF_PHASES = "phases"
CONF_CHARGING_MODE = "charging_mode"  # Legacy key — kept for hub_data result dict backward compat
CONF_TOTAL_ALLOCATED_CURRENT = "total_allocated_current"
CONF_PHASE_A_CURRENT = "phase_a_current"
CONF_PHASE_B_CURRENT = "phase_b_current"
CONF_PHASE_C_CURRENT = "phase_c_current"
CONF_EVSE_CURRENT_IMPORT = "evse_current_import"
CONF_EVSE_CURRENT_OFFERED = "evse_current_offered"
CONF_MAX_IMPORT_POWER = "max_import_power"
CONF_MIN_CURRENT = "min_current"
CONF_MAX_CURRENT = "max_current"

# Shared default values
DEFAULT_PHASE_VOLTAGE = 230
DEFAULT_UPDATE_FREQUENCY = 15
DEFAULT_CHARGER_PRIORITY = 1

# Current ramp rates (A per second) — limits how fast the commanded current changes
RAMP_UP_RATE = 0.1       # Max 0.1 A/s ramp up
RAMP_DOWN_RATE = 0.2     # Max 0.2 A/s ramp down

# EMA smoothing — exponential moving average on engine output before rate limiting
EMA_ALPHA = 0.3          # Weight of new reading (0.3 = smooth, 1.0 = no smoothing)
DEAD_BAND = 0.3          # Ignore changes smaller than this (Schmitt trigger, amps)
GRID_STALE_TIMEOUT = 60  # Seconds of grid CT unavailability before falling to min_current
SUSPENDED_EV_IDLE_TIMEOUT = 60  # Seconds of SuspendedEV + near-zero draw before treating as inactive

# Auto-reset detection — triggers reset_ocpp_evse when charger ignores profiles
AUTO_RESET_MISMATCH_THRESHOLD = 5    # consecutive mismatched cycles before reset
AUTO_RESET_COOLDOWN_SECONDS = 120    # seconds to wait after reset before checking again
ESCALATION_PROFILE_RESET_LIMIT = 3   # profile resets before escalating to hard reset
HARD_RESET_COOLDOWN_SECONDS = 300    # seconds to wait after hard reset (5 minutes)

# Operating mode configuration (per-load) — shared mode strings + urgency.
# The per-device-type mode lists and defaults live in each device's module.
CONF_OPERATING_MODE = "operating_mode"
OPERATING_MODE_STANDARD = "Standard"        # EVSE: charge from any source at max
OPERATING_MODE_CONTINUOUS = "Continuous"     # Plug: always on
OPERATING_MODE_SOLAR_PRIORITY = "Solar Priority"
OPERATING_MODE_SOLAR_ONLY = "Solar Only"
OPERATING_MODE_EXCESS = "Excess"
OPERATING_MODE_NORMAL = "Normal"                        # Hot water tank: baseline target
OPERATING_MODE_FREEZE_PROTECTION = "Freeze Protection"  # Hot water tank: away target only

# Mode urgency for distribution sorting (lower = higher urgency)
# Standard and Continuous share urgency 0 — same engine behavior, different labels
MODE_URGENCY = {
    OPERATING_MODE_STANDARD: 0,
    OPERATING_MODE_CONTINUOUS: 0,
    OPERATING_MODE_SOLAR_PRIORITY: 1,
    OPERATING_MODE_SOLAR_ONLY: 2,
    OPERATING_MODE_EXCESS: 3,
}
