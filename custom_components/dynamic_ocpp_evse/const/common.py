"""Shared constants - used across the hub and every device type.

This is the leaf module of the ``const`` package: it imports nothing from its
siblings, so the per-device modules (evse / plug / hot_water_tank) can safely
import the shared ``OPERATING_MODE_*`` keys, ``BEHAVIOR_*`` constants and the
``OperatingMode`` dataclass from here.
"""

import math
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

# Device type (load-level) - EVSE (OCPP), smart plug, hot water tank,
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
# The OCPP charge-point identifier - EVSE-only, hence the name.
CONF_CHARGER_ID = "charger_id"
CONF_LOAD_PRIORITY = "load_priority"
CONF_PRIORITY_ORDER = "priority_order"  # Transient form key: ordered list of device entry_ids (hub options)
CONF_CONNECTED_TO_PHASE = "connected_to_phase"  # Which phase(s) the device is wired to
CONF_UPDATE_FREQUENCY = "update_frequency"

# sensor attributes
CONF_PHASES = "phases"
CONF_CHARGING_MODE = "charging_mode"  # Legacy key - kept for hub_data result dict backward compat
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

# Current ramp rates (A per second) - limits how fast the commanded current changes
RAMP_UP_RATE = 0.1       # Max 0.1 A/s ramp up
RAMP_DOWN_RATE = 0.2     # Max 0.2 A/s ramp down
# Fraction of the REMAINING error a modulating load may close each second, on
# top of the fixed floors above. The floors alone are a constant slew, and a
# constant slew cannot track a moving surplus: measured on the rig
# (2026-09-08), a permit chasing a 0 -> 2 400 W swing needed 104 s at 0.1 A/s
# while the surplus turned over in 75 s, so the limiter was saturated the whole
# time and the load absorbed 719 W less than was available, on average.
#
# Closing a FRACTION instead is fast when far from target and gentle near it -
# the adaptive behaviour a constant rate cannot give - and because the step
# always shrinks as the error shrinks it cannot overshoot. Cascaded with the
# EMA above it stays overdamped, so this buys tracking without reintroducing
# the ring the floors were there to damp.
# 0.15/s measured best on the rig's moving-surplus test: mean tracking error
# fell 719 -> 596 W against the old constant slew, with register writes
# unchanged (3.2 -> 3.6 per minute). Raising it to 0.4/s made the error WORSE
# (668 W), which is the useful result - past ~0.15/s this stage stops being the
# bottleneck and the measurement chain upstream (the CT lag plus the engine's
# own grid EMA) dominates. Tuning this number further buys nothing; closing the
# remaining gap would need feed-forward from the solar reading rather than a
# faster follower.
#
# Expressed as a TIME CONSTANT rather than that per-second fraction, for the
# reason the two EMAs were converted before it: a fraction multiplied by the
# refresh interval is a per-CYCLE quantity wearing per-second clothes, and it
# made the refresh rate a control-loop tuning knob. It hid behind the 0.9 cap -
# past a ~6 s interval every site closed 90% of its error per cycle, so a 10 s
# site ramped with tau ~4.3 s where a 1 s site used tau ~6.2 s, and the 10 s
# site tracked BETTER for a reason nothing in the UI explained (rig, 2026-09-08:
# 717 W against 832 W).
#
# 5.6 s is the same number as EMA_TAU_S, and deliberately a SEPARATE constant:
# it is derived independently - the old 0.15/s over the 2 s default closes 0.30
# of the error, and tau = -2 / ln(1 - 0.30) = 5.6 s, which also keeps
# default-configured sites bit-identical - and the ramp and the input filter
# are different design decisions that should be retunable apart. The measured
# optimum was tau ~6.2 s (0.15/s at 1 s), well inside the rig's resolution.
RAMP_TAU_S = 5.6
# The PERMIT filter's own time constant, shorter than the input filter's on
# purpose. `apply_smoothing`'s EMA is the second exponential lag on the same
# signal: its input is the engine's answer, which was already computed from
# readings smoothed at EMA_TAU_S. A second 5.6 s of it buys no further noise
# rejection - there is little noise left to reject - and costs another 5.6 s of
# phase, which the rate limiter then inherits.
#
# Measured on the rig (2026-09-08), and this is the failure it caused rather
# than a preference: the ramp's proportional term never engaged at all. It
# closes `|delta| * approach` where delta is the distance to the SMOOTHED
# target, and the matched filter held that within 140 W of where the ramp
# already stood, so `max(floor, proportional)` chose the floor every cycle. The
# permit rose in near-constant 115 W steps (0.1 A/s, exactly RAMP_UP_RATE)
# while the real error was 600 W, and the adaptive rate that was added to fix
# constant-slew tracking was doing nothing.
#
# The same lag holds the permit UP after a surplus falls: export dipped to
# 9 147 W against an 11 000 W limit while the station drew 2 392 W against a
# 1 527 W ideal.
#
# 7 s, and it went UP rather than down, which was the opposite of the first
# guess. 2 s was tried first, on the reasoning that a second helping of the
# input filter's smoothing buys no noise rejection. It tracked better on every
# average and rang 600 W peak-to-peak indefinitely on a dead-flat input (rig,
# 2026-09-08) - the averages preferred it precisely BECAUSE it rang, since
# mean-|error| rewards a ring centred on the right answer over an honest lag.
#
# The real fault was in the rate limiter, not here: it took its proportional
# step from the distance to the SMOOTHED target, which this filter holds inside
# the fixed floor, so the adaptive step was frequently not the binding one.
# With the limiter reading the raw error instead, the loop moves faster and
# then needs MORE damping here, not less - and once damped it wastes less,
# because it is no longer hunting.
#
# Swept on dev/tests/dynamics.py across four refresh rates and two device ramp
# speeds. Against the previous pairing (5.6 s with the old limiter) 7 s is
# better on the fixed-point ring in 8 configurations of 8, on step rise time in
# 7, on tracking in 7 and on curtailed energy in 7. The one loss is a 10 s site
# with a slow device, by about 1% on two figures. At the 2 s default:
#     ring 300 W -> 0 W, rise 60 s -> 48 s, curtailed 55 W -> 33 W
#
# It is deliberately no longer equal to EMA_TAU_S. The two are cascaded on one
# signal, so they are not the same design decision: the input filter answers
# how noisy the readings are, this one answers how much phase the loop can
# afford. Their sharing a value was a coincidence of derivation, not a
# constraint - and holding them equal is what made the first attempt look like
# a choice between filtering and tracking.
PERMIT_TAU_S = 7.0
RAMP_APPROACH_MAX = 0.9     # never close more than this much of it in one cycle

# EMA smoothing - exponential moving average on engine output before rate limiting
# NOT the filter's weight any more, despite the name - EMA_TAU_S below is what
# sets the smoothing, and ``set_ema_interval`` writes the per-cadence weight
# into the EMA dict at the top of every cycle (engine/hub_calculation), so on a
# running site this value is never the live alpha. Two jobs survive:
#   * the default in ``readers._smooth`` for a dict that has not had an
#     interval set on it yet, which in production cannot happen (the call at
#     hub_calculation:844 precedes every smooth) but keeps direct callers and
#     tests honest;
#   * the DENOMINATOR of the charge controller's directional ratio,
#     ``CTRL_FAST_ALPHA / EMA_ALPHA`` - that pairing was calibrated as 0.8
#     against 0.3, and expressing it as a ratio is what carries it to any
#     cadence (see _smooth_directional).
# It is also the anchor EMA_TAU_S was derived from: 5.6 s is the tau whose
# weight at the 2 s default is exactly 0.3, which is what left existing sites
# bit-identical through that change. Do not retune it as though it were a
# filter setting.
EMA_ALPHA = 0.3

# The EMA's time constant, in SECONDS. ``EMA_ALPHA`` above is a weight per
# CALL, so on its own the filter's speed is a hidden function of how often the
# site refreshes: tau = interval / alpha. At the 2 s default that is ~6.7 s,
# but a site polled every 60 s to be kind to its inverter's Modbus silently
# gets tau ~200 s - longer than a cloud takes to pass, so every control loop
# on it is detuned by a setting that says nothing about filtering.
#
# Measured on the rig (2026-09-08) by changing NOTHING but the refresh: mean
# tracking error on a moving surplus went 596 W at 1 s to 1 164 W at 10 s.
#
# Chosen so that at DEFAULT_SITE_UPDATE_FREQUENCY (2 s) the effective weight is
# exactly EMA_ALPHA, leaving default-configured sites bit-identical:
#     tau = -2 / ln(1 - 0.3) = 5.6 s
# A first-order low-pass has a single REAL pole, so moving its time constant
# can never make that pole complex - no value of tau can introduce oscillation.
# That is what makes this safe to speed up, unlike an integral term.
EMA_TAU_S = 5.6


def ema_alpha_for(dt: float, tau: float = EMA_TAU_S) -> float:
    """The weight that gives ``tau`` seconds of smoothing at a ``dt`` s cadence.

    ``1 - exp(-dt/tau)`` is the exact discrete equivalent of a continuous
    first-order lag, so the filter's behaviour in SECONDS is the same however
    often it is sampled. Clamped to (0, 1]: a dt of 0 or less would divide by
    nothing, and a very slow cadence tends to 1 (no smoothing left to do,
    which is correct - there is nothing between the samples to smooth).

    It lives here, beside the time constant it converts, because BOTH tiers
    need it and they are not allowed to share code any other way: the readers
    filter the site's inputs and ``control/smoothing`` filters the permit that
    comes back out, but the actuation layer may import only const/helpers/units
    (AGENTS.md), never engine. A second copy of the formula would let the pair
    drift apart with nothing to notice.
    """
    # The site interval cannot be set below 1 s - config_flow/schemas.py caps
    # the field at 1..60 - so a smaller value never came from the UI. It can
    # only be a hand-edited entry, a migration, or a test. Clamp to that floor
    # rather than substituting a weight: 1 s is a real cadence with a real
    # answer (alpha 0.164 at EMA_TAU_S), where the historic EMA_ALPHA describes
    # a different filter speed altogether and would quietly apply it to a site
    # whose stored interval happened to be unreadable.
    dt = max(1.0, float(dt or 0.0))
    return min(1.0, max(1e-3, 1.0 - math.exp(-dt / float(tau))))


# The battery charge controller reads export and battery power through its OWN
# smoothers, which are DIRECTIONAL (engine/readers._smooth_directional): a move
# toward a limit - deeper export, heavier import, or the mirror for battery
# power - takes this weight; a move back toward zero keeps EMA_ALPHA. Two
# readings to converge, not one: 1.0 passed every lensing spike straight into
# the register and fed the register↔Excess-allowance loop (21 verdict flips on
# the lensing+EVSE rig against 1), and a single reading is also how a motor's
# start-up inrush looks. 0.6 gave back a third of the curtailment win. 0.8 kept
# the verdict at 1 flip and halved curtailment - dev/tests/test_charge_control_loop.py.
CTRL_FAST_ALPHA = 0.8
DEAD_BAND = 0.3          # Ignore changes smaller than this (Schmitt trigger, amps)
GRID_STALE_TIMEOUT = 60  # Seconds of grid CT unavailability before falling to min_current
INPUT_STALE_TIMEOUT = 60  # Seconds of solar/battery/inverter sensor unavailability before falling back to a safe value
SUSPENDED_EV_IDLE_TIMEOUT = 60  # Seconds of SuspendedEV + near-zero draw before treating as inactive

# Household hold - per-phase household is derived from the inverter output minus
# the managed draws. The draw side (OCPP, sub-second) rises the moment a car
# ramps, while the inverter output side lags 10-30 s (Modbus polling + input
# EMA), so the subtraction transiently clamps household to 0 and the engine
# would hand the real household's power out as phantom headroom. An asymmetric
# wall-clock hold bridges that window: household rises instantly, but can only
# fall to HOUSEHOLD_HOLD_RESIDUAL of the held value over
# HOUSEHOLD_HOLD_BRIDGE_SECONDS. Per-cycle factor:
#     decay = HOUSEHOLD_HOLD_RESIDUAL ** (cycle_seconds / HOUSEHOLD_HOLD_BRIDGE_SECONDS)
# (The reverse direction - an overstated household - is the safe direction and
# needs no hold, hence the asymmetry.)
HOUSEHOLD_HOLD_BRIDGE_SECONDS = 15.0  # Wall-clock length of the bridge window
HOUSEHOLD_HOLD_RESIDUAL = 0.1         # Fraction of the held value left after the window

# EVSE draw-settle detection - the measured draw is trusted as the EVSE's real
# footprint (freeing the unused gap to lower-priority loads) only once it has
# held steady for SETTLE_DRAW_SECONDS within SETTLE_DRAW_TOLERANCE.
# A car still ramping toward its permit keeps changing and stays "unsettled".
SETTLE_DRAW_TOLERANCE = 0.5   # Amps - draw change below this counts as steady
# How long the draw must hold steady, in SECONDS. It was 3 consecutive CYCLES,
# which is the same defect the filters had: a cycle count is a duration only
# once you know the refresh rate, so it meant 3 s on a 1 s site, 6 s at the 2 s
# default and 30 s at 10 s - and nothing said so. 15 s is a duration a car's
# ramp can be reasoned about against, whatever the site polls at.
SETTLE_DRAW_SECONDS = 15.0
# An EVSE only counts as settled-and-capped when its draw is also measurably
# below the permit we offered it last cycle - that is the under-drawing case
# the footprint model is meant to free. A car drawing essentially what we
# offered (util ≈ 1.0) is using all of it, so the permit, not the draw, is
# the correct pool footprint.
SETTLE_PERMIT_MARGIN = 1.0    # Amps - draw must be this far below last permit

# Auto-reset detection - triggers reset_ocpp_evse when charger ignores profiles
AUTO_RESET_MISMATCH_THRESHOLD = 5    # consecutive mismatched cycles before reset
AUTO_RESET_COOLDOWN_SECONDS = 120    # seconds to wait after reset before checking again
ESCALATION_PROFILE_RESET_LIMIT = 3   # profile resets before escalating to hard reset
HARD_RESET_COOLDOWN_SECONDS = 300    # seconds to wait after hard reset (5 minutes)

# Operating mode configuration (per-load). The shared pieces are only the
# OperatingMode dataclass and the BEHAVIOR_* engine behaviors below. Each
# device type defines its own operating modes independently - see
# const/evse.py, const/plug.py, const/hot_water_tank.py.
CONF_OPERATING_MODE = "operating_mode"

# Transient marker set in a plug load entry's data by async_migrate_entry
# (2.2 → 2.3): the operating-mode select migrates its restored "Solar Only"
# state to "Solar Priority" once, then clears the marker.
MIGRATE_PLUG_SOLAR_ONLY_FLAG = "_migrate_plug_solar_only"

# Set in a hub entry's data once its legacy hub-level inverter/battery fields
# have been imported into a standalone inverter entry (or for new hubs, which
# never had them) - makes the one-time auto-import idempotent across restarts.
MIGRATE_HUB_INVERTER_IMPORTED_FLAG = "_hub_inverter_imported"

# Engine behaviors - how a load competes for power. The distribution engine
# switches on the behavior, never on the device type or the mode label. Which
# behavior each operating mode uses is mapped centrally in const/modes.py
# (BEHAVIOR_BY_MODE) - the const device modules stay free of engine concepts.
# Modulating behaviors (EVSE - varies the current).
BEHAVIOR_FULL_POWER = "full_power"          # draw at max from any source
BEHAVIOR_SOLAR_PRIORITY = "solar_priority"  # follow solar, grid-backed minimum
BEHAVIOR_SOLAR_ONLY = "solar_only"          # solar surplus only, no grid
BEHAVIOR_EXCESS = "excess"                  # only run on excess export
# Binary behaviors (smart plug - on/off, never grid; with a battery the SOC
# band gates it, without a battery it falls back to live solar surplus).
BEHAVIOR_BINARY_ABOVE_MIN = "binary_above_min"        # run while battery > minimum SOC

# Minimum time a binary load (smart plug / relay) stays OFF once the engine has
# shed it, before any permit may switch it back on. Minutes; 0 disables it.
#
# It only ever delays switching ON, never a shed, so it cannot hold a load on
# below a protective floor - which is what makes it safe to apply to every
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
    """One device-type operating mode - the user-facing definition.

    key       stored string value (select entity state + runtime dict)
    label     user-facing display name
    priority  distribution urgency tier, 1-4 (lower = served first)
    icon      mdi icon for the select entity

    The engine behavior a mode competes with is mapped separately in
    const/modes.py, keyed by the mode object - so each module-level instance
    is a distinct mode. ``eq=False`` keeps identity equality/hashing: two
    device types whose modes coincide on every display field (e.g. EVSE and
    plug "Excess") are still distinct modes, never a collapsed dict key.
    """

    key: str
    label: str
    priority: int
    icon: str
