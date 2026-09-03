"""Shared constants — used across the hub and every device type.

This is the leaf module of the ``const`` package: it imports nothing from its
siblings, so the per-device modules (evse / plug / hot_water_tank) can safely
import the shared ``OPERATING_MODE_*`` keys, ``BEHAVIOR_*`` constants and the
``OperatingMode`` dataclass from here.
"""

from dataclasses import dataclass

DOMAIN = "dynamic_ocpp_evse"

# Entry types for the hub/load architecture
ENTRY_TYPE = "entry_type"
ENTRY_TYPE_HUB = "hub"
ENTRY_TYPE_LOAD = "load"
ENTRY_TYPE_GROUP = "group"
# An inverter (a power SOURCE, optionally carrying its own battery), linked to
# a hub via CONF_HUB_ENTRY_ID like loads and groups. Inverter entries reuse
# the hub-level CONF_INVERTER_* / CONF_BATTERY_* key names (const/hub.py) in
# their own options: schemas are shared with the legacy hub pages, and the
# one-time auto-import of a hub's legacy fields is a verbatim key copy.
ENTRY_TYPE_INVERTER = "inverter"

# configuration keys - common
CONF_NAME = "name"
CONF_ENTITY_ID = "entity_id"

# Device type (load-level) — EVSE (OCPP), smart plug, hot water tank,
# portable power station, group
CONF_DEVICE_TYPE = "device_type"
DEVICE_TYPE_EVSE = "evse"
DEVICE_TYPE_PLUG = "plug"
DEVICE_TYPE_HOT_WATER_TANK = "hot_water_tank"
DEVICE_TYPE_POWER_STATION = "power_station"
DEVICE_TYPE_GROUP = "group"
DEVICE_TYPE_INVERTER = "inverter"

# Load-specific configuration keys shared by every device type
CONF_HUB_ENTRY_ID = "hub_entry_id"
# The OCPP charge-point identifier — EVSE-only, hence the name.
CONF_CHARGER_ID = "charger_id"
CONF_LOAD_PRIORITY = "load_priority"
CONF_PRIORITY_ORDER = "priority_order"  # Transient form key: ordered list of device entry_ids (hub options)
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
DEFAULT_LOAD_PRIORITY = 1

# Current ramp rates (A per second) — limits how fast the commanded current changes
RAMP_UP_RATE = 0.1       # Max 0.1 A/s ramp up
RAMP_DOWN_RATE = 0.2     # Max 0.2 A/s ramp down

# EMA smoothing — exponential moving average on engine output before rate limiting
EMA_ALPHA = 0.3          # Weight of new reading (0.3 = smooth, 1.0 = no smoothing)
# The battery charge controller reads export and battery power through its OWN
# smoothers, which are DIRECTIONAL (engine/readers._smooth_directional): a move
# toward a limit — deeper export, heavier import, or the mirror for battery
# power — takes this weight; a move back toward zero keeps EMA_ALPHA. Two
# readings to converge, not one: 1.0 passed every lensing spike straight into
# the register and fed the register↔Excess-allowance loop (21 verdict flips on
# the lensing+EVSE rig against 1), and a single reading is also how a motor's
# start-up inrush looks. 0.6 gave back a third of the curtailment win. 0.8 kept
# the verdict at 1 flip and halved curtailment — dev/tests/test_charge_control_loop.py.
CTRL_FAST_ALPHA = 0.8
DEAD_BAND = 0.3          # Ignore changes smaller than this (Schmitt trigger, amps)
GRID_STALE_TIMEOUT = 60  # Seconds of grid CT unavailability before falling to min_current
INPUT_STALE_TIMEOUT = 60  # Seconds of solar/battery/inverter sensor unavailability before falling back to a safe value
SUSPENDED_EV_IDLE_TIMEOUT = 60  # Seconds of SuspendedEV + near-zero draw before treating as inactive

# Household hold — per-phase household is derived from the inverter output minus
# the managed draws. The draw side (OCPP, sub-second) rises the moment a car
# ramps, while the inverter output side lags 10-30 s (Modbus polling + input
# EMA), so the subtraction transiently clamps household to 0 and the engine
# would hand the real household's power out as phantom headroom. An asymmetric
# wall-clock hold bridges that window: household rises instantly, but can only
# fall to HOUSEHOLD_HOLD_RESIDUAL of the held value over
# HOUSEHOLD_HOLD_BRIDGE_SECONDS. Per-cycle factor:
#     decay = HOUSEHOLD_HOLD_RESIDUAL ** (cycle_seconds / HOUSEHOLD_HOLD_BRIDGE_SECONDS)
# (The reverse direction — an overstated household — is the safe direction and
# needs no hold, hence the asymmetry.)
HOUSEHOLD_HOLD_BRIDGE_SECONDS = 15.0  # Wall-clock length of the bridge window
HOUSEHOLD_HOLD_RESIDUAL = 0.1         # Fraction of the held value left after the window

# EVSE draw-settle detection — the measured draw is trusted as the EVSE's real
# footprint (freeing the unused gap to lower-priority loads) only once it has
# held steady for SETTLE_DRAW_CYCLES consecutive cycles within SETTLE_DRAW_TOLERANCE.
# A car still ramping toward its permit keeps changing and stays "unsettled".
SETTLE_DRAW_TOLERANCE = 0.5   # Amps — draw change below this counts as steady
SETTLE_DRAW_CYCLES = 3        # Consecutive steady cycles before the draw is trusted
# An EVSE only counts as settled-and-capped when its draw is also measurably
# below the permit we offered it last cycle — that is the under-drawing case
# the footprint model is meant to free. A car drawing essentially what we
# offered (util ≈ 1.0) is using all of it, so the permit, not the draw, is
# the correct pool footprint.
SETTLE_PERMIT_MARGIN = 1.0    # Amps — draw must be this far below last permit

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

# Transient marker set in a plug load entry's data by async_migrate_entry
# (2.2 → 2.3): the operating-mode select migrates its restored "Solar Only"
# state to "Solar Priority" once, then clears the marker.
MIGRATE_PLUG_SOLAR_ONLY_FLAG = "_migrate_plug_solar_only"

# Set in a hub entry's data once its legacy hub-level inverter/battery fields
# have been imported into a standalone inverter entry (or for new hubs, which
# never had them) — makes the one-time auto-import idempotent across restarts.
MIGRATE_HUB_INVERTER_IMPORTED_FLAG = "_hub_inverter_imported"

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

# Minimum time a binary load (smart plug / relay) stays OFF once the engine has
# shed it, before any permit may switch it back on. Minutes; 0 disables it.
#
# It only ever delays switching ON, never a shed, so it cannot hold a load on
# below a protective floor — which is what makes it safe to apply to every
# cause at once. What it bounds is CYCLE FREQUENCY: the appliance behind the
# relay, not the relay, is usually the fragile part. An EV whose outlet is cut
# mid-negotiation retries, and enough retries in a row lock its onboard charger
# out until it is unplugged (observed live 2026-08-29); compressors need a
# similar rest before restarting against head pressure.
CONF_BINARY_MIN_OFF_TIME = "binary_min_off_time"
DEFAULT_BINARY_MIN_OFF_TIME = 5  # minutes

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
