"""Shared entity mixins for hub and charger entities.

Provides HubEntityMixin and ChargerEntityMixin to eliminate duplicated
device_info, _write_to_*_data, and state-restore boilerplate across
number.py, select.py, switch.py, sensor.py, and button.py.
"""

import logging

from ..const import DOMAIN, CONF_NAME, CONF_HUB_ENTRY_ID, CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE, DEVICE_TYPE_PLUG, DEVICE_TYPE_HOT_WATER_TANK, DEVICE_TYPE_POWER_STATION
from .. import units

_LOGGER = logging.getLogger(__name__)


def _apply_restored_number(entity, last_state):
    """Set ``entity._attr_native_value`` from ``last_state``, clamped to range.

    A restored state is just the last value HA saw — it predates any change to
    the entity's bounds. Reconfiguring a charger's min/max, or shipping a new
    default range, otherwise brings the slider back outside its own
    native_min/native_max: HA renders it out of range and every consumer
    downstream inherits an impossible number (issue #38). Anything unparseable
    or missing leaves the constructor's default in place.
    """
    if units.is_unavailable(last_state):
        return
    try:
        value = float(last_state.state)
    except (ValueError, TypeError):
        return
    # A NaN would survive the clamp below (min/max propagate it) and land on the
    # slider as a permanently broken value.
    if units.is_unusable_number(value):
        return
    low, high = entity._attr_native_min_value, entity._attr_native_max_value
    clamped = min(max(value, low), high)
    if clamped != value:
        _LOGGER.info(
            "%s: restored value %s is outside the current %s–%s range — clamped to %s",
            entity._attr_name, value, low, high, clamped,
        )
    entity._attr_native_value = clamped


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
            "name": self.config_entry.data.get(CONF_NAME, "Site Load Management"),
            "manufacturer": "Load Juggler",
            "model": "Electrical System Hub",
        }

    def _write_to_hub_data(self, value):
        """Write a value to hass.data[DOMAIN]['hubs'][entry_id][_hub_data_key]."""
        hub_data = self.hass.data.get(DOMAIN, {}).get("hubs", {}).get(self.config_entry.entry_id)
        if hub_data is not None:
            hub_data[self._hub_data_key] = value

    async def _restore_and_publish_number(self):
        """Restore a NumberEntity's last state and publish to shared hub data."""
        _apply_restored_number(self, await self.async_get_last_state())
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
        from .. import get_hub_for_charger
        return get_hub_for_charger(self.hass, self.config_entry.entry_id)

    @property
    def device_info(self):
        device_type = self.config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        model = {
            DEVICE_TYPE_PLUG: "Smart Load",
            DEVICE_TYPE_HOT_WATER_TANK: "Hot Water Tank",
            DEVICE_TYPE_POWER_STATION: "Power Station",
        }.get(device_type, "EV Charger")
        hub = self._hub_entry
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Load Juggler",
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
        _apply_restored_number(self, await self.async_get_last_state())
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


class InverterEntityMixin:
    """Mixin for inverter entities (a power source linked to a hub).

    Provides:
      - device_info property (Inverter, linked to hub via via_device)
      - _write_to_inverter_data(value) using class attribute _inverter_data_key
    """

    _inverter_data_key = None

    @property
    def device_info(self):
        hub_entry_id = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Load Juggler",
            "model": "Inverter",
            "via_device": (DOMAIN, hub_entry_id) if hub_entry_id else None,
        }

    def _write_to_inverter_data(self, value):
        """Write to hass.data[DOMAIN]['inverters'][entry_id][_inverter_data_key].

        setdefault rather than a lookup: the switch can restore its state
        before the inverter entry's own setup has populated the bucket.
        """
        inverters = self.hass.data.setdefault(DOMAIN, {}).setdefault("inverters", {})
        inverters.setdefault(self.config_entry.entry_id, {})[
            self._inverter_data_key
        ] = value
