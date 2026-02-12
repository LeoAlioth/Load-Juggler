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

_LOGGER = logging.getLogger(__name__)


class DynamicOcppEvseConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dynamic OCPP EVSE."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        self._data = {}
        self._discovered_chargers = []
        self._selected_charger = None

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
            entity_registry = async_get_entity_registry(self.hass)
            entities = entity_registry.entities
            entity_ids = entities.keys()
            
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

            # Build list of current/power sensors for optional phase selectors
            current_power_entities = ['None']
            for state in self.hass.states.async_all():
                entity_id = state.entity_id
                if entity_id.startswith('sensor.'):
                    device_class = state.attributes.get('device_class')
                    if device_class in ['current', 'power']:
                        current_power_entities.append(entity_id)
            current_power_entities = sorted(current_power_entities)

            data_schema = vol.Schema({
                vol.Required(CONF_PHASE_A_CURRENT_ENTITY_ID, default=default_phase_a): selector({
                    "entity": {"domain": "sensor", "device_class": ["current", "power"]}
                }),
                vol.Optional(CONF_PHASE_B_CURRENT_ENTITY_ID, default=default_phase_b or 'None'): selector({
                    "select": {"options": current_power_entities}
                }),
                vol.Optional(CONF_PHASE_C_CURRENT_ENTITY_ID, default=default_phase_c or 'None'): selector({
                    "select": {"options": current_power_entities}
                }),
                vol.Required(CONF_MAIN_BREAKER_RATING, default=DEFAULT_MAIN_BREAKER_RATING): int,
                vol.Required(CONF_INVERT_PHASES, default=False): bool,
                vol.Required(CONF_MAX_IMPORT_POWER_ENTITY_ID, default=default_max_import_power): selector({
                    "entity": {"domain": ["sensor", "input_number"], "device_class": "power"}
                }),
                vol.Required(CONF_PHASE_VOLTAGE, default=DEFAULT_PHASE_VOLTAGE): int,
                vol.Required(CONF_EXCESS_EXPORT_THRESHOLD, default=DEFAULT_EXCESS_EXPORT_THRESHOLD): int,
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
            self._data.update(user_input)
            
            # Generate entity IDs for hub-created entities
            entity_id = self._data.get(CONF_ENTITY_ID)
            self._data[CONF_CHARGIN_MODE_ENTITY_ID] = f"select.{entity_id}_charging_mode"
            self._data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = f"number.{entity_id}_home_battery_soc_target"
            self._data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = f"switch.{entity_id}_allow_grid_charging"
            self._data[CONF_POWER_BUFFER_ENTITY_ID] = f"number.{entity_id}_power_buffer"
            
            return self.async_create_entry(
                title=self._data[CONF_NAME],
                data=self._data
            )

        # Get all battery and power sensors
        battery_entities = []
        power_entities = []
        
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            if entity_id.startswith('sensor.'):
                device_class = state.attributes.get('device_class')
                if device_class == 'battery':
                    battery_entities.append(entity_id)
                elif device_class == 'power':
                    power_entities.append(entity_id)
        
        battery_soc_options = ['None'] + sorted(battery_entities)
        battery_power_options = ['None'] + sorted(power_entities)
        
        data_schema = vol.Schema({
            vol.Optional(CONF_BATTERY_SOC_ENTITY_ID, default='None'): selector({
                "select": {"options": battery_soc_options}
            }),
            vol.Optional(CONF_BATTERY_POWER_ENTITY_ID, default='None'): selector({
                "select": {"options": battery_power_options}
            }),
            vol.Optional(CONF_BATTERY_MAX_CHARGE_POWER, default=DEFAULT_BATTERY_MAX_POWER): int,
            vol.Optional(CONF_BATTERY_MAX_DISCHARGE_POWER, default=DEFAULT_BATTERY_MAX_POWER): int,
        })
        
        return self.async_show_form(
            step_id="hub_battery",
            data_schema=data_schema,
            errors=errors,
            last_step=True
        )

    # ==================== CHARGER CONFIGURATION STEPS ====================

    async def async_step_select_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 1: Select which hub to add charger to."""
        errors: dict[str, str] = {}
        hubs = self._get_hub_entries()
        
        if user_input is not None:
            self._data[CONF_HUB_ENTRY_ID] = user_input["hub_entry_id"]
            return await self.async_step_discover_chargers()
        
        # If only one hub, skip selection
        if len(hubs) == 1:
            self._data[CONF_HUB_ENTRY_ID] = hubs[0].entry_id
            return await self.async_step_discover_chargers()
        
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

    async def async_step_charger_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3: Configure charger settings."""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            self._data.update(user_input)
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
            
            return self.async_create_entry(
                title=f"{self._selected_charger['name']} Charger",
                data=self._data
            )
        
        # Calculate next priority number
        existing_chargers = self._get_charger_entries()
        next_priority = len(existing_chargers) + 1
        
        data_schema = vol.Schema({
            vol.Required(CONF_CHARGER_PRIORITY, default=next_priority): selector({
                "number": {"min": 1, "max": 10, "mode": "box"}
            }),
            vol.Required(CONF_EVSE_MINIMUM_CHARGE_CURRENT, default=DEFAULT_MIN_CHARGE_CURRENT): int,
            vol.Required(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, default=DEFAULT_MAX_CHARGE_CURRENT): int,
            vol.Required(CONF_UPDATE_FREQUENCY, default=DEFAULT_UPDATE_FREQUENCY): int,
            vol.Required(CONF_OCPP_PROFILE_TIMEOUT, default=DEFAULT_OCPP_PROFILE_TIMEOUT): int,
            vol.Required(CONF_CHARGE_PAUSE_DURATION, default=DEFAULT_CHARGE_PAUSE_DURATION): int,
            vol.Required(CONF_STACK_LEVEL, default=DEFAULT_STACK_LEVEL): int,
        })
        
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
        else:
            return await self.async_step_reconfigure_charger()

    async def async_step_reconfigure_hub_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure hub grid settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        
        if user_input is not None:
            self._data.update(user_input)
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, **user_input})
            return await self.async_step_reconfigure_hub_battery()

        try:
            # Build list of current/power sensors
            current_power_entities = ['None']
            for state in self.hass.states.async_all():
                entity_id = state.entity_id
                if entity_id.startswith('sensor.'):
                    device_class = state.attributes.get('device_class')
                    if device_class in ['current', 'power']:
                        current_power_entities.append(entity_id)
            current_power_entities = sorted(current_power_entities)

            data_schema = vol.Schema({
                vol.Required(CONF_PHASE_A_CURRENT_ENTITY_ID, default=entry.data.get(CONF_PHASE_A_CURRENT_ENTITY_ID)): selector({
                    "entity": {"domain": "sensor", "device_class": ["current", "power"]}
                }),
                vol.Optional(CONF_PHASE_B_CURRENT_ENTITY_ID, default=entry.data.get(CONF_PHASE_B_CURRENT_ENTITY_ID, 'None')): selector({
                    "select": {"options": current_power_entities}
                }),
                vol.Optional(CONF_PHASE_C_CURRENT_ENTITY_ID, default=entry.data.get(CONF_PHASE_C_CURRENT_ENTITY_ID, 'None')): selector({
                    "select": {"options": current_power_entities}
                }),
                vol.Required(CONF_MAIN_BREAKER_RATING, default=entry.data.get(CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING)): int,
                vol.Required(CONF_INVERT_PHASES, default=entry.data.get(CONF_INVERT_PHASES, False)): bool,
                vol.Required(CONF_MAX_IMPORT_POWER_ENTITY_ID, default=entry.data.get(CONF_MAX_IMPORT_POWER_ENTITY_ID)): selector({
                    "entity": {"domain": ["sensor", "input_number"], "device_class": "power"}
                }),
                vol.Required(CONF_PHASE_VOLTAGE, default=entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)): int,
                vol.Required(CONF_EXCESS_EXPORT_THRESHOLD, default=entry.data.get(CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD)): int,
            })
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
        
        if user_input is not None:
            self._data.update(user_input)
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, **self._data})
            
            # Call reset service
            try:
                await self.hass.services.async_call(DOMAIN, "reset_ocpp_evse", {"entry_id": entry.entry_id})
            except Exception:
                pass
            
            return self.async_abort(reason="reconfigure_successful")

        # Get battery and power sensors
        battery_entities = []
        power_entities = []
        
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            if entity_id.startswith('sensor.'):
                device_class = state.attributes.get('device_class')
                if device_class == 'battery':
                    battery_entities.append(entity_id)
                elif device_class == 'power':
                    power_entities.append(entity_id)
        
        battery_soc_options = ['None'] + sorted(battery_entities)
        battery_power_options = ['None'] + sorted(power_entities)
        
        data_schema = vol.Schema({
            vol.Optional(CONF_BATTERY_SOC_ENTITY_ID, default=entry.data.get(CONF_BATTERY_SOC_ENTITY_ID, 'None')): selector({
                "select": {"options": battery_soc_options}
            }),
            vol.Optional(CONF_BATTERY_POWER_ENTITY_ID, default=entry.data.get(CONF_BATTERY_POWER_ENTITY_ID, 'None')): selector({
                "select": {"options": battery_power_options}
            }),
            vol.Optional(CONF_BATTERY_MAX_CHARGE_POWER, default=entry.data.get(CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER)): int,
            vol.Optional(CONF_BATTERY_MAX_DISCHARGE_POWER, default=entry.data.get(CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER)): int,
        })
        
        return self.async_show_form(
            step_id="reconfigure_hub_battery",
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
        
        if user_input is not None:
            self._data.update(user_input)
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, **user_input})
            return self.async_abort(reason="reconfigure_successful")
        
        data_schema = vol.Schema({
            vol.Required(CONF_CHARGER_PRIORITY, default=entry.data.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY)): selector({
                "number": {"min": 1, "max": 10, "mode": "box"}
            }),
            vol.Required(CONF_EVSE_MINIMUM_CHARGE_CURRENT, default=entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)): int,
            vol.Required(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, default=entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)): int,
            vol.Required(CONF_UPDATE_FREQUENCY, default=entry.data.get(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)): int,
            vol.Required(CONF_OCPP_PROFILE_TIMEOUT, default=entry.data.get(CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT)): int,
            vol.Required(CONF_CHARGE_PAUSE_DURATION, default=entry.data.get(CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION)): int,
            vol.Required(CONF_STACK_LEVEL, default=entry.data.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL)): int,
        })
        
        return self.async_show_form(
            step_id="reconfigure_charger",
            data_schema=data_schema,
            errors=errors,
            last_step=True
        )
