DOMAIN = "dynamic_ocpp_evse"

# Entry types for hub/charger architecture
ENTRY_TYPE = "entry_type"
ENTRY_TYPE_HUB = "hub"
ENTRY_TYPE_CHARGER = "charger"
ENTRY_TYPE_GROUP = "group"

# configuration keys - common
CONF_NAME = "name"
CONF_ENTITY_ID = "entity_id"

# Hub-specific configuration keys
CONF_PHASE_A_CURRENT_ENTITY_ID = "phase_a_current_entity_id"
CONF_PHASE_B_CURRENT_ENTITY_ID = "phase_b_current_entity_id"
CONF_PHASE_C_CURRENT_ENTITY_ID = "phase_c_current_entity_id"
CONF_MAIN_BREAKER_RATING = "main_breaker_rating"
CONF_INVERT_PHASES = "invert_phases"
CONF_MAX_IMPORT_POWER_ENTITY_ID = "max_import_power_entity_id"
CONF_ENABLE_MAX_IMPORT_POWER = "enable_max_import_power"  # Checkbox: create slider for max import power
CONF_PHASE_VOLTAGE = "phase_voltage"
CONF_EXCESS_EXPORT_THRESHOLD = "excess_export_threshold"  # Maximum allowed export before charging starts in Excess mode
CONF_SOLAR_PRODUCTION_ENTITY_ID = "solar_production_entity_id"  # Optional direct solar production sensor (W)

# Inverter configuration (hub-level)
CONF_INVERTER_MAX_POWER = "inverter_max_power"  # Total inverter capacity (W)
CONF_INVERTER_MAX_POWER_PER_PHASE = "inverter_max_power_per_phase"  # Per-phase inverter limit (W)
CONF_INVERTER_SUPPORTS_ASYMMETRIC = "inverter_supports_asymmetric"  # Can balance power across phases
CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID = "inverter_output_phase_a_entity_id"  # Per-phase inverter output sensor
CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID = "inverter_output_phase_b_entity_id"
CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID = "inverter_output_phase_c_entity_id"
CONF_WIRING_TOPOLOGY = "wiring_topology"  # "parallel" or "series"
WIRING_TOPOLOGY_PARALLEL = "parallel"  # Inverter feeds in parallel (AC-coupled, no battery typical)
WIRING_TOPOLOGY_SERIES = "series"  # Everything flows through inverter (hybrid, battery typical)
DEFAULT_WIRING_TOPOLOGY = WIRING_TOPOLOGY_PARALLEL

# Battery support configuration constants (hub-level)
CONF_BATTERY_POWER_ENTITY_ID = "battery_power_entity_id"
CONF_BATTERY_SOC_ENTITY_ID = "battery_soc_entity_id"
CONF_BATTERY_SOC_TARGET_ENTITY_ID = "battery_soc_target_entity_id"
CONF_BATTERY_SOC_MIN = "battery_soc_min"  # Minimum SOC below which EV should not charge
CONF_BATTERY_SOC_HYSTERESIS = "battery_soc_hysteresis"  # Hysteresis percentage for SOC thresholds
CONF_BATTERY_MAX_CHARGE_POWER = "battery_max_charge_power"  # W
CONF_BATTERY_MAX_DISCHARGE_POWER = "battery_max_discharge_power"  # W
CONF_ALLOW_GRID_CHARGING_ENTITY_ID = "allow_grid_charging_entity_id"
CONF_POWER_BUFFER_ENTITY_ID = "power_buffer_entity_id"
CONF_POWER_BUFFER = "power_buffer"

# Charger-specific configuration keys
CONF_HUB_ENTRY_ID = "hub_entry_id"
CONF_CHARGER_ID = "charger_id"
CONF_CHARGER_PRIORITY = "charger_priority"
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
CONF_UPDATE_FREQUENCY = "update_frequency"
CONF_SITE_UPDATE_FREQUENCY = "site_update_frequency"  # Hub-level: how often site sensors refresh
CONF_AUTO_DETECT_PHASE_MAPPING = "auto_detect_phase_mapping"  # Hub-level: detect L1/L2/L3 wiring mismatches
CONF_SOLAR_GRACE_PERIOD = "solar_grace_period"  # Hub-level: minutes before pausing in Solar/Excess mode
CONF_OCPP_PROFILE_TIMEOUT = "ocpp_profile_timeout"
CONF_CHARGE_PAUSE_DURATION = "charge_pause_duration"
CONF_STACK_LEVEL = "stack_level"

# Device type (charger-level) — EVSE (OCPP) or smart load
CONF_DEVICE_TYPE = "device_type"
DEVICE_TYPE_EVSE = "evse"
DEVICE_TYPE_PLUG = "plug"
CONF_PLUG_SWITCH_ENTITY_ID = "plug_switch_entity_id"  # HA switch entity to control on/off
CONF_PLUG_POWER_RATING = "plug_power_rating"  # Fixed power draw in watts
CONF_PLUG_POWER_MONITOR_ENTITY_ID = "plug_power_monitor_entity_id"  # Optional power monitoring sensor
CONF_CONNECTED_TO_PHASE = "connected_to_phase"  # Which phase(s) the device is wired to
DEFAULT_PLUG_POWER_RATING = 2000

# Circuit group — shared breaker limit for co-located loads
DEVICE_TYPE_GROUP = "group"
CONF_CIRCUIT_GROUP_CURRENT_LIMIT = "circuit_group_current_limit"
CONF_CIRCUIT_GROUP_MEMBERS = "circuit_group_members"
DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT = 20

# OCPP charger L1/L2/L3 → site phase mapping
CONF_CHARGER_L1_PHASE = "charger_l1_phase"
CONF_CHARGER_L2_PHASE = "charger_l2_phase"
CONF_CHARGER_L3_PHASE = "charger_l3_phase"

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

# OCPP integration entity suffixes for auto-discovery
OCPP_ENTITY_SUFFIX_CURRENT_IMPORT = "_current_import"
OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L1 = "_current_import_l1"
OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L2 = "_current_import_l2"
OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L3 = "_current_import_l3"
OCPP_ENTITY_SUFFIX_CURRENT_OFFERED = "_current_offered"
OCPP_ENTITY_SUFFIX_POWER_OFFERED = "_power_offered"
OCPP_ENTITY_SUFFIX_POWER_IMPORT = "_power_active_import"
OCPP_ENTITY_SUFFIX_STATUS = "_status"
OCPP_ENTITY_SUFFIX_STOP_REASON = "_stop_reason"

# Default values
DEFAULT_MIN_CHARGE_CURRENT = 6
DEFAULT_MAX_CHARGE_CURRENT = 16
DEFAULT_PHASE_VOLTAGE = 230
DEFAULT_MAIN_BREAKER_RATING = 25
DEFAULT_UPDATE_FREQUENCY = 15
DEFAULT_SITE_UPDATE_FREQUENCY = 2  # Fast site info refresh (seconds)
DEFAULT_OCPP_PROFILE_TIMEOUT = 120
DEFAULT_CHARGE_PAUSE_DURATION = 3  # minutes
DEFAULT_SOLAR_GRACE_PERIOD = 5  # minutes
DEFAULT_STACK_LEVEL = 3
DEFAULT_CHARGER_PRIORITY = 1
DEFAULT_EXCESS_EXPORT_THRESHOLD = 13000
DEFAULT_BATTERY_MAX_POWER = 5000
DEFAULT_BATTERY_SOC_MIN = 20  # Default minimum SOC (20%)
DEFAULT_BATTERY_SOC_TARGET = 80  # Default SOC target (80%)
DEFAULT_BATTERY_SOC_HYSTERESIS = 3  # Default hysteresis (3%)

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

# Distribution mode configuration (hub-level)
CONF_DISTRIBUTION_MODE = "distribution_mode"

# Operating mode configuration (per-load)
CONF_OPERATING_MODE = "operating_mode"
OPERATING_MODE_STANDARD = "Standard"        # EVSE: charge from any source at max
OPERATING_MODE_CONTINUOUS = "Continuous"     # Plug: always on
OPERATING_MODE_SOLAR_PRIORITY = "Solar Priority"
OPERATING_MODE_SOLAR_ONLY = "Solar Only"
OPERATING_MODE_EXCESS = "Excess"
DEFAULT_OPERATING_MODE_EVSE = OPERATING_MODE_STANDARD
DEFAULT_OPERATING_MODE_PLUG = OPERATING_MODE_CONTINUOUS

# Available modes per device type
OPERATING_MODES_EVSE = [
    OPERATING_MODE_STANDARD,
    OPERATING_MODE_SOLAR_PRIORITY,
    OPERATING_MODE_SOLAR_ONLY,
    OPERATING_MODE_EXCESS,
]
OPERATING_MODES_PLUG = [
    OPERATING_MODE_CONTINUOUS,
    OPERATING_MODE_SOLAR_ONLY,
    OPERATING_MODE_EXCESS,
]

# Mode urgency for distribution sorting (lower = higher urgency)
# Standard and Continuous share urgency 0 — same engine behavior, different labels
MODE_URGENCY = {
    OPERATING_MODE_STANDARD: 0,
    OPERATING_MODE_CONTINUOUS: 0,
    OPERATING_MODE_SOLAR_PRIORITY: 1,
    OPERATING_MODE_SOLAR_ONLY: 2,
    OPERATING_MODE_EXCESS: 3,
}
DISTRIBUTION_MODE_SHARED = "Shared"
DISTRIBUTION_MODE_PRIORITY = "Priority"
DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED = "Sequential - Optimized"
DISTRIBUTION_MODE_SEQUENTIAL_STRICT = "Sequential - Strict"
DEFAULT_DISTRIBUTION_MODE = DISTRIBUTION_MODE_PRIORITY
