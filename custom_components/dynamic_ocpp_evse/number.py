# filepath: \\192.168.1.98\config\custom_components\dynamic_ocpp_evse\number.py
import logging
from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN, CONF_MIN_CURRENT, CONF_MAX_CURRENT, CONF_EVSE_MINIMUM_CHARGE_CURRENT, CONF_EVSE_MAXIMUM_CHARGE_CURRENT

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the number entities."""
    name = config_entry.data["name"]
    async_add_entities([
        EVSEMinCurrentSlider(hass, config_entry, name),
        EVSEMaxCurrentSlider(hass, config_entry, name)
    ])

class EVSEMinCurrentSlider(NumberEntity):
    """Slider for minimum current."""
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} EVSE Min Current"
        self._attr_unique_id = f"{config_entry.entry_id}_min_current"
        self._min = config_entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, 6)
        self._max = config_entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, 16)
        self._attr_step = 1
        self._attr_value = config_entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, 6)

    @property
    def native_min_value(self) -> float:
        return self._min

    @property
    def native_max_value(self) -> float:
        return self._max

    @property
    def native_value(self) -> float:
        return self._attr_value

    async def async_set_value(self, value: float) -> None:
        self._attr_value = value
        self.async_write_ha_state()

class EVSEMaxCurrentSlider(NumberEntity):
    """Slider for maximum current."""
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str):
        self._hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} EVSE Max Current"
        self._attr_unique_id = f"{config_entry.entry_id}_max_current"
        self._min = config_entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, 6)
        self._max = config_entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, 16)
        self._attr_step = 1
        self._attr_value = config_entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, 16)

    @property
    def native_min_value(self) -> float:
        return self._min

    @property
    def native_max_value(self) -> float:
        return self._max

    @property
    def native_value(self) -> float:
        return self._attr_value

    async def async_set_value(self, value: float) -> None:
        self._attr_value = value
        self.async_write_ha_state()