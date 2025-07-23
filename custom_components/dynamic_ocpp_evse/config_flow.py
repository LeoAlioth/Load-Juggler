import re
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from typing import Any
from .const import *  # Make sure DOMAIN is defined in const.py

class DynamicOcppEvseConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dynamic OCPP EVSE."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        self._data = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Initial step: only ask for name and entity_id, then continue to grid step."""
        # If reconfiguring, skip user step and go directly to grid
        if hasattr(self, 'context') and self.context.get("entry_id"):
            return await self.async_step_grid()

        errors: dict[str, str] = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_grid()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Dynamic OCPP EVSE"): str,
                vol.Required(CONF_ENTITY_ID, default="dynamic_ocpp_evse"): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors, last_step=False
        )

    async def async_step_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the grid step (after user step)."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id")) if hasattr(self, 'context') and self.context.get("entry_id") else None
        if user_input is not None:
            # Try to get entity_id from self._data or from entry
            entity_id = self._data.get(CONF_ENTITY_ID)
            if not entity_id and entry:
                entity_id = entry.data.get(CONF_ENTITY_ID)
            if not entity_id:
                errors["base"] = "missing_entity_id"
                # Show the form again with an error if entity_id is missing
                return self.async_show_form(
                    step_id="grid", data_schema=step_grid_data_schema, errors=errors, last_step=False
                )
            user_input[CONF_CHARGIN_MODE_ENTITY_ID] = f"select.{entity_id}_charging_mode"
            user_input[CONF_MIN_CURRENT_ENTITY_ID] = f"number.{entity_id}_min_current"
            user_input[CONF_MAX_CURRENT_ENTITY_ID] = f"number.{entity_id}_max_current"
            self._data.update(user_input)
            # Per-step config entry update during reconfiguration
            if entry:
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, **self._data})
            return await self.async_step_evse()

        try:
            # Fetch available entities and apply regex match
            entity_registry = async_get_entity_registry(self.hass)
            entities = entity_registry.entities
            entity_ids = entities.keys()
            default_phase_a = next((entity_id for entity_id in entity_ids if re.match(r'sensor\..*m.*ac_current_a.*', entity_id)), None)
            default_phase_b = next((entity_id for entity_id in entity_ids if re.match(r'sensor\..*m.*ac_current_b.*', entity_id)), None)
            default_phase_c = next((entity_id for entity_id in entity_ids if re.match(r'sensor\..*m.*ac_current_c.*', entity_id)), None)
            default_evse_current_import = next((entity_id for entity_id in entity_ids if re.match(r'sensor\..*current_import.*', entity_id)), None)
            default_evse_current_offered = next((entity_id for entity_id in entity_ids if re.match(r'sensor\..*current_offered.*', entity_id)), None)
            default_max_import_power = next((entity_id for entity_id in entity_ids if re.match(r'sensor\..*power_limit.*', entity_id)), None)

            # Update the schema with the default values
            step_grid_data_schema = vol.Schema(
                {
                    vol.Required(CONF_PHASE_A_CURRENT_ENTITY_ID, default=entry.data.get(CONF_PHASE_A_CURRENT_ENTITY_ID, default_phase_a) if entry else default_phase_a): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                    vol.Required(CONF_PHASE_B_CURRENT_ENTITY_ID, default=entry.data.get(CONF_PHASE_B_CURRENT_ENTITY_ID, default_phase_b) if entry else default_phase_b): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                    vol.Required(CONF_PHASE_C_CURRENT_ENTITY_ID, default=entry.data.get(CONF_PHASE_C_CURRENT_ENTITY_ID, default_phase_c) if entry else default_phase_c): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                    vol.Required(CONF_MAIN_BREAKER_RATING, default=entry.data.get(CONF_MAIN_BREAKER_RATING, 25) if entry else 25): int,
                    vol.Required(CONF_INVERT_PHASES, default=entry.data.get(CONF_INVERT_PHASES, False) if entry else False): bool,
                    vol.Required(CONF_MAX_IMPORT_POWER_ENTITY_ID, default=entry.data.get(CONF_MAX_IMPORT_POWER_ENTITY_ID, default_max_import_power) if entry else default_max_import_power): selector({"entity": {"domain": "sensor", "device_class": "power"}}),
                    vol.Required(CONF_PHASE_VOLTAGE, default=entry.data.get(CONF_PHASE_VOLTAGE, 230) if entry else 230): int,
                    vol.Required(CONF_UPDATE_FREQUENCY, default=entry.data.get(CONF_UPDATE_FREQUENCY, 5) if entry else 5): int,
                    vol.Required(CONF_OCPP_PROFILE_TIMEOUT, default=entry.data.get(CONF_OCPP_PROFILE_TIMEOUT, 90) if entry else 90): int,
                    vol.Required(CONF_CHARGE_PAUSE_DURATION, default=entry.data.get(CONF_CHARGE_PAUSE_DURATION, 180) if entry else 180): int,
                    vol.Required(CONF_EXCESS_EXPORT_THRESHOLD, default=entry.data.get(CONF_EXCESS_EXPORT_THRESHOLD, 13000) if entry else 13000): int,
                }
            )
        except Exception as e:
            import logging
            _LOGGER = logging.getLogger(__name__)
            _LOGGER.error("Error in async_step_grid: %s", e, exc_info=True)
            errors["base"] = "unknown"
            # Fallback schema if error occurs
            step_grid_data_schema = vol.Schema({})

        return self.async_show_form(
            step_id="grid", data_schema=step_grid_data_schema, errors=errors, last_step=False
        )
    
    async def async_step_evse(self, user_input: dict[str, Any] | None = None):
        """Handle the EVSE configuration step."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"]) if hasattr(self, 'context') and self.context.get("entry_id") else None
        if user_input is not None:
            self._data.update(user_input)
            # Per-step config entry update during reconfiguration
            if entry:
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, **self._data})
            return await self.async_step_battery()

        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry:
            initial_data = {
                CONF_EVSE_MINIMUM_CHARGE_CURRENT: entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, 6),
                CONF_EVSE_MAXIMUM_CHARGE_CURRENT: entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, 16),
                CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID),
                CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: entry.data.get(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID),
                CONF_OCPP_PROFILE_TIMEOUT: entry.data.get(CONF_OCPP_PROFILE_TIMEOUT, 90),
                CONF_CHARGE_PAUSE_DURATION: entry.data.get(CONF_CHARGE_PAUSE_DURATION, 180),
                CONF_UPDATE_FREQUENCY: entry.data.get(CONF_UPDATE_FREQUENCY, 5),
            }
            data_schema = vol.Schema(
                {
                    vol.Required(CONF_EVSE_MINIMUM_CHARGE_CURRENT, default=initial_data[CONF_EVSE_MINIMUM_CHARGE_CURRENT]): int,
                    vol.Required(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, default=initial_data[CONF_EVSE_MAXIMUM_CHARGE_CURRENT]): int,
                    vol.Required(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID, default=initial_data[CONF_EVSE_CURRENT_IMPORT_ENTITY_ID]): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                    vol.Required(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID, default=initial_data[CONF_EVSE_CURRENT_OFFERED_ENTITY_ID]): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                    vol.Required(CONF_OCPP_PROFILE_TIMEOUT, default=initial_data[CONF_OCPP_PROFILE_TIMEOUT]): int,
                    vol.Required(CONF_CHARGE_PAUSE_DURATION, default=initial_data[CONF_CHARGE_PAUSE_DURATION]): int,
                    vol.Required(CONF_UPDATE_FREQUENCY, default=initial_data[CONF_UPDATE_FREQUENCY]): int,
                }
            )
            description = "The integration will create 'Charging Mode', 'Min Current', and 'Max Current' entities for you after setup. You can adjust them later from the entity settings."
            return self.async_show_form(
                step_id="evse",
                data_schema=data_schema,
                errors=errors,
                description_placeholders={"info": description},
                last_step=False
            )
        return self.async_show_form(
            step_id="evse",
            data_schema=vol.Schema({}),
            errors=errors,
            last_step=False
        )

    async def async_step_battery(self, user_input: dict[str, Any] | None = None):
        """Handle the battery configuration step."""
        import logging
        _LOGGER = logging.getLogger(__name__)
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"]) if hasattr(self, 'context') and self.context.get("entry_id") else None
        if user_input is not None:
            _LOGGER.debug("async_step_battery user_input: %s", user_input)
            self._data.update(user_input)
            # Per-step config entry update during reconfiguration
            if entry:
                _LOGGER.debug("Updating config entry in async_step_battery: %s", entry.entry_id)
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, **self._data})
                await self.hass.services.async_call(DOMAIN, "reset_ocpp_evse", {"entry_id": entry.entry_id})
                return self.async_abort(reason="Reconfiguration complete")
            else:
                return self.async_create_entry(title=self._data["name"], data=self._data)

        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry:
            # Get all battery and power sensors for select options
            entity_registry = async_get_entity_registry(self.hass)
            entities = entity_registry.entities
            battery_entities = [entity_id for entity_id in entities if entity_id.startswith('sensor.') and entities[entity_id].device_class == 'battery']
            power_entities = [entity_id for entity_id in entities if entity_id.startswith('sensor.') and entities[entity_id].device_class == 'power']
            battery_soc_options = ['None'] + battery_entities
            battery_power_options = ['None'] + power_entities
            initial_data = {
                CONF_BATTERY_SOC_ENTITY_ID: entry.data.get(CONF_BATTERY_SOC_ENTITY_ID) or 'None',
                CONF_BATTERY_POWER_ENTITY_ID: entry.data.get(CONF_BATTERY_POWER_ENTITY_ID) or 'None',
                CONF_BATTERY_MAX_CHARGE_POWER: entry.data.get(CONF_BATTERY_MAX_CHARGE_POWER, 5000),
                CONF_BATTERY_MAX_DISCHARGE_POWER: entry.data.get(CONF_BATTERY_MAX_DISCHARGE_POWER, 5000),
            }
            _LOGGER.debug("async_step_battery initial_data: %s", initial_data)
            data_schema = vol.Schema(
                {
                    vol.Optional(CONF_BATTERY_SOC_ENTITY_ID, default=initial_data[CONF_BATTERY_SOC_ENTITY_ID]): selector({"select": {"options": battery_soc_options}}),
                    vol.Optional(CONF_BATTERY_POWER_ENTITY_ID, default=initial_data[CONF_BATTERY_POWER_ENTITY_ID]): selector({"select": {"options": battery_power_options}}),
                    vol.Optional(CONF_BATTERY_MAX_CHARGE_POWER, default=initial_data[CONF_BATTERY_MAX_CHARGE_POWER]): int,
                    vol.Optional(CONF_BATTERY_MAX_DISCHARGE_POWER, default=initial_data[CONF_BATTERY_MAX_DISCHARGE_POWER]): int,
                }
            )
            description = "The integration will create a 'Battery SOC Target' number entity for you after setup. You can adjust it later from the entity settings."
            return self.async_show_form(
                step_id="battery",
                data_schema=data_schema,
                errors=errors,
                description_placeholders={"info": description},
                last_step=True
            )
        _LOGGER.debug("async_step_battery: No entry found for context entry_id")
        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema({}),
            errors=errors,
            last_step=True
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Entry point for reconfiguration, start at the grid step."""
        self._data = {}
        return await self.async_step_grid()