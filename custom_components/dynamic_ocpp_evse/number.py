# filepath: \\192.168.1.98\config\custom_components\dynamic_ocpp_evse\number.py
import logging
from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .const import DOMAIN, CONF_MIN_CURRENT, CONF_MAX_CURRENT, CONF_EVSE_MINIMUM_CHARGE_CURRENT, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, CONF_POWER_BUFFER

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the number entities."""
    name = config_entry.data["name"]
    
    # Always create the entities - they will be registered if they don't exist
    entities = [
        EVSEMinCurrentSlider(hass, config_entry, name),
        EVSEMaxCurrentSlider(hass, config_entry, name),
        BatterySOCTargetSlider(hass, config_entry, name),
        PowerBufferSlider(hass, config_entry, name),
    ]
    
    _LOGGER.info(f"Setting up number entities: {[entity.unique_id for entity in entities]}")
    async_add_entities(entities)

class EVSEMinCurrentSlider(NumberEntity, RestoreEntity):
    """Slider for minimum current."""
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} EVSE Min Current"
        self._attr_unique_id = f"{config_entry.entry_id}_min_current"
        self._attr_native_min_value = config_entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, 6)
        self._attr_native_max_value = config_entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, 16)
        self._attr_native_step = 1
        self._attr_native_value = config_entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, 6)

    async def async_added_to_hass(self) -> None:
        """Restore last state when added to hass."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ('unknown', 'unavailable'):
            try:
                self._attr_native_value = float(last_state.state)
                _LOGGER.debug(f"Restored {self._attr_name} to: {self._attr_native_value}")
            except (ValueError, TypeError):
                _LOGGER.debug(f"Could not restore {self._attr_name}, using default")

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()

class EVSEMaxCurrentSlider(NumberEntity, RestoreEntity):
    """Slider for maximum current."""
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str):
        self._hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} EVSE Max Current"
        self._attr_unique_id = f"{config_entry.entry_id}_max_current"
        self._attr_native_min_value = config_entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, 6)
        self._attr_native_max_value = config_entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, 16)
        self._attr_native_step = 1
        self._attr_native_value = config_entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, 16)

    async def async_added_to_hass(self) -> None:
        """Restore last state when added to hass."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ('unknown', 'unavailable'):
            try:
                self._attr_native_value = float(last_state.state)
                _LOGGER.debug(f"Restored {self._attr_name} to: {self._attr_native_value}")
            except (ValueError, TypeError):
                _LOGGER.debug(f"Could not restore {self._attr_name}, using default")

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()

class BatterySOCTargetSlider(NumberEntity, RestoreEntity):
    """Slider for battery SOC target (10-100%, step 5)."""
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Home Battery SOC Target"
        self._attr_unique_id = f"{config_entry.entry_id}_battery_soc_target"
        self._attr_native_min_value = 10
        self._attr_native_max_value = 100
        self._attr_native_step = 5
        self._attr_native_value = 80  # Default SOC target, can be customized

    async def async_added_to_hass(self) -> None:
        """Restore last state when added to hass."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ('unknown', 'unavailable'):
            try:
                self._attr_native_value = float(last_state.state)
                _LOGGER.debug(f"Restored {self._attr_name} to: {self._attr_native_value}")
            except (ValueError, TypeError):
                _LOGGER.debug(f"Could not restore {self._attr_name}, using default")

    async def async_set_native_value(self, value: float) -> None:
        # Clamp to step and range
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()

class PowerBufferSlider(NumberEntity, RestoreEntity):
    """Slider for power buffer in Watts (0-5000W, step 100).
    
    This buffer reduces the target charging power in Standard mode to prevent
    frequent charging stops. If the buffered target is below minimum charge rate,
    the system can use up to the full available power.
    """
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Power Buffer"
        self._attr_unique_id = f"{config_entry.entry_id}_power_buffer"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 5000
        self._attr_native_step = 100
        self._attr_native_value = 0  # Default: no buffer
        self._attr_native_unit_of_measurement = "W"

    async def async_added_to_hass(self) -> None:
        """Restore last state when added to hass."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ('unknown', 'unavailable'):
            try:
                self._attr_native_value = float(last_state.state)
                _LOGGER.debug(f"Restored {self._attr_name} to: {self._attr_native_value}")
            except (ValueError, TypeError):
                _LOGGER.debug(f"Could not restore {self._attr_name}, using default")

    async def async_set_native_value(self, value: float) -> None:
        # Clamp to step and range
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()
