"""Shared entity mixins for hub and charger entities.

Provides HubEntityMixin and ChargerEntityMixin to eliminate duplicated
device_info, _write_to_*_data, and state-restore boilerplate across
number.py, select.py, switch.py, sensor.py, and button.py.
"""

from .const import DOMAIN, CONF_NAME, CONF_HUB_ENTRY_ID, CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE, DEVICE_TYPE_PLUG


class HubEntityMixin:
    """Mixin for hub-level entities.

    Provides:
      - device_info property (Electrical System Hub)
      - _write_to_hub_data(value) using class attribute _hub_data_key
      - _restore_and_publish_number() for NumberEntity + RestoreEntity subclasses

    Subclasses must set _hub_data_key to the dict key in hass.data[DOMAIN]["hubs"][entry_id].
    """

    _hub_data_key = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE"),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "Electrical System Hub",
        }

    def _write_to_hub_data(self, value):
        """Write a value to hass.data[DOMAIN]['hubs'][entry_id][_hub_data_key]."""
        hub_data = self.hass.data.get(DOMAIN, {}).get("hubs", {}).get(self.config_entry.entry_id)
        if hub_data is not None:
            hub_data[self._hub_data_key] = value

    async def _restore_and_publish_number(self):
        """Restore a NumberEntity's last state and publish to shared hub data."""
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ('unknown', 'unavailable'):
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                pass
        self.async_write_ha_state()
        self._write_to_hub_data(self._attr_native_value)


class ChargerEntityMixin:
    """Mixin for charger-level entities.

    Provides:
      - device_info property (EV Charger / Smart Load, linked to hub)
      - _write_to_charger_data(value) using class attribute _charger_data_key
      - _restore_and_publish_number() for NumberEntity + RestoreEntity subclasses

    Subclasses must set _charger_data_key to the dict key in
    hass.data[DOMAIN]["chargers"][entry_id].

    Uses self.hub_entry if stored, otherwise looks up via get_hub_for_charger().
    """

    _charger_data_key = None

    @property
    def _hub_entry(self):
        """Get the hub ConfigEntry for this charger."""
        if hasattr(self, 'hub_entry') and self.hub_entry:
            return self.hub_entry
        from . import get_hub_for_charger
        return get_hub_for_charger(self.hass, self.config_entry.entry_id)

    @property
    def device_info(self):
        device_type = self.config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        model = "Smart Load" if device_type == DEVICE_TYPE_PLUG else "EV Charger"
        hub = self._hub_entry
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": model,
            "via_device": (DOMAIN, hub.entry_id) if hub else None,
        }

    def _write_to_charger_data(self, value):
        """Write a value to hass.data[DOMAIN]['chargers'][entry_id][_charger_data_key]."""
        charger_data = self.hass.data.get(DOMAIN, {}).get("chargers", {}).get(self.config_entry.entry_id)
        if charger_data is not None:
            charger_data[self._charger_data_key] = value

    async def _restore_and_publish_number(self):
        """Restore a NumberEntity's last state and publish to shared charger data."""
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ('unknown', 'unavailable'):
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                pass
        self.async_write_ha_state()
        self._write_to_charger_data(self._attr_native_value)


class GroupEntityMixin:
    """Mixin for circuit group entities.

    Provides:
      - device_info property (Circuit Group, linked to hub via via_device)
    """

    @property
    def device_info(self):
        hub_entry_id = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Load Juggler",
            "model": "Circuit Group",
            "via_device": (DOMAIN, hub_entry_id) if hub_entry_id else None,
        }
