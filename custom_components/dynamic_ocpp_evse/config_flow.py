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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_CHARGIN_MODE_ENTITY_ID] = f"select.{user_input[CONF_ENTITY_ID]}_charging_mode"
            entry = await self.async_create_entry(title=user_input["name"], data=user_input)
            await self.hass.services.async_call(DOMAIN, "reset_ocpp_evse", {"entry_id": entry["entry_id"]})
            return entry

        # Fetch available entities and apply regex match
        entity_registry = async_get_entity_registry(self.hass)
        entities = entity_registry.entities
        default_phase_a = next((entity_id for entity_id in entities if re.match(r'sensor\..*m.*ac_current_a.*', entity_id)), None)
        default_phase_b = next((entity_id for entity_id in entities if re.match(r'sensor\..*m.*ac_current_b.*', entity_id)), None)
        default_phase_c = next((entity_id for entity_id in entities if re.match(r'sensor\..*m.*ac_current_c.*', entity_id)), None)
        default_evse_current_import = next((entity_id for entity_id in entities if re.match(r'sensor\..*current_import.*', entity_id)), None)
        default_evse_current_offered = next((entity_id for entity_id in entities if re.match(r'sensor\..*current_offered.*', entity_id)), None)
        default_max_import_power = next((entity_id for entity_id in entities if re.match(r'sensor\..*power_limit.*', entity_id)), None)

        # Update the schema with the default values
        step_user_data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Dynamic OCPP EVSE"): str,
                vol.Required(CONF_ENTITY_ID, default="dynamic_ocpp_evse"): str,
                vol.Required(CONF_PHASE_A_CURRENT_ENTITY_ID, default=default_phase_a): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                vol.Required(CONF_PHASE_B_CURRENT_ENTITY_ID, default=default_phase_b): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                vol.Required(CONF_PHASE_C_CURRENT_ENTITY_ID, default=default_phase_c): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                vol.Required(CONF_MAIN_BREAKER_RATING, default=25): int,
                vol.Required(CONF_INVERT_PHASES, default=False): bool,
                vol.Required(CONF_DEFAULT_CHARGE_CURRENT, default=6): int,
                vol.Required(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID, default=default_evse_current_import): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                vol.Required(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID, default=default_evse_current_offered): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                vol.Required(CONF_MAX_IMPORT_POWER_ENTITY_ID, default=default_max_import_power): selector({"entity": {"domain": "sensor", "device_class": "power"}}),
                vol.Required(CONF_PHASE_VOLTAGE, default=230): int,
                vol.Required(CONF_UPDATE_FREQUENCY, default=30): int,
                vol.Required(CONF_OCPP_PROFILE_TIMEOUT, default=35): int,
                vol.Required(CONF_CHARGE_PAUSE_DURATION, default=180): int,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=step_user_data_schema, errors=errors
        )
    
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Handle the reconfigure step."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if user_input is not None:
            user_input[CONF_CHARGIN_MODE_ENTITY_ID] = f"select.{user_input[CONF_ENTITY_ID]}_charging_mode"
            self.hass.config_entries.async_update_entry(entry, data=user_input)
            await self.hass.services.async_call(DOMAIN, "reset_ocpp_evse", {"entry_id": entry.entry_id})
            return self.async_abort(reason="reconfigure_successful")

        if entry:
            initial_data = {
                CONF_NAME: entry.data.get(CONF_NAME),
                CONF_ENTITY_ID: entry.data.get(CONF_ENTITY_ID),
                CONF_PHASE_A_CURRENT_ENTITY_ID: entry.data.get(CONF_PHASE_A_CURRENT_ENTITY_ID),
                CONF_PHASE_B_CURRENT_ENTITY_ID: entry.data.get(CONF_PHASE_B_CURRENT_ENTITY_ID),
                CONF_PHASE_C_CURRENT_ENTITY_ID: entry.data.get(CONF_PHASE_C_CURRENT_ENTITY_ID),
                CONF_CHARGIN_MODE_ENTITY_ID: entry.data.get(CONF_CHARGIN_MODE_ENTITY_ID),
                CONF_MAIN_BREAKER_RATING: entry.data.get(CONF_MAIN_BREAKER_RATING, 25),
                CONF_INVERT_PHASES: entry.data.get(CONF_INVERT_PHASES, False),
                CONF_DEFAULT_CHARGE_CURRENT: entry.data.get(CONF_DEFAULT_CHARGE_CURRENT, 6),
                CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID),
                CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: entry.data.get(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID),
                CONF_MAX_IMPORT_POWER_ENTITY_ID: entry.data.get(CONF_MAX_IMPORT_POWER_ENTITY_ID),
                CONF_PHASE_VOLTAGE: entry.data.get(CONF_PHASE_VOLTAGE, 230),
                CONF_UPDATE_FREQUENCY: entry.data.get(CONF_UPDATE_FREQUENCY, 30),
                CONF_OCPP_PROFILE_TIMEOUT: entry.data.get(CONF_OCPP_PROFILE_TIMEOUT, 35),
                CONF_CHARGE_PAUSE_DURATION: entry.data.get(CONF_CHARGE_PAUSE_DURATION, 180),
            }
            data_schema = vol.Schema(
                {
                    vol.Required(CONF_NAME, default=initial_data[CONF_NAME]): str,
                    vol.Required(CONF_ENTITY_ID, default=initial_data[CONF_ENTITY_ID]): str,
                    vol.Required(CONF_PHASE_A_CURRENT_ENTITY_ID, default=initial_data[CONF_PHASE_A_CURRENT_ENTITY_ID]): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                    vol.Required(CONF_PHASE_B_CURRENT_ENTITY_ID, default=initial_data[CONF_PHASE_B_CURRENT_ENTITY_ID]): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                    vol.Required(CONF_PHASE_C_CURRENT_ENTITY_ID, default=initial_data[CONF_PHASE_C_CURRENT_ENTITY_ID]): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                    vol.Required(CONF_CHARGIN_MODE_ENTITY_ID, default=initial_data[CONF_CHARGIN_MODE_ENTITY_ID]): selector({"entity": {"domain": "select"}}),
                    vol.Required(CONF_MAIN_BREAKER_RATING, default=initial_data[CONF_MAIN_BREAKER_RATING]): int,
                    vol.Required(CONF_INVERT_PHASES, default=initial_data[CONF_INVERT_PHASES]): bool,
                    vol.Required(CONF_DEFAULT_CHARGE_CURRENT, default=initial_data[CONF_DEFAULT_CHARGE_CURRENT]): int,
                    vol.Required(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID, default=initial_data[CONF_EVSE_CURRENT_IMPORT_ENTITY_ID]): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                    vol.Required(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID, default=initial_data[CONF_EVSE_CURRENT_OFFERED_ENTITY_ID]): selector({"entity": {"domain": "sensor", "device_class": "current"}}),
                    vol.Required(CONF_MAX_IMPORT_POWER_ENTITY_ID, default=initial_data[CONF_MAX_IMPORT_POWER_ENTITY_ID]): selector({"entity": {"domain": "sensor", "device_class": "power"}}),
                    vol.Required(CONF_PHASE_VOLTAGE, default=initial_data[CONF_PHASE_VOLTAGE]): int,
                    vol.Required(CONF_UPDATE_FREQUENCY, default=initial_data[CONF_UPDATE_FREQUENCY]): int,
                    vol.Required(CONF_OCPP_PROFILE_TIMEOUT, default=initial_data[CONF_OCPP_PROFILE_TIMEOUT]): int, 
                    vol.Required(CONF_CHARGE_PAUSE_DURATION, default=initial_data[CONF_CHARGE_PAUSE_DURATION]): int,
                }
            )

            return self.async_show_form(
                step_id="reconfigure",
                data_schema=data_schema,
                errors=errors
            )