"""Config-entry relationship lookups (hub ↔ chargers / inverters / groups).

The integration's entries form a tree: one hub entry per site, with charger,
inverter and circuit-group entries pointing at it via ``CONF_HUB_ENTRY_ID``.
These helpers are the single place that walks that tree, for anything that
needs "the hub of this charger" or "the inverters of this hub".

Deliberately free of any runtime ``homeassistant`` import — the HA types are
annotations only, under ``TYPE_CHECKING``. That is what lets this module live
outside the package root: it used to sit in ``__init__.py``, where every
caller in ``engine/``, ``entities/`` and ``config_flow.py`` had to defer its
import to function scope to dodge the circular dependency back through the
HA-importing package root. Keeping it HA-import-free also means pure tooling
(``dev/tests/standalone_loader.py``) can load it directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CONF_HUB_ENTRY_ID,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_INVERTER,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def get_hub_for_charger(hass: HomeAssistant, charger_entry_id: str) -> ConfigEntry | None:
    """Get the hub config entry for a charger."""
    charger_data = hass.data[DOMAIN]["chargers"].get(charger_entry_id)
    if not charger_data:
        return None

    hub_entry_id = charger_data.get("hub_entry_id")
    hub_data = hass.data[DOMAIN]["hubs"].get(hub_entry_id)
    if not hub_data:
        return None

    return hub_data.get("entry")


def get_chargers_for_hub(hass: HomeAssistant, hub_entry_id: str) -> list[ConfigEntry]:
    """Get all charger config entries for a hub."""
    hub_data = hass.data[DOMAIN]["hubs"].get(hub_entry_id)
    if not hub_data:
        return []

    chargers = []
    for charger_entry_id in hub_data.get("chargers", []):
        charger_data = hass.data[DOMAIN]["chargers"].get(charger_entry_id)
        if charger_data:
            chargers.append(charger_data.get("entry"))

    return chargers


def _children_of_hub(hass: HomeAssistant, hub_entry_id: str, entry_type: str) -> list[ConfigEntry]:
    """Config entries of one type linked to a hub, ordered by entry_id.

    Resolved from the config-entry registry rather than the hub's runtime
    child lists. Those lists are rebuilt empty every time the hub reloads and
    are only refilled by children that set up afterwards, so a hub reload
    (adding an inverter schedules one) would silently drop already-loaded
    children from the site until the next restart — an inverter vanishing
    from the fleet takes its capacity with it. Config is config: read it from
    the entries themselves. Disabled entries are excluded, since HA never
    sets them up and they represent hardware the user switched off.
    """
    return sorted(
        (
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(ENTRY_TYPE) == entry_type
            and entry.data.get(CONF_HUB_ENTRY_ID) == hub_entry_id
            and entry.disabled_by is None
        ),
        key=lambda entry: entry.entry_id,
    )


def get_inverters_for_hub(hass: HomeAssistant, hub_entry_id: str) -> list[ConfigEntry]:
    """Get all inverter config entries for a hub, in a deterministic order.

    Sorted by entry_id — per-inverter runtime keys (EMA smoothing state,
    result attribution) rely on a stable iteration order.
    """
    return _children_of_hub(hass, hub_entry_id, ENTRY_TYPE_INVERTER)


def get_groups_for_hub(hass: HomeAssistant, hub_entry_id: str) -> list[ConfigEntry]:
    """Get all circuit group config entries for a hub."""
    return _children_of_hub(hass, hub_entry_id, ENTRY_TYPE_GROUP)
