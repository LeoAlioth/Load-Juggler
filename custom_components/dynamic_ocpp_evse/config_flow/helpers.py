"""Load Juggler - config-flow helpers: validation, ordering and discovery.

The module-level utilities the flow steps lean on, none of them bound to a flow
instance: the unit sets a form may offer (one declaration, shared with the
readers), the entity-unit and forecast-device validators, the optional-entity
key groups and the normalizers that clear them, entity auto-detection,
entry-title composition, the controlled-device and priority-order helpers
behind the priority page (and the circuit-group load pickers), the two OCPP
probes the charger wizard asks the charger, the station power-window check,
the charger's hidden-leg fill, and the hub phase count derived from the
configured grid CTs. The OCPP registry scan itself lives in the package-root
``ocpp_discovery.py``, where the engine can reach it too.

Anything both handlers need lives here rather than on either of them - the
unit maps below are the create/options twins' single shared declaration, and
that is what lets the create flow and the options flow stay unaware of each
other's handler class.
"""
import logging
import re
import voluptuous as vol
from homeassistant.components.energy.data import async_get_manager
from homeassistant.helpers.device_registry import (
    async_entries_for_config_entry as dr_async_entries_for_config_entry,
    async_get as async_get_device_registry,
)
from homeassistant.helpers.entity_registry import (
    async_entries_for_device as er_async_entries_for_device,
    async_get as async_get_entity_registry,
)
from homeassistant.helpers.selector import selector
from .. import units
from ..const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_CAPACITY_ENTITY_ID,
    CONF_BATTERY_CHARGE_TARGET_ENTITY_ID,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_LOAD_PRIORITY,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_HUB_ENTRY_ID,
    CONF_INVERT_PHASES,
    CONF_INVERTER_INPUT_IS_GRID,
    CONF_INVERTER_INPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_INPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_INPUT_PHASE_C_ENTITY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_ENTITY_ID,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_EARLY_ROOM_ENTITY_ID,
    CONF_EARLY_RATE_ENTITY_ID,
    CONF_PACING_ENERGY_ENTITY_ID,
    CONF_BASE_CONSUMPTION_ENTITY_ID,
    CONF_PACING_HORIZON_ENTITY_ID,
    CONF_FORECAST_EXCESS_FULL_ENTITY_ID,
    CONF_OUTAGE_ENTITY_ID,
    CONF_NAME,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
    CONF_PRIORITY_ORDER,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_STATION_AC_INPUT_ENTITY_ID,
    CONF_STATION_AC_OUTPUT_ENTITY_ID,
    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_TANK_POWER_ENTITY_ID,
    CHARGE_LIMIT_UNIT_AMPS,
    DEFAULT_LOAD_PRIORITY,
    DEFAULT_CHARGE_LIMIT_UNIT,
    DEFAULT_STATION_MAX_CHARGE_POWER,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
    CONF_INVERTER_FEATURES,
    INVERTER_FEATURE_BATTERY,
    INVERTER_FEATURE_BATTERY_CONTROL,
    INVERTER_FEATURE_SOLAR,
)
from ..const.sg_ready import (
    CONF_SG_MAX_BLOCK_TIME,
    CONF_SG_MAX_BLOCKS_PER_DAY,
    CONF_SG_MIN_STATE_TIME,
    CONF_SG_NOMINAL_POWER,
    CONF_SG_POWER_ENTITY_ID,
    CONF_SG_RELAY_1_ENTITY_ID,
    CONF_SG_RELAY_2_ENTITY_ID,
    DEFAULT_SG_MAX_BLOCK_TIME,
    DEFAULT_SG_MAX_BLOCKS_PER_DAY,
    DEFAULT_SG_MIN_STATE_TIME,
    DEFAULT_SG_NOMINAL_POWER,
)
from ..detection_patterns import PHASE_PATTERNS
from ..energy_prefs import SiteModel
from ..helpers import get_entry_value, normalize_optional_entity, ocpp_config_value
from ..phases import beside, match_meter_entities
from ..registry import get_inverters_for_hub

_LOGGER = logging.getLogger(__name__)

# One declaration, shared with the readers: a unit offered here must be one
# units.py can convert (see ENTITY_UNIT_CONTRACTS and test_unit_contracts.py).
_CURRENT_UNITS = units.CURRENT_UNITS
_POWER_UNITS = units.POWER_UNITS
_SOC_UNITS = units.SOC_UNITS
_VOLTAGE_UNITS = units.VOLTAGE_UNITS
_ENERGY_UNITS = units.ENERGY_UNITS

# The field→accepted-units maps _validate_entity_units is called with, one
# declaration per FIELD GROUP rather than per page. A page validates the
# groups it shows, composing them with ``|``:
#
#   hub grid (create + options)   _GRID_UNIT_MAP
#   inverter config (create)      _INVERTER_AC_UNIT_MAP | _SOLAR_UNIT_MAP
#   inverter battery (create)     _BATTERY_UNIT_MAP
#   inverter control (create)     _write_control_unit_map(data)
#   inverter (options, per page)  the matching one of the above
#
# Grouping rather than paging is what keeps a create/options twin pair honest:
# both sides of a group get the same units by construction, and a multi-step
# create chain can share a map with the single-page options twin because
# _validate_entity_units skips any key the submitted form didn't collect.
_GRID_UNIT_MAP = {
    CONF_PHASE_A_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
    CONF_PHASE_B_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
    CONF_PHASE_C_CURRENT_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
    CONF_MAX_IMPORT_POWER_ENTITY_ID: _POWER_UNITS,
}
_PACING_UNIT_MAP = {
    CONF_BASE_CONSUMPTION_ENTITY_ID: _POWER_UNITS,
    CONF_PACING_ENERGY_ENTITY_ID: _ENERGY_UNITS,
}
_INVERTER_AC_UNIT_MAP = {
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
    CONF_INVERTER_MAX_POWER_ENTITY_ID: _POWER_UNITS,
    CONF_INVERTER_INPUT_PHASE_A_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
    CONF_INVERTER_INPUT_PHASE_B_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
    CONF_INVERTER_INPUT_PHASE_C_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
}
_SOLAR_UNIT_MAP = {
    CONF_SOLAR_PRODUCTION_ENTITY_ID: _POWER_UNITS,
}
_BATTERY_UNIT_MAP = {
    CONF_BATTERY_POWER_ENTITY_ID: _POWER_UNITS,
    CONF_BATTERY_SOC_ENTITY_ID: _SOC_UNITS,
    CONF_BATTERY_CAPACITY_ENTITY_ID: _ENERGY_UNITS,
    CONF_BATTERY_CHARGE_TARGET_ENTITY_ID: _SOC_UNITS,
}
_WRITE_CONTROL_UNIT_MAP = {
    # The charge-limit register is NOT here: it is written in whatever unit the
    # user chose (CONF_CHARGE_LIMIT_UNIT), so it has no fixed physical domain -
    # _write_control_unit_map adds it against the choice instead.
    CONF_BATTERY_VOLTAGE_ENTITY_ID: _VOLTAGE_UNITS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID: _SOC_UNITS,
}


def _write_control_unit_map(data: dict) -> dict:
    """_WRITE_CONTROL_UNIT_MAP plus the charge-limit register, checked against
    the CHOSEN unit: the register is written raw in the unit the user declared
    (CONF_CHARGE_LIMIT_UNIT: DC amps on a Deye, watts elsewhere), so an "A"
    register configured as watts is exactly the mistake this catches."""
    chosen = data.get(CONF_CHARGE_LIMIT_UNIT) or DEFAULT_CHARGE_LIMIT_UNIT
    register_units = (
        _CURRENT_UNITS if chosen == CHARGE_LIMIT_UNIT_AMPS else _POWER_UNITS
    )
    return {**_WRITE_CONTROL_UNIT_MAP, CONF_CHARGE_LIMIT_ENTITY_ID: register_units}


def _validate_entity_units(
    hass, user_input: dict, field_unit_map: dict, errors: dict
) -> None:
    """Validate that provided entities report expected measurement units.

    Silently skips when an entity's state is unavailable/unknown or has no
    unit_of_measurement attribute - the user is never blocked by missing state.
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
    yet never blocks the user - mirroring _validate_entity_units - but a
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
            continue  # states not loaded yet - never block on missing state
        if any(isinstance(s.attributes.get("watts"), dict) for s in states):
            continue
        errors[CONF_SOLAR_FORECAST_DEVICE_IDS] = "forecast_device_no_watts"
        device = async_get_device_registry(hass).async_get(device_id)
        if device:
            return device.name_by_user or device.name or device_id
        return device_id
    return None


def _normalize_inverter_power_caps(data: dict) -> None:
    """0 means "not configured" for the inverter power caps → store as None.

    In-place, and the one copy for every page that collects them: the inverter
    create chain and the inverter options page.
    The schema builders do the reverse (``or 0``) so the round-trip holds.
    """
    for key in (CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE):
        if data.get(key) == 0:
            data[key] = None


# --- Optional-entity field groups, and the normalizers over them ---

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
    CONF_BATTERY_CAPACITY_ENTITY_ID,
    CONF_BATTERY_CHARGE_TARGET_ENTITY_ID,
]
_INVERTER_INPUT_KEYS = [
    CONF_INVERTER_INPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_INPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_INPUT_PHASE_C_ENTITY_ID,
]
_INVERTER_ENTITY_KEYS = [
    CONF_INVERTER_MAX_POWER_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    *_INVERTER_INPUT_KEYS,
]
_PACING_ENTITY_KEYS = [
    CONF_BASE_CONSUMPTION_ENTITY_ID,
    CONF_PACING_ENERGY_ENTITY_ID,
    CONF_PACING_HORIZON_ENTITY_ID,
    CONF_FORECAST_EXCESS_FULL_ENTITY_ID,
    CONF_OUTAGE_ENTITY_ID,
]
_PLUG_ENTITY_KEYS = [CONF_PLUG_POWER_MONITOR_ENTITY_ID, CONF_EARLY_ROOM_ENTITY_ID, CONF_EARLY_RATE_ENTITY_ID]
_TANK_ENTITY_KEYS = [CONF_TANK_POWER_ENTITY_ID, CONF_EARLY_ROOM_ENTITY_ID, CONF_EARLY_RATE_ENTITY_ID]
_SG_READY_ENTITY_KEYS = [CONF_SG_POWER_ENTITY_ID, CONF_EARLY_ROOM_ENTITY_ID, CONF_EARLY_RATE_ENTITY_ID]
_STATION_ENTITY_KEYS = [
    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
    CONF_STATION_AC_INPUT_ENTITY_ID,
    CONF_STATION_AC_OUTPUT_ENTITY_ID,
    CONF_EARLY_ROOM_ENTITY_ID,
    CONF_EARLY_RATE_ENTITY_ID,
]


def _normalize_optional_inputs(
    data: dict, step_entity_keys: list[str] | None = None
) -> dict:
    """Normalize optional entity inputs.

    Args:
        data: The user_input from the form step.
        step_entity_keys: Optional entity keys expected in this step.
            Keys missing from data are set to None (user cleared the field).
    """
    normalized = dict(data)
    for key in (
        _GRID_ENTITY_KEYS
        + _BATTERY_ENTITY_KEYS
        + _INVERTER_ENTITY_KEYS
        + _PACING_ENTITY_KEYS
        + _PLUG_ENTITY_KEYS
        + _TANK_ENTITY_KEYS
        + _SG_READY_ENTITY_KEYS
    ):
        if key in normalized:
            normalized[key] = normalize_optional_entity(normalized.get(key))
    # Entity selectors omit unselected fields - explicitly clear them
    if step_entity_keys:
        for key in step_entity_keys:
            if key not in normalized:
                normalized[key] = None
    return normalized


def _normalize_list(data: dict, key: str) -> None:
    """Store a multi-select's list at ``key``, [] when it was emptied - in place.

    Separate from _normalize_optional_inputs, which is per-key scalar: a
    multi-select yields a list and omits the key entirely once the user
    clears it, so an emptied selection must become [] (the forecast devices,
    the SOC slots - whose [] removes the Battery SOC Control switch and sensor
    again - the inverter features), never a stale stored value.
    """
    data[key] = [item for item in (data.get(key) or []) if item]


# --- Entity auto-detection (the suggested defaults a create page opens with) ---

def _entity_registry_ids(hass) -> list[str]:
    """Every registry entity that actually has a state, as detection candidates.

    Disabled/stale registry entries have no state and should not be offered as
    auto-detection candidates (they would end up as a suggested_value that is
    not in include_entities, breaking submission). The create flow caches the
    result for the life of one flow; the options flow's single detection call
    does not need to.
    """
    entity_registry = async_get_entity_registry(hass)
    return [
        eid
        for eid in entity_registry.entities.keys()
        if hass.states.get(eid) is not None
    ]


_PHASE_SLOTS = ("phase_a", "phase_b", "phase_c")


def _first_match(entity_ids: list[str], pattern: str) -> str | None:
    return next((eid for eid in entity_ids if re.match(pattern, eid)), None)


def _auto_detect_phase_entities(
    entity_ids: list[str], pattern_sets: list[dict]
) -> dict[str, str | None]:
    """Auto-detect a matching set of phase A/B/C entities from pattern sets.

    Returns dict with keys 'phase_a', 'phase_b', 'phase_c' - all three set
    from the first pattern set that matches all three, or all None.
    """
    for pattern_set in pattern_sets:
        found = {slot: _first_match(entity_ids, pattern_set["patterns"][slot])
                 for slot in _PHASE_SLOTS}
        if all(found.values()):
            return found
    return dict.fromkeys(_PHASE_SLOTS)


def _device_rejects(hass, eid: str, pattern_sets: list[dict] = PHASE_PATTERNS) -> bool:
    """A pattern set that matches ``eid`` tests its device, and the device is
    not the meter (detection_patterns' ``device``): ids a meter shares with
    another device - ha-solarman's "Grid L1 Power" on an Afore string inverter.
    The registry is read only for an entity such a pattern matches."""
    tests = [s["device"] for s in pattern_sets
             if "device" in s and any(re.match(p, eid) for p in s["patterns"].values())]
    if not tests:
        return False
    entry = async_get_entity_registry(hass).async_get(eid)
    device = entry and entry.device_id and async_get_device_registry(hass).async_get(entry.device_id)
    return not all(test(device or None) for test in tests)


def _device_rows(hass, entity_registry, device_id: str) -> list[dict]:
    """A device's sensors that have a state, in the shape
    phases.match_meter_entities reads: the kind from the device class, or
    from the unit for a sensor published without one."""
    rows = []
    for e in er_async_entries_for_device(entity_registry, device_id):
        state = hass.states.get(e.entity_id)
        if e.domain != "sensor" or state is None:
            continue
        unit = state.attributes.get("unit_of_measurement")
        kind = e.device_class or e.original_device_class or (
            "power" if unit in _POWER_UNITS else "current" if unit in _CURRENT_UNITS else None
        )
        rows.append({"entity_id": e.entity_id, "device_class": kind,
                     "name": e.name or e.original_name or ""})
    return rows


def _power_beside(hass, triple: dict[str, str | None]) -> dict[str, str | None]:
    """The meter's own watts in place of the amps a pattern found.

    A grid CT's power reading is signed and its current very often is not
    (see detection_patterns._power_first), and a meter publishing amps per
    phase usually publishes watts beside them. Each amps entity's DEVICE is
    asked for its power reading on that phase whose name runs alongside
    (phases.beside). All three or none, so the triple stays one unit; a
    triple that is already watts, or has an entity without a device (a YAML
    sensor), comes back as it is. A triple on a device its pattern's test
    turns down (_device_rejects) is none.
    """
    if any(eid and _device_rejects(hass, eid) for eid in triple.values()):
        return dict.fromkeys(_PHASE_SLOTS)
    entity_registry = async_get_entity_registry(hass)
    watts = {}
    for slot, eid in triple.items():
        entry = entity_registry.async_get(eid) if eid else None
        if entry is None or entry.device_id is None:
            return triple
        rows = _device_rows(hass, entity_registry, entry.device_id)
        if any(r["entity_id"] == eid and r["device_class"] == "power" for r in rows):
            return triple
        watts[slot] = beside(rows, eid, "power", slot[-1])
    return watts if all(watts.values()) else triple


def _count(triple: dict) -> int:
    return sum(1 for eid in triple.values() if eid)


def _meter_triple(hass, device_id: str) -> tuple[str, dict[str, str | None]]:
    """A device's per-phase readings as a grid connection's
    (phases.match_meter_entities, role "grid" - only that device's rows):
    ``("power" | "current", {phase_a, phase_b, phase_c})``, the kind that
    covers more phases, power on a tie."""
    meter = match_meter_entities(
        _device_rows(hass, async_get_entity_registry(hass), device_id), "grid"
    )
    return max(
        (
            (kind, {slot: meter.get(f"{kind}_{slot[-1]}") for slot in _PHASE_SLOTS})
            for kind in ("power", "current")
        ),
        key=lambda kt: _count(kt[1]),
    )


def _same_device_fill(hass, entity_ids: list[str], pattern_sets: list[dict]) -> dict[str, str | None]:
    """No pattern set matched all three phases, or the one that did was on a
    device its test turns down (_device_rejects): the first complete set left
    once such entities are dropped, as _power_beside offers it; else the
    phases the first set to match any did, completed from that entity's own
    device - a single-phase site's one CT, never a triple stitched together
    from several meters.

    The device's readings are matched as a grid connection's
    (phases.match_meter_entities, role "grid" - only that device's rows,
    never the whole registry: there a charger's current_import_l1..l3 would
    win on the shortest name). Its watts when they cover as many phases as
    its amps, the amps otherwise, and what the pattern found when the
    device covers fewer phases than that or there is no device.
    """
    entity_ids = [eid for eid in entity_ids if not _device_rejects(hass, eid, pattern_sets)]
    complete = _power_beside(hass, _auto_detect_phase_entities(entity_ids, pattern_sets))
    if all(complete.values()):
        return complete
    for pattern_set in pattern_sets:
        found = {slot: _first_match(entity_ids, pattern_set["patterns"][slot])
                 for slot in _PHASE_SLOTS}
        if any(found.values()):
            break
    else:
        return dict.fromkeys(_PHASE_SLOTS)
    entity_registry = async_get_entity_registry(hass)
    entry = entity_registry.async_get(next(eid for eid in found.values() if eid))
    if entry is None or entry.device_id is None:
        return found
    _, best = _meter_triple(hass, entry.device_id)
    return best if _count(best) >= _count(found) else found


# --- Suggestions from the Energy dashboard (the initial setup forms only) ---
#
# The dashboard is READ, never written: its preferences name the site's PV,
# battery and grid meter, and a create form opens with them in its empty
# fields. A value already in the form always wins (_fill_empty), and the
# options pages never call any of this - a field cleared there is cleared on
# purpose. The dashboard's sign convention is Load Juggler's (positive =
# import / discharge), so a sensor is offered as it is, never flipped.

_FORECAST_DOMAIN = "open_meteo_solar_forecast"


async def _energy_site(hass) -> SiteModel:
    """The Energy dashboard as a SiteModel; an empty one when it cannot be
    read (store not loaded, odd shape) - setup is never blocked by it."""
    try:
        return SiteModel.from_prefs((await async_get_manager(hass)).data)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Energy dashboard preferences not readable: %s", err)
        return SiteModel()


def _forecast_devices(hass, entry_ids) -> list[str]:
    """The Open-Meteo Solar Forecast devices of the dashboard's forecast
    config entries (its ``config_entry_solar_forecast`` holds entry ids, the
    form takes device ids). Other providers have no ``watts`` series."""
    device_registry = async_get_device_registry(hass)
    devices: list[str] = []
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != _FORECAST_DOMAIN:
            continue
        for device in dr_async_entries_for_config_entry(device_registry, entry_id):
            if device.id not in devices:
                devices.append(device.id)
    return devices


def _inverter_suggestions(hass, site: SiteModel, first: bool) -> dict:
    """What the dashboard offers a NEW inverter entry. Only the hub's first
    inverter gets any: on a fleet the dashboard's single battery or array
    cannot be attributed to one inverter. A sensor with no state is not offered."""
    if not first:
        return {}

    def known(entity_id):
        return entity_id if entity_id and hass.states.get(entity_id) is not None else None

    out: dict = {}
    features = [
        feature
        for feature, present in (
            (INVERTER_FEATURE_SOLAR, site.solar),
            (INVERTER_FEATURE_BATTERY, site.battery_out),
        )
        if present
    ]
    if features:
        out[CONF_INVERTER_FEATURES] = features
    # One total sensor only: several arrays would need a summing helper.
    if len(site.solar) == 1 and len(site.solar_power) == 1:
        out[CONF_SOLAR_PRODUCTION_ENTITY_ID] = known(site.solar_power[0])
    if devices := _forecast_devices(hass, site.solar_forecast_entries):
        out[CONF_SOLAR_FORECAST_DEVICE_IDS] = devices
    # The capacity sum and the SOC are per battery: exactly one.
    if len(site.battery_out) == 1:
        if len(site.battery_power) == 1:
            out[CONF_BATTERY_POWER_ENTITY_ID] = known(site.battery_power[0].rate)
        if len(site.battery_soc) == 1:
            out[CONF_BATTERY_SOC_ENTITY_ID] = known(site.battery_soc[0])
        if site.battery_capacity_kwh:
            out[CONF_BATTERY_CAPACITY_KWH] = site.battery_capacity_kwh
    return {k: v for k, v in out.items() if v is not None}


def _grid_meter_suggestion(hass, site: SiteModel) -> dict:
    """The hub's grid CTs from the dashboard's grid meter: the per-phase
    readings of the DEVICE of its raw power sensor, and ``invert_phases``
    when the dashboard declares that meter inverted and the readings are its
    watts (a meter's per-phase amps are import-positive whatever its watts
    do). ``{}`` unless there is exactly one grid power source with a device
    that yields at least one phase."""
    try:
        if len(site.grid_power) != 1:
            return {}
        spec = site.grid_power[0]
        registry = async_get_entity_registry(hass)
        # Never ``stat_rate`` itself when a raw sensor exists: it is HA's own
        # or a template's derived sensor, without a device.
        for entity_id in (spec.rate_inverted, spec.rate_from, spec.rate):
            entry = registry.async_get(entity_id) if entity_id else None
            if entry is None or entry.device_id is None:
                continue
            kind, triple = _meter_triple(hass, entry.device_id)
            if not _count(triple):
                return {}
            return {
                CONF_PHASE_A_CURRENT_ENTITY_ID: triple["phase_a"],
                CONF_PHASE_B_CURRENT_ENTITY_ID: triple["phase_b"],
                CONF_PHASE_C_CURRENT_ENTITY_ID: triple["phase_c"],
                CONF_INVERT_PHASES: kind == "power" and entity_id == spec.rate_inverted,
            }
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Grid meter suggestion failed: %s", err)
    return {}


def _fill_empty(defaults: dict, suggestions: dict) -> dict:
    """``defaults`` with each suggestion that fills a key that is missing,
    None, "" or [] - a value already there always wins."""
    return {
        **defaults,
        **{k: v for k, v in suggestions.items() if defaults.get(k) in (None, "", [])},
    }


def _auto_detect_entity(
    entity_ids: list[str], pattern_sets: list[dict]
) -> str | None:
    """Auto-detect a single entity from pattern sets. Returns first match."""
    for pattern_set in pattern_sets:
        match = _first_match(entity_ids, pattern_set["pattern"])
        if match:
            return match
    return None


def _compose_entry_title(name: str, type_label: str) -> str:
    """Compose a config-entry title without doubling the device-type label.

    The type label is appended only when the user's name doesn't already
    contain it - so a device left at its default name (e.g. "Hot Water Tank")
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
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_LOAD
        and e.data.get(CONF_HUB_ENTRY_ID) == hub_entry_id
    ]


def _load_options(hass, hub_entry_id: str) -> list[dict]:
    """Select options for every load on a hub (the circuit-group pickers)."""
    return [
        {"value": e.entry_id, "label": e.title}
        for e in _controlled_devices(hass, hub_entry_id)
    ]


def _devices_by_priority(devices: list) -> list:
    """Devices sorted by effective priority, then title for a stable tie-break."""
    return sorted(
        devices,
        key=lambda e: (
            get_entry_value(e, CONF_LOAD_PRIORITY, DEFAULT_LOAD_PRIORITY),
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
    unchanged - it still sorts by (mode urgency, priority).
    """
    placed = list(chosen)
    for entry in _devices_by_priority(devices):
        if entry.entry_id not in placed:
            placed.append(entry.entry_id)
    for rank, entry_id in enumerate(placed, start=1):
        child = hass.config_entries.async_get_entry(entry_id)
        if not child or get_entry_value(child, CONF_LOAD_PRIORITY, None) == rank:
            continue
        hass.config_entries.async_update_entry(
            child, options={**child.options, CONF_LOAD_PRIORITY: rank}
        )


async def _detect_meter_value_interval(hass, ocpp_device_id: str) -> int | None:
    """The charger's MeterValueSampleInterval in seconds, clamped to 5-300.

    How often it reports meter values - the practical minimum interval for
    sending charging profile updates. None if detection fails.
    """
    value = await ocpp_config_value(hass, ocpp_device_id, "MeterValueSampleInterval")
    if value is None:
        return None
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return None
    _LOGGER.info("OCPP MeterValueSampleInterval = %ds", interval)
    return max(5, min(300, interval))


def _check_power_window(data: dict, errors: dict) -> None:
    """A power station's max charge power may not sit below its min."""
    if data.get(
        CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
    ) < data.get(CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER):
        errors[CONF_STATION_MAX_CHARGE_POWER] = "station_max_below_min"


def _fill_hidden_legs(data: dict, hub_phases: int) -> None:
    """Store every unused charger leg as None - in place. A leg is unused when
    the form left it blank or the site has too few phases to show it; the
    stored None (not a missing key) is what overrides an earlier mapping."""
    for number, key in ((2, CONF_CHARGER_L2_PHASE), (3, CONF_CHARGER_L3_PHASE)):
        if hub_phases < number or not data.get(key):
            data[key] = None


def _hub_phase_count(hass, hub_entry_id: str | None) -> int:
    """Number of phases this site has, as the engine sees it.

    A site phase exists when it has a grid CT or an inverter output sensor
    (mirrors ``run_hub_calculation``'s phase derivation). The inverter output
    entities live on the hub's inverter child entries, so the whole fleet is
    consulted. Without that, an off-grid 3-phase site collapses to 1 phase,
    hiding the L2/L3 mapping fields and force-mapping every charger leg onto
    L1's phase.
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
    # Off-grid fallback: infer from the inverter output entities of every
    # inverter child entry. A phase counts once, no matter how many feed it.
    sources = [
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


def _validate_inverter_input(
    hass, entry_id, hub_entry_id, data: dict, errors: dict
) -> None:
    """An inverter's input (grid side) is the grid meter or sensors, not both,
    taken from the grid meter by at most one inverter per hub, and only beside
    output sensors (the household adds ``output - input``, so an input alone
    says nothing). ``entry_id`` is this inverter's own entry (None while it is
    being created), skipped when looking at its siblings."""
    sensors = [key for key in _INVERTER_INPUT_KEYS if data.get(key)]
    is_grid = bool(data.get(CONF_INVERTER_INPUT_IS_GRID))
    if is_grid and sensors:
        errors[CONF_INVERTER_INPUT_IS_GRID] = "input_grid_and_sensors"
        return
    if is_grid and any(
        inverter.entry_id != entry_id
        and get_entry_value(inverter, CONF_INVERTER_INPUT_IS_GRID, False)
        for inverter in get_inverters_for_hub(hass, hub_entry_id)
    ):
        errors[CONF_INVERTER_INPUT_IS_GRID] = "input_grid_taken"
        return
    has_output = any(
        data.get(key)
        for key in (
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
        )
    )
    if (sensors or is_grid) and not has_output:
        errors[sensors[0] if sensors else CONF_INVERTER_INPUT_IS_GRID] = (
            "input_without_output"
        )


def _validate_inverter_features(data: dict, errors: dict) -> None:
    """Battery write-control is a battery feature: without a battery declared
    there is nothing to write the charge limit or SOC ceiling FOR."""
    features = data.get(CONF_INVERTER_FEATURES) or []
    if (
        INVERTER_FEATURE_BATTERY_CONTROL in features
        and INVERTER_FEATURE_BATTERY not in features
    ):
        errors[CONF_INVERTER_FEATURES] = "control_needs_battery"
