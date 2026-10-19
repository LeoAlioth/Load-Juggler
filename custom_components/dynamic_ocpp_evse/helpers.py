"""Helper utilities for Load Juggler integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry

from .const import (
    CHARGE_RATE_UNIT_AMPS,
    CHARGE_RATE_UNIT_WATTS,
    CONF_CHARGER_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CAPACITY_ENTITY_ID,
    CONF_BATTERY_CHARGE_TARGET_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_SOC_FULL,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_CHARGE_CONTROL_DEADBAND_W,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_MINIMUM,
    CONF_CHARGE_LIMIT_NORMAL,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_INVERTER_FEATURES,
    CONF_SOC_LIMIT_ENTITY_IDS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CONF_SOC_LIMIT_SEMANTICS,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    INVERTER_FEATURE_BATTERY,
    INVERTER_FEATURE_BATTERY_CONTROL,
    INVERTER_FEATURE_SOLAR,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_SUPPORTS_ASYMMETRIC,
    CONF_INVERTER_INPUT_IS_GRID,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
)
from .registry import get_inverters_for_hub

_LOGGER = logging.getLogger(__name__)


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


def hub_has_battery(hass, hub_entry: ConfigEntry) -> bool:
    """True when any inverter entry linked to this hub has a battery.

    The single gate for every battery-dependent hub entity (SOC sensors and
    sliders, the Allow Grid Charging switch), shared across the sensor,
    number and switch platforms so they cannot drift apart.
    """
    return any(
        get_entry_value(entry, CONF_BATTERY_SOC_ENTITY_ID, None)
        or get_entry_value(entry, CONF_BATTERY_POWER_ENTITY_ID, None)
        for entry in get_inverters_for_hub(hass, hub_entry.entry_id)
    )


def has_battery_capacity(entry: ConfigEntry) -> bool:
    """True when the entry names its battery's size - a capacity sensor or a
    kWh figure."""
    return bool(
        get_entry_value(entry, CONF_BATTERY_CAPACITY_ENTITY_ID, None)
        or (get_entry_value(entry, CONF_BATTERY_CAPACITY_KWH, 0) or 0) > 0
    )


def fleet_has_battery_capacity(hass, hub_entry: ConfigEntry) -> bool:
    """Whether any battery on this hub's inverter entries has a size: the
    engine's forecast gate."""
    return any(
        has_battery_capacity(entry)
        for entry in get_inverters_for_hub(hass, hub_entry.entry_id)
    )


def fleet_has_forecast_sources(hass, hub_entry: ConfigEntry) -> bool:
    """True when any PV forecast source is configured on this hub's fleet.

    Forecast devices belong to the inverter whose array they model, but
    clipping is a site-level question, so the hub's forecast sensors light up
    as soon as ANY member has one.
    """
    return any(
        get_entry_value(entry, CONF_SOLAR_FORECAST_DEVICE_IDS, None)
        for entry in get_inverters_for_hub(hass, hub_entry.entry_id)
    )


async def ocpp_config_value(hass, ocpp_device_id: str | None, key: str):
    """One configuration key read from an OCPP charger, or None.

    Asks through the ocpp integration's ``get_configuration`` service (absent
    when that integration is not loaded) and accepts the three response shapes
    seen in the wild: ``{key: value}``, ``{"value": value}`` and the protocol's
    own ``{"configurationKey": [{"key": ..., "value": ...}]}``. Any failure is
    None - every caller treats an unknown value as "not detected".
    """
    if not ocpp_device_id or not hass.services.has_service("ocpp", "get_configuration"):
        return None
    try:
        response = await hass.services.async_call(
            "ocpp",
            "get_configuration",
            {"devid": ocpp_device_id, "ocpp_key": key},
            blocking=True,
            return_response=True,
        )
        if not isinstance(response, dict):
            return None
        value = response.get(key)
        if value is None:
            value = response.get("value")
        if value is None:
            value = next(
                (
                    item.get("value")
                    for item in response.get("configurationKey", [])
                    if isinstance(item, dict) and item.get("key") == key
                ),
                None,
            )
        return value
    except Exception as err:
        _LOGGER.debug("Could not read OCPP %s from %s: %s", key, ocpp_device_id, err)
        return None



async def detect_charge_rate_unit(hass, ocpp_device_id: str) -> str | None:
    """The charge rate unit the charger accepts: "A", "W", or None if unknown.

    From its ChargingScheduleAllowedChargingRateUnit; a charger that takes
    both is driven in amps.
    """
    value = await ocpp_config_value(
        hass, ocpp_device_id, "ChargingScheduleAllowedChargingRateUnit"
    )
    if not value:
        return None
    value = str(value).strip()
    _LOGGER.info("OCPP ChargingScheduleAllowedChargingRateUnit = %s", value)
    if "current" in value.lower():
        return CHARGE_RATE_UNIT_AMPS
    if "power" in value.lower():
        return CHARGE_RATE_UNIT_WATTS
    _LOGGER.warning(
        "Unrecognised ChargingScheduleAllowedChargingRateUnit value: %s", value
    )
    return None

_LEG_KEYS = (CONF_CHARGER_L1_PHASE, CONF_CHARGER_L2_PHASE, CONF_CHARGER_L3_PHASE)
_LEG_DEFAULTS = ("A", "B", "C")


def set_legs(l1, l2, l3) -> list[str]:
    """The site phases of a charger's SET legs, L1 first. A leg left None is
    unused, so the count is the charger's phase count. The legs are contiguous:
    nothing after the first unset one counts."""
    legs = []
    for phase in (l1, l2, l3):
        if not phase:
            break
        legs.append(phase)
    return legs


def stored_legs(entry: ConfigEntry) -> list[str]:
    """``set_legs`` of a stored charger entry. A key never stored means the
    legacy default (A/B/C); a stored None means the leg is unused."""
    return set_legs(
        *(get_entry_value(entry, k, d) for k, d in zip(_LEG_KEYS, _LEG_DEFAULTS))
    )


def unuse_repeated_legs(data: dict, options: dict) -> bool:
    """Migration 2.10: a charger leg that repeats an earlier leg's phase was the
    old way to say "this leg is unused" (a 1-phase charger stored L1=L2=L3, a
    2-phase site stored L3=L1). Turn each into None, in place, in whichever
    store holds the leg - the SAME phase mask results, so nothing changes at
    runtime. Reads the effective values the way get_entry_value does (a key
    never stored means the legacy default). Idempotent. An entry whose
    repeats would leave a gap (L2 repeated, L3 distinct) is left alone: that
    mapping is refused by the form and rewritten by the user, not guessed
    here. Returns True when something changed."""
    keys_present = [k for k in _LEG_KEYS if k in data or k in options]
    if not keys_present and CONF_CHARGER_ID not in data:
        return False

    def effective(key, default):
        store = options if key in options else data
        return normalize_optional_entity(store.get(key, default))

    legs = [effective(k, d) for k, d in zip(_LEG_KEYS, _LEG_DEFAULTS)]
    if legs[0] is None:
        return False
    seen, new = set(), []
    for phase in legs:
        new.append(None if phase is None or phase in seen else phase)
        seen.add(phase)
    if new == legs or (new[1] is None and new[2] is not None):
        return False
    for key, before, after in zip(_LEG_KEYS, legs, new):
        if before != after:
            (options if key in options else data)[key] = after
    return True


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

    # The phase mapping: legs are contiguous, and no two legs of one charger
    # share a site phase (that would book the full per-leg current on a phase
    # once per leg). A 1-phase charger is L1 only, 2-phase L1+L2.
    l1, l2, l3 = (data.get(key) or None for key in _LEG_KEYS)
    if l3 and not l2:
        errors[CONF_CHARGER_L3_PHASE] = "charger_phase_gap"
        return
    seen = []
    for key, phase in zip(_LEG_KEYS, (l1, l2, l3)):
        if not phase:
            break
        if phase in seen:
            errors[key] = "charger_phase_duplicate"
            return
        seen.append(phase)


def validate_offgrid_battery_requirement(
    grid_data: dict,
    errors: dict[str, str],
    hass,
    hub_entry_id: str,
) -> None:
    """Require a battery on hubs with no grid CTs (hard block).

    A hub with no grid CT entities runs off-grid: the battery SOC drives the
    mode logic and battery power drives off-grid solar-surplus detection, so
    both entities are mandatory on some inverter entry linked to the hub.
    Adds an error to ``errors`` in-place.

    Args:
        grid_data: config holding the phase-current entity keys (may be None).
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
    if has_grid_cts or any(
        get_entry_value(entry, CONF_BATTERY_SOC_ENTITY_ID, None)
        and get_entry_value(entry, CONF_BATTERY_POWER_ENTITY_ID, None)
        for entry in get_inverters_for_hub(hass, hub_entry_id)
    ):
        return
    errors["base"] = "battery_required_no_cts"


# --- Inverter features: what an inverter entry declares it has ---------------

# Every option key each feature owns. The config flow shows a section only for
# a declared feature, and clears these keys when the feature is not declared;
# the migration infers the list for entries from before it existed.
INVERTER_FEATURE_KEYS = {
    INVERTER_FEATURE_SOLAR: (
        CONF_SOLAR_PRODUCTION_ENTITY_ID,
        CONF_SOLAR_FORECAST_DEVICE_IDS,
    ),
    INVERTER_FEATURE_BATTERY: (
        CONF_BATTERY_SOC_ENTITY_ID,
        CONF_BATTERY_POWER_ENTITY_ID,
        CONF_BATTERY_MAX_CHARGE_POWER,
        CONF_BATTERY_MAX_DISCHARGE_POWER,
        CONF_BATTERY_SOC_FULL,
        CONF_BATTERY_CAPACITY_KWH,
        CONF_BATTERY_CAPACITY_ENTITY_ID,
        CONF_BATTERY_CHARGE_TARGET_ENTITY_ID,
    ),
    INVERTER_FEATURE_BATTERY_CONTROL: (
        CONF_CHARGE_LIMIT_ENTITY_ID,
        CONF_CHARGE_LIMIT_UNIT,
        CONF_BATTERY_VOLTAGE_ENTITY_ID,
        CONF_BATTERY_NOMINAL_VOLTAGE,
        CONF_CHARGE_LIMIT_NORMAL,
        CONF_CHARGE_LIMIT_MINIMUM,
        CONF_CHARGE_CONTROL_INTERVAL,
        CONF_CHARGE_CONTROL_DEADBAND_W,
        CONF_SOC_LIMIT_ENTITY_IDS,
        CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
        CONF_SOC_LIMIT_SEMANTICS,
    ),
}

# Keys that hold a list: cleared to an empty list, never None, because their
# readers iterate them.
_INVERTER_LIST_KEYS = frozenset({CONF_SOLAR_FORECAST_DEVICE_IDS, CONF_SOC_LIMIT_ENTITY_IDS})


def infer_inverter_features(options: dict) -> list:
    """The feature list an entry from before the list existed must have meant.

    Solar when it names a production sensor or a forecast device; battery when
    it names an SOC or power entity; write-control when it names a charge
    register or SOC slots (which also implies the battery). Nothing is read
    from the numeric fields on purpose - they carry the form's defaults on
    every entry, which is the whole reason the list exists.
    """
    features = []
    if options.get(CONF_SOLAR_PRODUCTION_ENTITY_ID) or options.get(
        CONF_SOLAR_FORECAST_DEVICE_IDS
    ):
        features.append(INVERTER_FEATURE_SOLAR)
    has_battery = bool(
        options.get(CONF_BATTERY_SOC_ENTITY_ID) or options.get(CONF_BATTERY_POWER_ENTITY_ID)
    )
    has_control = bool(
        options.get(CONF_CHARGE_LIMIT_ENTITY_ID) or options.get(CONF_SOC_LIMIT_ENTITY_IDS)
    )
    if has_battery or has_control:
        features.append(INVERTER_FEATURE_BATTERY)
    if has_control:
        features.append(INVERTER_FEATURE_BATTERY_CONTROL)
    return features


def strip_unfeatured_inverter_options(options: dict, features, *, clear_all=False) -> dict:
    """Clear every key of every feature NOT in ``features`` - in place, and
    returned. Entities and numbers go to None, lists to [] (their readers
    iterate). By default only keys the dict actually holds are touched, so a
    migration adds nothing; ``clear_all`` writes every undeclared key, which
    is what the options flow needs - its page dict never held the fields it
    did not show, and ``_save`` merges that page onto the stored options,
    where a missing key would leave the old value standing. None rather than
    a deletion for the same reason."""
    declared = set(features or ())
    for feature, keys in INVERTER_FEATURE_KEYS.items():
        if feature in declared:
            continue
        for key in keys:
            if clear_all or key in options:
                options[key] = [] if key in _INVERTER_LIST_KEYS else None
    return options


def inverter_features(entry: ConfigEntry) -> list:
    """The declared feature list of an inverter entry, inferred when the entry
    predates the list (a not-yet-migrated entry, or one seeded in a test)."""
    declared = get_entry_value(entry, CONF_INVERTER_FEATURES, None)
    if declared is not None:
        return list(declared)
    return infer_inverter_features({**entry.data, **entry.options})


# The wiring-topology setting is gone (2.9); the literal survives only so old
# hubs and not-yet-migrated inverter entries can still be read and stripped.
LEGACY_WIRING_TOPOLOGY = "wiring_topology"
_LEGACY_SERIES = "series"


# The legacy hub-level fields the auto-import moves onto an inverter entry:
# everything physically attached to the inverter, its PV array included.
# Site policy stays behind on the hub: SOC hysteresis, the SOC target/min
# sliders, base consumption, forecast SOC floor and the grid export limit.
# Nothing reads these off a hub at runtime any more - the import is the
# only way an old hub's values reach the engine. CONF_SOLAR_FORECAST_
# ENTITY_IDS (a direct sensor list that lived on dev for under an hour on
# 2026-08-14, before the device selector replaced it) is read nowhere; it
# stays listed only so the import strips it from a hub.
HUB_INVERTER_IMPORT_FIELDS = (
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_SUPPORTS_ASYMMETRIC,
    LEGACY_WIRING_TOPOLOGY,
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


def legacy_topology_to_input(hass, hub_entry_id, entry_id, options: dict) -> None:
    """Turn a legacy ``wiring_topology`` in ``options`` into the input setting,
    in place: the key is popped, and ``series`` becomes *the grid flows through
    this inverter* (``inverter_input_is_grid``), ``parallel`` or absent no
    input. Idempotent - a rerun finds no key and changes nothing.

    One tick per hub (the grid meter can only be one inverter's input): it
    goes to the series member with the lowest entry_id. A sibling takes it
    when it already holds the tick, or still holds legacy ``series`` with a
    lower entry_id - deterministic whatever order Home Assistant migrates the
    entries in. ``entry_id`` is None for an entry being imported (no id yet):
    any sibling that holds or would hold the tick then takes it.

    A PV inverter beside a hybrid (a parallel member next to a series one) has
    no exact equivalent - the old composite was a guess - so migrating the
    lowest-id parallel member of such a hub logs one warning.
    """
    legacy = options.pop(LEGACY_WIRING_TOPOLOGY, None)
    siblings = [
        (sibling.entry_id, {**sibling.data, **sibling.options})
        for sibling in (get_inverters_for_hub(hass, hub_entry_id) if hub_entry_id else ())
        if sibling.entry_id != entry_id
    ]

    def _is_series(cfg) -> bool:
        return bool(cfg.get(CONF_INVERTER_INPUT_IS_GRID)) or (
            cfg.get(LEGACY_WIRING_TOPOLOGY) == _LEGACY_SERIES
        )

    if legacy == _LEGACY_SERIES:
        for sibling_id, cfg in siblings:
            if cfg.get(CONF_INVERTER_INPUT_IS_GRID) or (
                cfg.get(LEGACY_WIRING_TOPOLOGY) == _LEGACY_SERIES
                and (entry_id is None or sibling_id < entry_id)
            ):
                return
        options[CONF_INVERTER_INPUT_IS_GRID] = True
    elif entry_id is not None and any(_is_series(cfg) for _, cfg in siblings):
        if all(entry_id < sibling_id for sibling_id, cfg in siblings if not _is_series(cfg)):
            hub = hass.config_entries.async_get_entry(hub_entry_id)
            _LOGGER.warning(
                "%s: a PV inverter beside a hybrid: if it feeds the grid side, "
                "give the hybrid its input sensors",
                hub.title if hub is not None else hub_entry_id,
            )


def strip_hub_inverter_fields(hass, hub_entry: ConfigEntry) -> None:
    """Drop the legacy inverter fields from a hub entry - the import's own
    trigger among them (see ``_setup_hub_entry``). No-op when it has none."""
    hass.config_entries.async_update_entry(
        hub_entry,
        data={k: v for k, v in hub_entry.data.items() if k not in HUB_INVERTER_IMPORT_FIELDS},
        options={k: v for k, v in hub_entry.options.items() if k not in HUB_INVERTER_IMPORT_FIELDS},
    )
