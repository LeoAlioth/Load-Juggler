"""Which setup fields sit under which heading (see sections.py).

Plain data and no Home Assistant import, so the tests can check it against the
translations: each group is (section key, field keys, collapsed), and the
section key is its translation key. A section key must differ from every field
key of its step. The create wizard and the Configure pages share the field keys,
so a family of groups serves both.
"""

_SITE = ("site", ("connected_to_phase", "load_priority"), False)
_FORECAST_LOAD = ("forecast", ("early_room_entity_id", "early_rate_entity_id", "catch_up_enabled"), False)
_TIMING_LOAD = ("timing", ("update_frequency", "solar_grace_period", "manual_override_timeout", "binary_min_off_time"), False)

_PLUG = [
    ("plug", ("plug_switch_entity_id", "plug_power_rating", "plug_max_current"), False),
    _SITE,
    ("monitor", ("plug_power_monitor_entity_id", "plug_restart_on_no_draw", "plug_restart_after",
                 "plug_finish_below_w", "plug_finish_for"), False),
    _FORECAST_LOAD,
    _TIMING_LOAD,
]

_TANK = [
    ("device", ("climate_entity_id", "heating_element_power", "tank_power_entity_id", "tank_off_operation_mode"), False),
    ("site", ("connected_to_phase", "load_priority", "tank_prioritize_below_normal"), False),
    ("forecast", ("early_room_entity_id", "early_rate_entity_id"), False),
    _TIMING_LOAD,
]

_STATION = [
    ("controls", ("station_charge_speed_entity_id", "station_reserve_entity_id", "station_charge_limit_entity_id"), False),
    ("sensors", ("station_battery_level_entity_id", "station_ac_input_entity_id", "station_ac_output_entity_id"), False),
    ("limits", ("station_min_charge_power", "station_max_charge_power", "station_normal_reserve",
                "station_storm_reserve"), False),
    _SITE,
    ("forecast", ("early_room_entity_id", "early_rate_entity_id", "pace_over_horizon", "catch_up_enabled"), False),
    ("timing", ("update_frequency", "solar_grace_period", "manual_override_timeout"), False),
]

_SG_READY = [
    ("relays", ("sg_relay_1_entity_id", "sg_relay_2_entity_id", "sg_power_entity_id", "sg_nominal_power"), False),
    ("rules", ("sg_min_state_time", "sg_max_block_time", "sg_max_blocks_per_day"), False),
    _SITE,
    ("forecast", ("early_room_entity_id", "early_rate_entity_id"), False),
    ("timing", ("manual_override_timeout",), False),
]

_VARIABLE_LOAD = [
    ("setpoint", ("vl_setpoint_entity_id", "vl_setpoint_unit", "vl_min", "vl_max", "vl_bias", "vl_fallback_setpoint"), False),
    ("site", ("charger_l1_phase", "charger_l2_phase", "charger_l3_phase", "load_priority"), False),
    ("readings", ("vl_current_l1_entity_id", "vl_current_l2_entity_id", "vl_current_l3_entity_id",
                  "vl_power_entity_id", "vl_connected_entity_id", "vl_charging_entity_id"), False),
    ("phase_advice", ("vl_phase_multi_above", "vl_phase_single_below", "vl_phase_min_interval"), False),
    ("forecast", ("early_room_entity_id", "early_rate_entity_id", "pace_over_horizon", "catch_up_enabled"), False),
    ("timing", ("update_frequency", "vl_resend_interval", "solar_grace_period", "manual_override_timeout"), False),
]

_CHARGER_CURRENT = [
    ("current", ("evse_minimum_charge_current", "evse_maximum_charge_current"), False),
    ("phases", ("charger_l1_phase", "charger_l2_phase", "charger_l3_phase"), False),
]

_CHARGER_TIMING = [
    ("profile", ("charge_rate_unit", "profile_validity_mode", "ocpp_profile_timeout", "stack_level"), False),
    ("pause", ("charge_pause_duration", "pause_profile_validity"), False),
    ("forecast", ("pace_over_horizon", "catch_up_enabled"), False),
    ("timing", ("update_frequency", "solar_grace_period"), False),
]

_HUB = [
    ("grid", ("phase_a_current_entity_id", "phase_b_current_entity_id", "phase_c_current_entity_id", "invert_phases"), False),
    ("limits", ("main_breaker_rating", "phase_voltage", "enable_max_import_power", "max_import_power_entity_id"), False),
    ("generator", ("grid_input_is_generator", "generator_rating"), False),
    ("export", ("grid_export_limit", "grid_export_limit_per_phase"), False),
    ("excess", ("excess_trigger_margin", "excess_hysteresis"), False),
    ("timing", ("site_update_frequency", "auto_detect_phase_mapping", "solar_grace_period"), False),
    ("battery", ("battery_soc_hysteresis", "battery_soc_freeze_floor"), False),
    ("base", ("base_consumption", "base_consumption_entity_id"), False),
    ("sources", ("forecast_soc_floor", "pacing_energy_entity_id", "pacing_horizon_entity_id",
                 "forecast_excess_full_entity_id"), False),
    ("early", ("forecast_excess_enabled", "forecast_clip_early_enabled", "forecast_excess_soc_source",
               "forecast_excess_soc"), False),
    ("outage", ("outage_entity_id", "outage_precharge_lead_hours", "outage_hold_hours"), False),
]

_FILTERS = [
    ("filters", ("filter_input_tau_s", "filter_permit_tau_s", "filter_ramp_tau_s", "filter_ctrl_fast_tau_s"), True),
    ("settling", ("filter_settle_seconds", "filter_dead_band", "filter_ramp_up_rate", "filter_ramp_down_rate"), True),
]

_INVERTER = [
    ("capacity", ("inverter_max_power", "inverter_max_power_entity_id", "inverter_max_power_per_phase",
                  "inverter_supports_asymmetric"), False),
    ("output", ("inverter_output_phase_a_entity_id", "inverter_output_phase_b_entity_id",
                "inverter_output_phase_c_entity_id"), False),
    ("input", ("inverter_input_phase_a_entity_id", "inverter_input_phase_b_entity_id",
               "inverter_input_phase_c_entity_id", "inverter_input_is_grid"), False),
    ("solar", ("solar_production_entity_id", "solar_forecast_device_ids"), False),
]

_BATTERY = [
    ("sensors", ("battery_soc_entity_id", "battery_power_entity_id", "battery_capacity_entity_id",
                 "battery_charge_target_entity_id"), False),
    ("capacity", ("battery_max_charge_power", "battery_max_discharge_power", "battery_capacity_kwh"), False),
]

_CONTROL = [
    ("limit", ("inverter_charge_limit_entity_id", "inverter_charge_limit_unit", "inverter_charge_limit_normal",
               "inverter_charge_limit_minimum", "inverter_charge_control_interval",
               "inverter_charge_control_deadband_w"), False),
    ("voltage", ("battery_voltage_entity_id", "battery_nominal_voltage"), False),
    ("ceiling", ("inverter_soc_limit_entity_ids", "inverter_soc_limit_normal_entity_id",
                 "inverter_soc_limit_semantics"), False),
]

CONFIG = {
    "hub_grid": _HUB,
    "inverter_config": _INVERTER,
    "inverter_battery": _BATTERY,
    "inverter_control": _CONTROL,
    "plug_config": _PLUG,
    "power_station_config": _STATION,
    "hot_water_tank_config": _TANK,
    "room_thermostat_config": _TANK,
    "sg_ready_config": _SG_READY,
    "variable_load_config": _VARIABLE_LOAD,
    "charger_current": _CHARGER_CURRENT,
    "charger_timing": _CHARGER_TIMING,
}

OPTIONS = {
    "hub_connection": _HUB,
    "hub_export": _HUB,
    "hub_policy": _HUB,
    "hub_filters": _FILTERS,
    "inverter_core": _INVERTER,
    "inverter_battery": _BATTERY,
    "inverter_control": _CONTROL,
    "plug": _PLUG,
    "power_station": _STATION,
    "hot_water_tank": _TANK,
    "room_thermostat": _TANK,
    "sg_ready": _SG_READY,
    "variable_load": _VARIABLE_LOAD,
    "charger_current": _CHARGER_CURRENT,
    "charger_timing": _CHARGER_TIMING,
}
