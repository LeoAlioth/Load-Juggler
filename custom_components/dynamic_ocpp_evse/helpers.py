"""Helper utilities for Load Juggler integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_HUB_ENTRY_ID,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_INVERTER,
    CONF_BATTERY_CAPACITY_KWH,
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


def hub_has_battery(hass, hub_entry: ConfigEntry) -> bool:
    """True when any battery exists on this hub's fleet — on the hub's own
    (legacy) battery fields, or on any inverter entry linked to it.

    The single gate for every battery-dependent hub entity (SOC sensors and
    sliders, the Allow Grid Charging switch), shared across the sensor,
    number and switch platforms so they cannot drift apart.
    """
    if get_entry_value(hub_entry, CONF_BATTERY_SOC_ENTITY_ID, None) or get_entry_value(
        hub_entry, CONF_BATTERY_POWER_ENTITY_ID, None
    ):
        return True
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (
            entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
            and entry.data.get(CONF_HUB_ENTRY_ID) == hub_entry.entry_id
            and (
                get_entry_value(entry, CONF_BATTERY_SOC_ENTITY_ID, None)
                or get_entry_value(entry, CONF_BATTERY_POWER_ENTITY_ID, None)
            )
        ):
            return True
    return False


def fleet_has_forecast_sources(hass, hub_entry: ConfigEntry) -> bool:
    """True when any PV forecast source is configured on this hub's fleet.

    Forecast devices belong to the inverter whose array they model, but
    clipping is a site-level question, so the hub's forecast sensors light up
    as soon as ANY member has one. The hub's own (legacy) fields count until
    the auto-import moves them onto an inverter entry.
    """
    entries = [hub_entry] + [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
        and entry.data.get(CONF_HUB_ENTRY_ID) == hub_entry.entry_id
    ]
    return any(
        get_entry_value(entry, CONF_SOLAR_FORECAST_DEVICE_IDS, None)
        or get_entry_value(entry, CONF_SOLAR_FORECAST_ENTITY_IDS, None)
        for entry in entries
    )


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
    grid_data: dict,
    battery_data: dict,
    errors: dict[str, str],
    hass=None,
    hub_entry_id: str | None = None,
) -> None:
    """Require a battery on hubs with no grid CTs (hard block).

    A hub with no grid CT entities runs off-grid: the battery SOC drives the
    mode logic and battery power drives off-grid solar-surplus detection, so
    both entities are mandatory — on the hub itself, or (when ``hass`` and
    ``hub_entry_id`` are given, i.e. the hub already exists) on any inverter
    entry linked to it. Adds an error to ``errors`` in-place.

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
        # A battery on a linked inverter entry satisfies the requirement —
        # after the auto-import that is where the battery normally lives.
        if hass is not None and hub_entry_id:
            for entry in hass.config_entries.async_entries(DOMAIN):
                if (
                    entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
                    and entry.data.get(CONF_HUB_ENTRY_ID) == hub_entry_id
                    and get_entry_value(entry, CONF_BATTERY_SOC_ENTITY_ID, None)
                    and get_entry_value(entry, CONF_BATTERY_POWER_ENTITY_ID, None)
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
    from the numeric fields on purpose — they carry the form's defaults on
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
    """Clear every key of every feature NOT in ``features`` — in place, and
    returned. Entities and numbers go to None, lists to [] (their readers
    iterate). By default only keys the dict actually holds are touched, so a
    migration adds nothing; ``clear_all`` writes every undeclared key, which
    is what the options flow needs — its page dict never held the fields it
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
