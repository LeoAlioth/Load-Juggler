"""Load Juggler - the read-only options pages: "Overview" and "How it decides".

The string builders behind the two pages that show rather than ask, plus the
small formatters and live-data readers they share. Kept module-level and pure
in (hass, entry_id) so they can be unit-tested without a flow instance.

Moved verbatim out of the single-file config_flow.py.
"""
from datetime import datetime, timezone
from homeassistant.helpers.entity_registry import (
    async_entries_for_config_entry as er_async_entries_for_config_entry,
    async_get as async_get_entity_registry,
)
from .. import units
from ..const import (
    CONF_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_SOC_FULL,
    CONF_BATTERY_SOC_MIN,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_LOAD_PRIORITY,
    CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
    CONF_CIRCUIT_GROUP_MEMBERS,
    CONF_DEVICE_TYPE,
    CONF_DISTRIBUTION_MODE,
    CONF_ENABLE_MAX_IMPORT_POWER,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_GRID_EXPORT_LIMIT,
    CONF_HEATING_ELEMENT_POWER,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_INVERTER_SUPPORTS_ASYMMETRIC,
    CONF_INVERT_PHASES,
    CONF_MAIN_BREAKER_RATING,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_NAME,
    CONF_OPERATING_MODE,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_PLUG_MAX_CURRENT,
    CONF_PLUG_POWER_RATING,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_WIRING_TOPOLOGY,
    DEFAULT_BATTERY_SOC_FULL,
    DEFAULT_BATTERY_SOC_MIN,
    DEFAULT_LOAD_PRIORITY,
    DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
    DEFAULT_DISTRIBUTION_MODE,
    DEFAULT_HEATING_ELEMENT_POWER,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_PLUG_MAX_CURRENT,
    DEFAULT_PLUG_POWER_RATING,
    DEFAULT_STATION_MAX_CHARGE_POWER,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DEFAULT_WIRING_TOPOLOGY,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_POWER_STATION,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
    resolve_operating_mode,
)
from ..helpers import get_entry_value
from ..registry import get_groups_for_hub, get_inverters_for_hub
from .helpers import (
    _LOGGER,
    _controlled_devices,
    _devices_by_priority,
    _hub_phase_count,
)

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
    runtime = (_runtime(hass).get("loads") or {}).get(load_entry.entry_id) or {}
    permit = runtime.get("_last_permit")
    if permit is None:
        permit = _entry_sensor_value(hass, load_entry, "_available_current")
    if permit is None:
        permit = (hub_data.get("load_available") or {}).get(load_entry.entry_id)
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
    """Circuit group entries linked to a hub (one implementation, in registry.py)."""
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
    runtime = (_runtime(hass).get("loads") or {}).get(load_entry.entry_id) or {}
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


def _whole(value):
    """A priority or rank as the integer it is — never ``2.0``."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


# Non-EVSE loads have their own status sensor with their own vocabulary
# (Heating / Idle, Charging / Full / Idle, On / Off); the shared load_status
# bucket speaks EVSE ("Unplugged" for a connector that does not exist).
_STATUS_UNIQUE_ID_SUFFIX = {
    DEVICE_TYPE_HOT_WATER_TANK: "_tank_status",
    DEVICE_TYPE_POWER_STATION: "_charging_status",
    DEVICE_TYPE_PLUG: "_charging_status",
}


def _device_status(hass, load_entry) -> str | None:
    """A non-EVSE load's own status sensor state, or None (EVSE, or no sensor)."""
    suffix = _STATUS_UNIQUE_ID_SUFFIX.get(
        load_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
    )
    if not suffix:
        return None
    unique_id = f"{load_entry.data.get(CONF_ENTITY_ID)}{suffix}"
    entity_id = async_get_entity_registry(hass).async_get_entity_id(
        "sensor", DOMAIN, unique_id
    )
    state = hass.states.get(entity_id) if entity_id else None
    if state is None or units.is_unavailable(state):
        return None
    return state.state


def _load_line(hass, hub_entry_id: str, load_entry, hub_data: dict) -> str:
    """One "name · mode · priority · permit · draw · status" line."""
    runtime = _runtime(hass)
    device_type = load_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
    parts = [f"**{load_entry.title}**", _DEVICE_TYPE_LABELS.get(device_type, "Load")]
    parts.append(_load_mode(hass, load_entry))

    priority = _whole(
        get_entry_value(load_entry, CONF_LOAD_PRIORITY, DEFAULT_LOAD_PRIORITY)
    )
    rank = _whole((runtime.get("load_ranks") or {}).get(load_entry.entry_id))
    if rank is not None and rank != priority:
        parts.append(f"priority {priority} (served {rank}.)")
    else:
        parts.append(f"priority {priority}")

    parts.append(f"permitted {_fmt(_load_permit(hass, load_entry, hub_data), 'A')}")

    draw = (runtime.get("load_allocations") or {}).get(load_entry.entry_id)
    if draw is None:
        draw = (hub_data.get("load_targets") or {}).get(load_entry.entry_id)
    parts.append(f"drawing {_fmt(draw, 'A')}")

    status = _device_status(hass, load_entry) or (runtime.get("load_status") or {}).get(
        load_entry.entry_id
    )
    parts.append(status or "status unknown")

    mask = (runtime.get("load_phase_masks") or {}).get(load_entry.entry_id)
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

    A key that is PRESENT and None is not that case: the engine ran and
    deliberately published no household figure, because one of the terms it
    would be built from was a substituted value rather than a reading (see
    engine/hub_result.py). Re-deriving it here from those same terms would put
    the fabrication back on the page — so the em dash stands.
    """
    household = hub_data.get("household_power")
    if isinstance(household, (int, float)):
        return household
    if "household_power" in hub_data:
        return None
    grid = hub_data.get("grid_power")
    solar = hub_data.get("solar_power")
    if not isinstance(grid, (int, float)) or not isinstance(solar, (int, float)):
        return None
    battery = hub_data.get("battery_power")
    managed = hub_data.get("total_evse_power")
    battery = battery if isinstance(battery, (int, float)) else 0
    managed = managed if isinstance(managed, (int, float)) else 0
    return max(0, round(grid + solar + battery - managed, 0))


def _forecast_overview_lines(hub_data: dict) -> list[str]:
    """The PV clipping forecast as the hub sees it — empty when it is off.

    The energy figures describe the NEXT clipping window: the rest of today
    while today still has clip left, tomorrow's peak once it has integrated
    away (``forecast_window_tomorrow``). The observers' lines appear only once
    they have data.
    """
    if hub_data.get("forecast_clipped_kwh") is None:
        return []
    window = "tomorrow" if hub_data.get("forecast_window_tomorrow") else "today"
    lines = ["", f"**☀️ PV forecast — next clipping window ({window})**"]
    lines.append(
        f"- Clippable: {_fmt(hub_data.get('forecast_clipped_kwh'), 'kWh', 2)}"
        f" · battery can store: {_fmt(hub_data.get('forecast_absorbable_kwh'), 'kWh', 2)}"
        f" · nowhere to go: {_fmt(hub_data.get('forecast_headroom_deficit_kwh'), 'kWh', 2)}"
    )
    lines.append(
        f"- Advised battery ceiling: {_fmt(hub_data.get('forecast_battery_max_soc'), '%', 0)}"
    )
    limit = hub_data.get("forecast_charge_limit_w")
    if limit is not None:
        limiting = any(
            (inv or {}).get("forecast_charge_limiting")
            for inv in (hub_data.get("inverters") or {}).values()
        )
        lines.append(
            f"- Fleet charge limit: {_fmt(limit, 'W', 0)}"
            f" ({'holding' if limiting else 'released'})"
        )
    clipped_today = hub_data.get("forecast_clipped_actual_kwh")
    if clipped_today is not None:
        yesterday = hub_data.get("forecast_clipped_actual_yesterday_kwh")
        lines.append(
            f"- Clipped so far today: {_fmt(clipped_today, 'kWh', 2)}"
            + (f" · yesterday: {_fmt(yesterday, 'kWh', 2)}" if yesterday is not None else "")
        )
    accuracy = hub_data.get("forecast_accuracy_pct")
    peakiness = hub_data.get("forecast_peakiness_pct")
    if accuracy is not None or peakiness is not None:
        lines.append(
            f"- Forecast accuracy: {_fmt(accuracy, '%', 0)}"
            f" · peakiness: {_fmt(peakiness, '%', 0)}"
        )
    return lines


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
            from ..engine.readers import _read_grid_phases

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
    grid_w = hub_data.get("grid_power")
    if isinstance(grid_w, (int, float)):
        flow = "Exporting" if grid_w < 0 else "Importing"
        lines.append(f"- {flow} now: {_fmt(abs(grid_w), 'W', 0)}")
    else:
        lines.append(f"- Net grid power: {_fmt(grid_w, 'W', 0)}")
    # The reconstructed figure the Excess verdict and the charge control steer
    # on: the meter with the managed loads' draws added back.
    lines.append(
        "- Export with managed loads off: "
        f"{_fmt(hub_data.get('total_export_power'), 'W', 0)}"
    )
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

    lines += _forecast_overview_lines(hub_data)

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
    priority = get_entry_value(entry, CONF_LOAD_PRIORITY, DEFAULT_LOAD_PRIORITY)
    rank = (runtime.get("load_ranks") or {}).get(entry.entry_id)
    lines.append(
        f"- Priority: {priority}"
        + (f" · served {rank}. this cycle" if rank is not None else "")
    )
    lines.append(f"- Configured envelope: {_load_limits_text(hass, entry)}")
    if device_type in (DEVICE_TYPE_EVSE, DEVICE_TYPE_POWER_STATION, DEVICE_TYPE_PLUG):
        lines.append(f"- Phase mapping (L1→L3): {_phase_mapping_text(hass, entry)}")

    lines += ["", "**📊 Right now**"]
    lines.append(f"- Permitted: {_fmt(_load_permit(hass, entry, hub_data), 'A')}")
    draw = (runtime.get("load_allocations") or {}).get(entry.entry_id)
    lines.append(f"- Actual draw: {_fmt(draw, 'A')}")
    status = (runtime.get("load_status") or {}).get(entry.entry_id)
    lines.append(f"- Status: {status or 'unknown'}")
    mask = (runtime.get("load_phase_masks") or {}).get(entry.entry_id)
    if mask:
        lines.append(f"- Drawing on phases: {mask}")
    load_runtime = (runtime.get("loads") or {}).get(entry.entry_id) or {}
    if load_runtime.get("dynamic_control") is False:
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
        from ..engine.readers import _read_inverter_output

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
            limiting = own.get("forecast_charge_limiting")
            state = "" if limiting is None else f" ({'holding' if limiting else 'released'})"
            lines.append(
                f"- Recommended charge limit: {_fmt(own.get('forecast_charge_limit_w'), 'W', 0)}{state}"
            )
    if own.get("forecast_accuracy_pct") is not None or own.get("forecast_gain") is not None:
        lines += ["", "**☀️ This array's forecast**"]
        lines.append(
            f"- Accuracy today: {_fmt(own.get('forecast_accuracy_pct'), '%', 0)}"
            f" · learned gain: {_fmt(own.get('forecast_gain'), '', 2)}"
            + (
                f" over {own.get('forecast_gain_days')} d"
                if own.get("forecast_gain_days") is not None
                else ""
            )
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
        draw = (runtime.get("load_allocations") or {}).get(member_id)
        status = (runtime.get("load_status") or {}).get(member_id)
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
                ((_runtime(hass).get("loads") or {}).get(e.entry_id) or {}).get(
                    "operating_mode"
                )
                or get_entry_value(e, CONF_OPERATING_MODE, None),
            ).priority,
            get_entry_value(e, CONF_LOAD_PRIORITY, DEFAULT_LOAD_PRIORITY),
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
