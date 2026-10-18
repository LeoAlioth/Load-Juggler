"""Hub / site-level constants - grid CTs, inverter, battery, distribution."""

# Hub-specific configuration keys
CONF_PHASE_A_CURRENT_ENTITY_ID = "phase_a_current_entity_id"
CONF_PHASE_B_CURRENT_ENTITY_ID = "phase_b_current_entity_id"
CONF_PHASE_C_CURRENT_ENTITY_ID = "phase_c_current_entity_id"
CONF_MAIN_BREAKER_RATING = "main_breaker_rating"
CONF_INVERT_PHASES = "invert_phases"
CONF_MAX_IMPORT_POWER_ENTITY_ID = "max_import_power_entity_id"
CONF_ENABLE_MAX_IMPORT_POWER = "enable_max_import_power"  # Checkbox: create slider for max import power
CONF_PHASE_VOLTAGE = "phase_voltage"
CONF_EXCESS_EXPORT_THRESHOLD = "excess_export_threshold"  # LEGACY (pre-2.4) - read
# ONLY by the migration: the <4 step derives CONF_GRID_EXPORT_LIMIT from it and
# the 2.8 step then prunes it from the entry. Replaced by
# CONF_GRID_EXPORT_LIMIT − CONF_EXCESS_TRIGGER_MARGIN; read only by the migration.
CONF_SOLAR_PRODUCTION_ENTITY_ID = "solar_production_entity_id"  # Optional direct solar production sensor (W)

# Inverter configuration (hub-level)
CONF_INVERTER_MAX_POWER = "inverter_max_power"  # Total inverter capacity (W)
CONF_INVERTER_MAX_POWER_PER_PHASE = "inverter_max_power_per_phase"  # Per-phase inverter limit (W)
# Optional sensor (W/kW) read each cycle in place of the fixed total capacity - an automation
# lowers it toward 0 on an inverter over-temperature alarm.
CONF_INVERTER_MAX_POWER_ENTITY_ID = "inverter_max_power_entity_id"
CONF_INVERTER_SUPPORTS_ASYMMETRIC = "inverter_supports_asymmetric"  # Can balance power across phases
CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID = "inverter_output_phase_a_entity_id"  # Per-phase inverter output sensor
CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID = "inverter_output_phase_b_entity_id"
CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID = "inverter_output_phase_c_entity_id"
# What the inverter takes in at its AC input (grid side), per phase, positive
# while it draws from the grid - optional, a PV string inverter has none. The
# tick says the grid meter's own reading IS this input (a hybrid the grid flows
# through, nothing in between); at most one inverter per hub has it.
CONF_INVERTER_INPUT_PHASE_A_ENTITY_ID = "inverter_input_phase_a_entity_id"
CONF_INVERTER_INPUT_PHASE_B_ENTITY_ID = "inverter_input_phase_b_entity_id"
CONF_INVERTER_INPUT_PHASE_C_ENTITY_ID = "inverter_input_phase_c_entity_id"
CONF_INVERTER_INPUT_IS_GRID = "inverter_input_is_grid"

# Battery support configuration constants (hub-level)
CONF_BATTERY_POWER_ENTITY_ID = "battery_power_entity_id"
CONF_BATTERY_SOC_ENTITY_ID = "battery_soc_entity_id"
CONF_BATTERY_SOC_FULL = "battery_soc_full"  # SOC at/above which the battery counts as "full" (plug Excess mode)
CONF_BATTERY_SOC_HYSTERESIS = "battery_soc_hysteresis"  # Hysteresis percentage for SOC thresholds
# Off-grid, below the minimum SOC the battery still carries the tanks and heaters (at their away setpoint) down to this SOC
CONF_BATTERY_SOC_FREEZE_FLOOR = "battery_soc_freeze_floor"
CONF_BATTERY_MAX_CHARGE_POWER = "battery_max_charge_power"  # W
CONF_BATTERY_MAX_DISCHARGE_POWER = "battery_max_discharge_power"  # W

# Site-level timing / detection
CONF_SITE_UPDATE_FREQUENCY = "site_update_frequency"  # Hub-level: how often site sensors refresh
CONF_AUTO_DETECT_PHASE_MAPPING = "auto_detect_phase_mapping"  # Hub-level: detect L1/L2/L3 wiring mismatches
CONF_SOLAR_GRACE_PERIOD = "solar_grace_period"  # Hub-level: minutes before pausing in Solar/Excess mode

# The Filters page (options only - never the setup wizard). Each dial's
# DEFAULT is the engine constant it overrides, in const/common (EMA_TAU_S,
# PERMIT_TAU_S, ...), and every consumer reads it as
#     get_entry_value(hub_entry, CONF_FILTER_*, <that constant>)
# so there is exactly ONE number per filter and an entry that has never opened
# the page behaves byte-identically to one from before the page existed. No
# DEFAULT_FILTER_* twins, deliberately: a second copy of a tuned constant is a
# second thing to keep in step. Keys are public config API once shipped.
CONF_FILTER_INPUT_TAU_S = "filter_input_tau_s"          # EMA_TAU_S
CONF_FILTER_PERMIT_TAU_S = "filter_permit_tau_s"        # PERMIT_TAU_S
CONF_FILTER_RAMP_TAU_S = "filter_ramp_tau_s"            # RAMP_TAU_S
CONF_FILTER_CTRL_FAST_TAU_S = "filter_ctrl_fast_tau_s"  # CTRL_FAST_TAU_S
CONF_FILTER_SETTLE_SECONDS = "filter_settle_seconds"    # SETTLE_DRAW_SECONDS
CONF_FILTER_DEAD_BAND = "filter_dead_band"              # DEAD_BAND (A)
CONF_FILTER_RAMP_UP_RATE = "filter_ramp_up_rate"        # RAMP_UP_RATE (A/s)
CONF_FILTER_RAMP_DOWN_RATE = "filter_ramp_down_rate"    # RAMP_DOWN_RATE (A/s)

# Hub default values
DEFAULT_MAIN_BREAKER_RATING = 25
DEFAULT_SITE_UPDATE_FREQUENCY = 2  # Fast site info refresh (seconds)
DEFAULT_SOLAR_GRACE_PERIOD = 5  # minutes
DEFAULT_EXCESS_EXPORT_THRESHOLD = 13000  # LEGACY - migration fallback only
DEFAULT_EXCESS_HYSTERESIS = 500  # W - see CONF_EXCESS_HYSTERESIS
DEFAULT_BATTERY_MAX_POWER = 5000
DEFAULT_BATTERY_SOC_MIN = 20  # Default minimum SOC (20%)
DEFAULT_BATTERY_SOC_TARGET = 80  # Default SOC target (80%)
DEFAULT_BATTERY_SOC_FULL = 97  # Default "full" SOC - plug Excess mode trigger (%)
DEFAULT_BATTERY_SOC_HYSTERESIS = 3  # Default hysteresis (3%)
DEFAULT_BATTERY_SOC_FREEZE_FLOOR = 5  # %

# PV clipping forecast (hub-level). Sites with more PV than they may export
# (e.g. 15 kWp behind a 5 kW export limit) should keep battery headroom for
# the forecast midday peak instead of filling up on morning production that
# could have been exported. The hub publishes advisory sensors only - a
# future battery-inverter device type will optionally write them to a device.
CONF_GRID_EXPORT_LIMIT = "grid_export_limit"  # W - the site's physical/contract
# export ceiling, and the ONE export number the user enters. Everything else
# derives from it: the Excess trigger engages at (limit − trigger margin), and
# the clipping forecast integrates production above (limit + base consumption).
# 0 = no export limit: the grid absorbs everything, so grid-side Excess never
# triggers (allowance is infinite) and the forecast is off.
CONF_EXCESS_TRIGGER_MARGIN = "excess_trigger_margin"  # W - how far below the
# export limit the Excess trigger sits. An inverter curtails slightly under
# the limit, so a trigger exactly AT the limit would never fire.
CONF_EXCESS_HYSTERESIS = "excess_hysteresis"  # W - release band once Excess is
# engaged: an engaged load stays on until the surplus the site cannot place
# falls this far below the trigger, so a load doesn't chatter at the trigger
# point. Distinct from the trigger margin, which sets WHERE Excess engages.
CONF_SOLAR_FORECAST_DEVICE_IDS = "solar_forecast_device_ids"  # list - one forecast
# DEVICE per PV array (the Open-Meteo Solar Forecast integration creates one
# config entry/device per array; several of its sensors carry the same `watts`
# series, so selecting sensors risks double-counting one array - the reader
# resolves exactly one watts-bearing sensor per device).
CONF_SOLAR_FORECAST_ENTITY_IDS = "solar_forecast_entity_ids"  # LEGACY - direct
# sensor list from the first dev iteration; read nowhere, only stripped from a
# hub by the one-time import.
CONF_BASE_CONSUMPTION = "base_consumption"  # W - typical daytime minimum house draw
# Optional sensor (W/kW or input_number) read each cycle in place of the typed figure above - Load
# Insights publishes the daytime minimum of its whole-house forecast.
CONF_BASE_CONSUMPTION_ENTITY_ID = "base_consumption_entity_id"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"  # kWh the SOC percentage spans; 0 = off
CONF_BATTERY_CAPACITY_ENTITY_ID = "battery_capacity_entity_id"  # Wh/kWh sensor; overrides the number while it reads
CONF_FORECAST_SOC_FLOOR = "forecast_soc_floor"  # % - never recommend a ceiling below this
# Pacing (per-load option CONF_PACE_OVER_HORIZON): the excess energy
# (kWh sensor) and sunset (timestamp sensor: when that energy has to be used by). With
# either unset, or unreadable, no load is paced.
CONF_PACING_ENERGY_ENTITY_ID = "pacing_energy_entity_id"
CONF_PACING_HORIZON_ENTITY_ID = "pacing_horizon_entity_id"
# Early-start (owner, 2026-10-18): a timestamp sensor for when the battery is next
# full by the forecast. Each load starts early only once the time left to it lets
# the load's minimum power use the pacing energy (calculations/forecast_excess.py,
# ``starts_early``). Unset: the pacing horizon above stands in.
CONF_FORECAST_EXCESS_FULL_ENTITY_ID = "forecast_excess_full_entity_id"
# Forecast-driven Excess (owner, 2026-10-17): the Excess verdict may start before
# the battery is full while the pacing energy says the day will overfill it -
# see calculations/forecast_excess.py. Off by default.
CONF_FORECAST_EXCESS_ENABLED = "forecast_excess_enabled"
CONF_FORECAST_EXCESS_SOC_SOURCE = "forecast_excess_soc_source"
CONF_FORECAST_EXCESS_SOC = "forecast_excess_soc"  # %, used with the fixed source
FORECAST_EXCESS_SOURCE_MIN = "minimum"
FORECAST_EXCESS_SOURCE_TARGET = "target"
FORECAST_EXCESS_SOURCE_FIXED = "fixed"
FORECAST_EXCESS_SOC_SOURCES = [
    FORECAST_EXCESS_SOURCE_MIN,
    FORECAST_EXCESS_SOURCE_TARGET,
    FORECAST_EXCESS_SOURCE_FIXED,
]
DEFAULT_FORECAST_EXCESS_ENABLED = False
DEFAULT_FORECAST_EXCESS_SOC_SOURCE = FORECAST_EXCESS_SOURCE_TARGET
DEFAULT_FORECAST_EXCESS_SOC = 80
DEFAULT_GRID_EXPORT_LIMIT = 0
DEFAULT_EXCESS_TRIGGER_MARGIN = 500
DEFAULT_BASE_CONSUMPTION = 300
DEFAULT_BATTERY_CAPACITY_KWH = 0
DEFAULT_FORECAST_SOC_FLOOR = 30
# How long a forecast source that stops delivering (unavailable, or no usable
# data) keeps contributing the series it last read - an internet outage takes
# the forecast integration down with it, and the last forecast it fetched still
# describes the day (2026-10-07: "the last available forecast for up to
# 24 hours"). Past it the source counts as missing, as before.
FORECAST_HOLD_S = 24 * 3600
FORECAST_SOC_HYSTERESIS = 2  # % - serves every forecast latch, so one
# setting sizes the whole feature's stickiness:
#  1. the published ceiling rises freely but falls only by more than this band,
#     so forecast refreshes don't chatter the advice;
#  2. the charge cap's SOC gate is a band this wide - it engages this far below
#     the ceiling and releases only twice this far below it, so an integer SOC
#     tick at either threshold cannot flap the cap (and its register writes);
#  3. the yield-to-Excess latch at the battery's DESTINATION is the same shape:
#     it engages exactly at the destination (below it the battery is served
#     first, never a percent early) and releases only this far below, because
#     the crossing moves the advice by whole kilowatts and an integer SOC
#     register would otherwise sit on the boundary and flip it.

# Distribution mode configuration (hub-level)
CONF_DISTRIBUTION_MODE = "distribution_mode"
DISTRIBUTION_MODE_SHARED = "Shared"
DISTRIBUTION_MODE_PRIORITY = "Priority"
DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED = "Sequential - Optimized"
DISTRIBUTION_MODE_SEQUENTIAL_STRICT = "Sequential - Strict"
DISTRIBUTION_MODES = [
    DISTRIBUTION_MODE_SHARED,
    DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
    DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
]
DEFAULT_DISTRIBUTION_MODE = DISTRIBUTION_MODE_PRIORITY
# Optimized trims a load for the next one's minimum only with current left over
# beyond the load's own maximum. To START the next load that leftover must be
# at least this (A); a next load already running keeps its minimum down to any
# leftover above 0. Without the band, a supply hovering at the first load's
# maximum flipped two 6-16 A chargers between 16/0 and 11/6 A.
OPTIMIZED_START_MARGIN = 1.0
