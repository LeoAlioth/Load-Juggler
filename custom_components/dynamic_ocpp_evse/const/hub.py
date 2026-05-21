"""Hub / site-level constants — grid CTs, inverter, battery, distribution."""

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

# Site-level timing / detection
CONF_SITE_UPDATE_FREQUENCY = "site_update_frequency"  # Hub-level: how often site sensors refresh
CONF_AUTO_DETECT_PHASE_MAPPING = "auto_detect_phase_mapping"  # Hub-level: detect L1/L2/L3 wiring mismatches
CONF_SOLAR_GRACE_PERIOD = "solar_grace_period"  # Hub-level: minutes before pausing in Solar/Excess mode

# Hub default values
DEFAULT_MAIN_BREAKER_RATING = 25
DEFAULT_SITE_UPDATE_FREQUENCY = 2  # Fast site info refresh (seconds)
DEFAULT_SOLAR_GRACE_PERIOD = 5  # minutes
DEFAULT_EXCESS_EXPORT_THRESHOLD = 13000
EXCESS_EXPORT_HYSTERESIS = 500  # W — deadband below the threshold; once Excess
# mode is on it stays on until export drops this far below the threshold,
# preventing charger on/off chatter when export hovers near the threshold.
DEFAULT_BATTERY_MAX_POWER = 5000
DEFAULT_BATTERY_SOC_MIN = 20  # Default minimum SOC (20%)
DEFAULT_BATTERY_SOC_TARGET = 80  # Default SOC target (80%)
DEFAULT_BATTERY_SOC_HYSTERESIS = 3  # Default hysteresis (3%)

# Distribution mode configuration (hub-level)
CONF_DISTRIBUTION_MODE = "distribution_mode"
DISTRIBUTION_MODE_SHARED = "Shared"
DISTRIBUTION_MODE_PRIORITY = "Priority"
DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED = "Sequential - Optimized"
DISTRIBUTION_MODE_SEQUENTIAL_STRICT = "Sequential - Strict"
DEFAULT_DISTRIBUTION_MODE = DISTRIBUTION_MODE_PRIORITY
