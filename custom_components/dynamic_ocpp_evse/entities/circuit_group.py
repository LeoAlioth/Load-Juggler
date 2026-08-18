import logging
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from ..const import *
from ..helpers import get_entry_value
from .mixins import GroupEntityMixin, SiteCycleConsumerMixin

_LOGGER = logging.getLogger(__name__)


class LoadJugglerCircuitGroupSensor(
    SiteCycleConsumerMixin, GroupEntityMixin, SensorEntity
):
    """Sensor showing circuit group allocation and headroom."""

    _attr_native_unit_of_measurement = "A"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:current-ac"

    def __init__(self, hass, config_entry, name, entity_id):
        self._init_entity(
            hass,
            config_entry,
            f"{name} Circuit Allocation",
            f"{entity_id}_circuit_allocation",
        )
        # The hub is read from the group entry's own data (GroupEntityMixin),
        # not passed in: it was the same value, from the same place, twice.
        self._attr_native_value = None
        self._headroom = None
        self._per_phase_draw = None
        self._current_limit = None
        self._member_ids = None

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

    def _read_site_data(self):
        all_group_data = self._hub_data().get("group_data", {})
        my_data = all_group_data.get(self.config_entry.entry_id)
        if my_data:
            self._attr_native_value = my_data.get("max_phase_draw", 0)
            self._headroom = my_data.get("headroom", 0)
            self._per_phase_draw = my_data.get("per_phase_draw")
            self._current_limit = my_data.get("current_limit")
            self._member_ids = my_data.get("member_ids")
