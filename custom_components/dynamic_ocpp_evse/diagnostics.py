"""Diagnostics: the whole site's configuration and live state in one file.

Home Assistant puts a **Download diagnostics** button on every device of a
config entry that provides this platform. Downloading from ANY Load Juggler
device — the hub or any load, group or inverter — yields the same
site-wide dump, because a child entry resolves to its hub first: debugging a
load almost always needs the hub's thresholds and its siblings' priorities.

What is in it, and why each part:

* ``config`` — every entry on the site, ``data`` and ``options`` verbatim.
  This is the part worth having: reproducing a report otherwise means asking
  for a dozen screenshots of option pages.
* ``live`` — the last published site result (the same dict the Overview page
  renders), so the numbers the engine decided on are next to the settings
  that produced them.
* ``runtime`` — the carried state: the Excess and SOC latches, the input
  EMAs, and the forecast observers including each array's stored 15-minute
  gain series. Filtered to JSON-safe values (see ``_jsonable``) because these
  buckets also hold entity objects and callables.

Nothing is redacted: this integration stores entity ids, priorities and
setpoints, and no credentials, tokens or coordinates. Entity ids do carry
whatever names the user gave their devices, which is worth knowing before
posting a dump in a public issue.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_TYPE,
    CONF_HUB_ENTRY_ID,
    CONF_NAME,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
)
from .helpers import inverter_features

# Runtime keys that hold live HA objects rather than state — dumping them adds
# pages of reprs and nothing to debug with.
_RUNTIME_SKIP = frozenset({"entry", "loads", "coordinator", "listeners"})

# How deep to walk a runtime structure before giving up. The gain series is a
# list of flat dicts, so three levels is plenty; the cap only stops a
# self-referencing bucket from hanging the download.
_MAX_DEPTH = 6


def _jsonable(value: Any, depth: int = 0) -> Any:
    """``value`` reduced to something the diagnostics store can serialise.

    Keeps scalars, and dicts/lists of them; renders datetimes as ISO strings;
    replaces anything else (an entity, a coordinator, a callable) with a short
    type marker rather than dropping it, so a dump still shows that something
    was there.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if depth >= _MAX_DEPTH:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        return {
            str(k): _jsonable(v, depth + 1)
            for k, v in value.items()
            if str(k) not in _RUNTIME_SKIP
        }
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in value]
    return f"<{type(value).__name__}>"


def _entry_dump(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """One config entry as configured, plus what the engine derived from it."""
    dump = {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "version": f"{entry.version}.{getattr(entry, 'minor_version', 0)}",
        "state": str(entry.state),
        "entry_type": entry.data.get(ENTRY_TYPE),
        "device_type": entry.data.get(CONF_DEVICE_TYPE),
        "data": _jsonable(dict(entry.data)),
        "options": _jsonable(dict(entry.options)),
    }
    # The declared inverter features decide which option pages exist at all,
    # and an entry that predates the list has them inferred — so record what
    # the integration actually acts on, not only what is stored.
    if entry.data.get(ENTRY_TYPE) == "inverter":
        dump["features_effective"] = list(inverter_features(entry))
    return dump


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """The whole site, from whichever entry the user pressed the button on."""
    domain_data = hass.data.get(DOMAIN, {}) or {}

    # Resolve to the hub: a child's own settings are rarely enough to explain
    # what it was allocated.
    hub_entry = entry
    if entry.data.get(ENTRY_TYPE, ENTRY_TYPE_HUB) != ENTRY_TYPE_HUB:
        hub_id = entry.data.get(CONF_HUB_ENTRY_ID)
        resolved = hass.config_entries.async_get_entry(hub_id) if hub_id else None
        if resolved is not None:
            hub_entry = resolved

    children = [
        child
        for child in hass.config_entries.async_entries(DOMAIN)
        if child.data.get(CONF_HUB_ENTRY_ID) == hub_entry.entry_id
    ]

    inverter_runtime = {
        child.entry_id: _jsonable((domain_data.get("inverters") or {}).get(child.entry_id))
        for child in children
        if child.data.get(ENTRY_TYPE) == "inverter"
    }
    load_runtime = {
        child.entry_id: _jsonable((domain_data.get("loads") or {}).get(child.entry_id))
        for child in children
        if child.data.get(ENTRY_TYPE) == "load"
    }

    return {
        "integration": {
            "domain": DOMAIN,
            "requested_from": {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "is_hub": hub_entry.entry_id == entry.entry_id,
            },
        },
        "config": {
            "hub": _entry_dump(hass, hub_entry),
            "children": [_entry_dump(hass, child) for child in children],
            "child_count": len(children),
        },
        "live": {
            "hub_data": _jsonable(
                (domain_data.get("hub_data") or {}).get(hub_entry.entry_id)
            ),
            "load_allocations": _jsonable(domain_data.get("load_allocations")),
            "load_status": _jsonable(domain_data.get("load_status")),
        },
        "runtime": {
            "hub": _jsonable((domain_data.get("hubs") or {}).get(hub_entry.entry_id)),
            "inverters": inverter_runtime,
            "loads": load_runtime,
        },
        "names": {
            child.entry_id: child.data.get(CONF_NAME, child.title)
            for child in children
        },
    }
