"""Helper utilities for Load Juggler integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
)


def prettify_name(name: str) -> str:
    """Convert raw device names (e.g. 'evbox_elvi') to human-friendly format.

    Replaces underscores with spaces. Applies title-case only when the name
    is all lowercase (preserves existing mixed case like 'EvBox Elvi').
    """
    name = name.replace("_", " ")
    if name == name.lower():
        name = name.title()
    return name


def normalize_optional_entity(value: str | None) -> str | None:
    """Normalize optional entity selector values.

    Converts placeholder strings like "None" to actual None.
    """
    if value in (None, "", "None"):
        return None
    return value


def get_entry_value(entry: ConfigEntry, key: str, default=None):
    """Get a config value, preferring entry.options over entry.data."""
    if entry.options and key in entry.options:
        value = entry.options.get(key)
    else:
        value = entry.data.get(key, default)
    return normalize_optional_entity(value)


def validate_charger_settings(data: dict[str, any], errors: dict[str, str]) -> None:
    """
    Validate charger settings.
    
    Adds validation errors to the provided error dict (modifies in-place).
    
    Args:
        data: Charger configuration data containing evse_minimum_charge_current and evse_maximum_charge_current
        errors: Dict to populate with validation errors (modifies in-place)
    """
    min_current = data.get("evse_minimum_charge_current")
    max_current = data.get("evse_maximum_charge_current")
    
    if min_current is not None and max_current is not None:
        if min_current <= 0 or max_current <= 0:
            errors["base"] = "invalid_current"
        elif min_current > max_current:
            errors["base"] = "min_exceeds_max"


def validate_offgrid_battery_requirement(
    grid_data: dict, battery_data: dict, errors: dict[str, str]
) -> None:
    """Require a battery on hubs with no grid CTs (hard block).

    A hub with no grid CT entities runs off-grid: the battery SOC drives the
    mode logic and battery power drives off-grid solar-surplus detection, so
    both entities are mandatory. Adds an error to ``errors`` in-place.

    Args:
        grid_data: config holding the phase-current entity keys (may be None).
        battery_data: config holding the battery SOC / power entity keys.
        errors: error dict to populate (modified in-place).
    """
    has_grid_cts = any(
        grid_data.get(key)
        for key in (
            CONF_PHASE_A_CURRENT_ENTITY_ID,
            CONF_PHASE_B_CURRENT_ENTITY_ID,
            CONF_PHASE_C_CURRENT_ENTITY_ID,
        )
    )
    if not has_grid_cts and not (
        battery_data.get(CONF_BATTERY_SOC_ENTITY_ID)
        and battery_data.get(CONF_BATTERY_POWER_ENTITY_ID)
    ):
        errors["base"] = "battery_required_no_cts"
