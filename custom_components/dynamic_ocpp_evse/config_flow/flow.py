"""Load Juggler - the config flow handler: everything up to a created entry.

``LoadJugglerConfigFlow`` drives initial setup: the hub, inverter, group and
per-device-type creation steps, the normalize/auto-detect helpers behind them,
OCPP discovery with its unit and meter-interval probes, and the legacy
hub-inverter import. The forms themselves come from ``schemas.py``; editing an
entry afterwards is ``options.py``.

Moved verbatim out of the single-file config_flow.py.
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
    CHARGE_RATE_UNIT_AMPS,
    CHARGE_RATE_UNIT_WATTS,
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
    CONF_CHARGER_PRIORITY,
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
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
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
    CONF_SOC_LIMIT_ENTITY_IDS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_SOLAR_GRACE_PERIOD,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_STACK_LEVEL,
    CONF_STATION_AC_INPUT_ENTITY_ID,
    CONF_STATION_AC_OUTPUT_ENTITY_ID,
    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
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
    ENTRY_TYPE_CHARGER,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
    MIGRATE_HUB_INVERTER_IMPORTED_FLAG,
)
from ..detection_patterns import PHASE_PATTERNS, PLUG_POWER_MONITOR_PATTERNS
from ..helpers import (
    get_entry_value,
    normalize_optional_entity,
    prettify_name,
    validate_charger_settings,
)
from .helpers import (
    _CURRENT_UNITS,
    _LOGGER,
    _POWER_UNITS,
    _SOC_UNITS,
    _compose_entry_title,
    _hub_phase_count,
    _validate_entity_units,
    _validate_forecast_devices,
    scan_ocpp_chargers,
)
from .schemas import SchemaBuilderMixin

class LoadJugglerConfigFlow(
    SchemaBuilderMixin, config_entries.ConfigFlow, domain=DOMAIN
):
    """Handle a config flow for Load Juggler."""

    VERSION = 2
    MINOR_VERSION = 4

    def __init__(self):
        self._data = {}
        self._discovered_chargers = []
        self._selected_charger = None
        self._entity_cache = None

    def _get_entity_registry_ids(self) -> list[str]:
        if self._entity_cache is None:
            entity_registry = async_get_entity_registry(self.hass)
            # Only include entities that are present in the state machine.
            # Disabled/stale registry entries have no state and should not be
            # offered as auto-detection candidates (they would end up as a
            # suggested_value that is not in include_entities, breaking submission).
            self._entity_cache = [
                eid
                for eid in entity_registry.entities.keys()
                if self.hass.states.get(eid) is not None
            ]
        return self._entity_cache

    def _get_hub_phase_count(self, hub_entry_id: str | None = None) -> int:
        """Number of phases this site has, as the engine sees it.

        Thin wrapper around the module-level ``_hub_phase_count`` so the flow
        and the read-only pages share one derivation.
        """
        return _hub_phase_count(
            self.hass, hub_entry_id or self._data.get(CONF_HUB_ENTRY_ID)
        )

    # Optional entity keys grouped by config step (for entity selector clearing)
    _GRID_ENTITY_KEYS = [
        CONF_PHASE_A_CURRENT_ENTITY_ID,
        CONF_PHASE_B_CURRENT_ENTITY_ID,
        CONF_PHASE_C_CURRENT_ENTITY_ID,
        CONF_MAX_IMPORT_POWER_ENTITY_ID,
    ]
    _BATTERY_ENTITY_KEYS = [
        CONF_SOLAR_PRODUCTION_ENTITY_ID,
        CONF_BATTERY_SOC_ENTITY_ID,
        CONF_BATTERY_POWER_ENTITY_ID,
    ]
    _INVERTER_ENTITY_KEYS = [
        CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    ]
    _PLUG_ENTITY_KEYS = [CONF_PLUG_POWER_MONITOR_ENTITY_ID]
    _TANK_ENTITY_KEYS = [CONF_TANK_POWER_ENTITY_ID, CONF_TANK_POWER_DEVICE_ID]
    _STATION_ENTITY_KEYS = [
        CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
        CONF_STATION_AC_INPUT_ENTITY_ID,
        CONF_STATION_AC_OUTPUT_ENTITY_ID,
    ]

    def _normalize_optional_inputs(
        self, data: dict[str, Any], step_entity_keys: list[str] | None = None
    ) -> dict[str, Any]:
        """Normalize optional entity inputs.

        Args:
            data: The user_input from the form step.
            step_entity_keys: Optional entity keys expected in this step.
                Keys missing from data are set to None (user cleared the field).
        """
        normalized = dict(data)
        for key in (
            self._GRID_ENTITY_KEYS
            + self._BATTERY_ENTITY_KEYS
            + self._INVERTER_ENTITY_KEYS
            + self._PLUG_ENTITY_KEYS
            + self._TANK_ENTITY_KEYS
        ):
            if key in normalized:
                normalized[key] = normalize_optional_entity(normalized.get(key))
        # Entity selectors omit unselected fields — explicitly clear them
        if step_entity_keys:
            for key in step_entity_keys:
                if key not in normalized:
                    normalized[key] = None
        return normalized

    def _normalize_forecast_list(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalize the solar forecast device list (battery step).

        Separate from _normalize_optional_inputs, which is per-key scalar: the
        multi-device selector yields a list and omits the key entirely when
        cleared, so an emptied selection must become [] (feature off), not a
        stale stored value. Submitting the form also drops any legacy
        directly-configured sensor list — the device selection replaces it.
        """
        data[CONF_SOLAR_FORECAST_DEVICE_IDS] = [
            d for d in (data.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or []) if d
        ]
        data[CONF_SOLAR_FORECAST_ENTITY_IDS] = []
        return data

    def _normalize_soc_limit_list(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalize the SOC-ceiling target list (inverter write-control step).

        Same reason as the forecast list above and not the scalar path: a
        multi-entity selector yields a list and omits the key entirely once the
        user clears it, so an emptied selection must become [] — which is what
        removes the Battery SOC Control switch and sensor again — rather than
        leaving the previously stored slots armed.
        """
        data[CONF_SOC_LIMIT_ENTITY_IDS] = [
            e for e in (data.get(CONF_SOC_LIMIT_ENTITY_IDS) or []) if e
        ]
        return data

    def _normalize_inverter_powers(self):
        """Normalize inverter power values: 0 means 'not configured' → store as None."""
        for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
            if key in self._data and self._data[key] == 0:
                self._data[key] = None

    def _auto_detect_phase_entities(
        self, pattern_sets: list[dict]
    ) -> dict[str, str | None]:
        """Auto-detect a matching set of phase A/B/C entities from pattern sets.

        Returns dict with keys 'phase_a', 'phase_b', 'phase_c' (values may be None).
        """
        entity_ids = self._get_entity_registry_ids()
        for pattern_set in pattern_sets:
            a = next(
                (
                    eid
                    for eid in entity_ids
                    if re.match(pattern_set["patterns"]["phase_a"], eid)
                ),
                None,
            )
            b = next(
                (
                    eid
                    for eid in entity_ids
                    if re.match(pattern_set["patterns"]["phase_b"], eid)
                ),
                None,
            )
            c = next(
                (
                    eid
                    for eid in entity_ids
                    if re.match(pattern_set["patterns"]["phase_c"], eid)
                ),
                None,
            )
            if a and b and c:
                return {"phase_a": a, "phase_b": b, "phase_c": c}
        return {"phase_a": None, "phase_b": None, "phase_c": None}

    def _auto_detect_entity(self, pattern_sets: list[dict]) -> str | None:
        """Auto-detect a single entity from pattern sets. Returns first match."""
        entity_ids = self._get_entity_registry_ids()
        for pattern_set in pattern_sets:
            match = next(
                (eid for eid in entity_ids if re.match(pattern_set["pattern"], eid)),
                None,
            )
            if match:
                return match
        return None

    def _auto_detect_entity_value(
        self, pattern_sets: list[dict], factor: float = 1.0
    ) -> int | None:
        """Auto-detect an entity and read its numeric state value.

        Returns int(state * factor), or None if not found / not numeric.
        """
        entity_id = self._auto_detect_entity(pattern_sets)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if not state:
            return None
        try:
            return int(float(state.state) * factor)
        except (ValueError, TypeError):
            return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Initial step: choose hub, EVSE charger, or smart outlet."""
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

    def _get_charger_entries(self) -> list:
        """Get all charger config entries."""
        return [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
        ]

    # ==================== HUB CONFIGURATION STEPS ====================

    def _entity_id_in_use(self, entity_id: str) -> bool:
        """True if entity_id is already used by another Load Juggler config entry."""
        return any(
            entry.data.get(CONF_ENTITY_ID) == entity_id
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )

    def _resolve_device_power_entity(self, device_id: str) -> str | None:
        """Return the first power-class sensor entity belonging to a device."""
        entity_registry = async_get_entity_registry(self.hass)
        for entity in entity_registry.entities.values():
            if entity.device_id != device_id:
                continue
            device_class = entity.device_class or entity.original_device_class
            if device_class == "power":
                return entity.entity_id
        return None

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
            user_input = self._normalize_optional_inputs(
                user_input, self._GRID_ENTITY_KEYS
            )
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_PHASE_A_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
                    CONF_PHASE_B_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
                    CONF_PHASE_C_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
                    CONF_MAX_IMPORT_POWER_ENTITY_ID: _POWER_UNITS,
                },
                errors,
            )
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
                data_schema=self._hub_grid_schema(user_input),
                errors=errors,
                last_step=True,
            )

        try:
            # Try to find a complete set of phases using pattern sets
            ct_detected = self._auto_detect_phase_entities(PHASE_PATTERNS)
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

            data_schema = self._hub_grid_schema(
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

    # ==================== CHARGER CONFIGURATION STEPS ====================

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
        """Charger step 1: Select which hub to add charger to."""
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

    async def async_step_plug_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Smart load configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._PLUG_ENTITY_KEYS
            )
            self._data.update(user_input)

            plug_name = self._data.get(CONF_NAME, "Smart Load")
            plug_entity_id = self._data.get(CONF_ENTITY_ID, "lj_smart_load")

            if self._entity_id_in_use(plug_entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            else:
                static_data = {
                    CONF_ENTITY_ID: plug_entity_id,
                    CONF_NAME: plug_name,
                    ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_PLUG,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                    CONF_PLUG_SWITCH_ENTITY_ID: self._data.get(
                        CONF_PLUG_SWITCH_ENTITY_ID
                    ),
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }

                return self.async_create_entry(
                    title=_compose_entry_title(plug_name, "Smart Load"),
                    data=static_data,
                    options=options_data,
                )

        existing_chargers = self._get_charger_entries()
        next_priority = len(existing_chargers) + 1

        # Name + entity_id fields, then the plug-specific schema. self._data is
        # merged last so a validation-error re-show keeps the user's input.
        plug_defaults = {
            CONF_CHARGER_PRIORITY: next_priority,
            CONF_PLUG_POWER_RATING: DEFAULT_PLUG_POWER_RATING,
            CONF_PLUG_MAX_CURRENT: DEFAULT_PLUG_MAX_CURRENT,
            CONF_CONNECTED_TO_PHASE: "A",
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_PLUG_POWER_MONITOR_ENTITY_ID: self._auto_detect_entity(
                PLUG_POWER_MONITOR_PATTERNS
            ),
        }
        plug_defaults.update(self._data)
        name_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=plug_defaults.get(CONF_NAME, "Smart Load")
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=plug_defaults.get(CONF_ENTITY_ID, "lj_smart_load"),
                ): str,
            }
        )
        plug_fields = self._plug_schema(plug_defaults)
        # Merge both schemas
        combined = vol.Schema({**name_schema.schema, **plug_fields.schema})

        return self.async_show_form(
            step_id="plug_config",
            data_schema=combined,
            errors=errors,
            last_step=True,
        )

    async def async_step_hot_water_tank_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hot water tank configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._TANK_ENTITY_KEYS
            )
            self._data.update(user_input)

            # A picked power device is resolved to its power-sensor entity now,
            # so runtime only ever deals with CONF_TANK_POWER_ENTITY_ID.
            device_id = self._data.pop(CONF_TANK_POWER_DEVICE_ID, None)
            if device_id and not self._data.get(CONF_TANK_POWER_ENTITY_ID):
                resolved = self._resolve_device_power_entity(device_id)
                if resolved:
                    self._data[CONF_TANK_POWER_ENTITY_ID] = resolved

            tank_name = self._data.get(CONF_NAME, "Hot Water Tank")
            tank_entity_id = self._data.get(CONF_ENTITY_ID, "lj_hot_water_tank")

            if self._entity_id_in_use(tank_entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            else:
                static_data = {
                    CONF_ENTITY_ID: tank_entity_id,
                    CONF_NAME: tank_name,
                    ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_HOT_WATER_TANK,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                    CONF_CLIMATE_ENTITY_ID: self._data.get(CONF_CLIMATE_ENTITY_ID),
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }
                return self.async_create_entry(
                    title=_compose_entry_title(tank_name, "Hot Water Tank"),
                    data=static_data,
                    options=options_data,
                )

        # Defaults; self._data is merged last so a validation-error re-show
        # keeps the user's input.
        tank_defaults = {
            CONF_CHARGER_PRIORITY: len(self._get_charger_entries()) + 1,
            CONF_HEATING_ELEMENT_POWER: DEFAULT_HEATING_ELEMENT_POWER,
            CONF_TANK_AWAY_TEMPERATURE: DEFAULT_TANK_AWAY_TEMPERATURE,
            CONF_TANK_NORMAL_TEMPERATURE: DEFAULT_TANK_NORMAL_TEMPERATURE,
            CONF_TANK_BOOST_TEMPERATURE: DEFAULT_TANK_BOOST_TEMPERATURE,
            CONF_CONNECTED_TO_PHASE: "A",
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
        }
        tank_defaults.update(self._data)
        name_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=tank_defaults.get(CONF_NAME, "Hot Water Tank"),
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=tank_defaults.get(CONF_ENTITY_ID, "lj_hot_water_tank"),
                ): str,
            }
        )
        tank_fields = self._hot_water_tank_schema(tank_defaults)
        combined = vol.Schema({**name_schema.schema, **tank_fields.schema})

        return self.async_show_form(
            step_id="hot_water_tank_config",
            data_schema=combined,
            errors=errors,
            last_step=True,
        )

    async def async_step_power_station_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Portable power station configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._STATION_ENTITY_KEYS
            )
            self._data.update(user_input)

            station_name = self._data.get(CONF_NAME, "Power Station")
            station_entity_id = self._data.get(CONF_ENTITY_ID, "lj_power_station")

            min_power = self._data.get(
                CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER
            )
            max_power = self._data.get(
                CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
            )
            if self._entity_id_in_use(station_entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            elif max_power < min_power:
                errors[CONF_STATION_MAX_CHARGE_POWER] = "station_max_below_min"
            else:
                static_data = {
                    CONF_ENTITY_ID: station_entity_id,
                    CONF_NAME: station_name,
                    ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_POWER_STATION,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                    CONF_STATION_CHARGE_SPEED_ENTITY_ID: self._data.get(
                        CONF_STATION_CHARGE_SPEED_ENTITY_ID
                    ),
                    CONF_STATION_RESERVE_ENTITY_ID: self._data.get(
                        CONF_STATION_RESERVE_ENTITY_ID
                    ),
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }
                return self.async_create_entry(
                    title=_compose_entry_title(station_name, "Power Station"),
                    data=static_data,
                    options=options_data,
                )

        # Defaults; self._data is merged last so a validation-error re-show
        # keeps the user's input.
        station_defaults = {
            CONF_CHARGER_PRIORITY: len(self._get_charger_entries()) + 1,
            CONF_STATION_MIN_CHARGE_POWER: DEFAULT_STATION_MIN_CHARGE_POWER,
            CONF_STATION_MAX_CHARGE_POWER: DEFAULT_STATION_MAX_CHARGE_POWER,
            CONF_STATION_NORMAL_RESERVE: DEFAULT_STATION_NORMAL_RESERVE,
            CONF_STATION_STORM_RESERVE: DEFAULT_STATION_STORM_RESERVE,
            CONF_CONNECTED_TO_PHASE: "A",
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
        }
        station_defaults.update(self._data)
        name_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=station_defaults.get(CONF_NAME, "Power Station"),
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=station_defaults.get(CONF_ENTITY_ID, "lj_power_station"),
                ): str,
            }
        )
        station_fields = self._power_station_schema(station_defaults)
        combined = vol.Schema({**name_schema.schema, **station_fields.schema})

        return self.async_show_form(
            step_id="power_station_config",
            data_schema=combined,
            errors=errors,
            last_step=True,
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
                entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
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
            user_input = self._normalize_optional_inputs(
                user_input,
                self._INVERTER_ENTITY_KEYS + [CONF_SOLAR_PRODUCTION_ENTITY_ID],
            )
            user_input = self._normalize_forecast_list(user_input)
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_SOLAR_PRODUCTION_ENTITY_ID: _POWER_UNITS,
                },
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
                **dict(self._build_hub_inverter_schema(defaults)),
                **dict(self._build_inverter_solar_schema(defaults)),
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
            user_input = self._normalize_optional_inputs(
                user_input,
                [CONF_BATTERY_SOC_ENTITY_ID, CONF_BATTERY_POWER_ENTITY_ID],
            )
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_BATTERY_POWER_ENTITY_ID: _POWER_UNITS,
                    CONF_BATTERY_SOC_ENTITY_ID: _SOC_UNITS,
                },
                errors,
            )
            if not errors:
                self._data.update(user_input)
                return await self.async_step_inverter_control()

        data_schema = self._inverter_battery_schema({**self._data, **(user_input or {})})
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
            user_input = self._normalize_optional_inputs(
                user_input,
                [
                    CONF_CHARGE_LIMIT_ENTITY_ID,
                    CONF_BATTERY_VOLTAGE_ENTITY_ID,
                    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
                ],
            )
            user_input = self._normalize_soc_limit_list(user_input)
            if not errors:
                self._data.update(user_input)
                # 0 means "not configured" for the power caps, as on the hub
                for key in (CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE):
                    if self._data.get(key) == 0:
                        self._data[key] = None

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

        data_schema = self._inverter_control_schema({**self._data, **(user_input or {})})
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

    async def _detect_charge_rate_unit(self, ocpp_device_id: str) -> str | None:
        """
        Detect the charge rate unit supported by the OCPP charger.

        Queries the charger via OCPP GetConfiguration for the
        ChargingScheduleAllowedChargingRateUnit key.

        Returns:
            "A" for Amperes, "W" for Watts, None if detection fails.
        """
        if not ocpp_device_id:
            _LOGGER.debug("No OCPP device ID — cannot detect charge rate unit")
            return None

        if not self.hass.services.has_service("ocpp", "get_configuration"):
            _LOGGER.debug("ocpp.get_configuration service not available")
            return None

        try:
            response = await self.hass.services.async_call(
                "ocpp",
                "get_configuration",
                {
                    "devid": ocpp_device_id,
                    "ocpp_key": "ChargingScheduleAllowedChargingRateUnit",
                },
                blocking=True,
                return_response=True,
            )

            if not response:
                _LOGGER.debug("Empty response from ocpp.get_configuration")
                return None

            # Parse the response — handle multiple possible formats
            value = None
            if isinstance(response, dict):
                # Direct key-value: {"ChargingScheduleAllowedChargingRateUnit": "Current"}
                value = response.get("ChargingScheduleAllowedChargingRateUnit")
                # Or nested: {"value": "Current"}
                if value is None:
                    value = response.get("value")
                # Or list format: {"configurationKey": [{"key": ..., "value": ...}]}
                if value is None:
                    for item in response.get("configurationKey", []):
                        if (
                            isinstance(item, dict)
                            and item.get("key")
                            == "ChargingScheduleAllowedChargingRateUnit"
                        ):
                            value = item.get("value")
                            break

            if not value:
                _LOGGER.debug(
                    "Could not parse charge rate unit from OCPP response: %s", response
                )
                return None

            value = str(value).strip()
            value_lower = value.lower()
            _LOGGER.info("OCPP ChargingScheduleAllowedChargingRateUnit = %s", value)

            if "current" in value_lower and "power" in value_lower:
                return CHARGE_RATE_UNIT_AMPS  # Both supported — prefer Amps
            elif "power" in value_lower:
                return CHARGE_RATE_UNIT_WATTS
            elif "current" in value_lower:
                return CHARGE_RATE_UNIT_AMPS
            else:
                _LOGGER.warning(
                    "Unrecognised ChargingScheduleAllowedChargingRateUnit value: %s",
                    value,
                )
                return None

        except Exception as e:
            _LOGGER.warning("Could not detect charge rate unit via OCPP: %s", e)
            return None

    async def _detect_meter_value_interval(self, ocpp_device_id: str) -> int | None:
        """Detect the MeterValueSampleInterval from the OCPP charger.

        This tells us how often the charger reports meter values, which is the
        practical minimum interval for sending charging profile updates.

        Returns:
            Interval in seconds, or None if detection fails.
        """
        if not ocpp_device_id:
            return None

        if not self.hass.services.has_service("ocpp", "get_configuration"):
            return None

        try:
            response = await self.hass.services.async_call(
                "ocpp",
                "get_configuration",
                {
                    "devid": ocpp_device_id,
                    "ocpp_key": "MeterValueSampleInterval",
                },
                blocking=True,
                return_response=True,
            )

            if not response:
                return None

            value = None
            if isinstance(response, dict):
                value = response.get("MeterValueSampleInterval")
                if value is None:
                    value = response.get("value")
                if value is None:
                    for item in response.get("configurationKey", []):
                        if (
                            isinstance(item, dict)
                            and item.get("key") == "MeterValueSampleInterval"
                        ):
                            value = item.get("value")
                            break

            if value is None:
                return None

            interval = int(value)
            _LOGGER.info("OCPP MeterValueSampleInterval = %ds", interval)
            # Clamp to our supported range (5–300s)
            return max(5, min(300, interval))

        except Exception as e:
            _LOGGER.debug("Could not detect MeterValueSampleInterval via OCPP: %s", e)
            return None

    async def async_step_charger_info(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3a: Name, entity ID, and priority."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            entity_id = self._data.get(CONF_ENTITY_ID)
            if entity_id and self._entity_id_in_use(entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            else:
                return await self.async_step_charger_current()

        existing_chargers = self._get_charger_entries()
        next_priority = len(existing_chargers) + 1

        data_schema = self._charger_info_schema(
            {
                CONF_NAME: self._data.get(
                    CONF_NAME, self._selected_charger["name"]
                ),
                CONF_ENTITY_ID: self._data.get(
                    CONF_ENTITY_ID, f"lj_{self._selected_charger['id']}"
                ),
                CONF_CHARGER_PRIORITY: self._data.get(
                    CONF_CHARGER_PRIORITY, next_priority
                ),
                CONF_OCPP_DEVICE_ID: self._data.get(
                    CONF_OCPP_DEVICE_ID, self._selected_charger.get("device_id")
                ),
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
        hub_phases = self._get_hub_phase_count()

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
                    data_schema=self._charger_current_schema(
                        self._data, hub_phases=hub_phases
                    ),
                    errors=errors,
                    last_step=False,
                )

            return await self.async_step_charger_timing()

        data_schema = self._charger_current_schema(
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
        detected_unit = await self._detect_charge_rate_unit(ocpp_device_id)
        detected_interval = await self._detect_meter_value_interval(ocpp_device_id)

        if user_input is not None:
            self._data.update(user_input)

            self._data[ENTRY_TYPE] = ENTRY_TYPE_CHARGER
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
                ENTRY_TYPE: ENTRY_TYPE_CHARGER,
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

        data_schema = self._charger_timing_schema(
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
        # Imported here rather than at module scope: options.py imports this
        # module for the schema builders, so the edge has to point one way.
        from .options import LoadJugglerOptionsFlow

        return LoadJugglerOptionsFlow()
