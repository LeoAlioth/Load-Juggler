import logging
from homeassistant.components.sensor import SensorEntity
from ..const import *
from ..helpers import get_entry_value
from .mixins import GroupEntityMixin

_LOGGER = logging.getLogger(__name__)


class LoadJugglerCircuitGroupSensor(GroupEntityMixin, SensorEntity):
    """Sensor showing circuit group allocation and headroom."""

    def __init__(self, hass, config_entry, name, entity_id, hub_entry_id):
        self.hass = hass
        self.config_entry = config_entry
        self._hub_entry_id = hub_entry_id
        self._attr_name = f"{name} Circuit Allocation"
        self._attr_unique_id = f"{entity_id}_circuit_allocation"
        self._attr_native_unit_of_measurement = "A"
        self._attr_device_class = "current"
        self._attr_icon = "mdi:current-ac"
        self._state = None
        self._headroom = None
        self._per_phase_draw = None
        self._current_limit = None
        self._member_ids = None

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        attrs = {}
        if self._current_limit is not None:
            attrs["current_limit"] = self._current_limit
        if self._headroom is not None:
            attrs["headroom"] = self._headroom
        if self._per_phase_draw is not None:
            attrs["phase_a_draw"] = round(self._per_phase_draw.get("A", 0), 1)
            attrs["phase_b_draw"] = round(self._per_phase_draw.get("B", 0), 1)
            attrs["phase_c_draw"] = round(self._per_phase_draw.get("C", 0), 1)
        if self._member_ids is not None:
            attrs["member_count"] = len(self._member_ids)
        return attrs

    async def async_update(self):
        try:
            hub_data = (
                self.hass.data.get(DOMAIN, {})
                .get("hub_data", {})
                .get(self._hub_entry_id, {})
            )
            all_group_data = hub_data.get("group_data", {})
            my_data = all_group_data.get(self.config_entry.entry_id)
            if my_data:
                self._state = my_data.get("max_phase_draw", 0)
                self._headroom = my_data.get("headroom", 0)
                self._per_phase_draw = my_data.get("per_phase_draw")
                self._current_limit = my_data.get("current_limit")
                self._member_ids = my_data.get("member_ids")
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)
