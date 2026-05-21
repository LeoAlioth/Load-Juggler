"""EVSE (OCPP charger) constants — entities, OCPP, charge limits, modes."""

from .common import (
    OPERATING_MODE_STANDARD,
    OPERATING_MODE_SOLAR_PRIORITY,
    OPERATING_MODE_SOLAR_ONLY,
    OPERATING_MODE_EXCESS,
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
CONF_STACK_LEVEL = "stack_level"

# OCPP charger L1/L2/L3 → site phase mapping
CONF_CHARGER_L1_PHASE = "charger_l1_phase"
CONF_CHARGER_L2_PHASE = "charger_l2_phase"
CONF_CHARGER_L3_PHASE = "charger_l3_phase"

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

# EVSE default values
DEFAULT_MIN_CHARGE_CURRENT = 6
DEFAULT_MAX_CHARGE_CURRENT = 16
DEFAULT_OCPP_PROFILE_TIMEOUT = 120
DEFAULT_CHARGE_PAUSE_DURATION = 3  # minutes
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

# EVSE operating modes
OPERATING_MODES_EVSE = [
    OPERATING_MODE_STANDARD,
    OPERATING_MODE_SOLAR_PRIORITY,
    OPERATING_MODE_SOLAR_ONLY,
    OPERATING_MODE_EXCESS,
]
DEFAULT_OPERATING_MODE_EVSE = OPERATING_MODE_STANDARD
