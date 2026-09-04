"""Load Juggler - config-flow helpers: validation, ordering and discovery.

The module-level utilities the flow steps lean on, none of them bound to a flow
instance: the unit sets a form may offer (one declaration, shared with the
readers), the entity-unit and forecast-device validators, the optional-entity
key groups and the normalizers that clear them, entity auto-detection, the
device-power resolver, entry-title composition, the controlled-device and
priority-order helpers behind the priority page, the two OCPP probes the
charger wizard asks the charger, and the hub phase count derived from the
configured grid CTs. The OCPP registry scan itself lives in the package-root
``ocpp_discovery.py``, where the engine can reach it too.

Anything both handlers need lives here rather than on either of them — the
unit maps below are the create/options twins' single shared declaration, and
that is what lets the create flow and the options flow stay unaware of each
other's handler class.
"""
import logging
import re
import voluptuous as vol
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.entity_registry import (
    async_entries_for_device as er_async_entries_for_device,
    async_get as async_get_entity_registry,
)
from homeassistant.helpers.selector import selector
from .. import units
from ..const import (
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_LOAD_PRIORITY,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_NAME,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
    CONF_PRIORITY_ORDER,
    CONF_SOC_LIMIT_ENTITY_IDS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_STATION_AC_INPUT_ENTITY_ID,
    CONF_STATION_AC_OUTPUT_ENTITY_ID,
    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
    CONF_TANK_POWER_DEVICE_ID,
    CONF_TANK_POWER_ENTITY_ID,
    CHARGE_LIMIT_UNIT_AMPS,
    CHARGE_RATE_UNIT_AMPS,
    CHARGE_RATE_UNIT_WATTS,
    DEFAULT_LOAD_PRIORITY,
    DEFAULT_CHARGE_LIMIT_UNIT,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
    CONF_INVERTER_FEATURES,
    INVERTER_FEATURE_BATTERY,
    INVERTER_FEATURE_BATTERY_CONTROL,
)
from ..helpers import get_entry_value, normalize_optional_entity
from ..registry import get_inverters_for_hub

_LOGGER = logging.getLogger(__name__)
_POWER_FACTOR = 0.9  # 90% of detected limit for safe headroom

# One declaration, shared with the readers: a unit offered here must be one
# units.py can convert (see ENTITY_UNIT_CONTRACTS and test_unit_contracts.py).
_CURRENT_UNITS = units.CURRENT_UNITS
_POWER_UNITS = units.POWER_UNITS
_SOC_UNITS = units.SOC_UNITS
_VOLTAGE_UNITS = units.VOLTAGE_UNITS

# The field→accepted-units maps _validate_entity_units is called with, one
# declaration per FIELD GROUP rather than per page. A page validates the
# groups it shows, composing them with ``|``:
#
#   hub grid (create + options)   _GRID_UNIT_MAP
#   inverter config (create)      _INVERTER_OUTPUT_UNIT_MAP | _SOLAR_UNIT_MAP
#   inverter battery (create)     _BATTERY_UNIT_MAP
#   inverter control (create)     _WRITE_CONTROL_UNIT_MAP
#   inverter (options, one page)  all four of the above
#   hub inverter (options)        _INVERTER_OUTPUT_UNIT_MAP
#   hub battery (options)         _SOLAR_UNIT_MAP | _BATTERY_UNIT_MAP
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
_INVERTER_OUTPUT_UNIT_MAP = {
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: _CURRENT_UNITS | _POWER_UNITS,
}
_SOLAR_UNIT_MAP = {
    CONF_SOLAR_PRODUCTION_ENTITY_ID: _POWER_UNITS,
}
_BATTERY_UNIT_MAP = {
    CONF_BATTERY_POWER_ENTITY_ID: _POWER_UNITS,
    CONF_BATTERY_SOC_ENTITY_ID: _SOC_UNITS,
}
_WRITE_CONTROL_UNIT_MAP = {
    # The charge-limit register is NOT here: it is written in whatever unit the
    # user chose (CONF_CHARGE_LIMIT_UNIT), so it has no fixed physical domain —
    # _validate_charge_limit_unit checks it against the choice instead.
    CONF_BATTERY_VOLTAGE_ENTITY_ID: _VOLTAGE_UNITS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID: _SOC_UNITS,
}


def _validate_charge_limit_unit(hass, user_input: dict, errors: dict) -> None:
    """Validate the charge-limit register entity against the CHOSEN unit.

    The register is written raw in the unit the user declared
    (CONF_CHARGE_LIMIT_UNIT: DC amps on a Deye, watts elsewhere), so unlike the
    physical-domain fields there is no canonical unit to convert into — an "A"
    register configured as watts is exactly the mistake this catches. Skips
    like _validate_entity_units: no entity, no state, or no unit → no error.
    """
    entity_id = user_input.get(CONF_CHARGE_LIMIT_ENTITY_ID)
    if not entity_id:
        return
    state = hass.states.get(entity_id)
    if units.is_unavailable(state):
        return
    unit = state.attributes.get("unit_of_measurement")
    if not unit:
        return
    chosen = user_input.get(CONF_CHARGE_LIMIT_UNIT) or DEFAULT_CHARGE_LIMIT_UNIT
    expected = _CURRENT_UNITS if chosen == CHARGE_LIMIT_UNIT_AMPS else _POWER_UNITS
    if unit not in expected:
        errors[CONF_CHARGE_LIMIT_ENTITY_ID] = "invalid_unit"


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


def _normalize_inverter_power_caps(data: dict) -> None:
    """0 means "not configured" for the inverter power caps → store as None.

    In-place, and the one copy for every page that collects them: the inverter
    create chain, the inverter options page and the legacy hub-inverter page.
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
        + _PLUG_ENTITY_KEYS
        + _TANK_ENTITY_KEYS
    ):
        if key in normalized:
            normalized[key] = normalize_optional_entity(normalized.get(key))
    # Entity selectors omit unselected fields — explicitly clear them
    if step_entity_keys:
        for key in step_entity_keys:
            if key not in normalized:
                normalized[key] = None
    return normalized


def _normalize_forecast_list(data: dict) -> dict:
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


def _normalize_soc_limit_list(data: dict) -> dict:
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


def _auto_detect_phase_entities(
    entity_ids: list[str], pattern_sets: list[dict]
) -> dict[str, str | None]:
    """Auto-detect a matching set of phase A/B/C entities from pattern sets.

    Returns dict with keys 'phase_a', 'phase_b', 'phase_c' (values may be None).
    """
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


def _auto_detect_entity(
    entity_ids: list[str], pattern_sets: list[dict]
) -> str | None:
    """Auto-detect a single entity from pattern sets. Returns first match."""
    for pattern_set in pattern_sets:
        match = next(
            (eid for eid in entity_ids if re.match(pattern_set["pattern"], eid)),
            None,
        )
        if match:
            return match
    return None


def _auto_detect_entity_value(
    hass, pattern_sets: list[dict], factor: float = 1.0
) -> int | None:
    """Auto-detect an entity and read its numeric state value.

    Returns int(state * factor), or None if not found / not numeric. Scans the
    registry itself — the one caller (a form hint) detects exactly once.
    """
    entity_id = _auto_detect_entity(_entity_registry_ids(hass), pattern_sets)
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if not state:
        return None
    try:
        return int(float(state.state) * factor)
    except (ValueError, TypeError):
        return None


def _resolve_device_power_entity(hass, device_id: str) -> str | None:
    """Return the first power-class sensor entity belonging to a device."""
    entity_registry = async_get_entity_registry(hass)
    for entity in entity_registry.entities.values():
        if entity.device_id != device_id:
            continue
        device_class = entity.device_class or entity.original_device_class
        if device_class == "power":
            return entity.entity_id
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
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_LOAD
        and e.data.get(CONF_HUB_ENTRY_ID) == hub_entry_id
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
    unchanged — it still sorts by (mode urgency, priority).
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


async def _detect_charge_rate_unit(hass, ocpp_device_id: str) -> str | None:
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

    if not hass.services.has_service("ocpp", "get_configuration"):
        _LOGGER.debug("ocpp.get_configuration service not available")
        return None

    try:
        response = await hass.services.async_call(
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


async def _detect_meter_value_interval(hass, ocpp_device_id: str) -> int | None:
    """Detect the MeterValueSampleInterval from the OCPP charger.

    This tells us how often the charger reports meter values, which is the
    practical minimum interval for sending charging profile updates.

    Returns:
        Interval in seconds, or None if detection fails.
    """
    if not ocpp_device_id:
        return None

    if not hass.services.has_service("ocpp", "get_configuration"):
        return None

    try:
        response = await hass.services.async_call(
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


def _normalize_features_list(data: dict) -> dict:
    """The features multi-select omits its key when emptied — store []."""
    normalized = dict(data)
    normalized[CONF_INVERTER_FEATURES] = list(normalized.get(CONF_INVERTER_FEATURES) or [])
    return normalized


def _validate_inverter_features(data: dict, errors: dict) -> None:
    """Battery write-control is a battery feature: without a battery declared
    there is nothing to write the charge limit or SOC ceiling FOR."""
    features = data.get(CONF_INVERTER_FEATURES) or []
    if (
        INVERTER_FEATURE_BATTERY_CONTROL in features
        and INVERTER_FEATURE_BATTERY not in features
    ):
        errors[CONF_INVERTER_FEATURES] = "control_needs_battery"
