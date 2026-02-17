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
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_PLUG,
    CONF_PLUG_POWER_RATING,
    DEFAULT_PLUG_POWER_RATING,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_BATTERY_MAX_POWER,
    DEFAULT_BATTERY_SOC_MIN,
    DEFAULT_BATTERY_SOC_TARGET,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
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
            # Plug entities: Device Power slider only (no min/max current)
            entities = [
                PlugDevicePowerSlider(hass, config_entry, name, entity_id),
            ]
        else:
            # EVSE entities: Min Current, Max Current
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

    def _write_to_charger_data(self, value):
        """Write min_current to shared charger data."""
        charger_data = self.hass.data.get(DOMAIN, {}).get("chargers", {}).get(self.config_entry.entry_id)
        if charger_data is not None:
            charger_data["min_current"] = value

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
        self._write_to_charger_data(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_charger_data(value)


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

    def _write_to_charger_data(self, value):
        """Write max_current to shared charger data."""
        charger_data = self.hass.data.get(DOMAIN, {}).get("chargers", {}).get(self.config_entry.entry_id)
        if charger_data is not None:
            charger_data["max_current"] = value

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
        self._write_to_charger_data(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_charger_data(value)


class PlugDevicePowerSlider(NumberEntity, RestoreEntity):
    """Slider for device power rating in Watts (smart load devices).

    The engine reads this entity's state to determine the plug's power draw
    for allocation calculations. When a power monitor is configured, the
    engine auto-updates this value with the averaged measured draw.
    """

    _attr_entity_category = EntityCategory.CONFIG

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

    def _write_to_charger_data(self, value):
        """Write device_power to shared charger data."""
        charger_data = self.hass.data.get(DOMAIN, {}).get("chargers", {}).get(self.config_entry.entry_id)
        if charger_data is not None:
            charger_data["device_power"] = value

    @property
    def device_info(self):
        """Return device information about this plug."""
        from . import get_hub_for_charger
        hub_entry = get_hub_for_charger(self.hass, self.config_entry.entry_id)
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "Smart Load",
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
        self._write_to_charger_data(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        # Clamp to step and range
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_charger_data(value)


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
        self._attr_native_value = DEFAULT_BATTERY_SOC_TARGET
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon = "mdi:battery-charging-80"

    def _write_to_hub_data(self, value):
        """Write battery_soc_target to shared hub data."""
        hub_data = self.hass.data.get(DOMAIN, {}).get("hubs", {}).get(self.config_entry.entry_id)
        if hub_data is not None:
            hub_data["battery_soc_target"] = value

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

        self.async_write_ha_state()
        self._write_to_hub_data(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value)))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_hub_data(value)


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

    def _write_to_hub_data(self, value):
        """Write battery_soc_min to shared hub data."""
        hub_data = self.hass.data.get(DOMAIN, {}).get("hubs", {}).get(self.config_entry.entry_id)
        if hub_data is not None:
            hub_data["battery_soc_min"] = value

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

        self.async_write_ha_state()
        self._write_to_hub_data(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value)))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_hub_data(value)


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

    def _write_to_hub_data(self, value):
        """Write power_buffer to shared hub data."""
        hub_data = self.hass.data.get(DOMAIN, {}).get("hubs", {}).get(self.config_entry.entry_id)
        if hub_data is not None:
            hub_data["power_buffer"] = value

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
        self._write_to_hub_data(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        # Clamp to step and range
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_hub_data(value)


class MaxImportPowerSlider(NumberEntity, RestoreEntity):
    """Slider for maximum grid import power in Watts (hub-level).

    Limits total power drawn from the grid. Useful when your electricity
    contract has a lower limit than the physical breaker rating.
    """

    _attr_entity_category = EntityCategory.CONFIG

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

    def _write_to_hub_data(self, value):
        """Write max_import_power to shared hub data."""
        hub_data = self.hass.data.get(DOMAIN, {}).get("hubs", {}).get(self.config_entry.entry_id)
        if hub_data is not None:
            hub_data["max_import_power"] = value

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

        self.async_write_ha_state()
        self._write_to_hub_data(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, round(value / self._attr_native_step) * self._attr_native_step))
        self._attr_native_value = value
        self.async_write_ha_state()
        self._write_to_hub_data(value)
