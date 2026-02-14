import re
import logging
import asyncio
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from typing import Any
from .const import *
from .helpers import normalize_optional_entity, validate_charger_settings

_LOGGER = logging.getLogger(__name__)

class DynamicOcppEvseConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dynamic OCPP EVSE."""

    VERSION = 2
    MINOR_VERSION = 1

   

    def __init__(self):
        self._data = {}
        self._discovered_chargers = []
        self._selected_charger = None
        self._entity_cache = None

    def _get_entity_registry_ids(self) -> list[str]:
        if self._entity_cache is None:
            entity_registry = async_get_entity_registry(self.hass)
            self._entity_cache = list(entity_registry.entities.keys())
        return self._entity_cache

    def _get_current_and_power_entities(self) -> list[str]:
        current_power_entities: list[str] = []
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            if entity_id.startswith("sensor."):
                device_class = state.attributes.get("device_class")
                if device_class in ["current", "power"]:
                    current_power_entities.append(entity_id)
        return sorted({entity for entity in current_power_entities if entity})

    def _battery_and_power_entities(self) -> tuple[list[str], list[str]]:
        battery_entities = []
        power_entities = []
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            if entity_id.startswith("sensor."):
                device_class = state.attributes.get("device_class")
                if device_class == "battery":
                    battery_entities.append(entity_id)
                elif device_class == "power":
                    power_entities.append(entity_id)
        return sorted(battery_entities), sorted(power_entities)

    def _optional_entity_options(self, entities: list[str]) -> list[dict[str, str]]:
        options = [{"value": "", "label": "None"}]
        options.extend({"value": entity, "label": entity} for entity in entities)
        return options

    def _build_hub_grid_schema(self, defaults: dict | None = None) -> list[tuple]:
        """Build grid/electrical fields as a reusable list."""
        defaults = defaults or {}
        current_power_entities = self._get_current_and_power_entities()
        optional_current_options = self._optional_entity_options(current_power_entities)
        
        return [
            (vol.Required(
                CONF_PHASE_A_CURRENT_ENTITY_ID,
                default=defaults.get(CONF_PHASE_A_CURRENT_ENTITY_ID),
            ), selector({"entity": {"domain": "sensor", "device_class": ["current", "power"]}})),
            (vol.Optional(
                CONF_PHASE_B_CURRENT_ENTITY_ID,
                default=normalize_optional_entity(defaults.get(CONF_PHASE_B_CURRENT_ENTITY_ID)) or "",
            ), selector({"select": {"options": optional_current_options, "mode": "dropdown"}})),
            (vol.Optional(
                CONF_PHASE_C_CURRENT_ENTITY_ID,
                default=normalize_optional_entity(defaults.get(CONF_PHASE_C_CURRENT_ENTITY_ID)) or "",
            ), selector({"select": {"options": optional_current_options, "mode": "dropdown"}})),
            (vol.Required(
                CONF_MAIN_BREAKER_RATING,
                default=defaults.get(CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING),
            ), int),
            (vol.Required(
                CONF_INVERT_PHASES,
                default=defaults.get(CONF_INVERT_PHASES, False),
            ), bool),
            (vol.Required(
                CONF_MAX_IMPORT_POWER_ENTITY_ID,
                default=defaults.get(CONF_MAX_IMPORT_POWER_ENTITY_ID),
            ), selector({"entity": {"domain": ["sensor", "input_number"], "device_class": "power"}})),
            (vol.Required(
                CONF_PHASE_VOLTAGE,
                default=defaults.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE),
            ), int),
            (vol.Required(
                CONF_EXCESS_EXPORT_THRESHOLD,
                default=defaults.get(CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD),
            ), int),
            (vol.Optional(
                CONF_SOLAR_PRODUCTION_ENTITY_ID,
                default=normalize_optional_entity(defaults.get(CONF_SOLAR_PRODUCTION_ENTITY_ID)) or "",
            ), selector({"select": {"options": self._optional_entity_options(
                self._battery_and_power_entities()[1]  # power entities list
            ), "mode": "dropdown"}})),
        ]

    def _build_hub_battery_schema(self, defaults: dict | None = None) -> list[tuple]:
        """Build battery fields as a reusable list."""
        defaults = defaults or {}
        battery_entities, power_entities = self._battery_and_power_entities()
        
        return [
            (vol.Optional(
                CONF_BATTERY_SOC_ENTITY_ID,
                default=normalize_optional_entity(defaults.get(CONF_BATTERY_SOC_ENTITY_ID)) or "",
            ), selector({"select": {"options": [{"value": "", "label": "None"}] + [{"value": e, "label": e} for e in battery_entities], "mode": "dropdown"}})),
            (vol.Optional(
                CONF_BATTERY_POWER_ENTITY_ID,
                default=normalize_optional_entity(defaults.get(CONF_BATTERY_POWER_ENTITY_ID)) or "",
            ), selector({"select": {"options": [{"value": "", "label": "None"}] + [{"value": e, "label": e} for e in power_entities], "mode": "dropdown"}})),
            (vol.Optional(
                CONF_BATTERY_MAX_CHARGE_POWER,
                default=defaults.get(CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER),
            ), int),
            (vol.Optional(
                CONF_BATTERY_MAX_DISCHARGE_POWER,
                default=defaults.get(CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER),
            ), int),
            (vol.Optional(
                CONF_BATTERY_SOC_HYSTERESIS,
                default=defaults.get(CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS),
            ), selector({"number": {"min": 1, "max": 10, "step": 1, "mode": "slider", "unit_of_measurement": "%"}})),
        ]

    def _build_hub_inverter_schema(self, defaults: dict | None = None) -> list[tuple]:
        """Build inverter configuration fields as a reusable list."""
        defaults = defaults or {}
        return [
            (vol.Optional(
                CONF_INVERTER_MAX_POWER,
                default=defaults.get(CONF_INVERTER_MAX_POWER, 0),
            ), selector({"number": {"min": 0, "max": 50000, "step": 100, "mode": "box", "unit_of_measurement": "W"}})),
            (vol.Optional(
                CONF_INVERTER_MAX_POWER_PER_PHASE,
                default=defaults.get(CONF_INVERTER_MAX_POWER_PER_PHASE, 0),
            ), selector({"number": {"min": 0, "max": 20000, "step": 100, "mode": "box", "unit_of_measurement": "W"}})),
            (vol.Required(
                CONF_INVERTER_SUPPORTS_ASYMMETRIC,
                default=defaults.get(CONF_INVERTER_SUPPORTS_ASYMMETRIC, False),
            ), bool),
        ]

    def _hub_schema(self, defaults: dict | None = None, include_grid: bool = True, include_battery: bool = True, include_inverter: bool = False) -> vol.Schema:
        """
        Build a combined hub schema from reusable field lists.
        This centralizes schema construction to reduce duplication.
        """
        defaults = defaults or {}
        fields_list: list[tuple] = []

        if include_grid:
            fields_list.extend(self._build_hub_grid_schema(defaults))
        if include_battery:
            fields_list.extend(self._build_hub_battery_schema(defaults))
        if include_inverter:
            fields_list.extend(self._build_hub_inverter_schema(defaults))

        return vol.Schema(dict(fields_list))

    def _hub_grid_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema with only grid/electrical fields."""
        return self._hub_schema(defaults, include_grid=True, include_battery=False)

    def _hub_battery_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema with only battery fields."""
        return self._hub_schema(defaults, include_grid=False, include_battery=True)

    def _hub_inverter_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema with only inverter fields."""
        return self._hub_schema(defaults, include_grid=False, include_battery=False, include_inverter=True)

    def _charger_schema(self, defaults: dict | None = None, detected_unit: str | None = None) -> vol.Schema:
        """Build schema for EVSE charger configuration.

        Args:
            defaults: Pre-filled values (from existing config or initial defaults).
            detected_unit: Charge rate unit detected via OCPP (A or W), or None.
        """
        defaults = defaults or {}
        unit_options = [
            {"value": CHARGE_RATE_UNIT_AMPS, "label": "Amperes (A)"},
            {"value": CHARGE_RATE_UNIT_WATTS, "label": "Watts (W)"},
        ]

        # Determine default for charge rate unit:
        # - Use existing config value if it's a concrete unit (A or W)
        # - Fall back to OCPP-detected value
        # - If neither available, leave empty for user to choose
        stored_unit = defaults.get(CONF_CHARGE_RATE_UNIT)
        if stored_unit in (CHARGE_RATE_UNIT_AMPS, CHARGE_RATE_UNIT_WATTS):
            unit_default = stored_unit
        elif detected_unit:
            unit_default = detected_unit
        else:
            unit_default = None

        # Build the charge rate unit field — with or without a default
        if unit_default:
            charge_rate_field = vol.Required(CONF_CHARGE_RATE_UNIT, default=unit_default)
        else:
            charge_rate_field = vol.Required(CONF_CHARGE_RATE_UNIT)

        return vol.Schema({
            vol.Required(
                CONF_CHARGER_PRIORITY,
                default=defaults.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
            vol.Required(
                CONF_EVSE_MINIMUM_CHARGE_CURRENT,
                default=defaults.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT),
            ): int,
            vol.Required(
                CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
                default=defaults.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT),
            ): int,
            charge_rate_field: selector({"select": {"options": unit_options, "mode": "dropdown"}}),
            vol.Required(
                CONF_PROFILE_VALIDITY_MODE,
                default=defaults.get(CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE),
            ): selector({
                "select": {
                    "options": [
                        {"value": PROFILE_VALIDITY_MODE_RELATIVE, "label": "Relative (duration-based)"},
                        {"value": PROFILE_VALIDITY_MODE_ABSOLUTE, "label": "Absolute (timestamp-based)"},
                    ],
                    "mode": "dropdown",
                }
            }),
            vol.Required(
                CONF_UPDATE_FREQUENCY,
                default=defaults.get(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY),
            ): int,
            vol.Required(
                CONF_OCPP_PROFILE_TIMEOUT,
                default=defaults.get(CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT),
            ): int,
            vol.Required(
                CONF_CHARGE_PAUSE_DURATION,
                default=defaults.get(CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION),
            ): int,
            vol.Required(
                CONF_STACK_LEVEL,
                default=defaults.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL),
            ): int,
        })

    def _plug_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema for smart plug / relay configuration."""
        defaults = defaults or {}
        power_monitor_options = self._optional_entity_options(
            self._battery_and_power_entities()[1]  # power sensor entities
        )
        phase_options = [
            {"value": "A", "label": "Phase A"},
            {"value": "B", "label": "Phase B"},
            {"value": "C", "label": "Phase C"},
            {"value": "AB", "label": "Phase A+B"},
            {"value": "BC", "label": "Phase B+C"},
            {"value": "AC", "label": "Phase A+C"},
            {"value": "ABC", "label": "Phase A+B+C"},
        ]
        return vol.Schema({
            vol.Required(
                CONF_PLUG_SWITCH_ENTITY_ID,
                default=defaults.get(CONF_PLUG_SWITCH_ENTITY_ID),
            ): selector({"entity": {"domain": "switch"}}),
            vol.Required(
                CONF_PLUG_POWER_RATING,
                default=defaults.get(CONF_PLUG_POWER_RATING, DEFAULT_PLUG_POWER_RATING),
            ): int,
            vol.Required(
                CONF_CONNECTED_TO_PHASE,
                default=defaults.get(CONF_CONNECTED_TO_PHASE, "A"),
            ): selector({"select": {"options": phase_options, "mode": "dropdown"}}),
            vol.Required(
                CONF_CHARGER_PRIORITY,
                default=defaults.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
            vol.Optional(
                CONF_PLUG_POWER_MONITOR_ENTITY_ID,
                default=normalize_optional_entity(defaults.get(CONF_PLUG_POWER_MONITOR_ENTITY_ID)) or "",
            ): selector({"select": {"options": power_monitor_options, "mode": "dropdown"}}),
            vol.Required(
                CONF_UPDATE_FREQUENCY,
                default=defaults.get(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY),
            ): int,
        })

    def _normalize_optional_inputs(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(data)
        for key in [
            CONF_PHASE_B_CURRENT_ENTITY_ID,
            CONF_PHASE_C_CURRENT_ENTITY_ID,
            CONF_BATTERY_SOC_ENTITY_ID,
            CONF_BATTERY_POWER_ENTITY_ID,
            CONF_SOLAR_PRODUCTION_ENTITY_ID,
            CONF_PLUG_POWER_MONITOR_ENTITY_ID,
        ]:
            if key in normalized:
                normalized[key] = normalize_optional_entity(normalized.get(key))
        return normalized

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Initial step: choose between hub or charger setup."""
        errors: dict[str, str] = {}
        
        # Check if any hubs exist
        hubs = self._get_hub_entries()
        
        if user_input is not None:
            setup_type = user_input.get("setup_type")
            if setup_type == "hub":
                return await self.async_step_hub_info()
            elif setup_type == "charger":
                if not hubs:
                    errors["base"] = "no_hub_configured"
                else:
                    return await self.async_step_select_hub()
        
        # Build options based on existing hubs
        options = [
            {"value": "hub", "label": "Configure Home Electrical System (Hub)"},
        ]
        if hubs:
            options.append({"value": "charger", "label": "Add a Charger"})
        
        data_schema = vol.Schema({
            vol.Required("setup_type", default="hub" if not hubs else "charger"): selector({
                "select": {
                    "options": options,
                    "mode": "list"
                }
            })
        })
        
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    def _get_hub_entries(self) -> list:
        """Get all hub config entries."""
        return [
            entry for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_HUB
        ]

    def _get_charger_entries(self) -> list:
        """Get all charger config entries."""
        return [
            entry for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
        ]

    # ==================== HUB CONFIGURATION STEPS ====================

    async def async_step_hub_info(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 1: Basic info (name and entity_id)."""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            self._data.update(user_input)
            self._data[ENTRY_TYPE] = ENTRY_TYPE_HUB
            return await self.async_step_hub_grid()

        data_schema = vol.Schema({
            vol.Required(CONF_NAME, default="Dynamic OCPP EVSE"): str,
            vol.Required(CONF_ENTITY_ID, default="dynamic_ocpp_evse"): str,
        })
        
        return self.async_show_form(
            step_id="hub_info",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    async def async_step_hub_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 2: Grid/electrical configuration."""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input)
            self._data.update(user_input)
            return await self.async_step_hub_battery()

        try:
            # Define pattern sets for different inverter types
            PHASE_PATTERNS = [
                {
                    "name": "SolarEdge",
                    "patterns": {
                        "phase_a": r'sensor\..*m.*ac_current_a.*',
                        "phase_b": r'sensor\..*m.*ac_current_b.*', 
                        "phase_c": r'sensor\..*m.*ac_current_c.*'
                    },
                    "unit": "A",
                },
                {
                    "name": "Solarman/Deye - external CTs",
                    "patterns": {
                        "phase_a": r'sensor\..*_external_ct1_current.*',
                        "phase_b": r'sensor\..*_external_ct2_current.*',
                        "phase_c": r'sensor\..*_external_ct3_current.*'
                    },
                    "unit": "A", 
                },
                {
                    "name": "Solarman/Deye - internal CTs",
                    "patterns": {
                        "phase_a": r'sensor\..*_internal_ct1_current.*',
                        "phase_b": r'sensor\..*_internal_ct2_current.*',
                        "phase_c": r'sensor\..*_internal_ct3_current.*'
                    },
                    "unit": "A", 
                },
                {
                    "name": "Solarman - grid power (individual phases)",
                    "patterns": {
                        "phase_a": r'sensor\..*grid_(?:1|l1|power_1|power_l1).*',
                        "phase_b": r'sensor\..*grid_(?:2|l2|power_2|power_l2).*', 
                        "phase_c": r'sensor\..*grid_(?:3|l3|power_3|power_l3).*'
                    },
                    "unit": "W", 
                }
            ]
            
            # Fetch available entities
            entity_ids = self._get_entity_registry_ids()
            
            # Try to find a complete set of phases using pattern sets
            default_phase_a = None
            default_phase_b = None
            default_phase_c = None
            
            for pattern_set in PHASE_PATTERNS:
                phase_a_match = next((entity_id for entity_id in entity_ids if re.match(pattern_set["patterns"]["phase_a"], entity_id)), None)
                phase_b_match = next((entity_id for entity_id in entity_ids if re.match(pattern_set["patterns"]["phase_b"], entity_id)), None)
                phase_c_match = next((entity_id for entity_id in entity_ids if re.match(pattern_set["patterns"]["phase_c"], entity_id)), None)
                
                if phase_a_match and phase_b_match and phase_c_match:
                    default_phase_a = phase_a_match
                    default_phase_b = phase_b_match
                    default_phase_c = phase_c_match
                    break
            
            # Fallback to individual pattern matching
            if not (default_phase_a and default_phase_b and default_phase_c):
                for pattern_set in PHASE_PATTERNS:
                    if not default_phase_a:
                        default_phase_a = next((entity_id for entity_id in entity_ids if re.match(pattern_set["patterns"]["phase_a"], entity_id)), None)
                    if not default_phase_b:
                        default_phase_b = next((entity_id for entity_id in entity_ids if re.match(pattern_set["patterns"]["phase_b"], entity_id)), None)
                    if not default_phase_c:
                        default_phase_c = next((entity_id for entity_id in entity_ids if re.match(pattern_set["patterns"]["phase_c"], entity_id)), None)
            
            # Find max import power sensor
            default_max_import_power = next((entity_id for entity_id in entity_ids if re.match(r'sensor\..*power_limit.*', entity_id)), None)

            # Find solar production sensor (power device class)
            default_solar_production = next(
                (entity_id for entity_id in entity_ids
                 if re.match(r'sensor\..*solar.*(?:production|power|generation).*', entity_id, re.IGNORECASE)
                 and entity_id.startswith("sensor.")),
                None
            )

            data_schema = self._hub_grid_schema({
                CONF_PHASE_A_CURRENT_ENTITY_ID: default_phase_a,
                CONF_PHASE_B_CURRENT_ENTITY_ID: default_phase_b,
                CONF_PHASE_C_CURRENT_ENTITY_ID: default_phase_c,
                CONF_MAX_IMPORT_POWER_ENTITY_ID: default_max_import_power,
                CONF_SOLAR_PRODUCTION_ENTITY_ID: default_solar_production,
                CONF_MAIN_BREAKER_RATING: DEFAULT_MAIN_BREAKER_RATING,
                CONF_INVERT_PHASES: False,
                CONF_PHASE_VOLTAGE: DEFAULT_PHASE_VOLTAGE,
                CONF_EXCESS_EXPORT_THRESHOLD: DEFAULT_EXCESS_EXPORT_THRESHOLD,
            })
            
        except Exception as e:
            _LOGGER.error("Error in async_step_hub_grid: %s", e, exc_info=True)
            errors["base"] = "unknown"
            data_schema = vol.Schema({})

        return self.async_show_form(
            step_id="hub_grid",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    async def async_step_hub_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 3: Battery configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input)
            self._data.update(user_input)
            return await self.async_step_hub_inverter()

        data_schema = self._hub_battery_schema({
            CONF_BATTERY_SOC_ENTITY_ID: None,
            CONF_BATTERY_POWER_ENTITY_ID: None,
            CONF_BATTERY_MAX_CHARGE_POWER: DEFAULT_BATTERY_MAX_POWER,
            CONF_BATTERY_MAX_DISCHARGE_POWER: DEFAULT_BATTERY_MAX_POWER,
            CONF_BATTERY_SOC_HYSTERESIS: DEFAULT_BATTERY_SOC_HYSTERESIS,
        })

        return self.async_show_form(
            step_id="hub_battery",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    async def async_step_hub_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 4: Inverter configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)

            # Normalize inverter power values: 0 means "not configured" → store as None
            for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
                if key in self._data and self._data[key] == 0:
                    self._data[key] = None

            # Generate entity IDs for hub-created entities
            entity_id = self._data.get(CONF_ENTITY_ID)
            self._data[CONF_CHARGING_MODE_ENTITY_ID] = f"select.{entity_id}_charging_mode"
            self._data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = f"number.{entity_id}_home_battery_soc_target"
            self._data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = f"switch.{entity_id}_allow_grid_charging"
            self._data[CONF_POWER_BUFFER_ENTITY_ID] = f"number.{entity_id}_power_buffer"

            # Split static vs mutable fields:
            static_data = {
                CONF_NAME: self._data.get(CONF_NAME),
                CONF_ENTITY_ID: self._data.get(CONF_ENTITY_ID),
                ENTRY_TYPE: ENTRY_TYPE_HUB,
            }
            # Options contain the mutable configuration values
            options_data = {k: v for k, v in self._data.items() if k not in static_data}

            # Create the config entry with only static data
            result = self.async_create_entry(
                title=static_data[CONF_NAME],
                data=static_data
            )

            # Schedule a background task to seed options after the entry is created
            async def _seed_options():
                # Wait briefly for the config entry to be registered
                await asyncio.sleep(0.1)
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if entry.data.get(CONF_ENTITY_ID) == static_data.get(CONF_ENTITY_ID) and entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_HUB:
                        try:
                            self.hass.config_entries.async_update_entry(
                                entry,
                                options={**entry.options, **options_data}
                            )
                        except Exception:
                            _LOGGER.exception("Failed to seed options for hub entry")
                        break

            asyncio.create_task(_seed_options())
            return result

        data_schema = self._hub_inverter_schema({
            CONF_INVERTER_MAX_POWER: 0,
            CONF_INVERTER_MAX_POWER_PER_PHASE: 0,
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: False,
        })

        return self.async_show_form(
            step_id="hub_inverter",
            data_schema=data_schema,
            errors=errors,
            last_step=True
        )

    # ==================== CHARGER CONFIGURATION STEPS ====================

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Handle integration discovery of OCPP chargers."""
        # Store discovery info
        self._data[CONF_HUB_ENTRY_ID] = discovery_info["hub_entry_id"]
        self._selected_charger = {
            "id": discovery_info["charger_id"],
            "name": discovery_info["charger_name"],
            "device_id": discovery_info.get("device_id"),
            "current_import_entity": discovery_info["current_import_entity"],
            "current_offered_entity": discovery_info["current_offered_entity"],
        }
        
        # Set unique ID to prevent duplicate discoveries
        await self.async_set_unique_id(f"{DOMAIN}_charger_{discovery_info['charger_id']}")
        self._abort_if_unique_id_configured()
        
        # Show confirmation form
        self.context["title_placeholders"] = {"name": self._selected_charger["name"]}
        return await self.async_step_charger_config()

    async def async_step_select_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 1: Select which hub to add charger to."""
        errors: dict[str, str] = {}
        hubs = self._get_hub_entries()

        if user_input is not None:
            self._data[CONF_HUB_ENTRY_ID] = user_input["hub_entry_id"]
            return await self.async_step_device_type()

        # If only one hub, skip selection
        if len(hubs) == 1:
            self._data[CONF_HUB_ENTRY_ID] = hubs[0].entry_id
            return await self.async_step_device_type()

        hub_options = [
            {"value": entry.entry_id, "label": entry.title}
            for entry in hubs
        ]

        data_schema = vol.Schema({
            vol.Required("hub_entry_id"): selector({
                "select": {"options": hub_options}
            })
        })

        return self.async_show_form(
            step_id="select_hub",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    async def async_step_device_type(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 1b: Choose device type (OCPP EVSE or Smart Plug/Relay)."""
        if user_input is not None:
            device_type = user_input.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
            if device_type == DEVICE_TYPE_PLUG:
                return await self.async_step_plug_config()
            return await self.async_step_discover_chargers()

        data_schema = vol.Schema({
            vol.Required(CONF_DEVICE_TYPE, default=DEVICE_TYPE_EVSE): selector({
                "select": {
                    "options": [
                        {"value": DEVICE_TYPE_EVSE, "label": "OCPP Charger (EVSE)"},
                        {"value": DEVICE_TYPE_PLUG, "label": "Smart Plug / Relay"},
                    ],
                    "mode": "list",
                }
            })
        })

        return self.async_show_form(
            step_id="device_type",
            data_schema=data_schema,
            last_step=False,
        )

    async def async_step_plug_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Smart plug configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input)
            self._data.update(user_input)

            plug_name = self._data.get(CONF_NAME, "Smart Plug")
            plug_entity_id = self._data.get(CONF_ENTITY_ID, "smart_plug")

            static_data = {
                CONF_ENTITY_ID: plug_entity_id,
                CONF_NAME: plug_name,
                ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                CONF_DEVICE_TYPE: DEVICE_TYPE_PLUG,
                CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                CONF_PLUG_SWITCH_ENTITY_ID: self._data.get(CONF_PLUG_SWITCH_ENTITY_ID),
            }
            options_data = {k: v for k, v in self._data.items() if k not in static_data}

            result = self.async_create_entry(
                title=f"{plug_name} Smart Plug",
                data=static_data,
            )

            async def _seed_plug_options():
                await asyncio.sleep(0.1)
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if (entry.data.get(CONF_ENTITY_ID) == plug_entity_id
                            and entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_PLUG):
                        try:
                            self.hass.config_entries.async_update_entry(
                                entry,
                                options={**entry.options, **options_data},
                            )
                        except Exception:
                            _LOGGER.exception("Failed to seed options for plug entry")
                        break

            asyncio.create_task(_seed_plug_options())
            return result

        existing_chargers = self._get_charger_entries()
        next_priority = len(existing_chargers) + 1

        # Name + entity_id fields, then the plug-specific schema
        name_schema = vol.Schema({
            vol.Required(CONF_NAME, default="Smart Plug"): str,
            vol.Required(CONF_ENTITY_ID, default="smart_plug"): str,
        })
        plug_fields = self._plug_schema({
            CONF_CHARGER_PRIORITY: next_priority,
            CONF_PLUG_POWER_RATING: DEFAULT_PLUG_POWER_RATING,
            CONF_CONNECTED_TO_PHASE: "A",
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
        })
        # Merge both schemas
        combined = vol.Schema({**name_schema.schema, **plug_fields.schema})

        return self.async_show_form(
            step_id="plug_config",
            data_schema=combined,
            errors=errors,
            last_step=True,
        )

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
                last_step=True
            )
        
        if user_input is not None:
            selected_charger_id = user_input.get("charger")
            for charger in self._discovered_chargers:
                if charger["id"] == selected_charger_id:
                    self._selected_charger = charger
                    break
            return await self.async_step_charger_config()
        
        charger_options = [
            {"value": charger["id"], "label": charger["name"]}
            for charger in self._discovered_chargers
        ]
        
        data_schema = vol.Schema({
            vol.Required("charger"): selector({
                "select": {"options": charger_options}
            })
        })
        
        return self.async_show_form(
            step_id="discover_chargers",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    async def _discover_ocpp_chargers(self) -> list:
        """Discover OCPP chargers from the OCPP integration."""
        chargers = []
        
        entity_registry = async_get_entity_registry(self.hass)
        device_registry = async_get_device_registry(self.hass)
        
        # Get already configured charger entity IDs to exclude them
        configured_charger_imports = set()
        for entry in self._get_charger_entries():
            configured_charger_imports.add(entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID))
        
        # Find entities with current_import suffix (OCPP chargers)
        for entity_id, entity in entity_registry.entities.items():
            if entity_id.endswith(OCPP_ENTITY_SUFFIX_CURRENT_IMPORT) and entity_id.startswith("sensor."):
                # Skip already configured chargers
                if entity_id in configured_charger_imports:
                    continue
                
                # Extract charger base name
                base_name = entity_id.replace("sensor.", "").replace(OCPP_ENTITY_SUFFIX_CURRENT_IMPORT, "")
                
                # Check if corresponding current_offered entity exists
                current_offered_id = f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_OFFERED}"
                if current_offered_id in entity_registry.entities:
                    # Get device info if available
                    device_name = base_name.replace("_", " ").title()
                    device_id = None
                    
                    if entity.device_id:
                        device = device_registry.async_get(entity.device_id)
                        if device:
                            device_name = device.name or device_name
                            device_id = device.id
                    
                    chargers.append({
                        "id": base_name,
                        "name": device_name,
                        "device_id": device_id,
                        "current_import_entity": entity_id,
                        "current_offered_entity": current_offered_id,
                    })
        
        return chargers

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
                        if isinstance(item, dict) and item.get("key") == "ChargingScheduleAllowedChargingRateUnit":
                            value = item.get("value")
                            break

            if not value:
                _LOGGER.debug("Could not parse charge rate unit from OCPP response: %s", response)
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
                _LOGGER.warning("Unrecognised ChargingScheduleAllowedChargingRateUnit value: %s", value)
                return None

        except Exception as e:
            _LOGGER.warning("Could not detect charge rate unit via OCPP: %s", e)
            return None

    async def async_step_charger_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3: Configure charger settings."""
        errors: dict[str, str] = {}
        
        # Detect charge rate unit via OCPP (used for form defaults and re-validation)
        ocpp_device_id = self._selected_charger.get("device_id")
        detected_unit = await self._detect_charge_rate_unit(ocpp_device_id)

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input)
            self._data.update(user_input)

            validate_charger_settings(self._data, errors)
            if errors:
                return self.async_show_form(
                    step_id="charger_config",
                    data_schema=self._charger_schema(self._data, detected_unit=detected_unit),
                    errors=errors,
                    description_placeholders={
                        "charger_name": self._selected_charger["name"],
                        "current_import": self._selected_charger["current_import_entity"],
                        "current_offered": self._selected_charger["current_offered_entity"],
                    },
                    last_step=True,
                )

            self._data[ENTRY_TYPE] = ENTRY_TYPE_CHARGER
            self._data[CONF_CHARGER_ID] = self._selected_charger["id"]
            self._data[CONF_OCPP_DEVICE_ID] = self._selected_charger.get("device_id")
            self._data[CONF_EVSE_CURRENT_IMPORT_ENTITY_ID] = self._selected_charger["current_import_entity"]
            self._data[CONF_EVSE_CURRENT_OFFERED_ENTITY_ID] = self._selected_charger["current_offered_entity"]
            
            # Generate entity IDs for charger-created entities
            charger_id = self._selected_charger["id"]
            self._data[CONF_MIN_CURRENT_ENTITY_ID] = f"number.{charger_id}_min_current"
            self._data[CONF_MAX_CURRENT_ENTITY_ID] = f"number.{charger_id}_max_current"
            self._data[CONF_NAME] = self._selected_charger["name"]
            self._data[CONF_ENTITY_ID] = charger_id
            self._data[ENTRY_TYPE] = ENTRY_TYPE_CHARGER
            
            # Split static vs mutable fields for charger
            static_data = {
                CONF_ENTITY_ID: charger_id,
                CONF_NAME: self._data.get(CONF_NAME),
                ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                CONF_CHARGER_ID: self._data.get(CONF_CHARGER_ID),
                CONF_OCPP_DEVICE_ID: self._data.get(CONF_OCPP_DEVICE_ID),
                CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: self._data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID),
                CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: self._data.get(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID),
            }
            options_data = {k: v for k, v in self._data.items() if k not in static_data}
            
            result = self.async_create_entry(
                title=f"{self._selected_charger['name']} Charger",
                data=static_data
            )
            
            async def _seed_charger_options():
                await asyncio.sleep(0.1)
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if entry.data.get(CONF_ENTITY_ID) == charger_id and entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER:
                        try:
                            self.hass.config_entries.async_update_entry(
                                entry,
                                options={**entry.options, **options_data}
                            )
                        except Exception:
                            _LOGGER.exception("Failed to seed options for charger entry")
                        break
            
            asyncio.create_task(_seed_charger_options())
            return result
        
        existing_chargers = self._get_charger_entries()
        next_priority = len(existing_chargers) + 1
        data_schema = self._charger_schema(
            {
                CONF_CHARGER_PRIORITY: next_priority,
                CONF_EVSE_MINIMUM_CHARGE_CURRENT: DEFAULT_MIN_CHARGE_CURRENT,
                CONF_EVSE_MAXIMUM_CHARGE_CURRENT: DEFAULT_MAX_CHARGE_CURRENT,
                CONF_PROFILE_VALIDITY_MODE: DEFAULT_PROFILE_VALIDITY_MODE,
                CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
                CONF_OCPP_PROFILE_TIMEOUT: DEFAULT_OCPP_PROFILE_TIMEOUT,
                CONF_CHARGE_PAUSE_DURATION: DEFAULT_CHARGE_PAUSE_DURATION,
                CONF_STACK_LEVEL: DEFAULT_STACK_LEVEL,
            },
            detected_unit=detected_unit,
        )
        
        return self.async_show_form(
            step_id="charger_config",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "charger_name": self._selected_charger["name"],
                "current_import": self._selected_charger["current_import_entity"],
                "current_offered": self._selected_charger["current_offered_entity"],
            },
            last_step=True
        )

    # ==================== RECONFIGURE STEPS ====================

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle reconfiguration."""
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        if not entry:
            return self.async_abort(reason="entry_not_found")
        
        self._data = dict(entry.data)
        entry_type = entry.data.get(ENTRY_TYPE)
        
        # Legacy entries without entry_type are hubs
        if not entry_type or entry_type == ENTRY_TYPE_HUB:
            return await self.async_step_reconfigure_hub_grid()
        elif entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_PLUG:
            return await self.async_step_reconfigure_plug()
        else:
            return await self.async_step_reconfigure_charger()

    async def async_step_reconfigure_hub_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure hub grid settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input)
            self._data.update(user_input)
            return await self.async_step_reconfigure_hub_battery()

        try:
            data_schema = self._hub_grid_schema(defaults)
        except Exception as e:
            _LOGGER.error("Error in reconfigure_hub_grid: %s", e, exc_info=True)
            errors["base"] = "unknown"
            data_schema = vol.Schema({})

        return self.async_show_form(
            step_id="reconfigure_hub_grid",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    async def async_step_reconfigure_hub_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure hub battery settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input)
            self._data.update(user_input)
            return await self.async_step_reconfigure_hub_inverter()

        data_schema = self._hub_battery_schema(defaults)

        return self.async_show_form(
            step_id="reconfigure_hub_battery",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    async def async_step_reconfigure_hub_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure hub inverter settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            self._data.update(user_input)
            # Normalize: 0 means "not configured" → store as None
            for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
                if key in self._data and self._data[key] == 0:
                    self._data[key] = None
            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, **self._data},
            )
            return self.async_abort(reason="reconfigure_successful")

        # Show existing values, defaulting 0 for None (user sees 0 = "not set")
        inverter_defaults = dict(defaults)
        for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
            if inverter_defaults.get(key) is None:
                inverter_defaults[key] = 0

        data_schema = self._hub_inverter_schema(inverter_defaults)

        return self.async_show_form(
            step_id="reconfigure_hub_inverter",
            data_schema=data_schema,
            errors=errors,
            last_step=True
        )

    async def async_step_reconfigure_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure charger settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        # Try OCPP detection so user can see/re-detect the correct value
        ocpp_device_id = entry.data.get(CONF_OCPP_DEVICE_ID)
        detected_unit = await self._detect_charge_rate_unit(ocpp_device_id)

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input)
            self._data.update(user_input)

            validate_charger_settings(self._data, errors)
            if errors:
                return self.async_show_form(
                    step_id="reconfigure_charger",
                    data_schema=self._charger_schema(self._data, detected_unit=detected_unit),
                    errors=errors,
                    last_step=True,
                )
            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, **self._data},
            )
            return self.async_abort(reason="reconfigure_successful")

        data_schema = self._charger_schema(defaults, detected_unit=detected_unit)
        
        return self.async_show_form(
            step_id="reconfigure_charger",
            data_schema=data_schema,
            errors=errors,
            last_step=True
        )

    async def async_step_reconfigure_plug(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure smart plug settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input)
            self._data.update(user_input)
            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, **self._data},
            )
            return self.async_abort(reason="reconfigure_successful")

        data_schema = self._plug_schema(defaults)

        return self.async_show_form(
            step_id="reconfigure_plug",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Get the options flow for this handler."""
        return DynamicOcppEvseOptionsFlow()


class DynamicOcppEvseOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Dynamic OCPP EVSE."""

    def __init__(self):
        self._data = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        entry_type = self.config_entry.data.get(ENTRY_TYPE)

        if entry_type == ENTRY_TYPE_HUB:
            return await self.async_step_hub_grid()
        if entry_type == ENTRY_TYPE_CHARGER:
            if self.config_entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_PLUG:
                return await self.async_step_plug()
            return await self.async_step_charger()
        return self.async_abort(reason="entry_not_found")

    async def async_step_hub(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        flow = DynamicOcppEvseConfigFlow()
        flow.hass = self.hass

        if user_input is not None:
            user_input = flow._normalize_optional_inputs(user_input)
            self._data.update(user_input)
            return await self.async_step_hub_inverter()

        data_schema = flow._hub_battery_schema(defaults)
        return self.async_show_form(
            step_id="hub",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_hub_inverter(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        flow = DynamicOcppEvseConfigFlow()
        flow.hass = self.hass

        if user_input is not None:
            self._data.update(user_input)
            # Normalize: 0 means "not configured" → store as None
            for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
                if key in self._data and self._data[key] == 0:
                    self._data[key] = None
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, **self._data},
            )

        # Show existing values, defaulting 0 for None
        inverter_defaults = dict(defaults)
        for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
            if inverter_defaults.get(key) is None:
                inverter_defaults[key] = 0

        data_schema = flow._hub_inverter_schema(inverter_defaults)
        return self.async_show_form(
            step_id="hub_inverter",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_hub_grid(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        flow = DynamicOcppEvseConfigFlow()
        flow.hass = self.hass

        if user_input is not None:
            user_input = flow._normalize_optional_inputs(user_input)
            self._data.update(user_input)
            return await self.async_step_hub()

        data_schema = flow._hub_grid_schema(defaults)
        return self.async_show_form(
            step_id="hub_grid",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_charger(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        flow = DynamicOcppEvseConfigFlow()
        flow.hass = self.hass

        # Try OCPP detection for charge rate unit
        ocpp_device_id = self.config_entry.data.get(CONF_OCPP_DEVICE_ID)
        detected_unit = await flow._detect_charge_rate_unit(ocpp_device_id)

        if user_input is not None:
            user_input = flow._normalize_optional_inputs(user_input)
            self._data.update(user_input)
            validate_charger_settings(self._data, errors)
            if errors:
                return self.async_show_form(
                    step_id="charger",
                    data_schema=flow._charger_schema(self._data, detected_unit=detected_unit),
                    errors=errors,
                )
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, **self._data},
            )

        data_schema = flow._charger_schema(defaults, detected_unit=detected_unit)
        return self.async_show_form(
            step_id="charger",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_plug(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        flow = DynamicOcppEvseConfigFlow()
        flow.hass = self.hass

        if user_input is not None:
            user_input = flow._normalize_optional_inputs(user_input)
            self._data.update(user_input)
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, **self._data},
            )

        data_schema = flow._plug_schema(defaults)
        return self.async_show_form(
            step_id="plug",
            data_schema=data_schema,
            errors=errors,
        )

