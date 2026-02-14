# filepath: custom_components/dynamic_ocpp_evse/number.py
import logging
from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .const import (
    DOMAIN, 
    ENTRY_TYPE, 
    ENTRY_TYPE_HUB, 
    ENTRY_TYPE_CHARGER,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_MIN_CURRENT, 
    CONF_MAX_CURRENT, 
    CONF_EVSE_MINIMUM_CHARGE_CURRENT, 
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT, 
    CONF_POWER_BUFFER,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_BATTERY_MAX_POWER,
    DEFAULT_BATTERY_SOC_MIN,
)
from .helpers import get_entry_value

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the number entities."""
    entry_type = config_entry.data.get(ENTRY_TYPE)
    name = config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE")
    entity_id = config_entry.data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")
    
    entities = []
    
    if entry_type == ENTRY_TYPE_HUB:
        # Check if battery is configured
        battery_soc_entity = get_entry_value(config_entry, "battery_soc_entity_id")
        battery_power_entity = get_entry_value(config_entry, "battery_power_entity_id")
        has_battery = bool(battery_soc_entity or battery_power_entity)
        
        # Always create Power Buffer (useful even without battery)
        entities.append(PowerBufferSlider(hass, config_entry, name, entity_id))
        
        # Only create battery entities if battery is configured
        if has_battery:
            entities.append(BatterySOCTargetSlider(hass, config_entry, name, entity_id))
            entities.append(BatterySOCMinSlider(hass, config_entry, name, entity_id))
            _LOGGER.info(f"Battery configured - creating battery number entities")
        else:
            _LOGGER.info(f"No battery configured - skipping battery number entities")
        
        _LOGGER.info(f"Setting up hub number entities: {[entity.unique_id for entity in entities]}")
    
    elif entry_type == ENTRY_TYPE_CHARGER:
        # Charger entities: Min Current, Max Current
        entities = [
            EVSEMinCurrentSlider(hass, config_entry, name, entity_id),
            EVSEMaxCurrentSlider(hass, config_entry, name, entity_id),
        ]
        _LOGGER.info(f"Setting up charger number entities: {[entity.unique_id for entity in entities]}")
    
    else:
        _LOGGER.debug("Skipping number setup for unknown entry type: %s", config_entry.title)
        return
    
    async_add_entities(entities)


# ==================== CHARGER NUMBER ENTITIES ====================

class EVSEMinCurrentSlider(NumberEntity, RestoreEntity):
    """Slider for minimum current (charger-level)."""
    
    _attr_entity_category = EntityCategory.CONFIG
    
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Min Current"
        self._attr_unique_id = f"{entity_id}_min_current"
        self._attr_native_min_value = get_entry_value(config_entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
        self._attr_native_max_value = get_entry_value(config_entry, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)
        self._attr_native_step = 0.5
        self._attr_native_value = get_entry_value(config_entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
        self._attr_native_unit_of_measurement = "A"
        self._attr_icon = "mdi:current-ac"

    @property
    def device_info(self):
        """Return device information about this charger."""
        from . import get_hub_for_charger
        hub_entry = get_hub_for_charger(self.hass, self.config_entry.entry_id)
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "EV Charger",
            "via_device": (DOMAIN, hub_entry.entry_id) if hub_entry else None,
        }

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
        
        # Write initial state
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class EVSEMaxCurrentSlider(NumberEntity, RestoreEntity):
    """Slider for maximum current (charger-level)."""
    
    _attr_entity_category = EntityCategory.CONFIG
    
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Max Current"
        self._attr_unique_id = f"{entity_id}_max_current"
        self._attr_native_min_value = get_entry_value(config_entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
        self._attr_native_max_value = get_entry_value(config_entry, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)
        self._attr_native_step = 0.5
        self._attr_native_value = get_entry_value(config_entry, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)
        self._attr_native_unit_of_measurement = "A"
        self._attr_icon = "mdi:current-ac"

    @property
    def device_info(self):
        """Return device information about this charger."""
        from . import get_hub_for_charger
        hub_entry = get_hub_for_charger(self.hass, self.config_entry.entry_id)
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "EV Charger",
            "via_device": (DOMAIN, hub_entry.entry_id) if hub_entry else None,
        }

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
        
        # Write initial state
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


# ==================== HUB NUMBER ENTITIES ====================

class BatterySOCTargetSlider(NumberEntity, RestoreEntity):
    """Slider for battery SOC target (0-100%, step 1) (hub-level).
    
    In Eco mode: Below target, charge at minimum rate. At/above target, charge at solar rate or full speed.
    In Solar mode: Below target, do not charge. At/above target, charge at solar rate.
    """
    
    _attr_entity_category = EntityCategory.CONFIG
    
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Home Battery SOC Target"
        self._attr_unique_id = f"{entity_id}_home_battery_soc_target"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 100
        self._attr_native_step = 1
        self._attr_native_value = 80  # Default SOC target
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon = "mdi:battery-charging-80"

    @property
    def device_info(self):
        """Return device information about this hub."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE"),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "Electrical System Hub",
        }

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
        
        # Write initial state
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        # Clamp to range
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value)))
        self._attr_native_value = value
        self.async_write_ha_state()


class BatterySOCMinSlider(NumberEntity, RestoreEntity):
    """Slider for minimum battery SOC (0-95%, step 1) (hub-level).
    
    Below this SOC level, EV charging will NOT occur in any mode.
    This is the absolute floor to protect the home battery.
    """
    
    _attr_entity_category = EntityCategory.CONFIG
    
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Home Battery SOC Min"
        self._attr_unique_id = f"{entity_id}_home_battery_soc_min"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 95
        self._attr_native_step = 1
        self._attr_native_value = DEFAULT_BATTERY_SOC_MIN  # Default 20%
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon = "mdi:battery-alert-variant-outline"

    @property
    def device_info(self):
        """Return device information about this hub."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE"),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "Electrical System Hub",
        }

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
        
        # Write initial state
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        # Clamp to range
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value)))
        self._attr_native_value = value
        self.async_write_ha_state()


class PowerBufferSlider(NumberEntity, RestoreEntity):
    """Slider for power buffer in Watts (0-5000W, step 100) (hub-level).
    
    This buffer reduces the target charging power in Standard mode to prevent
    frequent charging stops. If the buffered target is below minimum charge rate,
    the system can use up to the full available power.
    """
    
    _attr_entity_category = EntityCategory.CONFIG
    
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Power Buffer"
        self._attr_unique_id = f"{entity_id}_power_buffer"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 5000
        self._attr_native_step = 100
        self._attr_native_value = 0  # Default: no buffer
        self._attr_native_unit_of_measurement = "W"
        self._attr_icon = "mdi:buffer"

    @property
    def device_info(self):
        """Return device information about this hub."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE"),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "Electrical System Hub",
        }

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
        
        # Write initial state
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        # Clamp to step and range
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()
