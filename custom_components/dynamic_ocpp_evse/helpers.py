"""Helper utilities for Dynamic OCPP EVSE integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry


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
