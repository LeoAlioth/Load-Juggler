"""Shared constants — used across the hub and every device type.

This is the leaf module of the ``const`` package: it imports nothing from its
siblings, so the per-device modules (evse / plug / hot_water_tank) can safely
import the shared ``OPERATING_MODE_*`` keys, ``BEHAVIOR_*`` constants and the
``OperatingMode`` dataclass from here.
"""

from dataclasses import dataclass

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

# Operating mode configuration (per-load). The shared pieces are only the
# OperatingMode dataclass and the BEHAVIOR_* engine behaviors below. Each
# device type defines its own operating modes independently — see
# const/evse.py, const/plug.py, const/hot_water_tank.py.
CONF_OPERATING_MODE = "operating_mode"

# Transient marker set in a plug charger entry's data by async_migrate_entry
# (2.2 → 2.3): the operating-mode select migrates its restored "Solar Only"
# state to "Solar Priority" once, then clears the marker.
MIGRATE_PLUG_SOLAR_ONLY_FLAG = "_migrate_plug_solar_only"

# Engine behaviors — how a load competes for power. The distribution engine
# switches on the behavior, never on the device type or the mode label. Which
# behavior each operating mode uses is mapped centrally in const/modes.py
# (BEHAVIOR_BY_MODE) — the const device modules stay free of engine concepts.
# Modulating behaviors (EVSE — varies the current).
BEHAVIOR_FULL_POWER = "full_power"          # draw at max from any source
BEHAVIOR_SOLAR_PRIORITY = "solar_priority"  # follow solar, grid-backed minimum
BEHAVIOR_SOLAR_ONLY = "solar_only"          # solar surplus only, no grid
BEHAVIOR_EXCESS = "excess"                  # only run on excess export
# Binary behaviors (smart plug — on/off, never grid; with a battery the SOC
# band gates it, without a battery it falls back to live solar surplus).
BEHAVIOR_BINARY_ABOVE_MIN = "binary_above_min"        # run while battery > minimum SOC
BEHAVIOR_BINARY_ABOVE_TARGET = "binary_above_target"  # run while battery > target SOC
BEHAVIOR_BINARY_EXCESS = "binary_excess"              # run while battery near-full or exporting


@dataclass(frozen=True, eq=False)
class OperatingMode:
    """One device-type operating mode — the user-facing definition.

    key       stored string value (select entity state + runtime dict)
    label     user-facing display name
    priority  distribution urgency tier, 1-4 (lower = served first)
    icon      mdi icon for the select entity

    The engine behavior a mode competes with is mapped separately in
    const/modes.py, keyed by the mode object — so each module-level instance
    is a distinct mode. ``eq=False`` keeps identity equality/hashing: two
    device types whose modes coincide on every display field (e.g. EVSE and
    plug "Excess") are still distinct modes, never a collapsed dict key.
    """

    key: str
    label: str
    priority: int
    icon: str
