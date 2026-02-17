# filepath: custom_components/dynamic_ocpp_evse/number.py
import logging
from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .entity_mixins import HubEntityMixin, ChargerEntityMixin
from .const import (
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_CHARGER,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_PLUG_POWER_RATING,
    DEFAULT_PLUG_POWER_RATING,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_BATTERY_SOC_MIN,
    DEFAULT_BATTERY_SOC_TARGET,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_PLUG,
    CONF_ENABLE_MAX_IMPORT_POWER,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_MAIN_BREAKER_RATING,
    CONF_PHASE_VOLTAGE,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_PHASE_VOLTAGE,
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
        battery_soc_entity = get_entry_value(config_entry, CONF_BATTERY_SOC_ENTITY_ID)
        battery_power_entity = get_entry_value(config_entry, CONF_BATTERY_POWER_ENTITY_ID)
        has_battery = bool(battery_soc_entity or battery_power_entity)

        # Always create Power Buffer (useful even without battery)
        entities.append(PowerBufferSlider(hass, config_entry, name, entity_id))

        # Create Max Import Power slider when checkbox is enabled and no entity override
        enable_max_import = get_entry_value(config_entry, CONF_ENABLE_MAX_IMPORT_POWER, True)
        max_import_entity = get_entry_value(config_entry, CONF_MAX_IMPORT_POWER_ENTITY_ID, None)
        if enable_max_import and not max_import_entity:
            entities.append(MaxImportPowerSlider(hass, config_entry, name, entity_id))
            _LOGGER.info("Max import power slider created (no entity override)")
        elif max_import_entity:
            _LOGGER.info("Max import power using entity override: %s", max_import_entity)
        else:
            _LOGGER.info("Max import power limit disabled")

        # Only create battery entities if battery is configured
        if has_battery:
            entities.append(BatterySOCTargetSlider(hass, config_entry, name, entity_id))
            entities.append(BatterySOCMinSlider(hass, config_entry, name, entity_id))
            _LOGGER.info(f"Battery configured - creating battery number entities")
        else:
            _LOGGER.info(f"No battery configured - skipping battery number entities")

        _LOGGER.info(f"Setting up hub number entities: {[entity.unique_id for entity in entities]}")

    elif entry_type == ENTRY_TYPE_CHARGER:
        device_type = config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        if device_type == DEVICE_TYPE_PLUG:
            entities = [PlugDevicePowerSlider(hass, config_entry, name, entity_id)]
        else:
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

class EVSEMinCurrentSlider(ChargerEntityMixin, NumberEntity, RestoreEntity):
    """Slider for minimum current (charger-level)."""

    _attr_entity_category = EntityCategory.CONFIG
    _charger_data_key = "min_current"

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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_and_publish_number()

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_charger_data(value)


class EVSEMaxCurrentSlider(ChargerEntityMixin, NumberEntity, RestoreEntity):
    """Slider for maximum current (charger-level)."""

    _attr_entity_category = EntityCategory.CONFIG
    _charger_data_key = "max_current"

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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_and_publish_number()

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_charger_data(value)


class PlugDevicePowerSlider(ChargerEntityMixin, NumberEntity, RestoreEntity):
    """Slider for device power rating in Watts (smart load devices)."""

    _attr_entity_category = EntityCategory.CONFIG
    _charger_data_key = "device_power"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Device Power"
        self._attr_unique_id = f"{entity_id}_device_power"
        power_rating = get_entry_value(config_entry, CONF_PLUG_POWER_RATING, DEFAULT_PLUG_POWER_RATING)
        self._attr_native_min_value = 100
        self._attr_native_max_value = power_rating
        self._attr_native_step = 100
        self._attr_native_value = power_rating
        self._attr_native_unit_of_measurement = "W"
        self._attr_icon = "mdi:power-plug"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_and_publish_number()

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_charger_data(value)


# ==================== HUB NUMBER ENTITIES ====================

class BatterySOCTargetSlider(HubEntityMixin, NumberEntity, RestoreEntity):
    """Slider for battery SOC target (0-100%, step 1) (hub-level).

    In Eco mode: Below target, charge at minimum rate. At/above target, charge at solar rate or full speed.
    In Solar mode: Below target, do not charge. At/above target, charge at solar rate.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _hub_data_key = "battery_soc_target"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Home Battery SOC Target"
        self._attr_unique_id = f"{entity_id}_home_battery_soc_target"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 100
        self._attr_native_step = 1
        self._attr_native_value = DEFAULT_BATTERY_SOC_TARGET
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon = "mdi:battery-charging-80"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_and_publish_number()

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value)))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_hub_data(value)


class BatterySOCMinSlider(HubEntityMixin, NumberEntity, RestoreEntity):
    """Slider for minimum battery SOC (0-95%, step 1) (hub-level).

    Below this SOC level, EV charging will NOT occur in any mode.
    This is the absolute floor to protect the home battery.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _hub_data_key = "battery_soc_min"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Home Battery SOC Min"
        self._attr_unique_id = f"{entity_id}_home_battery_soc_min"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 95
        self._attr_native_step = 1
        self._attr_native_value = DEFAULT_BATTERY_SOC_MIN
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon = "mdi:battery-alert-variant-outline"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_and_publish_number()

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value)))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_hub_data(value)


class PowerBufferSlider(HubEntityMixin, NumberEntity, RestoreEntity):
    """Slider for power buffer in Watts (0-5000W, step 100) (hub-level)."""

    _attr_entity_category = EntityCategory.CONFIG
    _hub_data_key = "power_buffer"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Power Buffer"
        self._attr_unique_id = f"{entity_id}_power_buffer"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 5000
        self._attr_native_step = 100
        self._attr_native_value = 0
        self._attr_native_unit_of_measurement = "W"
        self._attr_icon = "mdi:buffer"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_and_publish_number()

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_hub_data(value)


class MaxImportPowerSlider(HubEntityMixin, NumberEntity, RestoreEntity):
    """Slider for maximum grid import power in Watts (hub-level)."""

    _attr_entity_category = EntityCategory.CONFIG
    _hub_data_key = "max_import_power"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Max Import Power"
        self._attr_unique_id = f"{entity_id}_max_import_power"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 50000
        self._attr_native_step = 100
        self._attr_native_unit_of_measurement = "W"
        self._attr_icon = "mdi:transmission-tower-import"

        # Default to full breaker capacity
        voltage = get_entry_value(config_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
        breaker = get_entry_value(config_entry, CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING)
        has_phase_b = bool(get_entry_value(config_entry, CONF_PHASE_B_CURRENT_ENTITY_ID, None))
        has_phase_c = bool(get_entry_value(config_entry, CONF_PHASE_C_CURRENT_ENTITY_ID, None))
        num_phases = 1 + (1 if has_phase_b else 0) + (1 if has_phase_c else 0)
        self._attr_native_value = round(voltage * breaker * num_phases / 100) * 100

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_and_publish_number()

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_hub_data(value)
