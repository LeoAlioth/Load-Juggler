import re
import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from typing import Any
from .const import *
from .detection_patterns import (
    PHASE_PATTERNS, INVERTER_OUTPUT_PATTERNS,
    BATTERY_SOC_PATTERNS, BATTERY_POWER_PATTERNS, SOLAR_PRODUCTION_PATTERNS,
    BATTERY_MAX_CHARGE_POWER_PATTERNS, BATTERY_MAX_DISCHARGE_POWER_PATTERNS,
)
from .helpers import normalize_optional_entity, prettify_name, validate_charger_settings

_LOGGER = logging.getLogger(__name__)
_POWER_FACTOR = 0.9  # 90% of detected limit for safe headroom


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

    @staticmethod
    def _optional_entity_field(key: str, default_val):
        """Create vol.Optional with suggested_value so the user can truly clear it.

        Using suggested_value instead of default lets the entity selector
        be cleared with X — vol.Optional(default=...) would silently
        re-fill the default on clear.
        """
        val = normalize_optional_entity(default_val)
        if val:
            return vol.Optional(key, description={"suggested_value": val})
        return vol.Optional(key)

    def _build_hub_grid_schema(self, defaults: dict | None = None) -> list[tuple]:
        """Build grid/electrical fields as a reusable list."""
        defaults = defaults or {}
        entity_sel_current_power = selector({"entity": {"domain": "sensor", "device_class": ["current", "power"]}})

        return [
            (vol.Required(
                CONF_PHASE_A_CURRENT_ENTITY_ID,
                default=defaults.get(CONF_PHASE_A_CURRENT_ENTITY_ID),
            ), entity_sel_current_power),
            (self._optional_entity_field(
                CONF_PHASE_B_CURRENT_ENTITY_ID,
                defaults.get(CONF_PHASE_B_CURRENT_ENTITY_ID),
            ), entity_sel_current_power),
            (self._optional_entity_field(
                CONF_PHASE_C_CURRENT_ENTITY_ID,
                defaults.get(CONF_PHASE_C_CURRENT_ENTITY_ID),
            ), entity_sel_current_power),
            (vol.Required(
                CONF_MAIN_BREAKER_RATING,
                default=defaults.get(CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING),
            ), selector({"number": {"min": 1, "max": 200, "step": 1, "mode": "box", "unit_of_measurement": "A"}})),
            (vol.Required(
                CONF_INVERT_PHASES,
                default=defaults.get(CONF_INVERT_PHASES, False),
            ), bool),
            (vol.Required(
                CONF_ENABLE_MAX_IMPORT_POWER,
                default=defaults.get(CONF_ENABLE_MAX_IMPORT_POWER, True),
            ), bool),
            (self._optional_entity_field(
                CONF_MAX_IMPORT_POWER_ENTITY_ID,
                defaults.get(CONF_MAX_IMPORT_POWER_ENTITY_ID),
            ), selector({"entity": {"domain": ["sensor", "input_number"], "device_class": "power"}})),
            (vol.Required(
                CONF_PHASE_VOLTAGE,
                default=defaults.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE),
            ), selector({"number": {"min": 100, "max": 400, "step": 1, "mode": "box", "unit_of_measurement": "V"}})),
            (vol.Required(
                CONF_EXCESS_EXPORT_THRESHOLD,
                default=defaults.get(CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD),
            ), selector({"number": {"min": 0, "max": 50000, "step": 100, "mode": "box", "unit_of_measurement": "W"}})),
            (vol.Optional(
                CONF_SITE_UPDATE_FREQUENCY,
                default=defaults.get(CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY),
            ), selector({"number": {"min": 1, "max": 60, "step": 1, "mode": "box", "unit_of_measurement": "s"}})),
        ]

    def _build_hub_battery_schema(self, defaults: dict | None = None) -> list[tuple]:
        """Build battery fields as a reusable list (includes solar entity selector)."""
        defaults = defaults or {}

        return [
            (self._optional_entity_field(
                CONF_SOLAR_PRODUCTION_ENTITY_ID,
                defaults.get(CONF_SOLAR_PRODUCTION_ENTITY_ID),
            ), selector({"entity": {"domain": "sensor", "device_class": "power"}})),
            (self._optional_entity_field(
                CONF_BATTERY_SOC_ENTITY_ID,
                defaults.get(CONF_BATTERY_SOC_ENTITY_ID),
            ), selector({"entity": {"domain": "sensor", "device_class": "battery"}})),
            (self._optional_entity_field(
                CONF_BATTERY_POWER_ENTITY_ID,
                defaults.get(CONF_BATTERY_POWER_ENTITY_ID),
            ), selector({"entity": {"domain": "sensor", "device_class": "power"}})),
            (vol.Optional(
                CONF_BATTERY_MAX_CHARGE_POWER,
                default=defaults.get(CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER),
            ), selector({"number": {"min": 0, "max": 50000, "step": 100, "mode": "box", "unit_of_measurement": "W"}})),
            (vol.Optional(
                CONF_BATTERY_MAX_DISCHARGE_POWER,
                default=defaults.get(CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER),
            ), selector({"number": {"min": 0, "max": 50000, "step": 100, "mode": "box", "unit_of_measurement": "W"}})),
            (vol.Optional(
                CONF_BATTERY_SOC_HYSTERESIS,
                default=defaults.get(CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS),
            ), selector({"number": {"min": 1, "max": 10, "step": 1, "mode": "slider", "unit_of_measurement": "%"}})),
        ]

    def _build_hub_inverter_schema(self, defaults: dict | None = None) -> list[tuple]:
        """Build inverter configuration fields as a reusable list."""
        defaults = defaults or {}
        entity_sel_current_power = selector({"entity": {"domain": "sensor", "device_class": ["current", "power"]}})
        topology_options = [
            {"value": WIRING_TOPOLOGY_PARALLEL, "label": "Parallel (AC-coupled / no battery)"},
            {"value": WIRING_TOPOLOGY_SERIES, "label": "Series (Hybrid / battery)"},
        ]
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
            (self._optional_entity_field(
                CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
                defaults.get(CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID),
            ), entity_sel_current_power),
            (self._optional_entity_field(
                CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
                defaults.get(CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID),
            ), entity_sel_current_power),
            (self._optional_entity_field(
                CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
                defaults.get(CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID),
            ), entity_sel_current_power),
            (vol.Required(
                CONF_WIRING_TOPOLOGY,
                default=defaults.get(CONF_WIRING_TOPOLOGY, DEFAULT_WIRING_TOPOLOGY),
            ), selector({"select": {"options": topology_options, "mode": "dropdown"}})),
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

    def _charger_info_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema for charger info step (name, entity ID, priority)."""
        defaults = defaults or {}
        return vol.Schema({
            vol.Required(
                CONF_NAME,
                default=defaults.get(CONF_NAME, ""),
            ): str,
            vol.Required(
                CONF_ENTITY_ID,
                default=defaults.get(CONF_ENTITY_ID, ""),
            ): str,
            vol.Required(
                CONF_CHARGER_PRIORITY,
                default=defaults.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
        })

    def _get_hub_phase_count(self, hub_entry_id: str | None = None) -> int:
        """Get the number of phases configured on the hub."""
        entry_id = hub_entry_id or self._data.get(CONF_HUB_ENTRY_ID)
        if not entry_id:
            return 3  # Default to 3 if unknown
        hub_entry = self.hass.config_entries.async_get_entry(entry_id)
        if not hub_entry:
            return 3
        count = 0
        for key in (CONF_PHASE_A_CURRENT_ENTITY_ID, CONF_PHASE_B_CURRENT_ENTITY_ID, CONF_PHASE_C_CURRENT_ENTITY_ID):
            val = hub_entry.options.get(key) or hub_entry.data.get(key)
            if val:
                count += 1
        return max(count, 1)

    def _charger_current_schema(self, defaults: dict | None = None, hub_phases: int = 3) -> vol.Schema:
        """Build schema for charger current limits and phase mapping.

        Only shows L2/L3 phase mapping fields when the hub has 2+/3+ phases.
        """
        defaults = defaults or {}
        phase_options = [
            {"value": "A", "label": "Phase A"},
            {"value": "B", "label": "Phase B"},
            {"value": "C", "label": "Phase C"},
        ]
        fields = {
            vol.Required(
                CONF_EVSE_MINIMUM_CHARGE_CURRENT,
                default=defaults.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT),
            ): selector({"number": {"min": 6, "max": 80, "step": 1, "mode": "box", "unit_of_measurement": "A"}}),
            vol.Required(
                CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
                default=defaults.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT),
            ): selector({"number": {"min": 6, "max": 80, "step": 1, "mode": "box", "unit_of_measurement": "A"}}),
            vol.Required(
                CONF_CHARGER_L1_PHASE,
                default=defaults.get(CONF_CHARGER_L1_PHASE, "A"),
            ): selector({"select": {"options": phase_options, "mode": "dropdown"}}),
        }
        if hub_phases >= 2:
            fields[vol.Required(
                CONF_CHARGER_L2_PHASE,
                default=defaults.get(CONF_CHARGER_L2_PHASE, "B"),
            )] = selector({"select": {"options": phase_options, "mode": "dropdown"}})
        if hub_phases >= 3:
            fields[vol.Required(
                CONF_CHARGER_L3_PHASE,
                default=defaults.get(CONF_CHARGER_L3_PHASE, "C"),
            )] = selector({"select": {"options": phase_options, "mode": "dropdown"}})
        return vol.Schema(fields)

    def _charger_timing_schema(self, defaults: dict | None = None, detected_unit: str | None = None) -> vol.Schema:
        """Build schema for charger timing and unit configuration."""
        defaults = defaults or {}
        unit_options = [
            {"value": CHARGE_RATE_UNIT_AMPS, "label": "Amperes (A)"},
            {"value": CHARGE_RATE_UNIT_WATTS, "label": "Watts (W)"},
        ]

        # Determine default for charge rate unit
        stored_unit = defaults.get(CONF_CHARGE_RATE_UNIT)
        if stored_unit in (CHARGE_RATE_UNIT_AMPS, CHARGE_RATE_UNIT_WATTS):
            unit_default = stored_unit
        elif detected_unit:
            unit_default = detected_unit
        else:
            unit_default = None

        if unit_default:
            charge_rate_field = vol.Required(CONF_CHARGE_RATE_UNIT, default=unit_default)
        else:
            charge_rate_field = vol.Required(CONF_CHARGE_RATE_UNIT)

        return vol.Schema({
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
            ): selector({"number": {"min": 5, "max": 300, "step": 1, "mode": "box", "unit_of_measurement": "s"}}),
            vol.Required(
                CONF_OCPP_PROFILE_TIMEOUT,
                default=defaults.get(CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT),
            ): selector({"number": {"min": 30, "max": 600, "step": 1, "mode": "box", "unit_of_measurement": "s"}}),
            vol.Required(
                CONF_CHARGE_PAUSE_DURATION,
                default=defaults.get(CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION),
            ): selector({"number": {"min": 0, "max": 600, "step": 1, "mode": "box", "unit_of_measurement": "s"}}),
            vol.Required(
                CONF_STACK_LEVEL,
                default=defaults.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL),
            ): selector({"number": {"min": 0, "max": 10, "step": 1, "mode": "box"}}),
        })

    def _plug_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema for smart load configuration."""
        defaults = defaults or {}
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
            ): selector({"number": {"min": 100, "max": 25000, "step": 100, "mode": "box", "unit_of_measurement": "W"}}),
            vol.Required(
                CONF_CONNECTED_TO_PHASE,
                default=defaults.get(CONF_CONNECTED_TO_PHASE, "A"),
            ): selector({"select": {"options": phase_options, "mode": "dropdown"}}),
            vol.Required(
                CONF_CHARGER_PRIORITY,
                default=defaults.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
            self._optional_entity_field(
                CONF_PLUG_POWER_MONITOR_ENTITY_ID,
                defaults.get(CONF_PLUG_POWER_MONITOR_ENTITY_ID),
            ): selector({"entity": {"domain": "sensor", "device_class": "power"}}),
            vol.Required(
                CONF_UPDATE_FREQUENCY,
                default=defaults.get(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY),
            ): selector({"number": {"min": 5, "max": 300, "step": 1, "mode": "box", "unit_of_measurement": "s"}}),
        })

    # Optional entity keys grouped by config step (for entity selector clearing)
    _GRID_ENTITY_KEYS = [CONF_PHASE_B_CURRENT_ENTITY_ID, CONF_PHASE_C_CURRENT_ENTITY_ID, CONF_MAX_IMPORT_POWER_ENTITY_ID]
    _BATTERY_ENTITY_KEYS = [CONF_SOLAR_PRODUCTION_ENTITY_ID, CONF_BATTERY_SOC_ENTITY_ID, CONF_BATTERY_POWER_ENTITY_ID]
    _INVERTER_ENTITY_KEYS = [CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID, CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID, CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID]
    _PLUG_ENTITY_KEYS = [CONF_PLUG_POWER_MONITOR_ENTITY_ID]

    def _normalize_optional_inputs(self, data: dict[str, Any], step_entity_keys: list[str] | None = None) -> dict[str, Any]:
        """Normalize optional entity inputs.

        Args:
            data: The user_input from the form step.
            step_entity_keys: Optional entity keys expected in this step.
                Keys missing from data are set to None (user cleared the field).
        """
        normalized = dict(data)
        for key in (self._GRID_ENTITY_KEYS + self._BATTERY_ENTITY_KEYS
                     + self._INVERTER_ENTITY_KEYS + self._PLUG_ENTITY_KEYS):
            if key in normalized:
                normalized[key] = normalize_optional_entity(normalized.get(key))
        # Entity selectors omit unselected fields — explicitly clear them
        if step_entity_keys:
            for key in step_entity_keys:
                if key not in normalized:
                    normalized[key] = None
        return normalized

    def _normalize_inverter_powers(self):
        """Normalize inverter power values: 0 means 'not configured' → store as None."""
        for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
            if key in self._data and self._data[key] == 0:
                self._data[key] = None

    def _auto_detect_phase_entities(self, pattern_sets: list[dict]) -> dict[str, str | None]:
        """Auto-detect a matching set of phase A/B/C entities from pattern sets.

        Returns dict with keys 'phase_a', 'phase_b', 'phase_c' (values may be None).
        """
        entity_ids = self._get_entity_registry_ids()
        for pattern_set in pattern_sets:
            a = next((eid for eid in entity_ids if re.match(pattern_set["patterns"]["phase_a"], eid)), None)
            b = next((eid for eid in entity_ids if re.match(pattern_set["patterns"]["phase_b"], eid)), None)
            c = next((eid for eid in entity_ids if re.match(pattern_set["patterns"]["phase_c"], eid)), None)
            if a and b and c:
                return {"phase_a": a, "phase_b": b, "phase_c": c}
        return {"phase_a": None, "phase_b": None, "phase_c": None}

    def _auto_detect_entity(self, pattern_sets: list[dict]) -> str | None:
        """Auto-detect a single entity from pattern sets. Returns first match."""
        entity_ids = self._get_entity_registry_ids()
        for pattern_set in pattern_sets:
            match = next((eid for eid in entity_ids if re.match(pattern_set["pattern"], eid)), None)
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

    def _create_entry_and_seed_options(
        self, title: str, static_data: dict, options_data: dict
    ) -> config_entries.FlowResult:
        """Create a config entry with options set directly."""
        return self.async_create_entry(
            title=title, data=static_data, options=options_data
        )

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
            user_input = self._normalize_optional_inputs(user_input, self._GRID_ENTITY_KEYS)
            self._data.update(user_input)
            return await self.async_step_hub_inverter()

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
                        default_phase_a = next((eid for eid in entity_ids if re.match(pattern_set["patterns"]["phase_a"], eid)), None)
                    if not default_phase_b:
                        default_phase_b = next((eid for eid in entity_ids if re.match(pattern_set["patterns"]["phase_b"], eid)), None)
                    if not default_phase_c:
                        default_phase_c = next((eid for eid in entity_ids if re.match(pattern_set["patterns"]["phase_c"], eid)), None)

            data_schema = self._hub_grid_schema({
                CONF_PHASE_A_CURRENT_ENTITY_ID: default_phase_a,
                CONF_PHASE_B_CURRENT_ENTITY_ID: default_phase_b,
                CONF_PHASE_C_CURRENT_ENTITY_ID: default_phase_c,
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
        """Hub step 4: Battery configuration (final step — creates entry)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input, self._BATTERY_ENTITY_KEYS)
            self._data.update(user_input)

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
            options_data = {k: v for k, v in self._data.items() if k not in static_data}

            return self._create_entry_and_seed_options(
                static_data[CONF_NAME], static_data, options_data
            )

        # Auto-detect solar / battery entities and power limits
        data_schema = self._hub_battery_schema({
            CONF_SOLAR_PRODUCTION_ENTITY_ID: self._auto_detect_entity(SOLAR_PRODUCTION_PATTERNS),
            CONF_BATTERY_SOC_ENTITY_ID: self._auto_detect_entity(BATTERY_SOC_PATTERNS),
            CONF_BATTERY_POWER_ENTITY_ID: self._auto_detect_entity(BATTERY_POWER_PATTERNS),
            CONF_BATTERY_MAX_CHARGE_POWER: (
                self._auto_detect_entity_value(BATTERY_MAX_CHARGE_POWER_PATTERNS, _POWER_FACTOR)
                or DEFAULT_BATTERY_MAX_POWER
            ),
            CONF_BATTERY_MAX_DISCHARGE_POWER: (
                self._auto_detect_entity_value(BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR)
                or DEFAULT_BATTERY_MAX_POWER
            ),
            CONF_BATTERY_SOC_HYSTERESIS: DEFAULT_BATTERY_SOC_HYSTERESIS,
        })

        return self.async_show_form(
            step_id="hub_battery",
            data_schema=data_schema,
            errors=errors,
            last_step=True
        )

    async def async_step_hub_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 3: Inverter configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input, self._INVERTER_ENTITY_KEYS)
            self._data.update(user_input)
            self._normalize_inverter_powers()
            return await self.async_step_hub_battery()

        # Auto-detect per-phase inverter output entities
        inv_detected = self._auto_detect_phase_entities(INVERTER_OUTPUT_PATTERNS)
        default_inv_a = inv_detected["phase_a"]
        default_inv_b = inv_detected["phase_b"]
        default_inv_c = inv_detected["phase_c"]

        # Auto-detect wiring topology: series if battery entities are detected
        default_topology = DEFAULT_WIRING_TOPOLOGY
        if self._auto_detect_entity(BATTERY_SOC_PATTERNS):
            default_topology = WIRING_TOPOLOGY_SERIES

        data_schema = self._hub_inverter_schema({
            CONF_INVERTER_MAX_POWER: 0,
            CONF_INVERTER_MAX_POWER_PER_PHASE: 0,
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: False,
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: default_inv_a,
            CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: default_inv_b,
            CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: default_inv_c,
            CONF_WIRING_TOPOLOGY: default_topology,
        })

        # Auto-detect battery discharge power for description hint
        battery_hint = self._auto_detect_entity_value(BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR)
        hint_text = f"{battery_hint}W detected" if battery_hint else "not detected"

        return self.async_show_form(
            step_id="hub_inverter",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
            description_placeholders={"battery_power_hint": hint_text},
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
        return await self.async_step_charger_info()

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
        """Charger step 1b: Choose device type (OCPP EVSE or Smart Load)."""
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
                        {"value": DEVICE_TYPE_PLUG, "label": "Smart Load"},
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
        """Smart load configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input, self._PLUG_ENTITY_KEYS)
            self._data.update(user_input)

            plug_name = self._data.get(CONF_NAME, "Smart Load")
            plug_entity_id = self._data.get(CONF_ENTITY_ID, "smart_load")

            static_data = {
                CONF_ENTITY_ID: plug_entity_id,
                CONF_NAME: plug_name,
                ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                CONF_DEVICE_TYPE: DEVICE_TYPE_PLUG,
                CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                CONF_PLUG_SWITCH_ENTITY_ID: self._data.get(CONF_PLUG_SWITCH_ENTITY_ID),
            }
            options_data = {k: v for k, v in self._data.items() if k not in static_data}

            return self._create_entry_and_seed_options(
                f"{plug_name} Smart Load", static_data, options_data
            )

        existing_chargers = self._get_charger_entries()
        next_priority = len(existing_chargers) + 1

        # Name + entity_id fields, then the plug-specific schema
        name_schema = vol.Schema({
            vol.Required(CONF_NAME, default="Smart Load"): str,
            vol.Required(CONF_ENTITY_ID, default="smart_load"): str,
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
            return await self.async_step_charger_info()
        
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
                    device_name = prettify_name(base_name)
                    device_id = None

                    if entity.device_id:
                        device = device_registry.async_get(entity.device_id)
                        if device:
                            device_name = prettify_name(device.name) if device.name else device_name
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

    async def async_step_charger_info(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3a: Name, entity ID, and priority."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_charger_current()

        existing_chargers = self._get_charger_entries()
        next_priority = len(existing_chargers) + 1

        data_schema = self._charger_info_schema({
            CONF_NAME: self._selected_charger["name"],
            CONF_ENTITY_ID: self._selected_charger["id"],
            CONF_CHARGER_PRIORITY: next_priority,
        })

        return self.async_show_form(
            step_id="charger_info",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "charger_name": self._selected_charger["name"],
                "current_import": self._selected_charger["current_import_entity"],
                "current_offered": self._selected_charger["current_offered_entity"],
            },
            last_step=False
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
                    data_schema=self._charger_current_schema(self._data, hub_phases=hub_phases),
                    errors=errors,
                    last_step=False,
                )

            return await self.async_step_charger_timing()

        data_schema = self._charger_current_schema({
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: DEFAULT_MIN_CHARGE_CURRENT,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: DEFAULT_MAX_CHARGE_CURRENT,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_CHARGER_L2_PHASE: "B",
            CONF_CHARGER_L3_PHASE: "C",
        }, hub_phases=hub_phases)

        return self.async_show_form(
            step_id="charger_current",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    async def async_step_charger_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3c: Units and timing configuration (final — creates entry)."""
        errors: dict[str, str] = {}

        # Detect charge rate unit via OCPP
        ocpp_device_id = self._selected_charger.get("device_id")
        detected_unit = await self._detect_charge_rate_unit(ocpp_device_id)

        if user_input is not None:
            self._data.update(user_input)

            self._data[ENTRY_TYPE] = ENTRY_TYPE_CHARGER
            self._data[CONF_CHARGER_ID] = self._selected_charger["id"]
            self._data[CONF_OCPP_DEVICE_ID] = self._selected_charger.get("device_id")
            self._data[CONF_EVSE_CURRENT_IMPORT_ENTITY_ID] = self._selected_charger["current_import_entity"]
            self._data[CONF_EVSE_CURRENT_OFFERED_ENTITY_ID] = self._selected_charger["current_offered_entity"]

            # Use user-provided name/entity_id from charger_info step
            charger_name = self._data.get(CONF_NAME, self._selected_charger["name"])
            charger_entity_id = self._data.get(CONF_ENTITY_ID, self._selected_charger["id"])
            self._data[CONF_MIN_CURRENT_ENTITY_ID] = f"number.{charger_entity_id}_min_current"
            self._data[CONF_MAX_CURRENT_ENTITY_ID] = f"number.{charger_entity_id}_max_current"

            # Split static vs mutable fields for charger
            static_data = {
                CONF_ENTITY_ID: charger_entity_id,
                CONF_NAME: charger_name,
                ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                CONF_CHARGER_ID: self._data.get(CONF_CHARGER_ID),
                CONF_OCPP_DEVICE_ID: self._data.get(CONF_OCPP_DEVICE_ID),
                CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: self._data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID),
                CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: self._data.get(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID),
            }
            options_data = {k: v for k, v in self._data.items() if k not in static_data}

            return self._create_entry_and_seed_options(
                f"{charger_name} Charger", static_data, options_data
            )

        data_schema = self._charger_timing_schema(
            {
                CONF_PROFILE_VALIDITY_MODE: DEFAULT_PROFILE_VALIDITY_MODE,
                CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
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
            user_input = self._normalize_optional_inputs(user_input, self._GRID_ENTITY_KEYS)
            self._data.update(user_input)
            return await self.async_step_reconfigure_hub_inverter()

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
        """Reconfigure hub battery settings (final step — saves)."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input, self._BATTERY_ENTITY_KEYS)
            self._data.update(user_input)
            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, **self._data},
            )
            return self.async_abort(reason="reconfigure_successful")

        data_schema = self._hub_battery_schema(defaults)

        return self.async_show_form(
            step_id="reconfigure_hub_battery",
            data_schema=data_schema,
            errors=errors,
            last_step=True
        )

    async def async_step_reconfigure_hub_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure hub inverter settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input, self._INVERTER_ENTITY_KEYS)
            self._data.update(user_input)
            self._normalize_inverter_powers()
            return await self.async_step_reconfigure_hub_battery()

        # Show existing values, defaulting 0 for None (user sees 0 = "not set")
        inverter_defaults = dict(defaults)
        for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
            if inverter_defaults.get(key) is None:
                inverter_defaults[key] = 0

        data_schema = self._hub_inverter_schema(inverter_defaults)

        # Auto-detect battery discharge power for description hint
        battery_hint = self._auto_detect_entity_value(BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR)
        hint_text = f"{battery_hint}W detected" if battery_hint else "not detected"

        return self.async_show_form(
            step_id="reconfigure_hub_inverter",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
            description_placeholders={"battery_power_hint": hint_text},
        )

    async def async_step_reconfigure_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure charger step 1: Info (priority only — name/id not editable)."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_reconfigure_charger_current()

        # Only show priority (name/id are static and not editable during reconfigure)
        data_schema = vol.Schema({
            vol.Required(
                CONF_CHARGER_PRIORITY,
                default=defaults.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
        })

        return self.async_show_form(
            step_id="reconfigure_charger",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    async def async_step_reconfigure_charger_current(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure charger step 2: Current limits and phase mapping."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}
        hub_entry_id = defaults.get(CONF_HUB_ENTRY_ID)
        hub_phases = self._get_hub_phase_count(hub_entry_id)

        if user_input is not None:
            self._data.update(user_input)
            # Auto-fill hidden phase mappings to match L1
            l1 = self._data.get(CONF_CHARGER_L1_PHASE, "A")
            if hub_phases < 2:
                self._data[CONF_CHARGER_L2_PHASE] = l1
            if hub_phases < 3:
                self._data[CONF_CHARGER_L3_PHASE] = l1

            validate_charger_settings(self._data, errors)
            if errors:
                return self.async_show_form(
                    step_id="reconfigure_charger_current",
                    data_schema=self._charger_current_schema(self._data, hub_phases=hub_phases),
                    errors=errors,
                    last_step=False,
                )
            return await self.async_step_reconfigure_charger_timing()

        data_schema = self._charger_current_schema(defaults, hub_phases=hub_phases)

        return self.async_show_form(
            step_id="reconfigure_charger_current",
            data_schema=data_schema,
            errors=errors,
            last_step=False
        )

    async def async_step_reconfigure_charger_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure charger step 3: Units and timing (final — saves)."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        ocpp_device_id = entry.data.get(CONF_OCPP_DEVICE_ID)
        detected_unit = await self._detect_charge_rate_unit(ocpp_device_id)

        if user_input is not None:
            self._data.update(user_input)
            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, **self._data},
            )
            return self.async_abort(reason="reconfigure_successful")

        data_schema = self._charger_timing_schema(defaults, detected_unit=detected_unit)

        return self.async_show_form(
            step_id="reconfigure_charger_timing",
            data_schema=data_schema,
            errors=errors,
            last_step=True
        )

    async def async_step_reconfigure_plug(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure smart load settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(user_input, self._PLUG_ENTITY_KEYS)
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
        self._flow = None  # Cached config flow for schema/helper access

    @property
    def _schema_helper(self) -> DynamicOcppEvseConfigFlow:
        """Cached DynamicOcppEvseConfigFlow instance for schema building."""
        if self._flow is None:
            self._flow = DynamicOcppEvseConfigFlow()
            self._flow.hass = self.hass
        return self._flow

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        entry_type = self.config_entry.data.get(ENTRY_TYPE)

        if entry_type == ENTRY_TYPE_HUB:
            return await self.async_step_hub_grid()
        if entry_type == ENTRY_TYPE_CHARGER:
            if self.config_entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_PLUG:
                return await self.async_step_plug()
            return await self.async_step_charger()
        return self.async_abort(reason="entry_not_found")

    async def async_step_hub_grid(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(user_input, f._GRID_ENTITY_KEYS)
            self._data.update(user_input)
            return await self.async_step_hub_inverter()

        # Auto-detect empty grid CT entity fields
        phase_keys = {
            "phase_a": CONF_PHASE_A_CURRENT_ENTITY_ID,
            "phase_b": CONF_PHASE_B_CURRENT_ENTITY_ID,
            "phase_c": CONF_PHASE_C_CURRENT_ENTITY_ID,
        }
        if any(not defaults.get(k) for k in phase_keys.values()):
            ct_detected = f._auto_detect_phase_entities(PHASE_PATTERNS)
            for phase, conf_key in phase_keys.items():
                if not defaults.get(conf_key):
                    defaults[conf_key] = ct_detected[phase]

        return self.async_show_form(
            step_id="hub_grid",
            data_schema=f._hub_grid_schema(defaults),
            errors=errors,
            last_step=False,
        )

    async def async_step_hub_inverter(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(user_input, f._INVERTER_ENTITY_KEYS)
            self._data.update(user_input)
            f._data = self._data
            f._normalize_inverter_powers()
            self._data = f._data
            return await self.async_step_hub()

        # Show existing values, defaulting 0 for None
        inverter_defaults = dict(defaults)
        for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
            if inverter_defaults.get(key) is None:
                inverter_defaults[key] = 0

        # Auto-detect empty inverter output entity fields
        inv_keys = {
            "phase_a": CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
            "phase_b": CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
            "phase_c": CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
        }
        if any(not inverter_defaults.get(k) for k in inv_keys.values()):
            inv_detected = f._auto_detect_phase_entities(INVERTER_OUTPUT_PATTERNS)
            for phase, conf_key in inv_keys.items():
                if not inverter_defaults.get(conf_key):
                    inverter_defaults[conf_key] = inv_detected[phase]

        # Suggest series topology if battery entities exist and topology is at default
        if inverter_defaults.get(CONF_WIRING_TOPOLOGY) == DEFAULT_WIRING_TOPOLOGY:
            has_battery = defaults.get(CONF_BATTERY_SOC_ENTITY_ID) or f._auto_detect_entity(BATTERY_SOC_PATTERNS)
            if has_battery:
                inverter_defaults[CONF_WIRING_TOPOLOGY] = WIRING_TOPOLOGY_SERIES

        # Auto-detect battery discharge power for description hint
        battery_hint = f._auto_detect_entity_value(BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR)
        hint_text = f"{battery_hint}W detected" if battery_hint else "not detected"

        return self.async_show_form(
            step_id="hub_inverter",
            data_schema=f._hub_inverter_schema(inverter_defaults),
            errors=errors,
            description_placeholders={"battery_power_hint": hint_text},
            last_step=False,
        )

    async def async_step_hub(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(user_input, f._BATTERY_ENTITY_KEYS)
            self._data.update(user_input)
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, **self._data},
            )

        # Auto-detect empty battery/solar entity fields
        auto_detect_map = {
            CONF_SOLAR_PRODUCTION_ENTITY_ID: SOLAR_PRODUCTION_PATTERNS,
            CONF_BATTERY_SOC_ENTITY_ID: BATTERY_SOC_PATTERNS,
            CONF_BATTERY_POWER_ENTITY_ID: BATTERY_POWER_PATTERNS,
        }
        for conf_key, patterns in auto_detect_map.items():
            if not defaults.get(conf_key):
                defaults[conf_key] = f._auto_detect_entity(patterns)

        # Auto-detect battery power limits when at default
        power_detect_map = {
            CONF_BATTERY_MAX_CHARGE_POWER: BATTERY_MAX_CHARGE_POWER_PATTERNS,
            CONF_BATTERY_MAX_DISCHARGE_POWER: BATTERY_MAX_DISCHARGE_POWER_PATTERNS,
        }
        for conf_key, patterns in power_detect_map.items():
            if defaults.get(conf_key) == DEFAULT_BATTERY_MAX_POWER:
                detected = f._auto_detect_entity_value(patterns, _POWER_FACTOR)
                if detected:
                    defaults[conf_key] = detected

        return self.async_show_form(
            step_id="hub",
            data_schema=f._hub_battery_schema(defaults),
            errors=errors,
            last_step=True,
        )

    async def async_step_charger(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        """Options charger step 1: Priority."""
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_charger_current()

        data_schema = vol.Schema({
            vol.Required(
                CONF_CHARGER_PRIORITY,
                default=defaults.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
        })
        return self.async_show_form(
            step_id="charger",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_charger_current(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        """Options charger step 2: Current limits and phase mapping."""
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper
        hub_entry_id = defaults.get(CONF_HUB_ENTRY_ID)
        hub_phases = f._get_hub_phase_count(hub_entry_id)

        if user_input is not None:
            self._data.update(user_input)
            # Auto-fill hidden phase mappings to match L1
            l1 = self._data.get(CONF_CHARGER_L1_PHASE, "A")
            if hub_phases < 2:
                self._data[CONF_CHARGER_L2_PHASE] = l1
            if hub_phases < 3:
                self._data[CONF_CHARGER_L3_PHASE] = l1
            validate_charger_settings(self._data, errors)
            if errors:
                return self.async_show_form(
                    step_id="charger_current",
                    data_schema=f._charger_current_schema(self._data, hub_phases=hub_phases),
                    errors=errors,
                    last_step=False,
                )
            return await self.async_step_charger_timing()

        return self.async_show_form(
            step_id="charger_current",
            data_schema=f._charger_current_schema(defaults, hub_phases=hub_phases),
            errors=errors,
            last_step=False,
        )

    async def async_step_charger_timing(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        """Options charger step 3: Units and timing (final — saves)."""
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        ocpp_device_id = self.config_entry.data.get(CONF_OCPP_DEVICE_ID)
        detected_unit = await f._detect_charge_rate_unit(ocpp_device_id)

        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, **self._data},
            )

        return self.async_show_form(
            step_id="charger_timing",
            data_schema=f._charger_timing_schema(defaults, detected_unit=detected_unit),
            errors=errors,
            last_step=True,
        )

    async def async_step_plug(self, user_input: dict[str, Any] | None = None) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(user_input, f._PLUG_ENTITY_KEYS)
            self._data.update(user_input)
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, **self._data},
            )

        return self.async_show_form(
            step_id="plug",
            data_schema=f._plug_schema(defaults),
            errors=errors,
            last_step=True,
        )

