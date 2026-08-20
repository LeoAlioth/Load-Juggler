import re
import logging
from datetime import datetime, timezone
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector
from homeassistant.helpers.entity_registry import (
    async_get as async_get_entity_registry,
    async_entries_for_device as er_async_entries_for_device,
    async_entries_for_config_entry as er_async_entries_for_config_entry,
)
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from typing import Any
from .const import *
from .detection_patterns import (
    PHASE_PATTERNS,
    INVERTER_OUTPUT_PATTERNS,
    BATTERY_SOC_PATTERNS,
    BATTERY_POWER_PATTERNS,
    SOLAR_PRODUCTION_PATTERNS,
    BATTERY_MAX_CHARGE_POWER_PATTERNS,
    BATTERY_MAX_DISCHARGE_POWER_PATTERNS,
    PLUG_POWER_MONITOR_PATTERNS,
)
from .helpers import (
    get_entry_value,
    normalize_optional_entity,
    prettify_name,
    validate_charger_settings,
    validate_offgrid_battery_requirement,
)
from . import units

_LOGGER = logging.getLogger(__name__)
_POWER_FACTOR = 0.9  # 90% of detected limit for safe headroom

# One declaration, shared with the readers: a unit offered here must be one
# units.py can convert (see ENTITY_UNIT_CONTRACTS and test_unit_contracts.py).
_CURRENT_UNITS = units.CURRENT_UNITS
_POWER_UNITS = units.POWER_UNITS
_SOC_UNITS = units.SOC_UNITS
_VOLTAGE_UNITS = units.VOLTAGE_UNITS


def _validate_entity_units(
    hass, user_input: dict, field_unit_map: dict, errors: dict
) -> None:
    """Validate that provided entities report expected measurement units.

    Silently skips when an entity's state is unavailable/unknown or has no
    unit_of_measurement attribute — the user is never blocked by missing state.
    Only flags an error when a unit is present and clearly wrong.
    """
    for field_key, valid_units in field_unit_map.items():
        entity_id = user_input.get(field_key)
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if units.is_unavailable(state):
            continue
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit not in valid_units:
            errors[field_key] = "invalid_unit"


def _validate_forecast_devices(hass, user_input: dict, errors: dict) -> str | None:
    """Every selected solar forecast device must offer a ``watts`` sensor.

    A forecast device (one Open-Meteo Solar Forecast config entry per PV
    array) exposes several sensors; the clipping forecast reads the ``watts``
    attribute (a mapping of block-start timestamps to average watts) from one
    of them. A device with sensor entities but none of their states loaded
    yet never blocks the user — mirroring _validate_entity_units — but a
    device whose loaded sensors carry no watts mapping is the wrong device.

    Returns the offending device's display name so the step can name it in
    the form error via description_placeholders, or None when valid.
    """
    entity_registry = async_get_entity_registry(hass)
    for device_id in user_input.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or []:
        sensors = [
            e.entity_id
            for e in er_async_entries_for_device(entity_registry, device_id)
            if e.domain == "sensor"
        ]
        states = [s for s in (hass.states.get(eid) for eid in sensors) if s is not None]
        if sensors and not states:
            continue  # states not loaded yet — never block on missing state
        if any(isinstance(s.attributes.get("watts"), dict) for s in states):
            continue
        errors[CONF_SOLAR_FORECAST_DEVICE_IDS] = "forecast_device_no_watts"
        device = async_get_device_registry(hass).async_get(device_id)
        if device:
            return device.name_by_user or device.name or device_id
        return device_id
    return None


def _compose_entry_title(name: str, type_label: str) -> str:
    """Compose a config-entry title without doubling the device-type label.

    The type label is appended only when the user's name doesn't already
    contain it — so a device left at its default name (e.g. "Hot Water Tank")
    becomes just "Hot Water Tank", not "Hot Water Tank Hot Water Tank", while
    a custom name like "Kitchen" still becomes "Kitchen Hot Water Tank".
    """
    name = (name or "").strip()
    if not name:
        return type_label
    if type_label.lower() in name.lower():
        return name
    return f"{name} {type_label}"


# --- Device-priority reordering (used by the hub options flow) ---

def _controlled_devices(hass, hub_entry_id: str) -> list:
    """All controllable load entries (EVSE, plug, tank) linked to a hub."""
    return [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
        and e.data.get(CONF_HUB_ENTRY_ID) == hub_entry_id
    ]


def _devices_by_priority(devices: list) -> list:
    """Devices sorted by effective priority, then title for a stable tie-break."""
    return sorted(
        devices,
        key=lambda e: (
            get_entry_value(e, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            e.title,
        ),
    )


def _priority_order_schema(devices: list) -> vol.Schema:
    """Ordered multi-select listing every controlled device, current order first."""
    ordered = _devices_by_priority(devices)
    options = [
        {"value": e.entry_id, "label": e.title or e.data.get(CONF_NAME, e.entry_id)}
        for e in ordered
    ]
    return vol.Schema(
        {
            vol.Required(
                CONF_PRIORITY_ORDER,
                default=[e.entry_id for e in ordered],
            ): selector(
                {
                    "select": {
                        "options": options,
                        "multiple": True,
                        "mode": "dropdown",
                        "sort": False,
                    }
                }
            ),
        }
    )


def _apply_priority_order(hass, devices: list, chosen: list) -> None:
    """Write rank 1..N to each device from the chosen order.

    Devices the user left out keep their current ranking at the end. Only the
    per-device priority number is touched, so the distribution engine is
    unchanged — it still sorts by (mode urgency, priority).
    """
    placed = list(chosen)
    for entry in _devices_by_priority(devices):
        if entry.entry_id not in placed:
            placed.append(entry.entry_id)
    for rank, entry_id in enumerate(placed, start=1):
        child = hass.config_entries.async_get_entry(entry_id)
        if not child or get_entry_value(child, CONF_CHARGER_PRIORITY, None) == rank:
            continue
        hass.config_entries.async_update_entry(
            child, options={**child.options, CONF_CHARGER_PRIORITY: rank}
        )


def scan_ocpp_chargers(hass) -> list[dict]:
    """Every OCPP charger in the entity registry that is not configured yet.

    The ONE scanner behind both entry points — the manual "Add OCPP Charger"
    flow and the automatic discovery spawned from ``_setup_hub_entry`` — so a
    discovered charger and a manually-added one always carry the same complete
    key set. Two things this settles for both paths:

    - ``device_id`` is the OCPP charge point id, i.e. the entity base name
      ("evbox_elvi"), never the internal HA device-registry UUID. The UUID is
      meaningless to the ocpp integration's services.
    - a charger is usable when it reports EITHER current_offered OR
      power_offered, so watts-only chargers are discovered too.

    Returns one dict per charger with keys: id, name, device_id,
    current_import_entity, current_import_l1/l2/l3_entity,
    current_offered_entity, power_offered_entity, power_import_entity
    (entity values are None when that entity does not exist).
    """
    chargers: list[dict] = []

    entity_registry = async_get_entity_registry(hass)
    device_registry = async_get_device_registry(hass)

    # Already-configured chargers are identified by their current_import entity
    configured_charger_imports = {
        entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
    }

    def _existing(entity_id: str) -> str | None:
        return entity_id if entity_id in entity_registry.entities else None

    for entity_id, entity in entity_registry.entities.items():
        if not (
            entity_id.startswith("sensor.")
            and entity_id.endswith(OCPP_ENTITY_SUFFIX_CURRENT_IMPORT)
        ):
            continue
        if entity_id in configured_charger_imports:
            continue

        # Extract charger base name
        base_name = entity_id.replace("sensor.", "").replace(
            OCPP_ENTITY_SUFFIX_CURRENT_IMPORT, ""
        )

        current_offered_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_OFFERED}"
        )
        # Fallback for watts-only chargers, which offer power instead of current
        power_offered_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_POWER_OFFERED}"
        )
        if not current_offered_entity and not power_offered_entity:
            continue

        # Per-phase current import (fallback 1) and power import (fallback 2)
        current_import_l1_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L1}"
        )
        current_import_l2_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L2}"
        )
        current_import_l3_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L3}"
        )
        power_import_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_POWER_IMPORT}"
        )

        # Prefer the device's friendly name for display purposes only
        device_name = prettify_name(base_name)
        if entity.device_id:
            device = device_registry.async_get(entity.device_id)
            if device and device.name:
                device_name = prettify_name(device.name)

        chargers.append(
            {
                "id": base_name,
                "name": device_name,
                "device_id": base_name,
                "current_import_entity": entity_id,
                "current_import_l1_entity": current_import_l1_entity,
                "current_import_l2_entity": current_import_l2_entity,
                "current_import_l3_entity": current_import_l3_entity,
                "current_offered_entity": current_offered_entity,
                "power_offered_entity": power_offered_entity,
                "power_import_entity": power_import_entity,
            }
        )

    return chargers


def _hub_phase_count(hass, hub_entry_id: str | None) -> int:
    """Number of phases this site has, as the engine sees it.

    A site phase exists when it has a grid CT or an inverter output sensor
    (mirrors ``run_hub_calculation``'s phase derivation). The inverter output
    entities may live on the hub's own legacy fields OR — after the one-time
    auto-import — on any of its inverter child entries, so the whole fleet is
    consulted. Without that, an off-grid 3-phase site collapses to 1 phase
    post-import, hiding the L2/L3 mapping fields and force-mapping every
    charger leg onto L1's phase.
    """
    if not hub_entry_id:
        return 3  # Default to 3 if unknown
    hub_entry = hass.config_entries.async_get_entry(hub_entry_id)
    if not hub_entry:
        return 3
    opts = {**hub_entry.data, **hub_entry.options}
    # Count from grid CT entities first
    count = sum(
        1
        for key in (
            CONF_PHASE_A_CURRENT_ENTITY_ID,
            CONF_PHASE_B_CURRENT_ENTITY_ID,
            CONF_PHASE_C_CURRENT_ENTITY_ID,
        )
        if opts.get(key)
    )
    if count > 0:
        return count
    # Off-grid fallback: infer from the inverter output entities of the whole
    # fleet — the hub's own (pre-import) fields plus every inverter child
    # entry. A phase counts once, no matter how many members feed it.
    from . import get_inverters_for_hub  # local: avoids a circular import

    sources = [opts] + [
        {**inverter.data, **inverter.options}
        for inverter in get_inverters_for_hub(hass, hub_entry_id)
    ]
    count = sum(
        1
        for key in (
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
        )
        if any(source.get(key) for source in sources)
    )
    return max(count, 1)


# ---------------------------------------------------------------------------
# Read-only pages: "Overview" (live) and "How it decides" (configuration)
# ---------------------------------------------------------------------------
#
# Both are one options step whose form carries an EMPTY schema — the whole body
# arrives through description_placeholders. HA renders markdown in flow
# descriptions, so these builders emit short lines, bold labels and hyphen
# lists; markdown TABLES are not rendered, so there are none.
#
# Everything below is a module-level pure function of (hass, entry_id) so it
# can be unit-tested without a flow instance, and reused later (e.g. for a
# setup-confirmation page).

_STALE_AFTER_SECONDS = 90  # hub_data older than this is called out as stale
_DASH = "—"

_DEVICE_TYPE_LABELS = {
    DEVICE_TYPE_EVSE: "EVSE",
    DEVICE_TYPE_PLUG: "Smart plug",
    DEVICE_TYPE_HOT_WATER_TANK: "Hot water tank",
    DEVICE_TYPE_POWER_STATION: "Power station",
}


def _fmt(value, unit: str = "", decimals: int = 1) -> str:
    """A number with its unit, or an em dash when there is nothing to show."""
    if value is None:
        return _DASH
    if isinstance(value, bool):
        return "yes" if value else "no"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{number:.0f}" if decimals == 0 else f"{number:.{decimals}f}"
    return f"{text} {unit}".strip()


def _fmt_age(seconds: float) -> str:
    """A rough age: seconds, minutes or hours — whichever reads best."""
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.0f} h ago"


def _runtime(hass) -> dict:
    """The integration's runtime bucket (empty dict before setup)."""
    return hass.data.get(DOMAIN) or {}


def _live_hub_data(hass, hub_entry_id: str | None) -> tuple[dict, str]:
    """Live hub_data plus a one-line freshness note.

    hub_data is written every cycle by whichever entity ran the calculation
    (the loads, or the hub sensor itself when a hub has no loads). Missing or
    old data is reported in the text rather than raised — these pages must
    never fail just because the engine has not run yet.
    """
    data = (_runtime(hass).get("hub_data") or {}).get(hub_entry_id) or {}
    if not data:
        return {}, (
            "⏳ **No live data yet** — the engine has not completed a "
            "calculation cycle for this site. Try again in a minute."
        )
    last_update = data.get("last_update")
    age = None
    if isinstance(last_update, datetime):
        try:
            age = (datetime.now(timezone.utc) - last_update).total_seconds()
        except (TypeError, ValueError):
            age = None
    if age is None:
        return data, "Live values (age unknown)."
    if age > _STALE_AFTER_SECONDS:
        return data, f"⚠️ **Stale** — last calculated {_fmt_age(age)}."
    return data, f"Live values, calculated {_fmt_age(age)}."


def _load_permit(hass, load_entry, hub_data: dict):
    """The current the engine last permitted this load, in amps.

    First choice is the engine's own record (``_last_permit``, written every
    cycle onto the load's runtime dict); then the load's "Available Current"
    sensor; then the full hub_data, which only carries per-load permits when
    the hub sensor itself ran the calculation.
    """
    runtime = (_runtime(hass).get("chargers") or {}).get(load_entry.entry_id) or {}
    permit = runtime.get("_last_permit")
    if permit is None:
        permit = _entry_sensor_value(hass, load_entry, "_available_current")
    if permit is None:
        permit = (hub_data.get("charger_available") or {}).get(load_entry.entry_id)
    return permit


def _entry_sensor_value(hass, entry, unique_id_suffix: str):
    """Numeric state of one of this entry's own sensors, via the registry.

    Some engine outputs (a load's permitted current, for instance) only live on
    the entity that publishes them, so the pages read them back from HA state
    instead of re-running the engine.
    """
    try:
        registry = async_get_entity_registry(hass)
        for ent in er_async_entries_for_config_entry(registry, entry.entry_id):
            if not (ent.unique_id or "").endswith(unique_id_suffix):
                continue
            state = hass.states.get(ent.entity_id)
            if units.is_unavailable(state):
                return None
            try:
                value = float(state.state)
            except (TypeError, ValueError):
                return state.state  # a status string is a legitimate answer here
            return None if units.is_unusable_number(value) else value
    except Exception:  # pragma: no cover — a display path must never raise
        _LOGGER.debug("Could not read %s for %s", unique_id_suffix, entry.entry_id)
    return None


def _groups_for_hub(hass, hub_entry_id: str) -> list:
    """Circuit group entries linked to a hub (one implementation, in __init__)."""
    from . import get_groups_for_hub  # local: avoids a circular import

    return get_groups_for_hub(hass, hub_entry_id)


def _group_cap_for(hass, hub_entry_id: str, load_entry_id: str) -> str | None:
    """"20 A shared with 2 loads" for a load in a circuit group, else None."""
    for group in _groups_for_hub(hass, hub_entry_id):
        members = get_entry_value(group, CONF_CIRCUIT_GROUP_MEMBERS, []) or []
        if load_entry_id not in members:
            continue
        limit = get_entry_value(
            group, CONF_CIRCUIT_GROUP_CURRENT_LIMIT, DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT
        )
        name = get_entry_value(group, CONF_NAME, None) or group.title
        return f"{name} capped at {_fmt(limit, 'A', 0)} across {len(members)} loads"
    return None


def _load_mode(hass, load_entry) -> str:
    """The load's current operating mode (live if set, else its default)."""
    device_type = load_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
    runtime = (_runtime(hass).get("chargers") or {}).get(load_entry.entry_id) or {}
    key = runtime.get("operating_mode") or get_entry_value(
        load_entry, CONF_OPERATING_MODE, None
    )
    return resolve_operating_mode(device_type, key).label


def _load_limits_text(hass, load_entry) -> str:
    """The load's configured envelope, in the units that device type uses."""
    device_type = load_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
    if device_type == DEVICE_TYPE_PLUG:
        rating = get_entry_value(
            load_entry, CONF_PLUG_POWER_RATING, DEFAULT_PLUG_POWER_RATING
        )
        max_current = get_entry_value(
            load_entry, CONF_PLUG_MAX_CURRENT, DEFAULT_PLUG_MAX_CURRENT
        )
        return f"{_fmt(rating, 'W', 0)} rated, plug max {_fmt(max_current, 'A', 0)}"
    if device_type == DEVICE_TYPE_HOT_WATER_TANK:
        element = get_entry_value(
            load_entry, CONF_HEATING_ELEMENT_POWER, DEFAULT_HEATING_ELEMENT_POWER
        )
        return f"{_fmt(element, 'W', 0)} element"
    if device_type == DEVICE_TYPE_POWER_STATION:
        low = get_entry_value(
            load_entry, CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER
        )
        high = get_entry_value(
            load_entry, CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
        )
        return f"{_fmt(low, 'W', 0)}–{_fmt(high, 'W', 0)}"
    low = get_entry_value(
        load_entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT
    )
    high = get_entry_value(
        load_entry, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT
    )
    return f"{_fmt(low, 'A', 0)}–{_fmt(high, 'A', 0)}"


def _phase_mapping_text(hass, load_entry) -> str:
    """Which site phases this load's legs are wired to."""
    hub_entry_id = load_entry.data.get(CONF_HUB_ENTRY_ID)
    phases = _hub_phase_count(hass, hub_entry_id)
    legs = [
        get_entry_value(load_entry, key, None)
        for key in (
            CONF_CHARGER_L1_PHASE,
            CONF_CHARGER_L2_PHASE,
            CONF_CHARGER_L3_PHASE,
        )
    ][:phases]
    mapped = [leg for leg in legs if leg]
    if not mapped:
        return "not mapped"
    return "→".join(mapped)


def _load_line(hass, hub_entry_id: str, load_entry, hub_data: dict) -> str:
    """One "name · mode · priority · permit · draw · status" line."""
    runtime = _runtime(hass)
    device_type = load_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
    parts = [f"**{load_entry.title}**", _DEVICE_TYPE_LABELS.get(device_type, "Load")]
    parts.append(_load_mode(hass, load_entry))

    priority = get_entry_value(load_entry, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY)
    rank = (runtime.get("charger_ranks") or {}).get(load_entry.entry_id)
    if rank is not None and rank != priority:
        parts.append(f"priority {priority} (served {rank}.)")
    else:
        parts.append(f"priority {priority}")

    parts.append(f"permitted {_fmt(_load_permit(hass, load_entry, hub_data), 'A')}")

    draw = (runtime.get("charger_allocations") or {}).get(load_entry.entry_id)
    if draw is None:
        draw = (hub_data.get("charger_targets") or {}).get(load_entry.entry_id)
    parts.append(f"drawing {_fmt(draw, 'A')}")

    status = (runtime.get("charger_status") or {}).get(load_entry.entry_id)
    parts.append(status or "status unknown")

    mask = (runtime.get("charger_phase_masks") or {}).get(load_entry.entry_id)
    if mask:
        parts.append(f"on {mask}")

    cap = _group_cap_for(hass, hub_entry_id, load_entry.entry_id)
    if cap:
        parts.append(f"⛓ {cap}")
    return " · ".join(parts)


def _battery_power_line(value) -> str:
    """"749 W discharging" from a signed battery power reading.

    The battery power convention everywhere in this integration (entity docs,
    fleet maths) is positive = discharging, negative = charging. Rendering the
    direction as a word instead of a sign also makes a miswired (inverted)
    sensor visible at a glance: a battery "discharging" in full sun below its
    target SOC is the classic symptom.
    """
    if not isinstance(value, (int, float)):
        return _fmt(value, "W", 0)
    if value == 0:
        return f"{_fmt(0, 'W', 0)} idle"
    direction = "discharging" if value > 0 else "charging"
    return f"{_fmt(abs(value), 'W', 0)} {direction}"


def _unmanaged_household_w(hub_data):
    """Household draw excluding managed loads.

    The engine publishes its own estimate as ``household_power`` (ground
    truth from a dedicated solar sensor, else derived from inverter output,
    else the supply identity). The identity below is only the fallback for
    hub_data written before that key existed.
    """
    household = hub_data.get("household_power")
    if isinstance(household, (int, float)):
        return household
    grid = hub_data.get("grid_power")
    solar = hub_data.get("solar_power")
    if not isinstance(grid, (int, float)) or not isinstance(solar, (int, float)):
        return None
    battery = hub_data.get("battery_power")
    managed = hub_data.get("total_evse_power")
    battery = battery if isinstance(battery, (int, float)) else 0
    managed = managed if isinstance(managed, (int, float)) else 0
    return max(0, round(grid + solar + battery - managed, 0))


def _hub_overview_lines(hass, entry) -> list[str]:
    """Site-wide live overview for a hub entry."""
    hub_data, note = _live_hub_data(hass, entry.entry_id)
    voltage = get_entry_value(entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
    lines = [note, ""]

    if hub_data.get("hub_status") and hub_data.get("hub_status") != "OK":
        lines.append(f"⚠️ **{hub_data['hub_status']}**")
    for warning in hub_data.get("hub_warnings") or []:
        lines.append(f"- {warning}")
    if hub_data.get("hub_warnings"):
        lines.append("")

    lines.append("**⚡ Grid**")
    # Whether the site is off-grid is a matter of CONFIG (no CTs), not of what
    # the sensors happen to report this second.
    configured_cts = [
        get_entry_value(entry, key, None)
        for key in (
            CONF_PHASE_A_CURRENT_ENTITY_ID,
            CONF_PHASE_B_CURRENT_ENTITY_ID,
            CONF_PHASE_C_CURRENT_ENTITY_ID,
        )
    ]
    if any(configured_cts):
        grid_phases = [None, None, None]
        try:
            from .engine.hub_calculation import _read_grid_phases

            grid_phases = _read_grid_phases(hass, entry, voltage)
        except Exception:  # pragma: no cover — display path
            _LOGGER.debug("Could not read grid phases for the overview", exc_info=True)
        for label, entity_id, value in zip(("A", "B", "C"), configured_cts, grid_phases):
            if not entity_id:
                continue
            # _read_grid_phases hands back its unavailable sentinel for a
            # configured-but-unreadable CT (it deliberately does not invent 0 A),
            # so the overview can say so instead of showing a confident "0.0 A".
            if units.is_unusable_number(value):
                lines.append(f"- Phase {label}: {_DASH} (sensor unreadable)")
                continue
            flow = "export" if value < 0 else "import"
            lines.append(f"- Phase {label}: {_fmt(abs(value), 'A')} {flow}")
    else:
        lines.append("- No grid CTs configured — off-grid site")
    lines.append(f"- Net grid power: {_fmt(hub_data.get('grid_power'), 'W', 0)}")
    lines.append(f"- Exported now: {_fmt(hub_data.get('total_export_power'), 'W', 0)}")
    if hub_data.get("grid_stale"):
        lines.append("- ⚠️ Grid readings are stale — holding the last known values")

    lines += ["", "**☀️ Solar & battery**"]
    lines.append(f"- Solar production: {_fmt(hub_data.get('solar_power'), 'W', 0)}")
    if hub_data.get("battery_soc") is not None:
        lines.append(
            f"- Battery SOC: {_fmt(hub_data.get('battery_soc'), '%', 0)}"
            f" (min {_fmt(hub_data.get('battery_soc_min'), '%', 0)},"
            f" target {_fmt(hub_data.get('battery_soc_target'), '%', 0)})"
        )
        lines.append(f"- Battery power: {_battery_power_line(hub_data.get('battery_power'))}")
    else:
        lines.append("- No battery configured")

    lines += ["", "**🧮 Power pools**"]
    lines.append(
        "- Site available: "
        f"{_fmt(hub_data.get('total_site_available_power'), 'W', 0)}"
    )
    lines.append(
        f"- Solar surplus: {_fmt(hub_data.get('available_solar_power'), 'W', 0)}"
    )
    lines.append(f"- Grid headroom: {_fmt(hub_data.get('available_grid_power'), 'W', 0)}")
    lines.append(
        f"- Battery headroom: {_fmt(hub_data.get('available_battery_power'), 'W', 0)}"
    )
    excess = hub_data.get("excess_available")
    if excess is not None:
        margin = hub_data.get("excess_margin_power")
        if isinstance(margin, (int, float)):
            detail = f"{_fmt(abs(margin), 'W', 0)} {'above' if margin >= 0 else 'below'} trigger"
        else:
            detail = f"margin {_fmt(margin, 'W', 0)}"
        lines.append(f"- Excess trigger: {'on' if excess else 'off'} ({detail})")
    lines.append(
        "- Per-phase headroom: "
        f"A {_fmt(hub_data.get('available_current_a'), 'A')}"
        f" · B {_fmt(hub_data.get('available_current_b'), 'A')}"
        f" · C {_fmt(hub_data.get('available_current_c'), 'A')}"
    )
    lines.append(
        f"- Managed loads drawing: {_fmt(hub_data.get('total_evse_power'), 'W', 0)}"
    )
    unmanaged = _unmanaged_household_w(hub_data)
    if unmanaged is not None:
        lines.append(
            f"- Unmanaged loads (household): {_fmt(unmanaged, 'W', 0)}"
        )

    hub_runtime = (_runtime(hass).get("hubs") or {}).get(entry.entry_id) or {}
    distribution = hub_runtime.get("distribution_mode") or get_entry_value(
        entry, CONF_DISTRIBUTION_MODE, DEFAULT_DISTRIBUTION_MODE
    )
    loads = _devices_by_priority(_controlled_devices(hass, entry.entry_id))
    lines += ["", f"**🔌 Loads** — distribution: {distribution}"]
    if not loads:
        lines.append("- No loads configured yet")
    for load in loads:
        lines.append(f"- {_load_line(hass, entry.entry_id, load, hub_data)}")

    group_data = hub_data.get("group_data") or {}
    groups = _groups_for_hub(hass, entry.entry_id)
    if groups:
        lines += ["", "**⛓ Circuit groups**"]
        for group in groups:
            live = group_data.get(group.entry_id) or {}
            limit = get_entry_value(
                group,
                CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
                DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
            )
            members = get_entry_value(group, CONF_CIRCUIT_GROUP_MEMBERS, []) or []
            detail = (
                f"worst phase {_fmt(live.get('max_phase_draw'), 'A')}, "
                f"headroom {_fmt(live.get('headroom'), 'A')}"
                if live
                else "no live data yet"
            )
            lines.append(
                f"- **{group.title}**: limit {_fmt(limit, 'A', 0)} · "
                f"{len(members)} loads · {detail}"
            )
    return lines


def _load_overview_lines(hass, entry) -> list[str]:
    """Live detail for one managed load (EVSE, plug, tank, power station)."""
    hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
    hub_data, note = _live_hub_data(hass, hub_entry_id)
    runtime = _runtime(hass)
    device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
    lines = [note, ""]

    lines.append("**🔌 This load**")
    lines.append(f"- Type: {_DEVICE_TYPE_LABELS.get(device_type, 'Load')}")
    lines.append(f"- Operating mode: {_load_mode(hass, entry)}")
    priority = get_entry_value(entry, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY)
    rank = (runtime.get("charger_ranks") or {}).get(entry.entry_id)
    lines.append(
        f"- Priority: {priority}"
        + (f" · served {rank}. this cycle" if rank is not None else "")
    )
    lines.append(f"- Configured envelope: {_load_limits_text(hass, entry)}")
    if device_type in (DEVICE_TYPE_EVSE, DEVICE_TYPE_POWER_STATION, DEVICE_TYPE_PLUG):
        lines.append(f"- Phase mapping (L1→L3): {_phase_mapping_text(hass, entry)}")

    lines += ["", "**📊 Right now**"]
    lines.append(f"- Permitted: {_fmt(_load_permit(hass, entry, hub_data), 'A')}")
    draw = (runtime.get("charger_allocations") or {}).get(entry.entry_id)
    lines.append(f"- Actual draw: {_fmt(draw, 'A')}")
    status = (runtime.get("charger_status") or {}).get(entry.entry_id)
    lines.append(f"- Status: {status or 'unknown'}")
    mask = (runtime.get("charger_phase_masks") or {}).get(entry.entry_id)
    if mask:
        lines.append(f"- Drawing on phases: {mask}")
    charger_runtime = (runtime.get("chargers") or {}).get(entry.entry_id) or {}
    if charger_runtime.get("dynamic_control") is False:
        lines.append("- ⚠️ Dynamic control is OFF — Load Juggler is not limiting this load")

    cap = _group_cap_for(hass, hub_entry_id, entry.entry_id)
    lines += ["", "**⛓ Circuit group**"]
    lines.append(f"- {cap}" if cap else "- Not in a circuit group")

    lines += ["", "**🏠 Site**"]
    lines.append(
        "- Site available: "
        f"{_fmt(hub_data.get('total_site_available_power'), 'W', 0)}"
    )
    lines.append(f"- Solar production: {_fmt(hub_data.get('solar_power'), 'W', 0)}")
    if hub_data.get("battery_soc") is not None:
        lines.append(f"- Battery SOC: {_fmt(hub_data.get('battery_soc'), '%', 0)}")
    return lines


def _inverter_overview_lines(hass, entry) -> list[str]:
    """Live detail for one inverter entry (output, battery, forecast advice)."""
    hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
    hub_data, note = _live_hub_data(hass, hub_entry_id)
    own = (hub_data.get("inverters") or {}).get(entry.entry_id) or {}
    voltage = get_entry_value(
        hass.config_entries.async_get_entry(hub_entry_id) or entry,
        CONF_PHASE_VOLTAGE,
        DEFAULT_PHASE_VOLTAGE,
    )
    lines = [note, ""]

    lines.append("**🔆 Output**")
    phase_lines: list[str] = []
    try:
        from .engine.hub_calculation import _read_inverter_output

        for label, key in (
            ("A", CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID),
            ("B", CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID),
            ("C", CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID),
        ):
            entity_id = get_entry_value(entry, key, None)
            if not entity_id:
                continue
            value = _read_inverter_output(hass, entity_id, voltage)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                value = None
            if isinstance(value, (int, float)) and value < 0:
                # Signed readings are real (#15): negative = power flowing INTO
                # this inverter, e.g. an AC-coupled inverter on its load port.
                phase_lines.append(
                    f"- Phase {label}: {_fmt(abs(value), 'A')} absorbing"
                    " (flowing in, e.g. via the load port)"
                )
            else:
                phase_lines.append(f"- Phase {label}: {_fmt(value, 'A')}")
    except Exception:  # pragma: no cover — display path
        _LOGGER.debug("Could not read inverter output for the overview", exc_info=True)
    lines += phase_lines or ["- No per-phase output sensors configured"]
    lines.append(f"- Solar production: {_fmt(own.get('solar_w'), 'W', 0)}")
    max_power = get_entry_value(entry, CONF_INVERTER_MAX_POWER, None)
    per_phase = get_entry_value(entry, CONF_INVERTER_MAX_POWER_PER_PHASE, None)
    if max_power or per_phase:
        lines.append(
            f"- Rated: {_fmt(max_power, 'W', 0)} total"
            f" · {_fmt(per_phase, 'W', 0)} per phase"
        )

    if get_entry_value(entry, CONF_BATTERY_SOC_ENTITY_ID, None):
        lines += ["", "**🔋 Battery**"]
        lines.append(f"- SOC: {_fmt(own.get('battery_soc'), '%', 0)}")
        lines.append(f"- Power: {_battery_power_line(own.get('battery_power'))}")
        lines.append(
            f"- Reserve floor: {_fmt(get_entry_value(entry, CONF_BATTERY_SOC_MIN, DEFAULT_BATTERY_SOC_MIN), '%', 0)}"
            f" · full at {_fmt(get_entry_value(entry, CONF_BATTERY_SOC_FULL, DEFAULT_BATTERY_SOC_FULL), '%', 0)}"
        )
        lines.append(
            "- Charge/discharge limits: "
            f"{_fmt(get_entry_value(entry, CONF_BATTERY_MAX_CHARGE_POWER, None), 'W', 0)}"
            f" / {_fmt(get_entry_value(entry, CONF_BATTERY_MAX_DISCHARGE_POWER, None), 'W', 0)}"
        )
        if own.get("forecast_battery_max_soc") is not None:
            lines += ["", "**📈 Forecast advice**"]
            lines.append(
                f"- Hold SOC below: {_fmt(own.get('forecast_battery_max_soc'), '%', 0)}"
            )
            lines.append(
                f"- Recommended charge limit: {_fmt(own.get('forecast_charge_limit_w'), 'W', 0)}"
            )
    else:
        lines += ["", "- No battery configured on this inverter"]
    return lines


def _group_overview_lines(hass, entry) -> list[str]:
    """Live detail for one circuit group: limit, members, headroom."""
    hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
    hub_data, note = _live_hub_data(hass, hub_entry_id)
    live = (hub_data.get("group_data") or {}).get(entry.entry_id) or {}
    runtime = _runtime(hass)
    limit = get_entry_value(
        entry, CONF_CIRCUIT_GROUP_CURRENT_LIMIT, DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT
    )
    members = get_entry_value(entry, CONF_CIRCUIT_GROUP_MEMBERS, []) or []
    lines = [note, ""]

    lines.append("**⛓ Shared breaker**")
    lines.append(f"- Limit: {_fmt(limit, 'A', 0)} per phase")
    if live:
        per_phase = live.get("per_phase_draw") or {}
        if isinstance(per_phase, dict) and per_phase:
            lines.append(
                "- Draw per phase: "
                + " · ".join(
                    f"{phase} {_fmt(value, 'A')}" for phase, value in per_phase.items()
                )
            )
        lines.append(f"- Worst phase: {_fmt(live.get('max_phase_draw'), 'A')}")
        lines.append(f"- Headroom: {_fmt(live.get('headroom'), 'A')}")

    lines += ["", "**🔌 Members**"]
    if not members:
        lines.append("- No loads selected")
    for member_id in members:
        member = hass.config_entries.async_get_entry(member_id)
        if member is None:
            lines.append(f"- (removed entry {member_id[-8:]})")
            continue
        draw = (runtime.get("charger_allocations") or {}).get(member_id)
        status = (runtime.get("charger_status") or {}).get(member_id)
        lines.append(
            f"- **{member.title}**: drawing {_fmt(draw, 'A')}"
            f" · {status or 'status unknown'}"
        )
    return lines


def _overview_text(hass, entry_id: str) -> str:
    """The Overview page body for any entry type, scoped to that entry."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return "The configuration entry no longer exists."
    entry_type = entry.data.get(ENTRY_TYPE, ENTRY_TYPE_HUB)
    if entry_type == ENTRY_TYPE_HUB:
        body = _hub_overview_lines(hass, entry)
    elif entry_type == ENTRY_TYPE_INVERTER:
        body = _inverter_overview_lines(hass, entry)
    elif entry_type == ENTRY_TYPE_GROUP:
        body = _group_overview_lines(hass, entry)
    else:
        body = _load_overview_lines(hass, entry)
    return "\n".join([f"### {entry.title}", ""] + body)


def _summary_text(hass, hub_entry_id: str) -> str:
    """"How it decides": what is configured, in engine evaluation order."""
    entry = hass.config_entries.async_get_entry(hub_entry_id)
    if entry is None:
        return "The configuration entry no longer exists."

    from . import get_inverters_for_hub  # local: avoids a circular import

    phases = _hub_phase_count(hass, hub_entry_id)
    voltage = get_entry_value(entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
    lines = [f"### {entry.title}", ""]

    # 1 — site basics
    lines.append("**🏠 Site**")
    lines.append(f"- ✓ {phases}-phase site at {_fmt(voltage, 'V', 0)}")
    grid_cts = [
        label
        for label, key in (
            ("A", CONF_PHASE_A_CURRENT_ENTITY_ID),
            ("B", CONF_PHASE_B_CURRENT_ENTITY_ID),
            ("C", CONF_PHASE_C_CURRENT_ENTITY_ID),
        )
        if get_entry_value(entry, key, None)
    ]
    if grid_cts:
        lines.append(
            f"- ✓ Grid CTs on phase {', '.join(grid_cts)}, main breaker "
            f"{_fmt(get_entry_value(entry, CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING), 'A', 0)} per phase"
        )
        if get_entry_value(entry, CONF_INVERT_PHASES, False):
            lines.append("- ✓ Grid readings are inverted before use")
    else:
        lines.append("- ✓ Off-grid — phases are inferred from inverter output")
    if get_entry_value(entry, CONF_ENABLE_MAX_IMPORT_POWER, False) or get_entry_value(
        entry, CONF_MAX_IMPORT_POWER_ENTITY_ID, None
    ):
        lines.append("- ✓ Import power is capped by the max-import limit")
    export_limit = get_entry_value(entry, CONF_GRID_EXPORT_LIMIT, None)
    if export_limit:
        lines.append(f"- ✓ Export limited to {_fmt(export_limit, 'W', 0)}")

    # 2 — inverter fleet and batteries
    inverters = get_inverters_for_hub(hass, hub_entry_id)
    lines += ["", "**🔆 Power sources**"]
    if inverters:
        lines.append(f"- ✓ {len(inverters)} inverter(s) in the fleet")
        for inverter in inverters:
            bits = [f"**{inverter.title}**"]
            max_power = get_entry_value(inverter, CONF_INVERTER_MAX_POWER, None)
            if max_power:
                bits.append(_fmt(max_power, "W", 0))
            bits.append(
                "asymmetric"
                if get_entry_value(inverter, CONF_INVERTER_SUPPORTS_ASYMMETRIC, False)
                else "symmetric"
            )
            topology = get_entry_value(
                inverter, CONF_WIRING_TOPOLOGY, DEFAULT_WIRING_TOPOLOGY
            )
            if topology:
                bits.append(str(topology))
            if get_entry_value(inverter, CONF_BATTERY_SOC_ENTITY_ID, None):
                bits.append(
                    "battery "
                    f"{_fmt(get_entry_value(inverter, CONF_BATTERY_SOC_MIN, DEFAULT_BATTERY_SOC_MIN), '%', 0)}"
                    "–"
                    f"{_fmt(get_entry_value(inverter, CONF_BATTERY_SOC_FULL, DEFAULT_BATTERY_SOC_FULL), '%', 0)}"
                )
            if get_entry_value(inverter, CONF_SOLAR_FORECAST_DEVICE_IDS, None):
                bits.append("PV forecast active")
            lines.append(f"- {' · '.join(bits)}")
    else:
        lines.append("- No inverter configured — grid capacity only")

    # 3 — distribution
    hub_runtime = (_runtime(hass).get("hubs") or {}).get(hub_entry_id) or {}
    distribution = hub_runtime.get("distribution_mode") or get_entry_value(
        entry, CONF_DISTRIBUTION_MODE, DEFAULT_DISTRIBUTION_MODE
    )
    lines += ["", "**⚖️ Sharing**"]
    lines.append(f"- ✓ Distribution mode: {distribution}")
    lines.append("- ✓ Served by mode urgency first, then by priority number")

    # 4 — loads, in the order the engine serves them
    loads = _controlled_devices(hass, hub_entry_id)
    ordered = sorted(
        loads,
        key=lambda e: (
            resolve_operating_mode(
                e.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE),
                ((_runtime(hass).get("chargers") or {}).get(e.entry_id) or {}).get(
                    "operating_mode"
                )
                or get_entry_value(e, CONF_OPERATING_MODE, None),
            ).priority,
            get_entry_value(e, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            e.title,
        ),
    )
    lines += ["", "**🔌 Loads, in serving order**"]
    if not ordered:
        lines.append("- No loads configured yet")
    for position, load in enumerate(ordered, start=1):
        device_type = load.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        bits = [
            f"**{position}. {load.title}**",
            _DEVICE_TYPE_LABELS.get(device_type, "Load"),
            _load_mode(hass, load),
            _load_limits_text(hass, load),
        ]
        if device_type in (
            DEVICE_TYPE_EVSE,
            DEVICE_TYPE_POWER_STATION,
            DEVICE_TYPE_PLUG,
        ):
            bits.append(f"phases {_phase_mapping_text(hass, load)}")
        cap = _group_cap_for(hass, hub_entry_id, load.entry_id)
        if cap:
            bits.append(f"⛓ {cap}")
        lines.append(f"- {' · '.join(bits)}")

    # 5 — post-distribution capping
    groups = _groups_for_hub(hass, hub_entry_id)
    if groups:
        lines += ["", "**⛓ Circuit groups (applied last)**"]
        for group in groups:
            members = get_entry_value(group, CONF_CIRCUIT_GROUP_MEMBERS, []) or []
            lines.append(
                f"- ✓ **{group.title}**: "
                f"{_fmt(get_entry_value(group, CONF_CIRCUIT_GROUP_CURRENT_LIMIT, DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT), 'A', 0)}"
                f" shared by {len(members)} load(s)"
            )
    return "\n".join(lines)


class LoadJugglerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Load Juggler."""

    VERSION = 2
    MINOR_VERSION = 4

    def __init__(self):
        self._data = {}
        self._discovered_chargers = []
        self._selected_charger = None
        self._entity_cache = None

    def _get_entity_registry_ids(self) -> list[str]:
        if self._entity_cache is None:
            entity_registry = async_get_entity_registry(self.hass)
            # Only include entities that are present in the state machine.
            # Disabled/stale registry entries have no state and should not be
            # offered as auto-detection candidates (they would end up as a
            # suggested_value that is not in include_entities, breaking submission).
            self._entity_cache = [
                eid
                for eid in entity_registry.entities.keys()
                if self.hass.states.get(eid) is not None
            ]
        return self._entity_cache

    def _entity_ids_for(
        self,
        device_classes: set,
        valid_units: frozenset | None = None,
        domains: list[str] | None = None,
    ) -> list[str]:
        """Build entity ID list for the include_entities selector parameter.

        Selection logic (same for every domain):
          device_class matches
          OR (device_class is None AND unit_of_measurement matches valid_units)
        """
        if domains is None:
            domains = ["sensor"]
        registry = async_get_entity_registry(self.hass)
        result = []
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            if entity_id.split(".", 1)[0] not in domains:
                continue
            # Prefer registry device_class override; fall back to state attribute.
            entry = registry.async_get(entity_id)
            if entry is not None:
                effective_class = entry.device_class or entry.original_device_class
            else:
                effective_class = state.attributes.get("device_class")
            if effective_class not in device_classes:
                continue
            if effective_class is None and valid_units:
                unit = state.attributes.get("unit_of_measurement")
                if not unit or unit not in valid_units:
                    continue
            result.append(entity_id)
        return result

    @staticmethod
    def _optional_entity_field(key: str, default_val):
        """Create vol.Optional with suggested_value so the user can truly clear it.

        Using suggested_value instead of default lets the entity selector
        be cleared with X — vol.Optional(default=...) would silently
        re-fill the default on clear.
        """
        val = normalize_optional_entity(default_val)
        if val:
            return vol.Optional(key, description={"suggested_value": val})
        return vol.Optional(key)

    def _build_hub_grid_schema(self, defaults: dict | None = None) -> list[tuple]:
        """Build grid/electrical fields as a reusable list."""
        defaults = defaults or {}
        entity_sel_current_power = selector(
            {
                "entity": {
                    "include_entities": self._entity_ids_for(
                        {None, "current", "power"},
                        valid_units=_CURRENT_UNITS | _POWER_UNITS,
                    ),
                }
            }
        )

        return [
            (
                self._optional_entity_field(
                    CONF_PHASE_A_CURRENT_ENTITY_ID,
                    defaults.get(CONF_PHASE_A_CURRENT_ENTITY_ID),
                ),
                entity_sel_current_power,
            ),
            (
                self._optional_entity_field(
                    CONF_PHASE_B_CURRENT_ENTITY_ID,
                    defaults.get(CONF_PHASE_B_CURRENT_ENTITY_ID),
                ),
                entity_sel_current_power,
            ),
            (
                self._optional_entity_field(
                    CONF_PHASE_C_CURRENT_ENTITY_ID,
                    defaults.get(CONF_PHASE_C_CURRENT_ENTITY_ID),
                ),
                entity_sel_current_power,
            ),
            (
                vol.Required(
                    CONF_MAIN_BREAKER_RATING,
                    default=defaults.get(
                        CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 1,
                            "max": 200,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "A",
                        }
                    }
                ),
            ),
            (
                vol.Required(
                    CONF_INVERT_PHASES,
                    default=defaults.get(CONF_INVERT_PHASES, False),
                ),
                bool,
            ),
            (
                vol.Required(
                    CONF_ENABLE_MAX_IMPORT_POWER,
                    default=defaults.get(CONF_ENABLE_MAX_IMPORT_POWER, True),
                ),
                bool,
            ),
            (
                self._optional_entity_field(
                    CONF_MAX_IMPORT_POWER_ENTITY_ID,
                    defaults.get(CONF_MAX_IMPORT_POWER_ENTITY_ID),
                ),
                selector(
                    {
                        "entity": {
                            "include_entities": self._entity_ids_for(
                                {None, "power"},
                                valid_units=_POWER_UNITS,
                                domains=["sensor", "input_number"],
                            ),
                        }
                    }
                ),
            ),
            (
                vol.Required(
                    CONF_PHASE_VOLTAGE,
                    default=defaults.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE),
                ),
                selector(
                    {
                        "number": {
                            "min": 100,
                            "max": 400,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "V",
                        }
                    }
                ),
            ),
            (
                # The ONE export number: the site's physical/contract ceiling.
                # The Excess trigger derives from it (limit − trigger margin)
                # and the PV clipping forecast integrates above it. 0 = no
                # limit: grid-side Excess never triggers, forecast off.
                vol.Optional(
                    CONF_GRID_EXPORT_LIMIT,
                    default=defaults.get(
                        CONF_GRID_EXPORT_LIMIT, DEFAULT_GRID_EXPORT_LIMIT
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 50000,
                            "step": 100,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_EXCESS_TRIGGER_MARGIN,
                    default=defaults.get(
                        CONF_EXCESS_TRIGGER_MARGIN, DEFAULT_EXCESS_TRIGGER_MARGIN
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 5000,
                            "step": 50,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
            ),
            (
                # The release band, not the trigger point: once Excess is
                # engaged the surplus may fall this far below the trigger
                # before an engaged load lets go.
                vol.Optional(
                    CONF_EXCESS_HYSTERESIS,
                    default=defaults.get(
                        CONF_EXCESS_HYSTERESIS, DEFAULT_EXCESS_HYSTERESIS
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 5000,
                            "step": 50,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_SITE_UPDATE_FREQUENCY,
                    default=defaults.get(
                        CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 1,
                            "max": 60,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "s",
                        }
                    }
                ),
            ),
            (
                vol.Required(
                    CONF_AUTO_DETECT_PHASE_MAPPING,
                    default=defaults.get(CONF_AUTO_DETECT_PHASE_MAPPING, True),
                ),
                bool,
            ),
            (
                vol.Required(
                    CONF_SOLAR_GRACE_PERIOD,
                    default=defaults.get(
                        CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 30,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "min",
                        }
                    }
                ),
            ),
            # --- Site policy ---
            # Hardware (inverters, batteries, PV arrays and their forecasts)
            # lives on the inverter entries; what stays here is site-wide
            # policy applied to whatever fleet those entries form.
            (
                vol.Optional(
                    CONF_BATTERY_SOC_HYSTERESIS,
                    default=defaults.get(
                        CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 1,
                            "max": 10,
                            "step": 1,
                            "mode": "slider",
                            "unit_of_measurement": "%",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_BASE_CONSUMPTION,
                    default=defaults.get(
                        CONF_BASE_CONSUMPTION, DEFAULT_BASE_CONSUMPTION
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 10000,
                            "step": 50,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_FORECAST_SOC_FLOOR,
                    default=defaults.get(
                        CONF_FORECAST_SOC_FLOOR, DEFAULT_FORECAST_SOC_FLOOR
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 90,
                            "step": 1,
                            "mode": "slider",
                            "unit_of_measurement": "%",
                        }
                    }
                ),
            ),
        ]

    def _build_inverter_solar_schema(self, defaults: dict | None = None) -> list[tuple]:
        """PV fields for an INVERTER entry — the array behind this inverter.

        Its production sensor and its Open-Meteo forecast device(s) belong to
        the inverter, not the site: a hybrid and an AC-coupled string inverter
        each have their own array. The engine sums the fleet's production and
        merges the fleet's forecast devices into ONE site forecast, because
        clipping is decided by the site-wide export limit.
        """
        defaults = defaults or {}
        return [
            (
                self._optional_entity_field(
                    CONF_SOLAR_PRODUCTION_ENTITY_ID,
                    defaults.get(CONF_SOLAR_PRODUCTION_ENTITY_ID),
                ),
                selector(
                    {
                        "entity": {
                            "include_entities": self._entity_ids_for(
                                {None, "power"}, valid_units=_POWER_UNITS
                            ),
                        }
                    }
                ),
            ),
            (
                # One forecast DEVICE per PV array — the Open-Meteo Solar
                # Forecast integration creates one device per array, and
                # several of its sensors carry the same watts series, so
                # letting the user pick sensors risks double-counting.
                vol.Optional(
                    CONF_SOLAR_FORECAST_DEVICE_IDS,
                    default=defaults.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or [],
                ),
                selector(
                    {
                        "device": {
                            "multiple": True,
                            "filter": {"integration": "open_meteo_solar_forecast"},
                        }
                    }
                ),
            ),
        ]

    def _build_hub_battery_schema(self, defaults: dict | None = None) -> list[tuple]:
        """LEGACY hub solar/battery fields — shown only while a hub still
        carries them, i.e. before the one-time auto-import moves them onto an
        inverter entry. New hubs never see this page: their solar sensor,
        forecast devices and battery hardware are configured per inverter, and
        the site policy that stays behind lives on the hub settings page.
        """
        defaults = defaults or {}

        fields = [
            (
                self._optional_entity_field(
                    CONF_SOLAR_PRODUCTION_ENTITY_ID,
                    defaults.get(CONF_SOLAR_PRODUCTION_ENTITY_ID),
                ),
                selector(
                    {
                        "entity": {
                            "include_entities": self._entity_ids_for(
                                {None, "power"}, valid_units=_POWER_UNITS
                            ),
                        }
                    }
                ),
            ),
            (
                self._optional_entity_field(
                    CONF_BATTERY_SOC_ENTITY_ID,
                    defaults.get(CONF_BATTERY_SOC_ENTITY_ID),
                ),
                selector(
                    {
                        "entity": {
                            "include_entities": self._entity_ids_for(
                                {None, "battery"}, valid_units=_SOC_UNITS
                            ),
                        }
                    }
                ),
            ),
            (
                self._optional_entity_field(
                    CONF_BATTERY_POWER_ENTITY_ID,
                    defaults.get(CONF_BATTERY_POWER_ENTITY_ID),
                ),
                selector(
                    {
                        "entity": {
                            "include_entities": self._entity_ids_for(
                                {None, "power"}, valid_units=_POWER_UNITS
                            ),
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_BATTERY_MAX_CHARGE_POWER,
                    default=defaults.get(
                        CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 50000,
                            "step": 100,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_BATTERY_MAX_DISCHARGE_POWER,
                    default=defaults.get(
                        CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 50000,
                            "step": 100,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_BATTERY_SOC_FULL,
                    default=defaults.get(
                        CONF_BATTERY_SOC_FULL, DEFAULT_BATTERY_SOC_FULL
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 50,
                            "max": 100,
                            "step": 1,
                            "mode": "slider",
                            "unit_of_measurement": "%",
                        }
                    }
                ),
            ),
            # --- PV clipping forecast (advisory battery headroom) ---
            # Active only when the grid export limit (hub grid step), a battery
            # capacity and at least one forecast entity are all set.
            (
                # One forecast DEVICE per PV array — the Open-Meteo Solar
                # Forecast integration creates one device per array, and
                # several of its sensors carry the same watts series, so
                # letting the user pick sensors risks double-counting.
                vol.Optional(
                    CONF_SOLAR_FORECAST_DEVICE_IDS,
                    default=defaults.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or [],
                ),
                selector(
                    {
                        "device": {
                            "multiple": True,
                            "filter": {"integration": "open_meteo_solar_forecast"},
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_BATTERY_CAPACITY_KWH,
                    default=defaults.get(
                        CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 1000,
                            "step": 0.1,
                            "mode": "box",
                            "unit_of_measurement": "kWh",
                        }
                    }
                ),
            ),
        ]
        return fields

    def _build_hub_inverter_schema(self, defaults: dict | None = None) -> list[tuple]:
        """Build inverter configuration fields as a reusable list."""
        defaults = defaults or {}
        entity_sel_current_power = selector(
            {
                "entity": {
                    "include_entities": self._entity_ids_for(
                        {None, "current", "power"},
                        valid_units=_CURRENT_UNITS | _POWER_UNITS,
                    ),
                }
            }
        )
        topology_options = [
            {
                "value": WIRING_TOPOLOGY_PARALLEL,
                "label": "Parallel (AC-coupled / no battery)",
            },
            {"value": WIRING_TOPOLOGY_SERIES, "label": "Series (Hybrid / battery)"},
        ]
        return [
            (
                vol.Optional(
                    CONF_INVERTER_MAX_POWER,
                    default=defaults.get(CONF_INVERTER_MAX_POWER, 0),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 50000,
                            "step": 100,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_INVERTER_MAX_POWER_PER_PHASE,
                    default=defaults.get(CONF_INVERTER_MAX_POWER_PER_PHASE, 0),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 20000,
                            "step": 100,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
            ),
            (
                vol.Required(
                    CONF_INVERTER_SUPPORTS_ASYMMETRIC,
                    default=defaults.get(CONF_INVERTER_SUPPORTS_ASYMMETRIC, False),
                ),
                bool,
            ),
            (
                self._optional_entity_field(
                    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
                    defaults.get(CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID),
                ),
                entity_sel_current_power,
            ),
            (
                self._optional_entity_field(
                    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
                    defaults.get(CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID),
                ),
                entity_sel_current_power,
            ),
            (
                self._optional_entity_field(
                    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
                    defaults.get(CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID),
                ),
                entity_sel_current_power,
            ),
            (
                vol.Required(
                    CONF_WIRING_TOPOLOGY,
                    default=defaults.get(CONF_WIRING_TOPOLOGY, DEFAULT_WIRING_TOPOLOGY),
                ),
                selector({"select": {"options": topology_options, "mode": "dropdown"}}),
            ),
        ]

    def _build_inverter_battery_schema(self, defaults: dict | None = None) -> list[tuple]:
        """Battery fields for an INVERTER entry — the battery physically behind
        this inverter. Reuses the hub-level key names (see ENTRY_TYPE_INVERTER
        in const/common.py), but deliberately excludes the hub-policy fields
        (SOC target/min sliders, hysteresis) and the hub-scoped solar
        production / forecast inputs."""
        defaults = defaults or {}
        entity_sel_power = selector(
            {
                "entity": {
                    "include_entities": self._entity_ids_for(
                        {None, "power"}, valid_units=_POWER_UNITS
                    ),
                }
            }
        )
        return [
            (
                self._optional_entity_field(
                    CONF_BATTERY_SOC_ENTITY_ID,
                    defaults.get(CONF_BATTERY_SOC_ENTITY_ID),
                ),
                selector(
                    {
                        "entity": {
                            "include_entities": self._entity_ids_for(
                                {None, "battery"}, valid_units=_SOC_UNITS
                            ),
                        }
                    }
                ),
            ),
            (
                self._optional_entity_field(
                    CONF_BATTERY_POWER_ENTITY_ID,
                    defaults.get(CONF_BATTERY_POWER_ENTITY_ID),
                ),
                entity_sel_power,
            ),
            (
                vol.Optional(
                    CONF_BATTERY_MAX_CHARGE_POWER,
                    default=defaults.get(
                        CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 50000,
                            "step": 100,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_BATTERY_MAX_DISCHARGE_POWER,
                    default=defaults.get(
                        CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 50000,
                            "step": 100,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_BATTERY_SOC_FULL,
                    default=defaults.get(
                        CONF_BATTERY_SOC_FULL, DEFAULT_BATTERY_SOC_FULL
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 50,
                            "max": 100,
                            "step": 1,
                            "mode": "slider",
                            "unit_of_measurement": "%",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_BATTERY_CAPACITY_KWH,
                    default=defaults.get(
                        CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 1000,
                            "step": 0.1,
                            "mode": "box",
                            "unit_of_measurement": "kWh",
                        }
                    }
                ),
            ),
        ]

    def _build_inverter_control_schema(self, defaults: dict | None = None) -> list[tuple]:
        """Write-control fields for an INVERTER entry — optional throughout.

        With no target entity the inverter stays advisory: the forecast's
        recommended charge limit is published as a sensor and nothing is
        written. Naming a register adds the opt-in switch that starts writes.

        Two independent controls share this page — the charge RATE (one register)
        and the SOC CEILING (a list of time-of-use slot entities). Each gets its
        own switch, and configuring one does not imply the other.
        """
        defaults = defaults or {}
        return [
            (
                self._optional_entity_field(
                    CONF_CHARGE_LIMIT_ENTITY_ID,
                    defaults.get(CONF_CHARGE_LIMIT_ENTITY_ID),
                ),
                selector({"entity": {"domain": "number"}}),
            ),
            (
                vol.Optional(
                    CONF_CHARGE_LIMIT_UNIT,
                    default=defaults.get(
                        CONF_CHARGE_LIMIT_UNIT, DEFAULT_CHARGE_LIMIT_UNIT
                    ),
                ),
                selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": CHARGE_LIMIT_UNIT_AMPS,
                                    "label": "Amps (DC, at battery voltage)",
                                },
                                {"value": CHARGE_LIMIT_UNIT_WATTS, "label": "Watts"},
                            ],
                            "mode": "dropdown",
                        }
                    }
                ),
            ),
            (
                self._optional_entity_field(
                    CONF_BATTERY_VOLTAGE_ENTITY_ID,
                    defaults.get(CONF_BATTERY_VOLTAGE_ENTITY_ID),
                ),
                selector(
                    {
                        "entity": {
                            "include_entities": self._entity_ids_for(
                                {None, "voltage"}, valid_units=_VOLTAGE_UNITS
                            ),
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_BATTERY_NOMINAL_VOLTAGE,
                    default=defaults.get(
                        CONF_BATTERY_NOMINAL_VOLTAGE, DEFAULT_BATTERY_NOMINAL_VOLTAGE
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 12,
                            "max": 1000,
                            "step": 0.1,
                            "mode": "box",
                            "unit_of_measurement": "V",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_CHARGE_LIMIT_NORMAL,
                    default=defaults.get(
                        CONF_CHARGE_LIMIT_NORMAL, DEFAULT_CHARGE_LIMIT_NORMAL
                    ),
                ),
                selector(
                    {"number": {"min": 0, "max": 1000, "step": 1, "mode": "box"}}
                ),
            ),
            (
                vol.Optional(
                    CONF_CHARGE_CONTROL_INTERVAL,
                    default=defaults.get(
                        CONF_CHARGE_CONTROL_INTERVAL, DEFAULT_CHARGE_CONTROL_INTERVAL
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 30,
                            "max": 3600,
                            "step": 30,
                            "mode": "box",
                            "unit_of_measurement": "s",
                        }
                    }
                ),
            ),
            (
                vol.Optional(
                    CONF_CHARGE_CONTROL_DEADBAND,
                    default=defaults.get(
                        CONF_CHARGE_CONTROL_DEADBAND, DEFAULT_CHARGE_CONTROL_DEADBAND
                    ),
                ),
                selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 50,
                            "step": 1,
                            "mode": "slider",
                            "unit_of_measurement": "%",
                        }
                    }
                ),
            ),
            (
                # MULTI-select on purpose: on a Deye the SOC ceiling is one
                # entity per time-of-use slot, so controlling the ceiling means
                # writing every slot the battery may charge in. input_number is
                # offered beside number because a user may front the slots with
                # their own helper.
                vol.Optional(
                    CONF_SOC_LIMIT_ENTITY_IDS,
                    default=defaults.get(CONF_SOC_LIMIT_ENTITY_IDS) or [],
                ),
                selector(
                    {
                        "entity": {
                            "multiple": True,
                            "domain": ["number", "input_number"],
                        }
                    }
                ),
            ),
            (
                # The live "normal" ceiling. An entity rather than a number so
                # whatever already owns the slots keeps owning them — we only
                # ever push below it. sensor is allowed too: a template sensor
                # deriving the ceiling from a schedule is a normal way to do it.
                self._optional_entity_field(
                    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
                    defaults.get(CONF_SOC_LIMIT_NORMAL_ENTITY_ID),
                ),
                selector(
                    {"entity": {"domain": ["input_number", "number", "sensor"]}}
                ),
            ),
        ]

    def _inverter_battery_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Schema for the inverter-entry battery step."""
        return vol.Schema(dict(self._build_inverter_battery_schema(defaults)))

    def _inverter_control_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Schema for the inverter-entry charge write-control step."""
        return vol.Schema(dict(self._build_inverter_control_schema(defaults)))

    def _inverter_combined_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Inverter + solar + battery + write-control on one page
        (inverter options flow)."""
        fields = self._build_hub_inverter_schema(defaults)
        fields.extend(self._build_inverter_solar_schema(defaults))
        fields.extend(self._build_inverter_battery_schema(defaults))
        fields.extend(self._build_inverter_control_schema(defaults))
        return vol.Schema(dict(fields))

    def _hub_schema(
        self,
        defaults: dict | None = None,
        include_grid: bool = True,
        include_battery: bool = True,
        include_inverter: bool = False,
    ) -> vol.Schema:
        """
        Build a combined hub schema from reusable field lists.
        This centralizes schema construction to reduce duplication.
        """
        defaults = defaults or {}
        fields_list: list[tuple] = []

        if include_grid:
            fields_list.extend(self._build_hub_grid_schema(defaults))
        if include_battery:
            fields_list.extend(self._build_hub_battery_schema(defaults))
        if include_inverter:
            fields_list.extend(self._build_hub_inverter_schema(defaults))

        return vol.Schema(dict(fields_list))

    def _hub_grid_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema with only grid/electrical fields."""
        return self._hub_schema(defaults, include_grid=True, include_battery=False)

    def _hub_battery_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema with only the legacy hub solar/battery fields."""
        return self._hub_schema(defaults, include_grid=False, include_battery=True)

    def _hub_inverter_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema with only inverter fields."""
        return self._hub_schema(
            defaults, include_grid=False, include_battery=False, include_inverter=True
        )

    def _charger_info_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema for charger info step (name, entity ID, priority, OCPP device ID)."""
        defaults = defaults or {}

        # Build dynamic fields based on what was detected
        fields = {
            vol.Required(
                CONF_NAME,
                default=defaults.get(CONF_NAME, ""),
            ): str,
            vol.Required(
                CONF_ENTITY_ID,
                default=defaults.get(CONF_ENTITY_ID, ""),
            ): str,
            vol.Required(
                CONF_CHARGER_PRIORITY,
                default=defaults.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
        }

        # Add OCPP Device ID as optional editable field (shown when detected)
        if defaults.get(CONF_OCPP_DEVICE_ID):
            fields[
                vol.Optional(
                    CONF_OCPP_DEVICE_ID,
                    default=defaults.get(CONF_OCPP_DEVICE_ID, ""),
                )
            ] = str

        return vol.Schema(fields)

    def _get_hub_phase_count(self, hub_entry_id: str | None = None) -> int:
        """Number of phases this site has, as the engine sees it.

        Thin wrapper around the module-level ``_hub_phase_count`` so the flow
        and the read-only pages share one derivation.
        """
        return _hub_phase_count(
            self.hass, hub_entry_id or self._data.get(CONF_HUB_ENTRY_ID)
        )

    def _charger_current_schema(
        self, defaults: dict | None = None, hub_phases: int = 3
    ) -> vol.Schema:
        """Build schema for charger current limits and phase mapping.

        Only shows L2/L3 phase mapping fields when the hub has 2+/3+ phases.
        """
        defaults = defaults or {}
        phase_options = [
            {"value": "A", "label": "Phase A"},
            {"value": "B", "label": "Phase B"},
            {"value": "C", "label": "Phase C"},
        ]
        fields = {
            vol.Required(
                CONF_EVSE_MINIMUM_CHARGE_CURRENT,
                default=defaults.get(
                    CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT
                ),
            ): selector(
                {
                    "number": {
                        "min": 6,
                        "max": 80,
                        "step": 1,
                        "mode": "box",
                        "unit_of_measurement": "A",
                    }
                }
            ),
            vol.Required(
                CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
                default=defaults.get(
                    CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT
                ),
            ): selector(
                {
                    "number": {
                        "min": 6,
                        "max": 80,
                        "step": 1,
                        "mode": "box",
                        "unit_of_measurement": "A",
                    }
                }
            ),
            vol.Required(
                CONF_CHARGER_L1_PHASE,
                default=defaults.get(CONF_CHARGER_L1_PHASE, "A"),
            ): selector({"select": {"options": phase_options, "mode": "dropdown"}}),
        }
        if hub_phases >= 2:
            fields[
                vol.Required(
                    CONF_CHARGER_L2_PHASE,
                    default=defaults.get(CONF_CHARGER_L2_PHASE, "B"),
                )
            ] = selector({"select": {"options": phase_options, "mode": "dropdown"}})
        if hub_phases >= 3:
            fields[
                vol.Required(
                    CONF_CHARGER_L3_PHASE,
                    default=defaults.get(CONF_CHARGER_L3_PHASE, "C"),
                )
            ] = selector({"select": {"options": phase_options, "mode": "dropdown"}})
        return vol.Schema(fields)

    def _charger_timing_schema(
        self, defaults: dict | None = None, detected_unit: str | None = None
    ) -> vol.Schema:
        """Build schema for charger timing and unit configuration."""
        defaults = defaults or {}
        unit_options = [
            {"value": CHARGE_RATE_UNIT_AMPS, "label": "Amperes (A)"},
            {"value": CHARGE_RATE_UNIT_WATTS, "label": "Watts (W)"},
        ]

        # Determine default for charge rate unit
        stored_unit = defaults.get(CONF_CHARGE_RATE_UNIT)
        if stored_unit in (CHARGE_RATE_UNIT_AMPS, CHARGE_RATE_UNIT_WATTS):
            unit_default = stored_unit
        elif detected_unit:
            unit_default = detected_unit
        else:
            unit_default = None

        if unit_default:
            charge_rate_field = vol.Required(
                CONF_CHARGE_RATE_UNIT, default=unit_default
            )
        else:
            charge_rate_field = vol.Required(CONF_CHARGE_RATE_UNIT)

        return vol.Schema(
            {
                charge_rate_field: selector(
                    {"select": {"options": unit_options, "mode": "dropdown"}}
                ),
                vol.Required(
                    CONF_PROFILE_VALIDITY_MODE,
                    default=defaults.get(
                        CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE
                    ),
                ): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "value": PROFILE_VALIDITY_MODE_RELATIVE,
                                    "label": "Relative (duration-based)",
                                },
                                {
                                    "value": PROFILE_VALIDITY_MODE_ABSOLUTE,
                                    "label": "Absolute (timestamp-based)",
                                },
                            ],
                            "mode": "dropdown",
                        }
                    }
                ),
                vol.Required(
                    CONF_UPDATE_FREQUENCY,
                    default=defaults.get(
                        CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 5,
                            "max": 300,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "s",
                        }
                    }
                ),
                vol.Required(
                    CONF_OCPP_PROFILE_TIMEOUT,
                    default=defaults.get(
                        CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 30,
                            "max": 600,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "s",
                        }
                    }
                ),
                vol.Required(
                    CONF_CHARGE_PAUSE_DURATION,
                    default=defaults.get(
                        CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 10,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "min",
                        }
                    }
                ),
                vol.Required(
                    CONF_STACK_LEVEL,
                    default=defaults.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL),
                ): selector(
                    {"number": {"min": 0, "max": 10, "step": 1, "mode": "box"}}
                ),
                vol.Required(
                    CONF_SOLAR_GRACE_PERIOD,
                    default=defaults.get(
                        CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 30,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "min",
                        }
                    }
                ),
            }
        )

    def _plug_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema for smart load configuration."""
        defaults = defaults or {}
        phase_options = [
            {"value": "A", "label": "Phase A"},
            {"value": "B", "label": "Phase B"},
            {"value": "C", "label": "Phase C"},
            {"value": "AB", "label": "Phase A+B"},
            {"value": "BC", "label": "Phase B+C"},
            {"value": "AC", "label": "Phase A+C"},
            {"value": "ABC", "label": "Phase A+B+C"},
        ]
        return vol.Schema(
            {
                vol.Required(
                    CONF_PLUG_SWITCH_ENTITY_ID,
                    default=defaults.get(CONF_PLUG_SWITCH_ENTITY_ID),
                ): selector({"entity": {"domain": "switch"}}),
                vol.Required(
                    CONF_PLUG_POWER_RATING,
                    default=defaults.get(
                        CONF_PLUG_POWER_RATING, DEFAULT_PLUG_POWER_RATING
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 100,
                            "max": 25000,
                            "step": 100,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
                vol.Required(
                    CONF_PLUG_MAX_CURRENT,
                    default=defaults.get(
                        CONF_PLUG_MAX_CURRENT, DEFAULT_PLUG_MAX_CURRENT
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 6,
                            "max": 63,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "A",
                        }
                    }
                ),
                vol.Required(
                    CONF_CONNECTED_TO_PHASE,
                    default=defaults.get(CONF_CONNECTED_TO_PHASE, "A"),
                ): selector({"select": {"options": phase_options, "mode": "dropdown"}}),
                vol.Required(
                    CONF_CHARGER_PRIORITY,
                    default=defaults.get(
                        CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY
                    ),
                ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
                self._optional_entity_field(
                    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
                    defaults.get(CONF_PLUG_POWER_MONITOR_ENTITY_ID),
                ): selector({"entity": {"domain": ["sensor", "input_number"]}}),
                vol.Required(
                    CONF_UPDATE_FREQUENCY,
                    default=defaults.get(
                        CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 5,
                            "max": 300,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "s",
                        }
                    }
                ),
                vol.Required(
                    CONF_SOLAR_GRACE_PERIOD,
                    default=defaults.get(
                        CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 30,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "min",
                        }
                    }
                ),
            }
        )

    def _hot_water_tank_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema for hot water tank configuration."""
        defaults = defaults or {}
        phase_options = [
            {"value": "A", "label": "Phase A"},
            {"value": "B", "label": "Phase B"},
            {"value": "C", "label": "Phase C"},
            {"value": "AB", "label": "Phase A+B"},
            {"value": "BC", "label": "Phase B+C"},
            {"value": "AC", "label": "Phase A+C"},
            {"value": "ABC", "label": "Phase A+B+C"},
        ]

        def _temp_selector():
            return selector(
                {
                    "number": {
                        "min": 10,
                        "max": 90,
                        "step": 1,
                        "mode": "box",
                        "unit_of_measurement": "°C",
                    }
                }
            )

        return vol.Schema(
            {
                vol.Required(
                    CONF_CLIMATE_ENTITY_ID,
                    default=defaults.get(CONF_CLIMATE_ENTITY_ID),
                ): selector({"entity": {"domain": "climate"}}),
                vol.Required(
                    CONF_HEATING_ELEMENT_POWER,
                    default=defaults.get(
                        CONF_HEATING_ELEMENT_POWER, DEFAULT_HEATING_ELEMENT_POWER
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 100,
                            "max": 25000,
                            "step": 100,
                            "mode": "box",
                            "unit_of_measurement": "W",
                        }
                    }
                ),
                vol.Required(
                    CONF_TANK_AWAY_TEMPERATURE,
                    default=defaults.get(
                        CONF_TANK_AWAY_TEMPERATURE, DEFAULT_TANK_AWAY_TEMPERATURE
                    ),
                ): _temp_selector(),
                vol.Required(
                    CONF_TANK_NORMAL_TEMPERATURE,
                    default=defaults.get(
                        CONF_TANK_NORMAL_TEMPERATURE, DEFAULT_TANK_NORMAL_TEMPERATURE
                    ),
                ): _temp_selector(),
                vol.Required(
                    CONF_TANK_BOOST_TEMPERATURE,
                    default=defaults.get(
                        CONF_TANK_BOOST_TEMPERATURE, DEFAULT_TANK_BOOST_TEMPERATURE
                    ),
                ): _temp_selector(),
                vol.Required(
                    CONF_TANK_PRIORITIZE_BELOW_NORMAL,
                    default=defaults.get(
                        CONF_TANK_PRIORITIZE_BELOW_NORMAL,
                        DEFAULT_TANK_PRIORITIZE_BELOW_NORMAL,
                    ),
                ): selector({"boolean": {}}),
                vol.Required(
                    CONF_CONNECTED_TO_PHASE,
                    default=defaults.get(CONF_CONNECTED_TO_PHASE, "A"),
                ): selector({"select": {"options": phase_options, "mode": "dropdown"}}),
                vol.Required(
                    CONF_CHARGER_PRIORITY,
                    default=defaults.get(
                        CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY
                    ),
                ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
                self._optional_entity_field(
                    CONF_TANK_POWER_ENTITY_ID,
                    defaults.get(CONF_TANK_POWER_ENTITY_ID),
                ): selector({"entity": {"domain": ["sensor", "input_number"]}}),
                vol.Optional(CONF_TANK_POWER_DEVICE_ID): selector(
                    {"device": {"entity": {"device_class": "power"}}}
                ),
                vol.Required(
                    CONF_UPDATE_FREQUENCY,
                    default=defaults.get(
                        CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 5,
                            "max": 300,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "s",
                        }
                    }
                ),
                vol.Required(
                    CONF_SOLAR_GRACE_PERIOD,
                    default=defaults.get(
                        CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 30,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "min",
                        }
                    }
                ),
            }
        )

    def _power_station_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema for portable power station configuration.

        The charge bounds are configured rather than read from the device, so a
        station whose hardware accepts more can be held below that. The reserve
        is the station's on/off gate — dropped below its current battery level it
        stops drawing from the wall — so both the day-to-day and the storm level
        are set here.
        """
        defaults = defaults or {}
        phase_options = [
            {"value": "A", "label": "Phase A"},
            {"value": "B", "label": "Phase B"},
            {"value": "C", "label": "Phase C"},
        ]

        def _power_selector():
            return selector(
                {
                    "number": {
                        "min": 0,
                        "max": 5000,
                        "step": STATION_CHARGE_POWER_STEP,
                        "mode": "box",
                        "unit_of_measurement": "W",
                    }
                }
            )

        def _percent_selector():
            return selector(
                {
                    "number": {
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "mode": "slider",
                        "unit_of_measurement": "%",
                    }
                }
            )

        return vol.Schema(
            {
                vol.Required(
                    CONF_STATION_CHARGE_SPEED_ENTITY_ID,
                    default=defaults.get(CONF_STATION_CHARGE_SPEED_ENTITY_ID),
                ): selector({"entity": {"domain": "number"}}),
                vol.Required(
                    CONF_STATION_RESERVE_ENTITY_ID,
                    default=defaults.get(CONF_STATION_RESERVE_ENTITY_ID),
                ): selector({"entity": {"domain": "number"}}),
                vol.Required(
                    CONF_STATION_BATTERY_LEVEL_ENTITY_ID,
                    default=defaults.get(CONF_STATION_BATTERY_LEVEL_ENTITY_ID),
                ): selector({"entity": {"domain": ["sensor", "input_number"]}}),
                self._optional_entity_field(
                    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
                    defaults.get(CONF_STATION_CHARGE_LIMIT_ENTITY_ID),
                ): selector({"entity": {"domain": ["number", "sensor"]}}),
                self._optional_entity_field(
                    CONF_STATION_AC_INPUT_ENTITY_ID,
                    defaults.get(CONF_STATION_AC_INPUT_ENTITY_ID),
                ): selector({"entity": {"domain": ["sensor", "input_number"]}}),
                self._optional_entity_field(
                    CONF_STATION_AC_OUTPUT_ENTITY_ID,
                    defaults.get(CONF_STATION_AC_OUTPUT_ENTITY_ID),
                ): selector({"entity": {"domain": ["sensor", "input_number"]}}),
                vol.Required(
                    CONF_STATION_MIN_CHARGE_POWER,
                    default=defaults.get(
                        CONF_STATION_MIN_CHARGE_POWER,
                        DEFAULT_STATION_MIN_CHARGE_POWER,
                    ),
                ): _power_selector(),
                vol.Required(
                    CONF_STATION_MAX_CHARGE_POWER,
                    default=defaults.get(
                        CONF_STATION_MAX_CHARGE_POWER,
                        DEFAULT_STATION_MAX_CHARGE_POWER,
                    ),
                ): _power_selector(),
                vol.Required(
                    CONF_STATION_NORMAL_RESERVE,
                    default=defaults.get(
                        CONF_STATION_NORMAL_RESERVE, DEFAULT_STATION_NORMAL_RESERVE
                    ),
                ): _percent_selector(),
                vol.Required(
                    CONF_STATION_STORM_RESERVE,
                    default=defaults.get(
                        CONF_STATION_STORM_RESERVE, DEFAULT_STATION_STORM_RESERVE
                    ),
                ): _percent_selector(),
                vol.Required(
                    CONF_CONNECTED_TO_PHASE,
                    default=defaults.get(CONF_CONNECTED_TO_PHASE, "A"),
                ): selector({"select": {"options": phase_options, "mode": "dropdown"}}),
                vol.Required(
                    CONF_CHARGER_PRIORITY,
                    default=defaults.get(
                        CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY
                    ),
                ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
                vol.Required(
                    CONF_UPDATE_FREQUENCY,
                    default=defaults.get(
                        CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 5,
                            "max": 300,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "s",
                        }
                    }
                ),
                vol.Required(
                    CONF_SOLAR_GRACE_PERIOD,
                    default=defaults.get(
                        CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 30,
                            "step": 1,
                            "mode": "box",
                            "unit_of_measurement": "min",
                        }
                    }
                ),
            }
        )

    # Optional entity keys grouped by config step (for entity selector clearing)
    _GRID_ENTITY_KEYS = [
        CONF_PHASE_A_CURRENT_ENTITY_ID,
        CONF_PHASE_B_CURRENT_ENTITY_ID,
        CONF_PHASE_C_CURRENT_ENTITY_ID,
        CONF_MAX_IMPORT_POWER_ENTITY_ID,
    ]
    _BATTERY_ENTITY_KEYS = [
        CONF_SOLAR_PRODUCTION_ENTITY_ID,
        CONF_BATTERY_SOC_ENTITY_ID,
        CONF_BATTERY_POWER_ENTITY_ID,
    ]
    _INVERTER_ENTITY_KEYS = [
        CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    ]
    _PLUG_ENTITY_KEYS = [CONF_PLUG_POWER_MONITOR_ENTITY_ID]
    _TANK_ENTITY_KEYS = [CONF_TANK_POWER_ENTITY_ID, CONF_TANK_POWER_DEVICE_ID]
    _STATION_ENTITY_KEYS = [
        CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
        CONF_STATION_AC_INPUT_ENTITY_ID,
        CONF_STATION_AC_OUTPUT_ENTITY_ID,
    ]

    def _normalize_optional_inputs(
        self, data: dict[str, Any], step_entity_keys: list[str] | None = None
    ) -> dict[str, Any]:
        """Normalize optional entity inputs.

        Args:
            data: The user_input from the form step.
            step_entity_keys: Optional entity keys expected in this step.
                Keys missing from data are set to None (user cleared the field).
        """
        normalized = dict(data)
        for key in (
            self._GRID_ENTITY_KEYS
            + self._BATTERY_ENTITY_KEYS
            + self._INVERTER_ENTITY_KEYS
            + self._PLUG_ENTITY_KEYS
            + self._TANK_ENTITY_KEYS
        ):
            if key in normalized:
                normalized[key] = normalize_optional_entity(normalized.get(key))
        # Entity selectors omit unselected fields — explicitly clear them
        if step_entity_keys:
            for key in step_entity_keys:
                if key not in normalized:
                    normalized[key] = None
        return normalized

    def _normalize_forecast_list(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalize the solar forecast device list (battery step).

        Separate from _normalize_optional_inputs, which is per-key scalar: the
        multi-device selector yields a list and omits the key entirely when
        cleared, so an emptied selection must become [] (feature off), not a
        stale stored value. Submitting the form also drops any legacy
        directly-configured sensor list — the device selection replaces it.
        """
        data[CONF_SOLAR_FORECAST_DEVICE_IDS] = [
            d for d in (data.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or []) if d
        ]
        data[CONF_SOLAR_FORECAST_ENTITY_IDS] = []
        return data

    def _normalize_soc_limit_list(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalize the SOC-ceiling target list (inverter write-control step).

        Same reason as the forecast list above and not the scalar path: a
        multi-entity selector yields a list and omits the key entirely once the
        user clears it, so an emptied selection must become [] — which is what
        removes the Battery SOC Control switch and sensor again — rather than
        leaving the previously stored slots armed.
        """
        data[CONF_SOC_LIMIT_ENTITY_IDS] = [
            e for e in (data.get(CONF_SOC_LIMIT_ENTITY_IDS) or []) if e
        ]
        return data

    def _normalize_inverter_powers(self):
        """Normalize inverter power values: 0 means 'not configured' → store as None."""
        for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
            if key in self._data and self._data[key] == 0:
                self._data[key] = None

    def _auto_detect_phase_entities(
        self, pattern_sets: list[dict]
    ) -> dict[str, str | None]:
        """Auto-detect a matching set of phase A/B/C entities from pattern sets.

        Returns dict with keys 'phase_a', 'phase_b', 'phase_c' (values may be None).
        """
        entity_ids = self._get_entity_registry_ids()
        for pattern_set in pattern_sets:
            a = next(
                (
                    eid
                    for eid in entity_ids
                    if re.match(pattern_set["patterns"]["phase_a"], eid)
                ),
                None,
            )
            b = next(
                (
                    eid
                    for eid in entity_ids
                    if re.match(pattern_set["patterns"]["phase_b"], eid)
                ),
                None,
            )
            c = next(
                (
                    eid
                    for eid in entity_ids
                    if re.match(pattern_set["patterns"]["phase_c"], eid)
                ),
                None,
            )
            if a and b and c:
                return {"phase_a": a, "phase_b": b, "phase_c": c}
        return {"phase_a": None, "phase_b": None, "phase_c": None}

    def _auto_detect_entity(self, pattern_sets: list[dict]) -> str | None:
        """Auto-detect a single entity from pattern sets. Returns first match."""
        entity_ids = self._get_entity_registry_ids()
        for pattern_set in pattern_sets:
            match = next(
                (eid for eid in entity_ids if re.match(pattern_set["pattern"], eid)),
                None,
            )
            if match:
                return match
        return None

    def _auto_detect_entity_value(
        self, pattern_sets: list[dict], factor: float = 1.0
    ) -> int | None:
        """Auto-detect an entity and read its numeric state value.

        Returns int(state * factor), or None if not found / not numeric.
        """
        entity_id = self._auto_detect_entity(pattern_sets)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if not state:
            return None
        try:
            return int(float(state.state) * factor)
        except (ValueError, TypeError):
            return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Initial step: choose hub, EVSE charger, or smart outlet."""
        errors: dict[str, str] = {}

        # Check if any hubs exist
        hubs = self._get_hub_entries()

        if user_input is not None:
            setup_type = user_input.get("setup_type")
            if setup_type == "hub":
                return await self.async_step_hub_info()
            elif setup_type in ("evse", "plug", "tank", "station"):
                if not hubs:
                    errors["base"] = "no_hub_configured"
                else:
                    self._data[CONF_DEVICE_TYPE] = {
                        "evse": DEVICE_TYPE_EVSE,
                        "plug": DEVICE_TYPE_PLUG,
                        "tank": DEVICE_TYPE_HOT_WATER_TANK,
                        "station": DEVICE_TYPE_POWER_STATION,
                    }[setup_type]
                    return await self.async_step_select_hub()
            elif setup_type == "group":
                if not hubs:
                    errors["base"] = "no_hub_configured"
                else:
                    self._data[CONF_DEVICE_TYPE] = DEVICE_TYPE_GROUP
                    return await self.async_step_select_hub()
            elif setup_type == "inverter":
                if not hubs:
                    errors["base"] = "no_hub_configured"
                else:
                    self._data[CONF_DEVICE_TYPE] = DEVICE_TYPE_INVERTER
                    return await self.async_step_select_hub()

        # Build options based on existing hubs
        options = [
            {"value": "hub", "label": "Configure Home Electrical System (Hub)"},
        ]
        if hubs:
            options.append({"value": "evse", "label": "Add OCPP Charger (EVSE)"})
            options.append({"value": "plug", "label": "Add Smart Outlet / Relay"})
            options.append({"value": "tank", "label": "Add Hot Water Tank"})
            options.append(
                {"value": "station", "label": "Add Portable Power Station"}
            )
            options.append(
                {"value": "inverter", "label": "Add Inverter / Home Battery"}
            )
            options.append(
                {"value": "group", "label": "Add Circuit Group (Shared Breaker)"}
            )

        data_schema = vol.Schema(
            {
                vol.Required(
                    "setup_type", default="hub" if not hubs else "evse"
                ): selector({"select": {"options": options, "mode": "list"}})
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors, last_step=False
        )

    def _get_hub_entries(self) -> list:
        """Get all hub config entries."""
        return [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_HUB
        ]

    def _get_charger_entries(self) -> list:
        """Get all charger config entries."""
        return [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
        ]

    # ==================== HUB CONFIGURATION STEPS ====================

    def _entity_id_in_use(self, entity_id: str) -> bool:
        """True if entity_id is already used by another Load Juggler config entry."""
        return any(
            entry.data.get(CONF_ENTITY_ID) == entity_id
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )

    def _resolve_device_power_entity(self, device_id: str) -> str | None:
        """Return the first power-class sensor entity belonging to a device."""
        entity_registry = async_get_entity_registry(self.hass)
        for entity in entity_registry.entities.values():
            if entity.device_id != device_id:
                continue
            device_class = entity.device_class or entity.original_device_class
            if device_class == "power":
                return entity.entity_id
        return None

    async def async_step_hub_info(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 1: Basic info (name and entity_id)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            entity_id = self._data.get(CONF_ENTITY_ID)
            if entity_id and self._entity_id_in_use(entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            else:
                self._data[ENTRY_TYPE] = ENTRY_TYPE_HUB
                return await self.async_step_hub_grid()

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=self._data.get(CONF_NAME, "Site Load Management"),
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=self._data.get(CONF_ENTITY_ID, "lj_site_load_management"),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="hub_info", data_schema=data_schema, errors=errors, last_step=False
        )

    async def async_step_hub_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 2: grid, electrical and site policy — creates the entry.

        A NEW hub carries no hardware of its own: inverters, batteries, PV
        production sensors and PV forecast sources all live on Inverter
        entries added afterwards ("Add Inverter / Home Battery"). What stays
        here is the grid connection plus the site-wide policy applied to
        whatever fleet those entries form.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._GRID_ENTITY_KEYS
            )
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_PHASE_A_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
                    CONF_PHASE_B_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
                    CONF_PHASE_C_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
                    CONF_MAX_IMPORT_POWER_ENTITY_ID: _POWER_UNITS,
                },
                errors,
            )
            if not errors:
                self._data.update(user_input)

                # Generate entity IDs for hub-created entities
                entity_id = self._data.get(CONF_ENTITY_ID)
                self._data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = (
                    f"number.{entity_id}_home_battery_soc_target"
                )
                self._data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = (
                    f"switch.{entity_id}_allow_grid_charging"
                )
                self._data[CONF_POWER_BUFFER_ENTITY_ID] = (
                    f"number.{entity_id}_power_buffer"
                )

                # Split static vs mutable fields:
                static_data = {
                    CONF_NAME: self._data.get(CONF_NAME),
                    CONF_ENTITY_ID: self._data.get(CONF_ENTITY_ID),
                    ENTRY_TYPE: ENTRY_TYPE_HUB,
                    # Born without legacy inverter/battery fields — nothing to
                    # auto-import, and the legacy hub pages stay hidden.
                    MIGRATE_HUB_INVERTER_IMPORTED_FLAG: True,
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }

                return self.async_create_entry(
                    title=static_data[CONF_NAME],
                    data=static_data,
                    options=options_data,
                )
            return self.async_show_form(
                step_id="hub_grid",
                data_schema=self._hub_grid_schema(user_input),
                errors=errors,
                last_step=True,
            )

        try:
            # Try to find a complete set of phases using pattern sets
            ct_detected = self._auto_detect_phase_entities(PHASE_PATTERNS)
            default_phase_a = ct_detected["phase_a"]
            default_phase_b = ct_detected["phase_b"]
            default_phase_c = ct_detected["phase_c"]

            # Fallback: pick individual phases from different pattern sets
            if not (default_phase_a and default_phase_b and default_phase_c):
                entity_ids = self._get_entity_registry_ids()
                for pattern_set in PHASE_PATTERNS:
                    if not default_phase_a:
                        default_phase_a = next(
                            (
                                eid
                                for eid in entity_ids
                                if re.match(pattern_set["patterns"]["phase_a"], eid)
                            ),
                            None,
                        )
                    if not default_phase_b:
                        default_phase_b = next(
                            (
                                eid
                                for eid in entity_ids
                                if re.match(pattern_set["patterns"]["phase_b"], eid)
                            ),
                            None,
                        )
                    if not default_phase_c:
                        default_phase_c = next(
                            (
                                eid
                                for eid in entity_ids
                                if re.match(pattern_set["patterns"]["phase_c"], eid)
                            ),
                            None,
                        )

            data_schema = self._hub_grid_schema(
                {
                    CONF_PHASE_A_CURRENT_ENTITY_ID: default_phase_a,
                    CONF_PHASE_B_CURRENT_ENTITY_ID: default_phase_b,
                    CONF_PHASE_C_CURRENT_ENTITY_ID: default_phase_c,
                    CONF_MAIN_BREAKER_RATING: DEFAULT_MAIN_BREAKER_RATING,
                    CONF_INVERT_PHASES: False,
                    CONF_PHASE_VOLTAGE: DEFAULT_PHASE_VOLTAGE,
                    CONF_GRID_EXPORT_LIMIT: DEFAULT_GRID_EXPORT_LIMIT,
                    CONF_EXCESS_TRIGGER_MARGIN: DEFAULT_EXCESS_TRIGGER_MARGIN,
                    CONF_EXCESS_HYSTERESIS: DEFAULT_EXCESS_HYSTERESIS,
                    CONF_AUTO_DETECT_PHASE_MAPPING: True,
                    CONF_BATTERY_SOC_HYSTERESIS: DEFAULT_BATTERY_SOC_HYSTERESIS,
                }
            )

        except Exception as e:
            _LOGGER.error("Error in async_step_hub_grid: %s", e, exc_info=True)
            errors["base"] = "unknown"
            data_schema = vol.Schema({})

        return self.async_show_form(
            step_id="hub_grid", data_schema=data_schema, errors=errors, last_step=True
        )

    # ==================== CHARGER CONFIGURATION STEPS ====================

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Handle integration discovery of OCPP chargers."""
        # Store discovery info. The payload is a scan_ocpp_chargers() dict (see
        # __init__._discover_and_notify_chargers), so every entity key the
        # manual flow stores is carried through to the created entry — a
        # discovered charger must not end up with None for its per-phase
        # current/power entities just because it came in through discovery.
        self._data[CONF_HUB_ENTRY_ID] = discovery_info["hub_entry_id"]
        self._selected_charger = {
            "id": discovery_info["charger_id"],
            "name": discovery_info["charger_name"],
            "device_id": discovery_info.get("device_id"),
            "current_import_entity": discovery_info["current_import_entity"],
            "current_import_l1_entity": discovery_info.get("current_import_l1_entity"),
            "current_import_l2_entity": discovery_info.get("current_import_l2_entity"),
            "current_import_l3_entity": discovery_info.get("current_import_l3_entity"),
            "current_offered_entity": discovery_info.get("current_offered_entity"),
            "power_offered_entity": discovery_info.get("power_offered_entity"),
            "power_import_entity": discovery_info.get("power_import_entity"),
        }

        # Set unique ID to prevent duplicate discoveries
        await self.async_set_unique_id(
            f"{DOMAIN}_charger_{discovery_info['charger_id']}"
        )
        self._abort_if_unique_id_configured()

        # Show confirmation form
        self.context["title_placeholders"] = {"name": self._selected_charger["name"]}
        return await self.async_step_charger_info()

    async def _route_after_hub_selection(self) -> config_entries.FlowResult:
        """Route to the correct step after hub is selected."""
        device_type = self._data.get(CONF_DEVICE_TYPE)
        if device_type == DEVICE_TYPE_PLUG:
            return await self.async_step_plug_config()
        if device_type == DEVICE_TYPE_HOT_WATER_TANK:
            return await self.async_step_hot_water_tank_config()
        if device_type == DEVICE_TYPE_POWER_STATION:
            return await self.async_step_power_station_config()
        if device_type == DEVICE_TYPE_GROUP:
            return await self.async_step_group_config()
        if device_type == DEVICE_TYPE_INVERTER:
            return await self.async_step_inverter_config()
        return await self.async_step_discover_chargers()

    async def async_step_select_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 1: Select which hub to add charger to."""
        errors: dict[str, str] = {}
        hubs = self._get_hub_entries()

        if user_input is not None:
            self._data[CONF_HUB_ENTRY_ID] = user_input["hub_entry_id"]
            return await self._route_after_hub_selection()

        # If only one hub, skip selection
        if len(hubs) == 1:
            self._data[CONF_HUB_ENTRY_ID] = hubs[0].entry_id
            return await self._route_after_hub_selection()

        hub_options = [
            {"value": entry.entry_id, "label": entry.title} for entry in hubs
        ]

        data_schema = vol.Schema(
            {
                vol.Required("hub_entry_id"): selector(
                    {"select": {"options": hub_options}}
                )
            }
        )

        return self.async_show_form(
            step_id="select_hub",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_plug_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Smart load configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._PLUG_ENTITY_KEYS
            )
            self._data.update(user_input)

            plug_name = self._data.get(CONF_NAME, "Smart Load")
            plug_entity_id = self._data.get(CONF_ENTITY_ID, "lj_smart_load")

            if self._entity_id_in_use(plug_entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            else:
                static_data = {
                    CONF_ENTITY_ID: plug_entity_id,
                    CONF_NAME: plug_name,
                    ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_PLUG,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                    CONF_PLUG_SWITCH_ENTITY_ID: self._data.get(
                        CONF_PLUG_SWITCH_ENTITY_ID
                    ),
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }

                return self.async_create_entry(
                    title=_compose_entry_title(plug_name, "Smart Load"),
                    data=static_data,
                    options=options_data,
                )

        existing_chargers = self._get_charger_entries()
        next_priority = len(existing_chargers) + 1

        # Name + entity_id fields, then the plug-specific schema. self._data is
        # merged last so a validation-error re-show keeps the user's input.
        plug_defaults = {
            CONF_CHARGER_PRIORITY: next_priority,
            CONF_PLUG_POWER_RATING: DEFAULT_PLUG_POWER_RATING,
            CONF_PLUG_MAX_CURRENT: DEFAULT_PLUG_MAX_CURRENT,
            CONF_CONNECTED_TO_PHASE: "A",
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_PLUG_POWER_MONITOR_ENTITY_ID: self._auto_detect_entity(
                PLUG_POWER_MONITOR_PATTERNS
            ),
        }
        plug_defaults.update(self._data)
        name_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=plug_defaults.get(CONF_NAME, "Smart Load")
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=plug_defaults.get(CONF_ENTITY_ID, "lj_smart_load"),
                ): str,
            }
        )
        plug_fields = self._plug_schema(plug_defaults)
        # Merge both schemas
        combined = vol.Schema({**name_schema.schema, **plug_fields.schema})

        return self.async_show_form(
            step_id="plug_config",
            data_schema=combined,
            errors=errors,
            last_step=True,
        )

    async def async_step_hot_water_tank_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hot water tank configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._TANK_ENTITY_KEYS
            )
            self._data.update(user_input)

            # A picked power device is resolved to its power-sensor entity now,
            # so runtime only ever deals with CONF_TANK_POWER_ENTITY_ID.
            device_id = self._data.pop(CONF_TANK_POWER_DEVICE_ID, None)
            if device_id and not self._data.get(CONF_TANK_POWER_ENTITY_ID):
                resolved = self._resolve_device_power_entity(device_id)
                if resolved:
                    self._data[CONF_TANK_POWER_ENTITY_ID] = resolved

            tank_name = self._data.get(CONF_NAME, "Hot Water Tank")
            tank_entity_id = self._data.get(CONF_ENTITY_ID, "lj_hot_water_tank")

            if self._entity_id_in_use(tank_entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            else:
                static_data = {
                    CONF_ENTITY_ID: tank_entity_id,
                    CONF_NAME: tank_name,
                    ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_HOT_WATER_TANK,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                    CONF_CLIMATE_ENTITY_ID: self._data.get(CONF_CLIMATE_ENTITY_ID),
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }
                return self.async_create_entry(
                    title=_compose_entry_title(tank_name, "Hot Water Tank"),
                    data=static_data,
                    options=options_data,
                )

        # Defaults; self._data is merged last so a validation-error re-show
        # keeps the user's input.
        tank_defaults = {
            CONF_CHARGER_PRIORITY: len(self._get_charger_entries()) + 1,
            CONF_HEATING_ELEMENT_POWER: DEFAULT_HEATING_ELEMENT_POWER,
            CONF_TANK_AWAY_TEMPERATURE: DEFAULT_TANK_AWAY_TEMPERATURE,
            CONF_TANK_NORMAL_TEMPERATURE: DEFAULT_TANK_NORMAL_TEMPERATURE,
            CONF_TANK_BOOST_TEMPERATURE: DEFAULT_TANK_BOOST_TEMPERATURE,
            CONF_CONNECTED_TO_PHASE: "A",
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
        }
        tank_defaults.update(self._data)
        name_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=tank_defaults.get(CONF_NAME, "Hot Water Tank"),
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=tank_defaults.get(CONF_ENTITY_ID, "lj_hot_water_tank"),
                ): str,
            }
        )
        tank_fields = self._hot_water_tank_schema(tank_defaults)
        combined = vol.Schema({**name_schema.schema, **tank_fields.schema})

        return self.async_show_form(
            step_id="hot_water_tank_config",
            data_schema=combined,
            errors=errors,
            last_step=True,
        )

    async def async_step_power_station_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Portable power station configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._STATION_ENTITY_KEYS
            )
            self._data.update(user_input)

            station_name = self._data.get(CONF_NAME, "Power Station")
            station_entity_id = self._data.get(CONF_ENTITY_ID, "lj_power_station")

            min_power = self._data.get(
                CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER
            )
            max_power = self._data.get(
                CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
            )
            if self._entity_id_in_use(station_entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            elif max_power < min_power:
                errors[CONF_STATION_MAX_CHARGE_POWER] = "station_max_below_min"
            else:
                static_data = {
                    CONF_ENTITY_ID: station_entity_id,
                    CONF_NAME: station_name,
                    ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_POWER_STATION,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                    CONF_STATION_CHARGE_SPEED_ENTITY_ID: self._data.get(
                        CONF_STATION_CHARGE_SPEED_ENTITY_ID
                    ),
                    CONF_STATION_RESERVE_ENTITY_ID: self._data.get(
                        CONF_STATION_RESERVE_ENTITY_ID
                    ),
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }
                return self.async_create_entry(
                    title=_compose_entry_title(station_name, "Power Station"),
                    data=static_data,
                    options=options_data,
                )

        # Defaults; self._data is merged last so a validation-error re-show
        # keeps the user's input.
        station_defaults = {
            CONF_CHARGER_PRIORITY: len(self._get_charger_entries()) + 1,
            CONF_STATION_MIN_CHARGE_POWER: DEFAULT_STATION_MIN_CHARGE_POWER,
            CONF_STATION_MAX_CHARGE_POWER: DEFAULT_STATION_MAX_CHARGE_POWER,
            CONF_STATION_NORMAL_RESERVE: DEFAULT_STATION_NORMAL_RESERVE,
            CONF_STATION_STORM_RESERVE: DEFAULT_STATION_STORM_RESERVE,
            CONF_CONNECTED_TO_PHASE: "A",
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
        }
        station_defaults.update(self._data)
        name_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=station_defaults.get(CONF_NAME, "Power Station"),
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=station_defaults.get(CONF_ENTITY_ID, "lj_power_station"),
                ): str,
            }
        )
        station_fields = self._power_station_schema(station_defaults)
        combined = vol.Schema({**name_schema.schema, **station_fields.schema})

        return self.async_show_form(
            step_id="power_station_config",
            data_schema=combined,
            errors=errors,
            last_step=True,
        )

    # ==================== CIRCUIT GROUP CONFIGURATION STEPS ====================

    async def async_step_group_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Circuit group step 1: Name and current limit."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            entity_id = self._data.get(CONF_ENTITY_ID)
            if entity_id and self._entity_id_in_use(entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            else:
                return await self.async_step_group_members()

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=self._data.get(CONF_NAME, "Circuit Group")
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=self._data.get(CONF_ENTITY_ID, "lj_circuit_group"),
                ): str,
                vol.Required(
                    CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
                    default=self._data.get(
                        CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
                        DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 1,
                            "max": 100,
                            "step": 1,
                            "unit_of_measurement": "A",
                            "mode": "box",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="group_config",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_group_members(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Circuit group step 2: Select member loads."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(CONF_CIRCUIT_GROUP_MEMBERS, [])
            if not selected:
                errors["base"] = "no_members_selected"
            else:
                self._data[CONF_CIRCUIT_GROUP_MEMBERS] = selected

                group_name = self._data.get(CONF_NAME, "Circuit Group")
                group_entity_id = self._data.get(CONF_ENTITY_ID, "lj_circuit_group")

                static_data = {
                    CONF_ENTITY_ID: group_entity_id,
                    CONF_NAME: group_name,
                    ENTRY_TYPE: ENTRY_TYPE_GROUP,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_GROUP,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                }
                options_data = {
                    CONF_CIRCUIT_GROUP_CURRENT_LIMIT: self._data.get(
                        CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
                        DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
                    ),
                    CONF_CIRCUIT_GROUP_MEMBERS: selected,
                }

                return self.async_create_entry(
                    title=group_name,
                    data=static_data,
                    options=options_data,
                )

        # Build list of loads on this hub for multi-select
        hub_entry_id = self._data.get(CONF_HUB_ENTRY_ID)
        load_options = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if (
                entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
                and entry.data.get(CONF_HUB_ENTRY_ID) == hub_entry_id
            ):
                load_options.append(
                    {
                        "value": entry.entry_id,
                        "label": entry.title,
                    }
                )

        if not load_options:
            errors["base"] = "no_loads_available"

        data_schema = vol.Schema(
            {
                vol.Required(CONF_CIRCUIT_GROUP_MEMBERS): selector(
                    {
                        "select": {
                            "options": load_options,
                            "multiple": True,
                            "mode": "list",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="group_members",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    # ==================== INVERTER CONFIGURATION STEPS ====================

    async def async_step_inverter_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Inverter step 1: name, capacity, topology, output and PV sensors."""
        errors: dict[str, str] = {}
        bad_forecast_entity = None

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input,
                self._INVERTER_ENTITY_KEYS + [CONF_SOLAR_PRODUCTION_ENTITY_ID],
            )
            user_input = self._normalize_forecast_list(user_input)
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_SOLAR_PRODUCTION_ENTITY_ID: _POWER_UNITS,
                },
                errors,
            )
            bad_forecast_entity = _validate_forecast_devices(
                self.hass, user_input, errors
            )
            entity_id = user_input.get(CONF_ENTITY_ID)
            if entity_id and self._entity_id_in_use(entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_inverter_battery()

        defaults = {**self._data, **(user_input or {})}
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=defaults.get(CONF_NAME, "Inverter")
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=defaults.get(CONF_ENTITY_ID, "lj_inverter"),
                ): str,
                **dict(self._build_hub_inverter_schema(defaults)),
                **dict(self._build_inverter_solar_schema(defaults)),
            }
        )

        return self.async_show_form(
            step_id="inverter_config",
            data_schema=data_schema,
            errors=errors,
            description_placeholders=(
                {"entity": bad_forecast_entity} if bad_forecast_entity else None
            ),
            last_step=False,
        )

    async def async_step_inverter_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Inverter step 2: the battery behind this inverter (all optional) —
        creates the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input,
                [CONF_BATTERY_SOC_ENTITY_ID, CONF_BATTERY_POWER_ENTITY_ID],
            )
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_BATTERY_POWER_ENTITY_ID: _POWER_UNITS,
                    CONF_BATTERY_SOC_ENTITY_ID: _SOC_UNITS,
                },
                errors,
            )
            if not errors:
                self._data.update(user_input)
                return await self.async_step_inverter_control()

        data_schema = self._inverter_battery_schema({**self._data, **(user_input or {})})
        return self.async_show_form(
            step_id="inverter_battery",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_inverter_control(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Inverter step 3: optional battery write-control — creates the entry.

        Skip it (submit empty) to keep the inverter advisory: the clipping
        forecast still publishes its recommended charge limit and max SOC as
        sensors, Load Juggler just doesn't write them anywhere.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input,
                [
                    CONF_CHARGE_LIMIT_ENTITY_ID,
                    CONF_BATTERY_VOLTAGE_ENTITY_ID,
                    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
                ],
            )
            user_input = self._normalize_soc_limit_list(user_input)
            if not errors:
                self._data.update(user_input)
                # 0 means "not configured" for the power caps, as on the hub
                for key in (CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE):
                    if self._data.get(key) == 0:
                        self._data[key] = None

                name = self._data.get(CONF_NAME, "Inverter")
                static_data = {
                    CONF_NAME: name,
                    CONF_ENTITY_ID: self._data.get(CONF_ENTITY_ID),
                    ENTRY_TYPE: ENTRY_TYPE_INVERTER,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_INVERTER,
                    CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                }
                options_data = {
                    k: v for k, v in self._data.items() if k not in static_data
                }
                options_data.pop(CONF_DEVICE_TYPE, None)

                # The hub's entity gating (battery sliders/switch, forecast
                # sensors) depends on which inverter entries exist — nothing
                # reloads a hub when a child appears, so schedule it here.
                # Only when the hub is actually loaded: reloading a not-yet-
                # loaded entry raises, and during startup it reloads anyway.
                hub_entry_id = self._data.get(CONF_HUB_ENTRY_ID)
                hub_entry = (
                    self.hass.config_entries.async_get_entry(hub_entry_id)
                    if hub_entry_id
                    else None
                )
                if (
                    hub_entry is not None
                    and hub_entry.state is config_entries.ConfigEntryState.LOADED
                ):
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(hub_entry_id)
                    )

                return self.async_create_entry(
                    title=_compose_entry_title(name, "Inverter"),
                    data=static_data,
                    options=options_data,
                )

        data_schema = self._inverter_control_schema({**self._data, **(user_input or {})})
        return self.async_show_form(
            step_id="inverter_control",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    # The legacy hub-level fields the auto-import moves onto an inverter entry:
    # everything physically attached to the inverter, its PV array included.
    # Site policy stays behind on the hub: SOC hysteresis, the SOC target/min
    # sliders, base consumption, forecast SOC floor and the grid export limit.
    _HUB_INVERTER_IMPORT_FIELDS = (
        CONF_SOLAR_PRODUCTION_ENTITY_ID,
        CONF_SOLAR_FORECAST_DEVICE_IDS,
        CONF_SOLAR_FORECAST_ENTITY_IDS,
        CONF_INVERTER_MAX_POWER,
        CONF_INVERTER_MAX_POWER_PER_PHASE,
        CONF_INVERTER_SUPPORTS_ASYMMETRIC,
        CONF_WIRING_TOPOLOGY,
        CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
        CONF_BATTERY_SOC_ENTITY_ID,
        CONF_BATTERY_POWER_ENTITY_ID,
        CONF_BATTERY_MAX_CHARGE_POWER,
        CONF_BATTERY_MAX_DISCHARGE_POWER,
        CONF_BATTERY_SOC_FULL,
        CONF_BATTERY_CAPACITY_KWH,
    )

    def _blank_hub_legacy_inverter_fields(self, hub_entry) -> None:
        """Strip the imported fields from the hub entry and set the imported
        flag — the hub must stop acting as the implicit legacy fleet member
        the moment the standalone inverter entry represents the hardware."""
        new_data = dict(hub_entry.data)
        new_data[MIGRATE_HUB_INVERTER_IMPORTED_FLAG] = True
        for key in self._HUB_INVERTER_IMPORT_FIELDS:
            new_data.pop(key, None)
        new_options = {
            k: v
            for k, v in hub_entry.options.items()
            if k not in self._HUB_INVERTER_IMPORT_FIELDS
        }
        self.hass.config_entries.async_update_entry(
            hub_entry, data=new_data, options=new_options
        )

    def _imported_inverter_name(self, imported: dict) -> str:
        """Name the auto-created entry after the hardware it represents.

        The hub's own name makes a poor inverter name ("Site Load Management
        Inverter"), so look at the device behind the first hardware entity
        being imported — usually the inverter's integration device, which is
        already called something like "Deye Hybrid" or "SolarEdge SE17K".
        Falls back to plain "Inverter", the same default the manual flow uses.
        """
        entity_registry = async_get_entity_registry(self.hass)
        device_registry = async_get_device_registry(self.hass)
        for key in (
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
            CONF_BATTERY_SOC_ENTITY_ID,
            CONF_BATTERY_POWER_ENTITY_ID,
            CONF_SOLAR_PRODUCTION_ENTITY_ID,
        ):
            entity_id = imported.get(key)
            if not entity_id:
                continue
            registry_entry = entity_registry.async_get(entity_id)
            if registry_entry is None or not registry_entry.device_id:
                continue
            device = device_registry.async_get(registry_entry.device_id)
            if device is None:
                continue
            device_name = device.name_by_user or device.name
            if device_name:
                return prettify_name(device_name)
        return "Inverter"

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Auto-import of a hub's legacy inverter/battery/PV fields onto a
        standalone inverter entry (spawned from _setup_hub_entry).

        Runs whenever such a field is still on the hub, so a later release
        that moves one more field (the solar sensor and forecast devices, say)
        converges on the next restart without a second migration path. When
        the hub's inverter entry already exists, the newly-found fields are
        merged into it rather than creating a second one.

        Idempotency: the unique_id makes a duplicate entry impossible, and the
        hub is blanked-and-flagged BEFORE the already-configured abort — so a
        restart between entry creation and blanking still converges instead of
        double-counting the battery (the engine's implicit legacy member and
        the entry would otherwise both exist).
        """
        hub_entry_id = import_data.get("hub_entry_id")
        hub_entry = (
            self.hass.config_entries.async_get_entry(hub_entry_id)
            if hub_entry_id
            else None
        )
        if hub_entry is None:
            return self.async_abort(reason="entry_not_found")

        unique_id = f"{hub_entry_id}_inverter_import"
        await self.async_set_unique_id(unique_id)

        # Snapshot the legacy fields, then blank the hub — in this order, and
        # before the duplicate check, so every path leaves the hub clean.
        imported = {
            key: get_entry_value(hub_entry, key, None)
            for key in self._HUB_INVERTER_IMPORT_FIELDS
        }
        imported = {k: v for k, v in imported.items() if v is not None}
        self._blank_hub_legacy_inverter_fields(hub_entry)

        # An entry from an earlier import round already represents this
        # hardware — merge the fields this round found onto it. Existing
        # values win: the user may have edited them on the inverter since.
        existing = next(
            (
                entry
                for entry in self.hass.config_entries.async_entries(DOMAIN)
                if entry.unique_id == unique_id
            ),
            None,
        )
        if existing is not None:
            merged = {**imported, **existing.options}
            if merged != dict(existing.options):
                _LOGGER.info(
                    "Moved leftover hub fields (%s) onto the existing inverter "
                    "entry %s",
                    ", ".join(sorted(imported)) or "none",
                    existing.title,
                )
                self.hass.config_entries.async_update_entry(existing, options=merged)
            return self.async_abort(reason="already_configured")

        hub_name = hub_entry.data.get(CONF_NAME, hub_entry.title)
        hub_prefix = hub_entry.data.get(CONF_ENTITY_ID, "lj_hub")
        name = self._imported_inverter_name(imported)
        static_data = {
            CONF_NAME: name,
            CONF_ENTITY_ID: f"{hub_prefix}_inverter",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_DEVICE_TYPE: DEVICE_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub_entry_id,
            "imported_from_hub": True,
        }
        _LOGGER.info(
            "Imported legacy hub inverter/battery config of %s into a new "
            "inverter entry '%s' (%s)",
            hub_name,
            name,
            ", ".join(sorted(imported)) or "no fields",
        )
        return self.async_create_entry(
            title=_compose_entry_title(name, "Inverter"),
            data=static_data,
            options=imported,
        )

    # ==================== EVSE CHARGER CONFIGURATION STEPS ====================

    async def async_step_discover_chargers(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 2: Discover OCPP chargers."""
        errors: dict[str, str] = {}

        # Find OCPP devices
        self._discovered_chargers = await self._discover_ocpp_chargers()

        if not self._discovered_chargers:
            errors["base"] = "no_ocpp_chargers_found"
            return self.async_show_form(
                step_id="discover_chargers",
                data_schema=vol.Schema({}),
                errors=errors,
                last_step=True,
            )

        if user_input is not None:
            selected_charger_id = user_input.get("charger")
            self._selected_charger = next(
                (c for c in self._discovered_chargers
                 if c["id"] == selected_charger_id),
                None,
            )
            if self._selected_charger is not None:
                return await self.async_step_charger_info()
            # Selected charger is no longer among the discovered set (it
            # disappeared between rendering and submitting the form). Re-show
            # the form rather than proceeding with _selected_charger = None,
            # which would crash the downstream steps.
            errors["base"] = "charger_not_found"

        charger_options = [
            {"value": charger["id"], "label": charger["name"]}
            for charger in self._discovered_chargers
        ]

        data_schema = vol.Schema(
            {
                vol.Required("charger"): selector(
                    {"select": {"options": charger_options}}
                )
            }
        )

        return self.async_show_form(
            step_id="discover_chargers",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def _discover_ocpp_chargers(self) -> list:
        """Discover OCPP chargers from the OCPP integration.

        Thin wrapper around the module-level ``scan_ocpp_chargers``, which the
        automatic discovery in ``__init__.py`` uses too.
        """
        return scan_ocpp_chargers(self.hass)

    async def _detect_charge_rate_unit(self, ocpp_device_id: str) -> str | None:
        """
        Detect the charge rate unit supported by the OCPP charger.

        Queries the charger via OCPP GetConfiguration for the
        ChargingScheduleAllowedChargingRateUnit key.

        Returns:
            "A" for Amperes, "W" for Watts, None if detection fails.
        """
        if not ocpp_device_id:
            _LOGGER.debug("No OCPP device ID — cannot detect charge rate unit")
            return None

        if not self.hass.services.has_service("ocpp", "get_configuration"):
            _LOGGER.debug("ocpp.get_configuration service not available")
            return None

        try:
            response = await self.hass.services.async_call(
                "ocpp",
                "get_configuration",
                {
                    "devid": ocpp_device_id,
                    "ocpp_key": "ChargingScheduleAllowedChargingRateUnit",
                },
                blocking=True,
                return_response=True,
            )

            if not response:
                _LOGGER.debug("Empty response from ocpp.get_configuration")
                return None

            # Parse the response — handle multiple possible formats
            value = None
            if isinstance(response, dict):
                # Direct key-value: {"ChargingScheduleAllowedChargingRateUnit": "Current"}
                value = response.get("ChargingScheduleAllowedChargingRateUnit")
                # Or nested: {"value": "Current"}
                if value is None:
                    value = response.get("value")
                # Or list format: {"configurationKey": [{"key": ..., "value": ...}]}
                if value is None:
                    for item in response.get("configurationKey", []):
                        if (
                            isinstance(item, dict)
                            and item.get("key")
                            == "ChargingScheduleAllowedChargingRateUnit"
                        ):
                            value = item.get("value")
                            break

            if not value:
                _LOGGER.debug(
                    "Could not parse charge rate unit from OCPP response: %s", response
                )
                return None

            value = str(value).strip()
            value_lower = value.lower()
            _LOGGER.info("OCPP ChargingScheduleAllowedChargingRateUnit = %s", value)

            if "current" in value_lower and "power" in value_lower:
                return CHARGE_RATE_UNIT_AMPS  # Both supported — prefer Amps
            elif "power" in value_lower:
                return CHARGE_RATE_UNIT_WATTS
            elif "current" in value_lower:
                return CHARGE_RATE_UNIT_AMPS
            else:
                _LOGGER.warning(
                    "Unrecognised ChargingScheduleAllowedChargingRateUnit value: %s",
                    value,
                )
                return None

        except Exception as e:
            _LOGGER.warning("Could not detect charge rate unit via OCPP: %s", e)
            return None

    async def _detect_meter_value_interval(self, ocpp_device_id: str) -> int | None:
        """Detect the MeterValueSampleInterval from the OCPP charger.

        This tells us how often the charger reports meter values, which is the
        practical minimum interval for sending charging profile updates.

        Returns:
            Interval in seconds, or None if detection fails.
        """
        if not ocpp_device_id:
            return None

        if not self.hass.services.has_service("ocpp", "get_configuration"):
            return None

        try:
            response = await self.hass.services.async_call(
                "ocpp",
                "get_configuration",
                {
                    "devid": ocpp_device_id,
                    "ocpp_key": "MeterValueSampleInterval",
                },
                blocking=True,
                return_response=True,
            )

            if not response:
                return None

            value = None
            if isinstance(response, dict):
                value = response.get("MeterValueSampleInterval")
                if value is None:
                    value = response.get("value")
                if value is None:
                    for item in response.get("configurationKey", []):
                        if (
                            isinstance(item, dict)
                            and item.get("key") == "MeterValueSampleInterval"
                        ):
                            value = item.get("value")
                            break

            if value is None:
                return None

            interval = int(value)
            _LOGGER.info("OCPP MeterValueSampleInterval = %ds", interval)
            # Clamp to our supported range (5–300s)
            return max(5, min(300, interval))

        except Exception as e:
            _LOGGER.debug("Could not detect MeterValueSampleInterval via OCPP: %s", e)
            return None

    async def async_step_charger_info(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3a: Name, entity ID, and priority."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            entity_id = self._data.get(CONF_ENTITY_ID)
            if entity_id and self._entity_id_in_use(entity_id):
                errors[CONF_ENTITY_ID] = "entity_id_in_use"
            else:
                return await self.async_step_charger_current()

        existing_chargers = self._get_charger_entries()
        next_priority = len(existing_chargers) + 1

        data_schema = self._charger_info_schema(
            {
                CONF_NAME: self._data.get(
                    CONF_NAME, self._selected_charger["name"]
                ),
                CONF_ENTITY_ID: self._data.get(
                    CONF_ENTITY_ID, f"lj_{self._selected_charger['id']}"
                ),
                CONF_CHARGER_PRIORITY: self._data.get(
                    CONF_CHARGER_PRIORITY, next_priority
                ),
                CONF_OCPP_DEVICE_ID: self._data.get(
                    CONF_OCPP_DEVICE_ID, self._selected_charger.get("device_id")
                ),
            }
        )

        # Build list of detected entities for display
        detected_entities = []
        if self._selected_charger.get("device_id"):
            detected_entities.append(
                f"OCPP Device ID: {self._selected_charger['device_id']}"
            )
        if self._selected_charger.get("current_import_entity"):
            detected_entities.append(
                f"Current Import: {self._selected_charger['current_import_entity']}"
            )
        if self._selected_charger.get("current_import_l1_entity"):
            detected_entities.append(
                f"Current Import L1: {self._selected_charger['current_import_l1_entity']}"
            )
        if self._selected_charger.get("current_import_l2_entity"):
            detected_entities.append(
                f"Current Import L2: {self._selected_charger['current_import_l2_entity']}"
            )
        if self._selected_charger.get("current_import_l3_entity"):
            detected_entities.append(
                f"Current Import L3: {self._selected_charger['current_import_l3_entity']}"
            )
        if self._selected_charger.get("current_offered_entity"):
            detected_entities.append(
                f"Current Offered: {self._selected_charger['current_offered_entity']}"
            )
        if self._selected_charger.get("power_offered_entity"):
            detected_entities.append(
                f"Power Offered: {self._selected_charger['power_offered_entity']}"
            )
        if self._selected_charger.get("power_import_entity"):
            detected_entities.append(
                f"Power Import: {self._selected_charger['power_import_entity']}"
            )

        entities_text = "\n- ".join(detected_entities) if detected_entities else "None"

        return self.async_show_form(
            step_id="charger_info",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "charger_name": self._selected_charger["name"],
                "detected_entities": entities_text,
            },
            last_step=False,
        )

    async def async_step_charger_current(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3b: Current limits and phase mapping."""
        errors: dict[str, str] = {}
        hub_phases = self._get_hub_phase_count()

        if user_input is not None:
            self._data.update(user_input)
            # Auto-fill hidden phase mappings to match L1 (prevents mask mismatch)
            l1 = self._data.get(CONF_CHARGER_L1_PHASE, "A")
            if hub_phases < 2:
                self._data[CONF_CHARGER_L2_PHASE] = l1
            if hub_phases < 3:
                self._data[CONF_CHARGER_L3_PHASE] = l1

            validate_charger_settings(self._data, errors)
            if errors:
                return self.async_show_form(
                    step_id="charger_current",
                    data_schema=self._charger_current_schema(
                        self._data, hub_phases=hub_phases
                    ),
                    errors=errors,
                    last_step=False,
                )

            return await self.async_step_charger_timing()

        data_schema = self._charger_current_schema(
            {
                CONF_EVSE_MINIMUM_CHARGE_CURRENT: DEFAULT_MIN_CHARGE_CURRENT,
                CONF_EVSE_MAXIMUM_CHARGE_CURRENT: DEFAULT_MAX_CHARGE_CURRENT,
                CONF_CHARGER_L1_PHASE: "A",
                CONF_CHARGER_L2_PHASE: "B",
                CONF_CHARGER_L3_PHASE: "C",
            },
            hub_phases=hub_phases,
        )

        return self.async_show_form(
            step_id="charger_current",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_charger_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Charger step 3c: Units and timing configuration (final — creates entry)."""
        errors: dict[str, str] = {}

        # The OCPP device ID may have been edited on the charger_info step
        # (the timing step has no such field), so it lives in self._data.
        # Fall back to the detected value from discovery.
        detected_device_id = (
            self._selected_charger.get("device_id")
            if self._selected_charger
            else None
        )
        ocpp_device_id = self._data.get(CONF_OCPP_DEVICE_ID) or detected_device_id

        # Detect charger capabilities via OCPP
        detected_unit = await self._detect_charge_rate_unit(ocpp_device_id)
        detected_interval = await self._detect_meter_value_interval(ocpp_device_id)

        if user_input is not None:
            self._data.update(user_input)

            self._data[ENTRY_TYPE] = ENTRY_TYPE_CHARGER
            self._data[CONF_CHARGER_ID] = self._selected_charger["id"]
            # Keep the OCPP device ID resolved above (user edit or detected).
            self._data[CONF_OCPP_DEVICE_ID] = ocpp_device_id
            self._data[CONF_EVSE_CURRENT_IMPORT_ENTITY_ID] = self._selected_charger[
                "current_import_entity"
            ]
            self._data[CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID] = (
                self._selected_charger.get("current_import_l1_entity")
            )
            self._data[CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID] = (
                self._selected_charger.get("current_import_l2_entity")
            )
            self._data[CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID] = (
                self._selected_charger.get("current_import_l3_entity")
            )
            self._data[CONF_EVSE_CURRENT_OFFERED_ENTITY_ID] = self._selected_charger[
                "current_offered_entity"
            ]
            self._data[CONF_EVSE_POWER_OFFERED_ENTITY_ID] = self._selected_charger.get(
                "power_offered_entity"
            )
            self._data[CONF_EVSE_POWER_IMPORT_ENTITY_ID] = self._selected_charger.get(
                "power_import_entity"
            )

            # Use user-provided name/entity_id from charger_info step
            charger_name = self._data.get(CONF_NAME, self._selected_charger["name"])
            charger_entity_id = self._data.get(
                CONF_ENTITY_ID, f"lj_{self._selected_charger['id']}"
            )
            self._data[CONF_MIN_CURRENT_ENTITY_ID] = (
                f"number.{charger_entity_id}_min_current"
            )
            self._data[CONF_MAX_CURRENT_ENTITY_ID] = (
                f"number.{charger_entity_id}_max_current"
            )

            # Split static vs mutable fields for charger
            static_data = {
                CONF_ENTITY_ID: charger_entity_id,
                CONF_NAME: charger_name,
                ENTRY_TYPE: ENTRY_TYPE_CHARGER,
                CONF_HUB_ENTRY_ID: self._data.get(CONF_HUB_ENTRY_ID),
                CONF_CHARGER_ID: self._data.get(CONF_CHARGER_ID),
                CONF_OCPP_DEVICE_ID: self._data.get(CONF_OCPP_DEVICE_ID),
                CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: self._data.get(
                    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID
                ),
                CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID: self._data.get(
                    CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID
                ),
                CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID: self._data.get(
                    CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID
                ),
                CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID: self._data.get(
                    CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID
                ),
                CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: self._data.get(
                    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID
                ),
                CONF_EVSE_POWER_OFFERED_ENTITY_ID: self._data.get(
                    CONF_EVSE_POWER_OFFERED_ENTITY_ID
                ),
                CONF_EVSE_POWER_IMPORT_ENTITY_ID: self._data.get(
                    CONF_EVSE_POWER_IMPORT_ENTITY_ID
                ),
            }
            options_data = {k: v for k, v in self._data.items() if k not in static_data}

            return self.async_create_entry(
                title=_compose_entry_title(charger_name, "Charger"),
                data=static_data,
                options=options_data,
            )

        data_schema = self._charger_timing_schema(
            {
                CONF_PROFILE_VALIDITY_MODE: DEFAULT_PROFILE_VALIDITY_MODE,
                CONF_UPDATE_FREQUENCY: detected_interval or DEFAULT_UPDATE_FREQUENCY,
                CONF_OCPP_PROFILE_TIMEOUT: DEFAULT_OCPP_PROFILE_TIMEOUT,
                CONF_CHARGE_PAUSE_DURATION: DEFAULT_CHARGE_PAUSE_DURATION,
                CONF_STACK_LEVEL: DEFAULT_STACK_LEVEL,
            },
            detected_unit=detected_unit,
        )

        return self.async_show_form(
            step_id="charger_timing",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Get the options flow for this handler."""
        return LoadJugglerOptionsFlow()


class LoadJugglerOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Load Juggler."""

    def __init__(self):
        self._data = {}
        self._flow = None  # Cached config flow for schema/helper access

    @property
    def _schema_helper(self) -> LoadJugglerConfigFlow:
        """Cached LoadJugglerConfigFlow instance for schema building."""
        if self._flow is None:
            self._flow = LoadJugglerConfigFlow()
            self._flow.hass = self.hass
        return self._flow

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """The one entry point for editing an entry — a small menu.

        "Configure" is the single edit path (there is no reconfigure flow), so
        this menu also hosts the two read-only pages: a live Overview for every
        entry type, and "How it decides" for the hub.
        """
        entry_type = self.config_entry.data.get(ENTRY_TYPE, ENTRY_TYPE_HUB)
        menu_options = ["settings", "overview"]
        if entry_type == ENTRY_TYPE_HUB:
            menu_options.append("summary")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_overview(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Read-only live overview, scoped to this entry.

        Rendered as a MENU, not a form: a form's submit button is fixed to
        "Next"/"Submit" by HA, which reads as if something gets saved. Menu
        options give real, labeled buttons instead — "Refresh" re-enters this
        step (rebuilding the text from live data), "Back" returns to init.
        """
        try:
            text = _overview_text(self.hass, self.config_entry.entry_id)
        except Exception:  # pragma: no cover — a display page must not break
            _LOGGER.exception("Could not build the overview page")
            text = "⚠️ Could not read the live data — see the Home Assistant log."
        return self.async_show_menu(
            step_id="overview",
            menu_options=["overview", "init"],
            description_placeholders={"overview": text},
        )

    async def async_step_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Read-only "how it decides" page (hub only).

        A menu for the same reason as async_step_overview — the only action
        here is going back, and a form's "Next" button would misname it.
        """
        try:
            text = _summary_text(self.hass, self.config_entry.entry_id)
        except Exception:  # pragma: no cover — a display page must not break
            _LOGGER.exception("Could not build the summary page")
            text = "⚠️ Could not read the configuration — see the Home Assistant log."
        return self.async_show_menu(
            step_id="summary",
            menu_options=["init"],
            description_placeholders={"summary": text},
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Route to the editable pages for this entry type."""
        # A pre-2.0 entry carries no entry_type — those are hubs (async_setup
        # stamps the type on load, so this only matters before the first load).
        entry_type = self.config_entry.data.get(ENTRY_TYPE, ENTRY_TYPE_HUB)

        if entry_type == ENTRY_TYPE_HUB:
            return await self.async_step_hub_grid()
        if entry_type == ENTRY_TYPE_CHARGER:
            device_type = self.config_entry.data.get(CONF_DEVICE_TYPE)
            if device_type == DEVICE_TYPE_PLUG:
                return await self.async_step_plug()
            if device_type == DEVICE_TYPE_HOT_WATER_TANK:
                return await self.async_step_hot_water_tank()
            if device_type == DEVICE_TYPE_POWER_STATION:
                return await self.async_step_power_station()
            return await self.async_step_charger()
        if entry_type == ENTRY_TYPE_GROUP:
            return await self.async_step_group()
        if entry_type == ENTRY_TYPE_INVERTER:
            return await self.async_step_inverter()
        return self.async_abort(reason="entry_not_found")

    async def async_step_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Options for an inverter entry — one page: inverter, PV and battery."""
        errors: dict[str, str] = {}
        bad_forecast_entity = None
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(
                user_input,
                f._INVERTER_ENTITY_KEYS
                + [
                    CONF_SOLAR_PRODUCTION_ENTITY_ID,
                    CONF_BATTERY_SOC_ENTITY_ID,
                    CONF_BATTERY_POWER_ENTITY_ID,
                    CONF_CHARGE_LIMIT_ENTITY_ID,
                    CONF_BATTERY_VOLTAGE_ENTITY_ID,
                    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
                ],
            )
            user_input = f._normalize_forecast_list(user_input)
            user_input = f._normalize_soc_limit_list(user_input)
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_SOLAR_PRODUCTION_ENTITY_ID: _POWER_UNITS,
                    CONF_BATTERY_POWER_ENTITY_ID: _POWER_UNITS,
                    CONF_BATTERY_SOC_ENTITY_ID: _SOC_UNITS,
                },
                errors,
            )
            bad_forecast_entity = _validate_forecast_devices(
                self.hass, user_input, errors
            )
            if not errors:
                for key in (CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE):
                    if user_input.get(key) == 0:
                        user_input[key] = None
                return self.async_create_entry(
                    title="",
                    data={**self.config_entry.options, **user_input},
                )
            defaults = {**defaults, **user_input}

        return self.async_show_form(
            step_id="inverter",
            data_schema=f._inverter_combined_schema(defaults),
            errors=errors,
            description_placeholders=(
                {"entity": bad_forecast_entity} if bad_forecast_entity else None
            ),
        )

    async def async_step_hub_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(user_input, f._GRID_ENTITY_KEYS)
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_PHASE_A_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
                    CONF_PHASE_B_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
                    CONF_PHASE_C_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
                    CONF_MAX_IMPORT_POWER_ENTITY_ID: _POWER_UNITS,
                },
                errors,
            )
            # Dropping the grid CTs here is what makes a hub off-grid, so this
            # is where the battery requirement belongs now that the battery
            # itself lives on an inverter entry.
            validate_offgrid_battery_requirement(
                user_input, defaults, errors,
                hass=self.hass, hub_entry_id=self.config_entry.entry_id,
            )
            if not errors:
                self._data.update(user_input)
                # Post-import the hardware (inverters, batteries, PV sensors
                # and forecast sources) is edited on the inverter entries —
                # the legacy hub pages are skipped entirely.
                if self.config_entry.data.get(MIGRATE_HUB_INVERTER_IMPORTED_FLAG):
                    return await self.async_step_priority()
                return await self.async_step_hub_inverter()
            return self.async_show_form(
                step_id="hub_grid",
                data_schema=f._hub_grid_schema(user_input),
                errors=errors,
                last_step=False,
            )

        # No auto-detection when editing an existing hub — only the initial
        # install scans for entities. Re-detecting here can grab entities from
        # an unrelated system (e.g. a second inverter in another building),
        # silently adding phantom phases. Show the existing values as-is.
        return self.async_show_form(
            step_id="hub_grid",
            data_schema=f._hub_grid_schema(defaults),
            errors=errors,
            last_step=False,
        )

    async def async_step_hub_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(
                user_input, f._INVERTER_ENTITY_KEYS
            )
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: _CURRENT_UNITS
                    | _POWER_UNITS,
                },
                errors,
            )
            if not errors:
                self._data.update(user_input)
                f._data = self._data
                f._normalize_inverter_powers()
                self._data = f._data
                return await self.async_step_hub()
            battery_hint = f._auto_detect_entity_value(
                BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR
            )
            hint_text = f"{battery_hint}W detected" if battery_hint else "not detected"
            return self.async_show_form(
                step_id="hub_inverter",
                data_schema=f._hub_inverter_schema(user_input),
                errors=errors,
                last_step=False,
                description_placeholders={"battery_power_hint": hint_text},
            )

        # Show existing values, defaulting 0 for None
        inverter_defaults = dict(defaults)
        for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
            if inverter_defaults.get(key) is None:
                inverter_defaults[key] = 0

        # No auto-detection when editing an existing hub — only the initial
        # install scans for entities. Re-detecting the inverter output phases
        # here can grab a different inverter's per-phase sensors (e.g. a 3-phase
        # system in another building), creating phantom L2/L3 phases that split
        # the available power across phases that don't exist on this site. Show
        # the existing values as-is.

        # Battery discharge power hint (informational text only — sets nothing)
        battery_hint = f._auto_detect_entity_value(
            BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR
        )
        hint_text = f"{battery_hint}W detected" if battery_hint else "not detected"

        return self.async_show_form(
            step_id="hub_inverter",
            data_schema=f._hub_inverter_schema(inverter_defaults),
            errors=errors,
            description_placeholders={"battery_power_hint": hint_text},
            last_step=False,
        )

    async def async_step_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """LEGACY hub solar/battery page — reachable only while the hub still
        carries those fields (i.e. before the one-time auto-import)."""
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(
                user_input, f._BATTERY_ENTITY_KEYS
            )
            user_input = f._normalize_forecast_list(user_input)
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_SOLAR_PRODUCTION_ENTITY_ID: _POWER_UNITS,
                    CONF_BATTERY_POWER_ENTITY_ID: _POWER_UNITS,
                    CONF_BATTERY_SOC_ENTITY_ID: _SOC_UNITS,
                },
                errors,
            )
            bad_forecast_entity = _validate_forecast_devices(
                self.hass, user_input, errors
            )
            validate_offgrid_battery_requirement(
                {**defaults, **self._data}, user_input, errors,
                hass=self.hass, hub_entry_id=self.config_entry.entry_id,
            )
            if not errors:
                self._data.update(user_input)
                return await self.async_step_priority()
            return self.async_show_form(
                step_id="hub",
                data_schema=f._hub_battery_schema(user_input),
                errors=errors,
                description_placeholders=(
                    {"entity": bad_forecast_entity} if bad_forecast_entity else None
                ),
                last_step=False,
            )

        # No auto-detection when editing an existing hub — only the initial
        # install scans for entities. Re-detecting here can grab battery/solar
        # entities from an unrelated system. Show the existing values as-is.
        return self.async_show_form(
            step_id="hub",
            data_schema=f._hub_battery_schema(defaults),
            errors=errors,
            last_step=False,
        )

    async def async_step_priority(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub options final step: reorder all controlled devices by priority.

        Presents one ordered multi-select listing every load (EVSE, smart plug,
        hot-water tank) linked to this hub. The selection order becomes the
        served-first order: the first chip is priority 1, the next is 2, and so
        on. This is the single place to set relative priority — the per-device
        number is written back to each child entry from the chosen order.
        """
        devices = _controlled_devices(self.hass, self.config_entry.entry_id)

        def _save_hub() -> config_entries.FlowResult:
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, **self._data},
            )

        # No loads to order yet — just persist the hub settings and finish.
        if not devices:
            return _save_hub()

        if user_input is not None:
            _apply_priority_order(
                self.hass, devices, list(user_input.get(CONF_PRIORITY_ORDER, []))
            )
            return _save_hub()

        return self.async_show_form(
            step_id="priority",
            data_schema=_priority_order_schema(devices),
            last_step=True,
        )

    async def async_step_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Options charger step 1: Priority and OCPP device ID."""
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_charger_current()

        # Build schema with priority and OCPP Device ID
        fields = {
            vol.Required(
                CONF_CHARGER_PRIORITY,
                default=defaults.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            ): selector({"number": {"min": 1, "max": 10, "mode": "box"}}),
        }

        # Add OCPP Device ID as editable field if it exists
        ocpp_device_id = defaults.get(CONF_OCPP_DEVICE_ID)
        if ocpp_device_id:
            fields[
                vol.Optional(
                    CONF_OCPP_DEVICE_ID,
                    default=ocpp_device_id,
                )
            ] = str

        data_schema = vol.Schema(fields)
        return self.async_show_form(
            step_id="charger",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_charger_current(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Options charger step 2: Current limits and phase mapping."""
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper
        hub_entry_id = defaults.get(CONF_HUB_ENTRY_ID)
        hub_phases = f._get_hub_phase_count(hub_entry_id)

        if user_input is not None:
            self._data.update(user_input)
            # Auto-fill hidden phase mappings to match L1
            l1 = self._data.get(CONF_CHARGER_L1_PHASE, "A")
            if hub_phases < 2:
                self._data[CONF_CHARGER_L2_PHASE] = l1
            if hub_phases < 3:
                self._data[CONF_CHARGER_L3_PHASE] = l1
            validate_charger_settings(self._data, errors)
            if errors:
                return self.async_show_form(
                    step_id="charger_current",
                    data_schema=f._charger_current_schema(
                        self._data, hub_phases=hub_phases
                    ),
                    errors=errors,
                    last_step=False,
                )
            return await self.async_step_charger_timing()

        return self.async_show_form(
            step_id="charger_current",
            data_schema=f._charger_current_schema(defaults, hub_phases=hub_phases),
            errors=errors,
            last_step=False,
        )

    async def async_step_charger_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Options charger step 3: Units and timing (final — saves)."""
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        # Options-first: an edited device ID lives in entry.options.
        ocpp_device_id = get_entry_value(self.config_entry, CONF_OCPP_DEVICE_ID, None)
        detected_unit = await f._detect_charge_rate_unit(ocpp_device_id)

        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, **self._data},
            )

        return self.async_show_form(
            step_id="charger_timing",
            data_schema=f._charger_timing_schema(defaults, detected_unit=detected_unit),
            errors=errors,
            last_step=True,
        )

    async def async_step_plug(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(user_input, f._PLUG_ENTITY_KEYS)
            self._data.update(user_input)
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, **self._data},
            )

        return self.async_show_form(
            step_id="plug",
            data_schema=f._plug_schema(defaults),
            errors=errors,
            last_step=True,
        )

    async def async_step_hot_water_tank(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(user_input, f._TANK_ENTITY_KEYS)
            self._data.update(user_input)

            device_id = self._data.pop(CONF_TANK_POWER_DEVICE_ID, None)
            if device_id and not self._data.get(CONF_TANK_POWER_ENTITY_ID):
                resolved = f._resolve_device_power_entity(device_id)
                if resolved:
                    self._data[CONF_TANK_POWER_ENTITY_ID] = resolved

            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, **self._data},
            )

        return self.async_show_form(
            step_id="hot_water_tank",
            data_schema=f._hot_water_tank_schema(defaults),
            errors=errors,
            last_step=True,
        )

    async def async_step_power_station(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(
                user_input, f._STATION_ENTITY_KEYS
            )
            self._data.update(user_input)
            if self._data.get(
                CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
            ) < self._data.get(
                CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER
            ):
                errors[CONF_STATION_MAX_CHARGE_POWER] = "station_max_below_min"
            else:
                return self.async_create_entry(
                    title="",
                    data={**self.config_entry.options, **self._data},
                )

        return self.async_show_form(
            step_id="power_station",
            data_schema=f._power_station_schema({**defaults, **self._data}),
            errors=errors,
            last_step=True,
        )

    async def async_step_group(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Options flow for circuit group: current limit + member selection."""
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            selected = user_input.get(CONF_CIRCUIT_GROUP_MEMBERS, [])
            if not selected:
                errors["base"] = "no_members_selected"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_CIRCUIT_GROUP_CURRENT_LIMIT: user_input.get(
                            CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
                            DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
                        ),
                        CONF_CIRCUIT_GROUP_MEMBERS: selected,
                    },
                )

        # Build list of loads on this hub for multi-select
        hub_entry_id = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
        load_options = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if (
                entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
                and entry.data.get(CONF_HUB_ENTRY_ID) == hub_entry_id
            ):
                load_options.append(
                    {
                        "value": entry.entry_id,
                        "label": entry.title,
                    }
                )

        current_members = defaults.get(CONF_CIRCUIT_GROUP_MEMBERS, [])

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
                    default=defaults.get(
                        CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
                        DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 1,
                            "max": 100,
                            "step": 1,
                            "unit_of_measurement": "A",
                            "mode": "box",
                        }
                    }
                ),
                vol.Required(
                    CONF_CIRCUIT_GROUP_MEMBERS,
                    default=current_members,
                ): selector(
                    {
                        "select": {
                            "options": load_options,
                            "multiple": True,
                            "mode": "list",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="group",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )
