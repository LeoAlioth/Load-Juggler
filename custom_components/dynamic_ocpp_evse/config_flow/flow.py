"""Load Juggler - the config flow handler: everything up to a created entry.

``LoadJugglerConfigFlow`` drives initial setup: the hub, inverter, group and
per-device-type creation steps, OCPP charger discovery, and the legacy
hub-inverter import. The forms come from ``schemas.py``; the normalizers,
auto-detection and OCPP capability probes the steps lean on come from
``helpers.py``, shared with the options flow. Editing an entry afterwards is
``options.py``.

The three one-page load types (plug, hot-water tank, power station) run on the
shared ``_async_create_load_page``; the hub, inverter and EVSE wizards are
hand-written, each being a chain with its own routing.
"""
import re
import voluptuous as vol
from typing import Any
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.selector import selector
from ..const import (
    CONF_ALLOW_GRID_CHARGING_ENTITY_ID,
    CONF_AUTO_DETECT_PHASE_MAPPING,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_SOC_FULL,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_BATTERY_SOC_TARGET_ENTITY_ID,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_CHARGER_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_LOAD_PRIORITY,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_PAUSE_DURATION,
    CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
    CONF_CIRCUIT_GROUP_MEMBERS,
    CONF_CLIMATE_ENTITY_ID,
    CONF_CONNECTED_TO_PHASE,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_POWER_IMPORT_ENTITY_ID,
    CONF_EVSE_POWER_OFFERED_ENTITY_ID,
    CONF_EXCESS_HYSTERESIS,
    CONF_EXCESS_TRIGGER_MARGIN,
    CONF_GRID_EXPORT_LIMIT,
    CONF_HEATING_ELEMENT_POWER,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_INVERTER_SUPPORTS_ASYMMETRIC,
    CONF_INVERT_PHASES,
    CONF_MAIN_BREAKER_RATING,
    CONF_MAX_CURRENT_ENTITY_ID,
    CONF_MIN_CURRENT_ENTITY_ID,
    CONF_NAME,
    CONF_OCPP_DEVICE_ID,
    CONF_OCPP_PROFILE_TIMEOUT,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_PLUG_MAX_CURRENT,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
    CONF_PLUG_POWER_RATING,
    CONF_PLUG_SWITCH_ENTITY_ID,
    CONF_POWER_BUFFER_ENTITY_ID,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_SOLAR_GRACE_PERIOD,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_STACK_LEVEL,
    CONF_STATION_CHARGE_SPEED_ENTITY_ID,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_STATION_NORMAL_RESERVE,
    CONF_STATION_RESERVE_ENTITY_ID,
    CONF_STATION_STORM_RESERVE,
    CONF_TANK_AWAY_TEMPERATURE,
    CONF_TANK_BOOST_TEMPERATURE,
    CONF_TANK_NORMAL_TEMPERATURE,
    CONF_TANK_POWER_DEVICE_ID,
    CONF_TANK_POWER_ENTITY_ID,
    CONF_UPDATE_FREQUENCY,
    CONF_WIRING_TOPOLOGY,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_CHARGE_PAUSE_DURATION,
    DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
    DEFAULT_EXCESS_HYSTERESIS,
    DEFAULT_EXCESS_TRIGGER_MARGIN,
    DEFAULT_GRID_EXPORT_LIMIT,
    DEFAULT_HEATING_ELEMENT_POWER,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_OCPP_PROFILE_TIMEOUT,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_PLUG_MAX_CURRENT,
    DEFAULT_PLUG_POWER_RATING,
    DEFAULT_PROFILE_VALIDITY_MODE,
    DEFAULT_SOLAR_GRACE_PERIOD,
    DEFAULT_STACK_LEVEL,
    DEFAULT_STATION_MAX_CHARGE_POWER,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DEFAULT_STATION_NORMAL_RESERVE,
    DEFAULT_STATION_STORM_RESERVE,
    DEFAULT_TANK_AWAY_TEMPERATURE,
    DEFAULT_TANK_BOOST_TEMPERATURE,
    DEFAULT_TANK_NORMAL_TEMPERATURE,
    DEFAULT_UPDATE_FREQUENCY,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_GROUP,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_INVERTER,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_POWER_STATION,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
    FIELD_OCPP_DEVICE,
    MIGRATE_HUB_INVERTER_IMPORTED_FLAG,
)
from ..detection_patterns import PHASE_PATTERNS, PLUG_POWER_MONITOR_PATTERNS
from ..helpers import (
    get_entry_value,
    prettify_name,
    validate_charger_settings,
)
from .helpers import (
    _BATTERY_UNIT_MAP,
    _GRID_ENTITY_KEYS,
    _GRID_UNIT_MAP,
    _INVERTER_ENTITY_KEYS,
    _INVERTER_OUTPUT_UNIT_MAP,
    _LOGGER,
    _PLUG_ENTITY_KEYS,
    _SOLAR_UNIT_MAP,
    _STATION_ENTITY_KEYS,
    _TANK_ENTITY_KEYS,
    _WRITE_CONTROL_UNIT_MAP,
    _auto_detect_entity,
    _auto_detect_phase_entities,
    _compose_entry_title,
    _detect_charge_rate_unit,
    _detect_meter_value_interval,
    _entity_registry_ids,
    _hub_phase_count,
    _normalize_forecast_list,
    _normalize_inverter_power_caps,
    _normalize_optional_inputs,
    _normalize_soc_limit_list,
    _resolve_device_power_entity,
    _validate_charge_limit_unit,
    _validate_entity_units,
    _validate_forecast_devices,
    ocpp_charger_for_device,
    scan_ocpp_chargers,
)
from .options import LoadJugglerOptionsFlow
from .schemas import (
    _build_hub_inverter_schema,
    _build_inverter_solar_schema,
    _charger_current_schema,
    _charger_info_schema,
    _charger_timing_schema,
    _hot_water_tank_schema,
    _hub_grid_schema,
    _inverter_battery_schema,
    _inverter_control_schema,
    _plug_schema,
    _power_station_schema,
)


class LoadJugglerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Load Juggler."""

    VERSION = 2
    MINOR_VERSION = 5

    def __init__(self):
        self._data = {}
        self._discovered_chargers = []
        self._selected_charger = None
        self._entity_cache = None

    def _get_entity_registry_ids(self) -> list[str]:
        """The auto-detection candidates, scanned once per flow."""
        if self._entity_cache is None:
            self._entity_cache = _entity_registry_ids(self.hass)
        return self._entity_cache

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Initial step: choose what to add — hub, EVSE, smart load, group, inverter."""
        errors: dict[str, str] = {}

        # Check if any hubs exist
        hubs = self._get_hub_entries()

        if user_input is not None:
            setup_type = user_input.get("setup_type")
            if setup_type == "hub":
                return await self.async_step_hub_info()
            elif setup_type in ("evse", "plug", "tank", "station"):
                if not hubs:
                    errors["base"] = "no_hub_configured"
                else:
                    self._data[CONF_DEVICE_TYPE] = {
                        "evse": DEVICE_TYPE_EVSE,
                        "plug": DEVICE_TYPE_PLUG,
                        "tank": DEVICE_TYPE_HOT_WATER_TANK,
                        "station": DEVICE_TYPE_POWER_STATION,
                    }[setup_type]
                    return await self.async_step_select_hub()
            elif setup_type == "group":
                if not hubs:
                    errors["base"] = "no_hub_configured"
                else:
                    self._data[CONF_DEVICE_TYPE] = DEVICE_TYPE_GROUP
                    return await self.async_step_select_hub()
            elif setup_type == "inverter":
                if not hubs:
                    errors["base"] = "no_hub_configured"
                else:
                    self._data[CONF_DEVICE_TYPE] = DEVICE_TYPE_INVERTER
                    return await self.async_step_select_hub()

        # Build options based on existing hubs
        options = [
            {"value": "hub", "label": "Configure Home Electrical System (Hub)"},
        ]
        if hubs:
            options.append({"value": "evse", "label": "Add OCPP Charger (EVSE)"})
            options.append({"value": "plug", "label": "Add Smart Outlet / Relay"})
            options.append({"value": "tank", "label": "Add Hot Water Tank"})
            options.append(
                {"value": "station", "label": "Add Portable Power Station"}
            )
            options.append(
                {"value": "inverter", "label": "Add Inverter / Home Battery"}
            )
            options.append(
                {"value": "group", "label": "Add Circuit Group (Shared Breaker)"}
            )

        data_schema = vol.Schema(
            {
                vol.Required(
                    "setup_type", default="hub" if not hubs else "evse"
                ): selector({"select": {"options": options, "mode": "list"}})
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors, last_step=False
        )

    def _get_hub_entries(self) -> list:
        """Get all hub config entries."""
        return [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_HUB
        ]

    def _get_load_entries(self) -> list:
        """Get all load config entries."""
        return [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_LOAD
        ]

    # ==================== HUB CONFIGURATION STEPS ====================

    def _entity_id_in_use(self, entity_id: str) -> bool:
        """True if entity_id is already used by another Load Juggler config entry."""
        return any(
            entry.data.get(CONF_ENTITY_ID) == entity_id
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )

    async def async_step_hub_info(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 1: Basic info (name and entity_id)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            entity_id = self._data.get(CONF_ENTITY_ID)
            if entity_id and self._entity_id_in_use(entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            else:
                self._data[ENTRY_TYPE] = ENTRY_TYPE_HUB
                return await self.async_step_hub_grid()

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=self._data.get(CONF_NAME, "Site Load Management"),
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=self._data.get(CONF_ENTITY_ID, "lj_site_load_management"),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="hub_info", data_schema=data_schema, errors=errors, last_step=False
        )

    async def async_step_hub_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 2: grid, electrical and site policy — creates the entry.

        A NEW hub carries no hardware of its own: inverters, batteries, PV
        production sensors and PV forecast sources all live on Inverter
        entries added afterwards ("Add Inverter / Home Battery"). What stays
        here is the grid connection plus the site-wide policy applied to
        whatever fleet those entries form.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = _normalize_optional_inputs(user_input, _GRID_ENTITY_KEYS)
            _validate_entity_units(self.hass, user_input, _GRID_UNIT_MAP, errors)
            if not errors:
                self._data.update(user_input)

                # Generate entity IDs for hub-created entities
                entity_id = self._data.get(CONF_ENTITY_ID)
                self._data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = (
                    f"number.{entity_id}_home_battery_soc_target"
                )
                self._data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = (
                    f"switch.{entity_id}_allow_grid_charging"
                )
                self._data[CONF_POWER_BUFFER_ENTITY_ID] = (
                    f"number.{entity_id}_power_buffer"
                )

                # Split static vs mutable fields:
                static_data = {
                    CONF_NAME: self._data.get(CONF_NAME),
                    CONF_ENTITY_ID: self._data.get(CONF_ENTITY_ID),
                    ENTRY_TYPE: ENTRY_TYPE_HUB,
                    # Born without legacy inverter/battery fields — nothing to
                    # auto-import, and the legacy hub pages stay hidden.
                    MIGRATE_HUB_INVERTER_IMPORTED_FLAG: True,
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }

                return self.async_create_entry(
                    title=static_data[CONF_NAME],
                    data=static_data,
                    options=options_data,
                )
            return self.async_show_form(
                step_id="hub_grid",
                data_schema=_hub_grid_schema(self.hass, user_input),
                errors=errors,
                last_step=True,
            )

        try:
            # Try to find a complete set of phases using pattern sets
            ct_detected = _auto_detect_phase_entities(
                self._get_entity_registry_ids(), PHASE_PATTERNS
            )
            default_phase_a = ct_detected["phase_a"]
            default_phase_b = ct_detected["phase_b"]
            default_phase_c = ct_detected["phase_c"]

            # Fallback: pick individual phases from different pattern sets
            if not (default_phase_a and default_phase_b and default_phase_c):
                entity_ids = self._get_entity_registry_ids()
                for pattern_set in PHASE_PATTERNS:
                    if not default_phase_a:
                        default_phase_a = next(
                            (
                                eid
                                for eid in entity_ids
                                if re.match(pattern_set["patterns"]["phase_a"], eid)
                            ),
                            None,
                        )
                    if not default_phase_b:
                        default_phase_b = next(
                            (
                                eid
                                for eid in entity_ids
                                if re.match(pattern_set["patterns"]["phase_b"], eid)
                            ),
                            None,
                        )
                    if not default_phase_c:
                        default_phase_c = next(
                            (
                                eid
                                for eid in entity_ids
                                if re.match(pattern_set["patterns"]["phase_c"], eid)
                            ),
                            None,
                        )

            data_schema = _hub_grid_schema(
                self.hass,
                {
                    CONF_PHASE_A_CURRENT_ENTITY_ID: default_phase_a,
                    CONF_PHASE_B_CURRENT_ENTITY_ID: default_phase_b,
                    CONF_PHASE_C_CURRENT_ENTITY_ID: default_phase_c,
                    CONF_MAIN_BREAKER_RATING: DEFAULT_MAIN_BREAKER_RATING,
                    CONF_INVERT_PHASES: False,
                    CONF_PHASE_VOLTAGE: DEFAULT_PHASE_VOLTAGE,
                    CONF_GRID_EXPORT_LIMIT: DEFAULT_GRID_EXPORT_LIMIT,
                    CONF_EXCESS_TRIGGER_MARGIN: DEFAULT_EXCESS_TRIGGER_MARGIN,
                    CONF_EXCESS_HYSTERESIS: DEFAULT_EXCESS_HYSTERESIS,
                    CONF_AUTO_DETECT_PHASE_MAPPING: True,
                    CONF_BATTERY_SOC_HYSTERESIS: DEFAULT_BATTERY_SOC_HYSTERESIS,
                }
            )

        except Exception as e:
            _LOGGER.error("Error in async_step_hub_grid: %s", e, exc_info=True)
            errors["base"] = "unknown"
            data_schema = vol.Schema({})

        return self.async_show_form(
            step_id="hub_grid", data_schema=data_schema, errors=errors, last_step=True
        )

    # ==================== LOAD CONFIGURATION STEPS ====================

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Handle integration discovery of OCPP chargers."""
        # Store discovery info. The payload is a scan_ocpp_chargers() dict (see
        # __init__._discover_and_notify_chargers), so every entity key the
        # manual flow stores is carried through to the created entry — a
        # discovered charger must not end up with None for its per-phase
        # current/power entities just because it came in through discovery.
        self._data[CONF_HUB_ENTRY_ID] = discovery_info["hub_entry_id"]
        self._selected_charger = {
            "id": discovery_info["charger_id"],
            "name": discovery_info["charger_name"],
            "device_id": discovery_info.get("device_id"),
            # The HA device-registry UUID, for the charger_info device picker
            # only — never stored, and never handed to an ocpp service.
            "ha_device_id": discovery_info.get("ha_device_id"),
            "current_import_entity": discovery_info["current_import_entity"],
            "current_import_l1_entity": discovery_info.get("current_import_l1_entity"),
            "current_import_l2_entity": discovery_info.get("current_import_l2_entity"),
            "current_import_l3_entity": discovery_info.get("current_import_l3_entity"),
            "current_offered_entity": discovery_info.get("current_offered_entity"),
            "power_offered_entity": discovery_info.get("power_offered_entity"),
            "power_import_entity": discovery_info.get("power_import_entity"),
        }

        # Set unique ID to prevent duplicate discoveries
        await self.async_set_unique_id(
            f"{DOMAIN}_charger_{discovery_info['charger_id']}"
        )
        self._abort_if_unique_id_configured()

        # Show confirmation form
        self.context["title_placeholders"] = {"name": self._selected_charger["name"]}
        return await self.async_step_charger_info()

    async def _route_after_hub_selection(self) -> config_entries.FlowResult:
        """Route to the correct step after hub is selected."""
        device_type = self._data.get(CONF_DEVICE_TYPE)
        if device_type == DEVICE_TYPE_PLUG:
            return await self.async_step_plug_config()
        if device_type == DEVICE_TYPE_HOT_WATER_TANK:
            return await self.async_step_hot_water_tank_config()
        if device_type == DEVICE_TYPE_POWER_STATION:
            return await self.async_step_power_station_config()
        if device_type == DEVICE_TYPE_GROUP:
            return await self.async_step_group_config()
        if device_type == DEVICE_TYPE_INVERTER:
            return await self.async_step_inverter_config()
        return await self.async_step_discover_chargers()

    async def async_step_select_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Load step 1: Select which hub to add the load to."""
        errors: dict[str, str] = {}
        hubs = self._get_hub_entries()

        if user_input is not None:
            self._data[CONF_HUB_ENTRY_ID] = user_input["hub_entry_id"]
            return await self._route_after_hub_selection()

        # If only one hub, skip selection
        if len(hubs) == 1:
            self._data[CONF_HUB_ENTRY_ID] = hubs[0].entry_id
            return await self._route_after_hub_selection()

        hub_options = [
            {"value": entry.entry_id, "label": entry.title} for entry in hubs
        ]

        data_schema = vol.Schema(
            {
                vol.Required("hub_entry_id"): selector(
                    {"select": {"options": hub_options}}
                )
            }
        )

        return self.async_show_form(
            step_id="select_hub",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def _async_create_load_page(
        self,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
        schema,
        device_type: str,
        type_label: str,
        default_name: str,
        default_entity_id: str,
        defaults: dict[str, Any],
        entity_keys: list[str] | None = None,
        static_keys: tuple = (),
        prepare=None,
        validate=None,
    ) -> config_entries.FlowResult:
        """Run a one-page load-creation step: name it, configure it, create it.

        Shared by the plug, hot-water-tank and power-station steps, which are
        the same page three times over: name and entity_id on top of the
        device's own fields, and a submit that checks the entity_id is free and
        splits what was collected into the static ``data`` half (identity, type,
        hub and the entities the device is defined by — ``static_keys``) and the
        editable ``options`` half (everything else).

        The EVSE charger does NOT use this — it comes with a discovery step and
        a three-page wizard. Hooks: ``prepare`` rewrites the submitted data
        before the checks, ``validate`` adds device-specific ones.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(_normalize_optional_inputs(user_input, entity_keys))
            if prepare is not None:
                prepare(self._data)

            name = self._data.get(CONF_NAME, default_name)
            entity_id = self._data.get(CONF_ENTITY_ID, default_entity_id)

            if self._entity_id_in_use(entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            if validate is not None and not errors:
                validate(self._data, errors)
            if not errors:
                static_data = {
                    CONF_ENTITY_ID: entity_id,
                    CONF_NAME: name,
                    ENTRY_TYPE: ENTRY_TYPE_LOAD,
                    CONF_DEVICE_TYPE: device_type,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                    **{key: self._data.get(key) for key in static_keys},
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }
                return self.async_create_entry(
                    title=_compose_entry_title(name, type_label),
                    data=static_data,
                    options=options_data,
                )

        # Name + entity_id fields, then the device's own schema. self._data is
        # merged last so a validation-error re-show keeps the user's input.
        form_defaults = {**defaults, **self._data}
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME, default=form_defaults.get(CONF_NAME, default_name)
                    ): str,
                    vol.Required(
                        CONF_ENTITY_ID,
                        default=form_defaults.get(CONF_ENTITY_ID, default_entity_id),
                    ): str,
                    **schema(form_defaults).schema,
                }
            ),
            errors=errors,
            last_step=True,
        )

    async def async_step_plug_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Smart load configuration step."""
        return await self._async_create_load_page(
            user_input,
            step_id="plug_config",
            schema=_plug_schema,
            device_type=DEVICE_TYPE_PLUG,
            type_label="Smart Load",
            default_name="Smart Load",
            default_entity_id="lj_smart_load",
            defaults={
                CONF_LOAD_PRIORITY: len(self._get_load_entries()) + 1,
                CONF_PLUG_POWER_RATING: DEFAULT_PLUG_POWER_RATING,
                CONF_PLUG_MAX_CURRENT: DEFAULT_PLUG_MAX_CURRENT,
                CONF_CONNECTED_TO_PHASE: "A",
                CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
                CONF_PLUG_POWER_MONITOR_ENTITY_ID: _auto_detect_entity(
                    self._get_entity_registry_ids(), PLUG_POWER_MONITOR_PATTERNS
                ),
            },
            entity_keys=_PLUG_ENTITY_KEYS,
            static_keys=(CONF_PLUG_SWITCH_ENTITY_ID,),
        )

    async def async_step_hot_water_tank_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hot water tank configuration step."""

        def _resolve_power_device(data: dict[str, Any]) -> None:
            """A picked power device is resolved to its power-sensor entity now,
            so runtime only ever deals with CONF_TANK_POWER_ENTITY_ID."""
            device_id = data.pop(CONF_TANK_POWER_DEVICE_ID, None)
            if device_id and not data.get(CONF_TANK_POWER_ENTITY_ID):
                resolved = _resolve_device_power_entity(self.hass, device_id)
                if resolved:
                    data[CONF_TANK_POWER_ENTITY_ID] = resolved

        return await self._async_create_load_page(
            user_input,
            step_id="hot_water_tank_config",
            schema=_hot_water_tank_schema,
            device_type=DEVICE_TYPE_HOT_WATER_TANK,
            type_label="Hot Water Tank",
            default_name="Hot Water Tank",
            default_entity_id="lj_hot_water_tank",
            defaults={
                CONF_LOAD_PRIORITY: len(self._get_load_entries()) + 1,
                CONF_HEATING_ELEMENT_POWER: DEFAULT_HEATING_ELEMENT_POWER,
                CONF_TANK_AWAY_TEMPERATURE: DEFAULT_TANK_AWAY_TEMPERATURE,
                CONF_TANK_NORMAL_TEMPERATURE: DEFAULT_TANK_NORMAL_TEMPERATURE,
                CONF_TANK_BOOST_TEMPERATURE: DEFAULT_TANK_BOOST_TEMPERATURE,
                CONF_CONNECTED_TO_PHASE: "A",
                CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
                CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
            },
            entity_keys=_TANK_ENTITY_KEYS,
            static_keys=(CONF_CLIMATE_ENTITY_ID,),
            prepare=_resolve_power_device,
        )

    async def async_step_power_station_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Portable power station configuration step."""

        def _check_power_window(
            data: dict[str, Any], errors: dict[str, str]
        ) -> None:
            if data.get(
                CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
            ) < data.get(
                CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER
            ):
                errors[CONF_STATION_MAX_CHARGE_POWER] = "station_max_below_min"

        return await self._async_create_load_page(
            user_input,
            step_id="power_station_config",
            schema=_power_station_schema,
            device_type=DEVICE_TYPE_POWER_STATION,
            type_label="Power Station",
            default_name="Power Station",
            default_entity_id="lj_power_station",
            defaults={
                CONF_LOAD_PRIORITY: len(self._get_load_entries()) + 1,
                CONF_STATION_MIN_CHARGE_POWER: DEFAULT_STATION_MIN_CHARGE_POWER,
                CONF_STATION_MAX_CHARGE_POWER: DEFAULT_STATION_MAX_CHARGE_POWER,
                CONF_STATION_NORMAL_RESERVE: DEFAULT_STATION_NORMAL_RESERVE,
                CONF_STATION_STORM_RESERVE: DEFAULT_STATION_STORM_RESERVE,
                CONF_CONNECTED_TO_PHASE: "A",
                CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
                CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
            },
            entity_keys=_STATION_ENTITY_KEYS,
            static_keys=(
                CONF_STATION_CHARGE_SPEED_ENTITY_ID,
                CONF_STATION_RESERVE_ENTITY_ID,
            ),
            validate=_check_power_window,
        )

    # ==================== CIRCUIT GROUP CONFIGURATION STEPS ====================

    async def async_step_group_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Circuit group step 1: Name and current limit."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            entity_id = self._data.get(CONF_ENTITY_ID)
            if entity_id and self._entity_id_in_use(entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            else:
                return await self.async_step_group_members()

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=self._data.get(CONF_NAME, "Circuit Group")
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=self._data.get(CONF_ENTITY_ID, "lj_circuit_group"),
                ): str,
                vol.Required(
                    CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
                    default=self._data.get(
                        CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
                        DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 1,
                            "max": 100,
                            "step": 1,
                            "unit_of_measurement": "A",
                            "mode": "box",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="group_config",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_group_members(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Circuit group step 2: Select member loads."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(CONF_CIRCUIT_GROUP_MEMBERS, [])
            if not selected:
                errors["base"] = "no_members_selected"
            else:
                self._data[CONF_CIRCUIT_GROUP_MEMBERS] = selected

                group_name = self._data.get(CONF_NAME, "Circuit Group")
                group_entity_id = self._data.get(CONF_ENTITY_ID, "lj_circuit_group")

                static_data = {
                    CONF_ENTITY_ID: group_entity_id,
                    CONF_NAME: group_name,
                    ENTRY_TYPE: ENTRY_TYPE_GROUP,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_GROUP,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                }
                options_data = {
                    CONF_CIRCUIT_GROUP_CURRENT_LIMIT: self._data.get(
                        CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
                        DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
                    ),
                    CONF_CIRCUIT_GROUP_MEMBERS: selected,
                }

                return self.async_create_entry(
                    title=group_name,
                    data=static_data,
                    options=options_data,
                )

        # Build list of loads on this hub for multi-select
        hub_entry_id = self._data.get(CONF_HUB_ENTRY_ID)
        load_options = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if (
                entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_LOAD
                and entry.data.get(CONF_HUB_ENTRY_ID) == hub_entry_id
            ):
                load_options.append(
                    {
                        "value": entry.entry_id,
                        "label": entry.title,
                    }
                )

        if not load_options:
            errors["base"] = "no_loads_available"

        data_schema = vol.Schema(
            {
                vol.Required(CONF_CIRCUIT_GROUP_MEMBERS): selector(
                    {
                        "select": {
                            "options": load_options,
                            "multiple": True,
                            "mode": "list",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="group_members",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    # ==================== INVERTER CONFIGURATION STEPS ====================

    async def async_step_inverter_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Inverter step 1: name, capacity, topology, output and PV sensors."""
        errors: dict[str, str] = {}
        bad_forecast_entity = None

        if user_input is not None:
            user_input = _normalize_optional_inputs(
                user_input,
                _INVERTER_ENTITY_KEYS + [CONF_SOLAR_PRODUCTION_ENTITY_ID],
            )
            user_input = _normalize_forecast_list(user_input)
            _validate_entity_units(
                self.hass,
                user_input,
                _INVERTER_OUTPUT_UNIT_MAP | _SOLAR_UNIT_MAP,
                errors,
            )
            bad_forecast_entity = _validate_forecast_devices(
                self.hass, user_input, errors
            )
            entity_id = user_input.get(CONF_ENTITY_ID)
            if entity_id and self._entity_id_in_use(entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_inverter_battery()

        defaults = {**self._data, **(user_input or {})}
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=defaults.get(CONF_NAME, "Inverter")
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=defaults.get(CONF_ENTITY_ID, "lj_inverter"),
                ): str,
                **dict(_build_hub_inverter_schema(self.hass, defaults)),
                **dict(_build_inverter_solar_schema(self.hass, defaults)),
            }
        )

        return self.async_show_form(
            step_id="inverter_config",
            data_schema=data_schema,
            errors=errors,
            description_placeholders=(
                {"entity": bad_forecast_entity} if bad_forecast_entity else None
            ),
            last_step=False,
        )

    async def async_step_inverter_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Inverter step 2: the battery behind this inverter (all optional) —
        creates the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = _normalize_optional_inputs(
                user_input,
                [CONF_BATTERY_SOC_ENTITY_ID, CONF_BATTERY_POWER_ENTITY_ID],
            )
            _validate_entity_units(self.hass, user_input, _BATTERY_UNIT_MAP, errors)
            if not errors:
                self._data.update(user_input)
                return await self.async_step_inverter_control()

        data_schema = _inverter_battery_schema(
            self.hass, {**self._data, **(user_input or {})}
        )
        return self.async_show_form(
            step_id="inverter_battery",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_inverter_control(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Inverter step 3: optional battery write-control — creates the entry.

        Skip it (submit empty) to keep the inverter advisory: the clipping
        forecast still publishes its recommended charge limit and max SOC as
        sensors, Load Juggler just doesn't write them anywhere.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = _normalize_optional_inputs(
                user_input,
                [
                    CONF_CHARGE_LIMIT_ENTITY_ID,
                    CONF_BATTERY_VOLTAGE_ENTITY_ID,
                    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
                ],
            )
            user_input = _normalize_soc_limit_list(user_input)
            _validate_entity_units(
                self.hass, user_input, _WRITE_CONTROL_UNIT_MAP, errors
            )
            _validate_charge_limit_unit(self.hass, user_input, errors)
            if not errors:
                self._data.update(user_input)
                _normalize_inverter_power_caps(self._data)

                name = self._data.get(CONF_NAME, "Inverter")
                static_data = {
                    CONF_NAME: name,
                    CONF_ENTITY_ID: self._data.get(CONF_ENTITY_ID),
                    ENTRY_TYPE: ENTRY_TYPE_INVERTER,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_INVERTER,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }
                options_data.pop(CONF_DEVICE_TYPE, None)

                # The hub's entity gating (battery sliders/switch, forecast
                # sensors) depends on which inverter entries exist — nothing
                # reloads a hub when a child appears, so schedule it here.
                # Only when the hub is actually loaded: reloading a not-yet-
                # loaded entry raises, and during startup it reloads anyway.
                hub_entry_id = self._data.get(CONF_HUB_ENTRY_ID)
                hub_entry = (
                    self.hass.config_entries.async_get_entry(hub_entry_id)
                    if hub_entry_id
                    else None
                )
                if (
                    hub_entry is not None
                    and hub_entry.state is config_entries.ConfigEntryState.LOADED
                ):
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(hub_entry_id)
                    )

                return self.async_create_entry(
                    title=_compose_entry_title(name, "Inverter"),
                    data=static_data,
                    options=options_data,
                )

        data_schema = _inverter_control_schema(
            self.hass, {**self._data, **(user_input or {})}
        )
        return self.async_show_form(
            step_id="inverter_control",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    # The legacy hub-level fields the auto-import moves onto an inverter entry:
    # everything physically attached to the inverter, its PV array included.
    # Site policy stays behind on the hub: SOC hysteresis, the SOC target/min
    # sliders, base consumption, forecast SOC floor and the grid export limit.
    _HUB_INVERTER_IMPORT_FIELDS = (
        CONF_SOLAR_PRODUCTION_ENTITY_ID,
        CONF_SOLAR_FORECAST_DEVICE_IDS,
        CONF_SOLAR_FORECAST_ENTITY_IDS,
        CONF_INVERTER_MAX_POWER,
        CONF_INVERTER_MAX_POWER_PER_PHASE,
        CONF_INVERTER_SUPPORTS_ASYMMETRIC,
        CONF_WIRING_TOPOLOGY,
        CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
        CONF_BATTERY_SOC_ENTITY_ID,
        CONF_BATTERY_POWER_ENTITY_ID,
        CONF_BATTERY_MAX_CHARGE_POWER,
        CONF_BATTERY_MAX_DISCHARGE_POWER,
        CONF_BATTERY_SOC_FULL,
        CONF_BATTERY_CAPACITY_KWH,
    )

    def _blank_hub_legacy_inverter_fields(self, hub_entry) -> None:
        """Strip the imported fields from the hub entry and set the imported
        flag — the hub must stop acting as the implicit legacy fleet member
        the moment the standalone inverter entry represents the hardware."""
        new_data = dict(hub_entry.data)
        new_data[MIGRATE_HUB_INVERTER_IMPORTED_FLAG] = True
        for key in self._HUB_INVERTER_IMPORT_FIELDS:
            new_data.pop(key, None)
        new_options = {
            k: v
            for k, v in hub_entry.options.items()
            if k not in self._HUB_INVERTER_IMPORT_FIELDS
        }
        self.hass.config_entries.async_update_entry(
            hub_entry, data=new_data, options=new_options
        )

    def _imported_inverter_name(self, imported: dict) -> str:
        """Name the auto-created entry after the hardware it represents.

        The hub's own name makes a poor inverter name ("Site Load Management
        Inverter"), so look at the device behind the first hardware entity
        being imported — usually the inverter's integration device, which is
        already called something like "Deye Hybrid" or "SolarEdge SE17K".
        Falls back to plain "Inverter", the same default the manual flow uses.
        """
        entity_registry = async_get_entity_registry(self.hass)
        device_registry = async_get_device_registry(self.hass)
        for key in (
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
            CONF_BATTERY_SOC_ENTITY_ID,
            CONF_BATTERY_POWER_ENTITY_ID,
            CONF_SOLAR_PRODUCTION_ENTITY_ID,
        ):
            entity_id = imported.get(key)
            if not entity_id:
                continue
            registry_entry = entity_registry.async_get(entity_id)
            if registry_entry is None or not registry_entry.device_id:
                continue
            device = device_registry.async_get(registry_entry.device_id)
            if device is None:
                continue
            device_name = device.name_by_user or device.name
            if device_name:
                return prettify_name(device_name)
        return "Inverter"

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Auto-import of a hub's legacy inverter/battery/PV fields onto a
        standalone inverter entry (spawned from _setup_hub_entry).

        Runs whenever such a field is still on the hub, so a later release
        that moves one more field (the solar sensor and forecast devices, say)
        converges on the next restart without a second migration path. When
        the hub's inverter entry already exists, the newly-found fields are
        merged into it rather than creating a second one.

        Idempotency: the unique_id makes a duplicate entry impossible, and the
        hub is blanked-and-flagged BEFORE the already-configured abort — so a
        restart between entry creation and blanking still converges instead of
        double-counting the battery (the engine's implicit legacy member and
        the entry would otherwise both exist).
        """
        hub_entry_id = import_data.get("hub_entry_id")
        hub_entry = (
            self.hass.config_entries.async_get_entry(hub_entry_id)
            if hub_entry_id
            else None
        )
        if hub_entry is None:
            return self.async_abort(reason="entry_not_found")

        unique_id = f"{hub_entry_id}_inverter_import"
        await self.async_set_unique_id(unique_id)

        # Snapshot the legacy fields, then blank the hub — in this order, and
        # before the duplicate check, so every path leaves the hub clean.
        imported = {
            key: get_entry_value(hub_entry, key, None)
            for key in self._HUB_INVERTER_IMPORT_FIELDS
        }
        imported = {k: v for k, v in imported.items() if v is not None}
        self._blank_hub_legacy_inverter_fields(hub_entry)

        # An entry from an earlier import round already represents this
        # hardware — merge the fields this round found onto it. Existing
        # values win: the user may have edited them on the inverter since.
        existing = next(
            (
                entry
                for entry in self.hass.config_entries.async_entries(DOMAIN)
                if entry.unique_id == unique_id
            ),
            None,
        )
        if existing is not None:
            merged = {**imported, **existing.options}
            if merged != dict(existing.options):
                _LOGGER.info(
                    "Moved leftover hub fields (%s) onto the existing inverter "
                    "entry %s",
                    ", ".join(sorted(imported)) or "none",
                    existing.title,
                )
                self.hass.config_entries.async_update_entry(existing, options=merged)
            return self.async_abort(reason="already_configured")

        hub_name = hub_entry.data.get(CONF_NAME, hub_entry.title)
        hub_prefix = hub_entry.data.get(CONF_ENTITY_ID, "lj_hub")
        name = self._imported_inverter_name(imported)
        static_data = {
            CONF_NAME: name,
            CONF_ENTITY_ID: f"{hub_prefix}_inverter",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_DEVICE_TYPE: DEVICE_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub_entry_id,
            "imported_from_hub": True,
        }
        _LOGGER.info(
            "Imported legacy hub inverter/battery config of %s into a new "
            "inverter entry '%s' (%s)",
            hub_name,
            name,
            ", ".join(sorted(imported)) or "no fields",
        )
        return self.async_create_entry(
            title=_compose_entry_title(name, "Inverter"),
            data=static_data,
            options=imported,
        )

    # ==================== EVSE CHARGER CONFIGURATION STEPS ====================

    async def async_step_discover_chargers(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 2: Discover OCPP chargers."""
        errors: dict[str, str] = {}

        # Find OCPP devices
        self._discovered_chargers = await self._discover_ocpp_chargers()

        if not self._discovered_chargers:
            errors["base"] = "no_ocpp_chargers_found"
            return self.async_show_form(
                step_id="discover_chargers",
                data_schema=vol.Schema({}),
                errors=errors,
                last_step=True,
            )

        if user_input is not None:
            selected_charger_id = user_input.get("charger")
            self._selected_charger = next(
                (c for c in self._discovered_chargers
                 if c["id"] == selected_charger_id),
                None,
            )
            if self._selected_charger is not None:
                return await self.async_step_charger_info()
            # Selected charger is no longer among the discovered set (it
            # disappeared between rendering and submitting the form). Re-show
            # the form rather than proceeding with _selected_charger = None,
            # which would crash the downstream steps.
            errors["base"] = "charger_not_found"

        charger_options = [
            {"value": charger["id"], "label": charger["name"]}
            for charger in self._discovered_chargers
        ]

        data_schema = vol.Schema(
            {
                vol.Required("charger"): selector(
                    {"select": {"options": charger_options}}
                )
            }
        )

        return self.async_show_form(
            step_id="discover_chargers",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def _discover_ocpp_chargers(self) -> list:
        """Discover OCPP chargers from the OCPP integration.

        Thin wrapper around the module-level ``scan_ocpp_chargers``, which the
        automatic discovery in ``__init__.py`` uses too.
        """
        return scan_ocpp_chargers(self.hass)

    async def async_step_charger_info(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3a: Name, entity ID, priority, and the OCPP device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = dict(user_input)
            # The picker is flow-only: what the entry stores is the charge point
            # id the picked device resolves to, never its registry UUID.
            picked_device = user_input.pop(FIELD_OCPP_DEVICE, None)
            self._data.update(user_input)

            if picked_device:
                resolved = ocpp_charger_for_device(self.hass, picked_device)
                if resolved is None:
                    errors[FIELD_OCPP_DEVICE] = "ocpp_device_not_usable"
                else:
                    # Everything about the charger's OCPP side follows the
                    # picked device — charge point id and every sensor entity,
                    # from the same derivation the discovery scan uses. Only
                    # "id" stays put: the discovery unique_id and
                    # CONF_CHARGER_ID were already claimed on it.
                    self._selected_charger = {
                        **self._selected_charger,
                        **resolved,
                        "id": self._selected_charger["id"],
                    }
                    self._data[CONF_OCPP_DEVICE_ID] = resolved["device_id"]

            entity_id = self._data.get(CONF_ENTITY_ID)
            if entity_id and self._entity_id_in_use(entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            if not errors:
                return await self.async_step_charger_current()

        existing_loads = self._get_load_entries()
        next_priority = len(existing_loads) + 1

        data_schema = _charger_info_schema(
            {
                CONF_NAME: self._data.get(
                    CONF_NAME, self._selected_charger["name"]
                ),
                CONF_ENTITY_ID: self._data.get(
                    CONF_ENTITY_ID, f"lj_{self._selected_charger['id']}"
                ),
                CONF_LOAD_PRIORITY: self._data.get(
                    CONF_LOAD_PRIORITY, next_priority
                ),
                FIELD_OCPP_DEVICE: self._selected_charger.get("ha_device_id"),
            }
        )

        # Build list of detected entities for display
        detected_entities = []
        if self._selected_charger.get("device_id"):
            detected_entities.append(
                f"OCPP Device ID: {self._selected_charger['device_id']}"
            )
        if self._selected_charger.get("current_import_entity"):
            detected_entities.append(
                f"Current Import: {self._selected_charger['current_import_entity']}"
            )
        if self._selected_charger.get("current_import_l1_entity"):
            detected_entities.append(
                f"Current Import L1: {self._selected_charger['current_import_l1_entity']}"
            )
        if self._selected_charger.get("current_import_l2_entity"):
            detected_entities.append(
                f"Current Import L2: {self._selected_charger['current_import_l2_entity']}"
            )
        if self._selected_charger.get("current_import_l3_entity"):
            detected_entities.append(
                f"Current Import L3: {self._selected_charger['current_import_l3_entity']}"
            )
        if self._selected_charger.get("current_offered_entity"):
            detected_entities.append(
                f"Current Offered: {self._selected_charger['current_offered_entity']}"
            )
        if self._selected_charger.get("power_offered_entity"):
            detected_entities.append(
                f"Power Offered: {self._selected_charger['power_offered_entity']}"
            )
        if self._selected_charger.get("power_import_entity"):
            detected_entities.append(
                f"Power Import: {self._selected_charger['power_import_entity']}"
            )

        entities_text = "\n- ".join(detected_entities) if detected_entities else "None"

        return self.async_show_form(
            step_id="charger_info",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "charger_name": self._selected_charger["name"],
                "detected_entities": entities_text,
            },
            last_step=False,
        )

    async def async_step_charger_current(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3b: Current limits and phase mapping."""
        errors: dict[str, str] = {}
        hub_phases = _hub_phase_count(self.hass, self._data.get(CONF_HUB_ENTRY_ID))

        if user_input is not None:
            self._data.update(user_input)
            # Auto-fill hidden phase mappings to match L1 (prevents mask mismatch)
            l1 = self._data.get(CONF_CHARGER_L1_PHASE, "A")
            if hub_phases < 2:
                self._data[CONF_CHARGER_L2_PHASE] = l1
            if hub_phases < 3:
                self._data[CONF_CHARGER_L3_PHASE] = l1

            validate_charger_settings(self._data, errors)
            if errors:
                return self.async_show_form(
                    step_id="charger_current",
                    data_schema=_charger_current_schema(
                        self._data, hub_phases=hub_phases
                    ),
                    errors=errors,
                    last_step=False,
                )

            return await self.async_step_charger_timing()

        data_schema = _charger_current_schema(
            {
                CONF_EVSE_MINIMUM_CHARGE_CURRENT: DEFAULT_MIN_CHARGE_CURRENT,
                CONF_EVSE_MAXIMUM_CHARGE_CURRENT: DEFAULT_MAX_CHARGE_CURRENT,
                CONF_CHARGER_L1_PHASE: "A",
                CONF_CHARGER_L2_PHASE: "B",
                CONF_CHARGER_L3_PHASE: "C",
            },
            hub_phases=hub_phases,
        )

        return self.async_show_form(
            step_id="charger_current",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_charger_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3c: Units and timing configuration (final — creates entry)."""
        errors: dict[str, str] = {}

        # The OCPP device ID may have been edited on the charger_info step
        # (the timing step has no such field), so it lives in self._data.
        # Fall back to the detected value from discovery.
        detected_device_id = (
            self._selected_charger.get("device_id")
            if self._selected_charger
            else None
        )
        ocpp_device_id = self._data.get(CONF_OCPP_DEVICE_ID) or detected_device_id

        # Detect charger capabilities via OCPP
        detected_unit = await _detect_charge_rate_unit(self.hass, ocpp_device_id)
        detected_interval = await _detect_meter_value_interval(
            self.hass, ocpp_device_id
        )

        if user_input is not None:
            self._data.update(user_input)

            self._data[ENTRY_TYPE] = ENTRY_TYPE_LOAD
            self._data[CONF_CHARGER_ID] = self._selected_charger["id"]
            # Keep the OCPP device ID resolved above (user edit or detected).
            self._data[CONF_OCPP_DEVICE_ID] = ocpp_device_id
            self._data[CONF_EVSE_CURRENT_IMPORT_ENTITY_ID] = self._selected_charger[
                "current_import_entity"
            ]
            self._data[CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID] = (
                self._selected_charger.get("current_import_l1_entity")
            )
            self._data[CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID] = (
                self._selected_charger.get("current_import_l2_entity")
            )
            self._data[CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID] = (
                self._selected_charger.get("current_import_l3_entity")
            )
            self._data[CONF_EVSE_CURRENT_OFFERED_ENTITY_ID] = self._selected_charger[
                "current_offered_entity"
            ]
            self._data[CONF_EVSE_POWER_OFFERED_ENTITY_ID] = self._selected_charger.get(
                "power_offered_entity"
            )
            self._data[CONF_EVSE_POWER_IMPORT_ENTITY_ID] = self._selected_charger.get(
                "power_import_entity"
            )

            # Use user-provided name/entity_id from charger_info step
            charger_name = self._data.get(CONF_NAME, self._selected_charger["name"])
            charger_entity_id = self._data.get(
                CONF_ENTITY_ID, f"lj_{self._selected_charger['id']}"
            )
            self._data[CONF_MIN_CURRENT_ENTITY_ID] = (
                f"number.{charger_entity_id}_min_current"
            )
            self._data[CONF_MAX_CURRENT_ENTITY_ID] = (
                f"number.{charger_entity_id}_max_current"
            )

            # Split static vs mutable fields for charger
            static_data = {
                CONF_ENTITY_ID: charger_entity_id,
                CONF_NAME: charger_name,
                ENTRY_TYPE: ENTRY_TYPE_LOAD,
                CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                CONF_CHARGER_ID: self._data.get(CONF_CHARGER_ID),
                CONF_OCPP_DEVICE_ID: self._data.get(CONF_OCPP_DEVICE_ID),
                CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: self._data.get(
                    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID
                ),
                CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID: self._data.get(
                    CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID
                ),
                CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID: self._data.get(
                    CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID
                ),
                CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID: self._data.get(
                    CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID
                ),
                CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: self._data.get(
                    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID
                ),
                CONF_EVSE_POWER_OFFERED_ENTITY_ID: self._data.get(
                    CONF_EVSE_POWER_OFFERED_ENTITY_ID
                ),
                CONF_EVSE_POWER_IMPORT_ENTITY_ID: self._data.get(
                    CONF_EVSE_POWER_IMPORT_ENTITY_ID
                ),
            }
            options_data = {k: v for k, v in self._data.items() if k not in static_data}

            return self.async_create_entry(
                title=_compose_entry_title(charger_name, "Charger"),
                data=static_data,
                options=options_data,
            )

        data_schema = _charger_timing_schema(
            {
                CONF_PROFILE_VALIDITY_MODE: DEFAULT_PROFILE_VALIDITY_MODE,
                CONF_UPDATE_FREQUENCY: detected_interval or DEFAULT_UPDATE_FREQUENCY,
                CONF_OCPP_PROFILE_TIMEOUT: DEFAULT_OCPP_PROFILE_TIMEOUT,
                CONF_CHARGE_PAUSE_DURATION: DEFAULT_CHARGE_PAUSE_DURATION,
                CONF_STACK_LEVEL: DEFAULT_STACK_LEVEL,
            },
            detected_unit=detected_unit,
        )

        return self.async_show_form(
            step_id="charger_timing",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Get the options flow for this handler."""
        return LoadJugglerOptionsFlow()
