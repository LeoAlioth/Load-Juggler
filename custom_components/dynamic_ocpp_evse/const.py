DOMAIN = "dynamic_ocpp_evse"

# Entry types for hub/charger architecture
ENTRY_TYPE = "entry_type"
ENTRY_TYPE_HUB = "hub"
ENTRY_TYPE_CHARGER = "charger"

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
CONF_PHASE_VOLTAGE = "phase_voltage"
CONF_EXCESS_EXPORT_THRESHOLD = "excess_export_threshold"  # Maximum allowed export before charging starts in Excess mode

# Battery support configuration constants (hub-level)
CONF_BATTERY_POWER_ENTITY_ID = "battery_power_entity_id"
CONF_BATTERY_SOC_ENTITY_ID = "battery_soc_entity_id"
CONF_BATTERY_SOC_TARGET_ENTITY_ID = "battery_soc_target_entity_id"
CONF_BATTERY_MAX_CHARGE_POWER = "battery_max_charge_power"  # W
CONF_BATTERY_MAX_DISCHARGE_POWER = "battery_max_discharge_power"  # W
CONF_ALLOW_GRID_CHARGING_ENTITY_ID = "allow_grid_charging_entity_id"
CONF_POWER_BUFFER_ENTITY_ID = "power_buffer_entity_id"
CONF_POWER_BUFFER = "power_buffer"

# Hub entity IDs (created by hub)
CONF_CHARGIN_MODE_ENTITY_ID = "charging_mode_entity_id"

# Charger-specific configuration keys
CONF_HUB_ENTRY_ID = "hub_entry_id"
CONF_CHARGER_ID = "charger_id"
CONF_CHARGER_PRIORITY = "charger_priority"
CONF_OCPP_DEVICE_ID = "ocpp_device_id"
CONF_EVSE_CURRENT_IMPORT_ENTITY_ID = "evse_current_import_entity_id"
CONF_EVSE_CURRENT_OFFERED_ENTITY_ID = "evse_current_offered_entity_id"
CONF_EVSE_MINIMUM_CHARGE_CURRENT = "evse_minimum_charge_current"  # defaults to 6
CONF_EVSE_MAXIMUM_CHARGE_CURRENT = "evse_maximum_charge_current"  # defaults to 16
CONF_MIN_CURRENT_ENTITY_ID = "min_current_entity_id"
CONF_MAX_CURRENT_ENTITY_ID = "max_current_entity_id"
CONF_UPDATE_FREQUENCY = "update_frequency"
CONF_OCPP_PROFILE_TIMEOUT = "ocpp_profile_timeout"
CONF_CHARGE_PAUSE_DURATION = "charge_pause_duration"
CONF_STACK_LEVEL = "stack_level"

# sensor attributes
CONF_PHASES = "phases"
CONF_CHARING_MODE = "charging_mode"
CONF_AVAILABLE_CURRENT = "available_current"
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
OCPP_ENTITY_SUFFIX_CURRENT_OFFERED = "_current_offered"
OCPP_ENTITY_SUFFIX_STATUS = "_status"
OCPP_ENTITY_SUFFIX_STOP_REASON = "_stop_reason"

# Default values
DEFAULT_MIN_CHARGE_CURRENT = 6
DEFAULT_MAX_CHARGE_CURRENT = 16
DEFAULT_PHASE_VOLTAGE = 230
DEFAULT_MAIN_BREAKER_RATING = 25
DEFAULT_UPDATE_FREQUENCY = 5
DEFAULT_OCPP_PROFILE_TIMEOUT = 90
DEFAULT_CHARGE_PAUSE_DURATION = 180
DEFAULT_STACK_LEVEL = 3
DEFAULT_CHARGER_PRIORITY = 1
DEFAULT_EXCESS_EXPORT_THRESHOLD = 13000
DEFAULT_BATTERY_MAX_POWER = 5000

# Charge rate unit configuration (per charger)
CONF_CHARGE_RATE_UNIT = "charge_rate_unit"
CHARGE_RATE_UNIT_AUTO = "auto"
CHARGE_RATE_UNIT_AMPS = "A"
CHARGE_RATE_UNIT_WATTS = "W"
DEFAULT_CHARGE_RATE_UNIT = CHARGE_RATE_UNIT_AUTO
