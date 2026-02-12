"""Helper utilities for Dynamic OCPP EVSE integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry


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