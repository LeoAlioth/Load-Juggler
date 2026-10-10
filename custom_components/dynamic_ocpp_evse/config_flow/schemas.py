"""Load Juggler - the config-flow schema builders, as plain functions.

Every voluptuous schema the flow shows the user is built here: the hub, grid,
battery and inverter forms (and the field-list builders they compose), the
per-device-type forms for EVSE / plug / hot water tank / power station, and the
entity-selector plumbing behind them.

Functions rather than a mixin: the only handler state a builder ever read was
``hass``, for the entity registry, so it is simply a parameter - and only on
the builders that offer entity selectors. The rest take nothing but their
``defaults`` dict. That is what lets the create flow and the options flow each
call them directly instead of one borrowing the other's handler instance.

Every builder takes the form's ``defaults`` and returns either a field list
(``_build_*``, for the pages that compose several groups) or a ``vol.Schema``.
"""
import voluptuous as vol
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.selector import selector
from ..const import (
    CHARGE_LIMIT_UNIT_AMPS,
    CHARGE_LIMIT_UNIT_WATTS,
    CHARGE_RATE_UNIT_AMPS,
    CHARGE_RATE_UNIT_WATTS,
    CONF_AUTO_DETECT_PHASE_MAPPING,
    CONF_BASE_CONSUMPTION,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CAPACITY_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_BATTERY_SOC_FREEZE_FLOOR,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_LOAD_PRIORITY,
    CONF_CHARGE_CONTROL_DEADBAND_W,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_MINIMUM,
    CONF_CHARGE_LIMIT_NORMAL,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_CHARGE_PAUSE_DURATION,
    CONF_CHARGE_RATE_UNIT,
    CONF_CLIMATE_ENTITY_ID,
    CONF_CONNECTED_TO_PHASE,
    CONF_ENABLE_MAX_IMPORT_POWER,
    CONF_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EXCESS_HYSTERESIS,
    CONF_EXCESS_TRIGGER_MARGIN,
    CONF_FORECAST_SOC_FLOOR,
    CONF_GRID_EXPORT_LIMIT,
    CONF_HEATING_ELEMENT_POWER,
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
    CONF_OCPP_PROFILE_TIMEOUT,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_BINARY_MIN_OFF_TIME,
    CONF_PLUG_MAX_CURRENT,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
    CONF_PLUG_POWER_RATING,
    CONF_PLUG_RESTART_AFTER,
    CONF_PLUG_RESTART_ON_NO_DRAW,
    CONF_PLUG_FINISH_BELOW_W,
    CONF_PLUG_FINISH_FOR,
    CONF_PLUG_SWITCH_ENTITY_ID,
    CONF_PROFILE_VALIDITY_MODE,
    # The Filters page: its keys, and the constants that are its defaults.
    CONF_FILTER_CTRL_FAST_TAU_S,
    CONF_FILTER_DEAD_BAND,
    CONF_FILTER_INPUT_TAU_S,
    CONF_FILTER_PERMIT_TAU_S,
    CONF_FILTER_RAMP_DOWN_RATE,
    CONF_FILTER_RAMP_TAU_S,
    CONF_FILTER_RAMP_UP_RATE,
    CONF_FILTER_SETTLE_SECONDS,
    CTRL_FAST_TAU_S,
    DEAD_BAND,
    EMA_TAU_S,
    PERMIT_TAU_S,
    RAMP_DOWN_RATE,
    RAMP_TAU_S,
    RAMP_UP_RATE,
    SETTLE_DRAW_SECONDS,
    CONF_SITE_UPDATE_FREQUENCY,
    CONF_SOC_LIMIT_ENTITY_IDS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CONF_SOC_LIMIT_SEMANTICS,
    DEFAULT_SOC_LIMIT_SEMANTICS,
    SOC_LIMIT_SEMANTICS_CEILING,
    SOC_LIMIT_SEMANTICS_FLOOR,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_GRACE_PERIOD,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_STACK_LEVEL,
    CONF_STATION_AC_INPUT_ENTITY_ID,
    CONF_STATION_AC_OUTPUT_ENTITY_ID,
    CONF_STATION_BATTERY_LEVEL_ENTITY_ID,
    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
    CONF_STATION_CHARGE_SPEED_ENTITY_ID,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_STATION_NORMAL_RESERVE,
    CONF_STATION_RESERVE_ENTITY_ID,
    CONF_STATION_STORM_RESERVE,
    CONF_TANK_POWER_ENTITY_ID,
    CONF_TANK_OFF_OPERATION_MODE,
    CONF_TANK_PRIORITIZE_BELOW_NORMAL,
    CONF_UPDATE_FREQUENCY,
    CONF_WIRING_TOPOLOGY,
    DEFAULT_BASE_CONSUMPTION,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_BATTERY_MAX_POWER,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_BATTERY_SOC_FREEZE_FLOOR,
    DEFAULT_LOAD_PRIORITY,
    DEFAULT_CHARGE_CONTROL_DEADBAND_W,
    DEFAULT_CHARGE_CONTROL_INTERVAL,
    DEFAULT_CHARGE_LIMIT_MINIMUM,
    DEFAULT_CHARGE_LIMIT_NORMAL,
    DEFAULT_CHARGE_LIMIT_UNIT,
    DEFAULT_CHARGE_PAUSE_DURATION,
    DEFAULT_EXCESS_HYSTERESIS,
    DEFAULT_EXCESS_TRIGGER_MARGIN,
    DEFAULT_FORECAST_SOC_FLOOR,
    DEFAULT_GRID_EXPORT_LIMIT,
    DEFAULT_HEATING_ELEMENT_POWER,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_OCPP_PROFILE_TIMEOUT,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_BINARY_MIN_OFF_TIME,
    DEFAULT_PLUG_MAX_CURRENT,
    DEFAULT_PLUG_POWER_RATING,
    DEFAULT_PLUG_RESTART_AFTER,
    DEFAULT_PLUG_FINISH_BELOW_W,
    DEFAULT_PLUG_FINISH_FOR,
    DEFAULT_PLUG_RESTART_ON_NO_DRAW,
    DEFAULT_PROFILE_VALIDITY_MODE,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DEFAULT_SOLAR_GRACE_PERIOD,
    DEFAULT_STACK_LEVEL,
    DEFAULT_STATION_MAX_CHARGE_POWER,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DEFAULT_STATION_NORMAL_RESERVE,
    DEFAULT_STATION_STORM_RESERVE,
    DEFAULT_TANK_PRIORITIZE_BELOW_NORMAL,
    DEFAULT_UPDATE_FREQUENCY,
    DEFAULT_WIRING_TOPOLOGY,
    FIELD_OCPP_DEVICE,
    OCPP_INTEGRATION_DOMAIN,
    PROFILE_VALIDITY_MODE_ABSOLUTE,
    PROFILE_VALIDITY_MODE_RELATIVE,
    STATION_CHARGE_POWER_MAX,
    STATION_CHARGE_POWER_STEP,
    WIRING_TOPOLOGY_PARALLEL,
    WIRING_TOPOLOGY_SERIES,
    CONF_INVERTER_FEATURES,
    INVERTER_FEATURE_SOLAR,
    INVERTER_FEATURES,
)
from ..helpers import normalize_optional_entity
from .helpers import (
    _CURRENT_UNITS,
    _POWER_UNITS,
    _SOC_UNITS,
    _ENERGY_UNITS,
    _VOLTAGE_UNITS,
)


# The site phases a LOAD may occupy, in the order every picker shows them.
#
# ONE list, because there were three: the plug's and the tank's carried all
# seven masks while the station's carried only the three single-phase ones -
# even though ``_build_power_station_load`` has always derived
# ``phases = len(connected_to_phase)`` and ``_phase_draw`` has always spread a
# draw across whatever mask it is given. A picker offering less than the engine
# supports is a picker bug, not a limit.
#
# Labels are derived from the masks so the two cannot drift. Distinct from the
# charger LEG mapping in _charger_current_schema, which is one phase per leg.
PHASE_MASK_OPTIONS = [
    {"value": mask, "label": f"Phase {'+'.join(mask)}"}
    for mask in ("A", "B", "C", "AB", "BC", "AC", "ABC")
]


def _entity_ids_for(
    hass,
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
    registry = async_get_entity_registry(hass)
    result = []
    for state in hass.states.async_all():
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


def _optional_entity_field(key: str, defaults: dict):
    """vol.Optional with the stored value as suggested_value, so the user can
    truly clear it.

    Using suggested_value instead of default lets the selector be cleared
    with X - vol.Optional(default=...) would silently re-fill the default on
    clear.
    """
    val = normalize_optional_entity(defaults.get(key))
    if val:
        return vol.Optional(key, description={"suggested_value": val})
    return vol.Optional(key)


def _entity_sel(hass, device_classes: set, valid_units, domains=None):
    """An entity selector offering what _entity_ids_for picks."""
    return selector(
        {
            "entity": {
                "include_entities": _entity_ids_for(
                    hass, device_classes, valid_units, domains
                ),
            }
        }
    )


def _num(key, defaults, default, lo, hi, step, unit, mode="box", required=False):
    """One number field: ``defaults.get(key, default)`` in a lo..hi selector.

    ``unit`` None leaves the selector unitless; ``required`` makes the marker
    vol.Required (vol.Optional otherwise).
    """
    number = {"min": lo, "max": hi, "step": step, "mode": mode}
    if unit is not None:
        number["unit_of_measurement"] = unit
    marker = vol.Required if required else vol.Optional
    return marker(key, default=defaults.get(key, default)), selector({"number": number})


def _identity_fields(defaults: dict, name: str, entity_id: str) -> list[tuple]:
    """The name and entity-id prefix every create page opens with."""
    return [
        (vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, name)), str),
        (
            vol.Required(CONF_ENTITY_ID, default=defaults.get(CONF_ENTITY_ID, entity_id)),
            str,
        ),
    ]


def _build_hub_grid_schema(hass, defaults: dict | None = None) -> list[tuple]:
    """Build grid/electrical fields as a reusable list."""
    defaults = defaults or {}
    current_power = _entity_sel(
        hass, {None, "current", "power"}, _CURRENT_UNITS | _POWER_UNITS
    )
    return [
        *(
            (_optional_entity_field(key, defaults), current_power)
            for key in (
                CONF_PHASE_A_CURRENT_ENTITY_ID,
                CONF_PHASE_B_CURRENT_ENTITY_ID,
                CONF_PHASE_C_CURRENT_ENTITY_ID,
            )
        ),
        _num(
            CONF_MAIN_BREAKER_RATING, defaults, DEFAULT_MAIN_BREAKER_RATING,
            1, 200, 1, "A", required=True,
        ),
        (
            vol.Required(
                CONF_INVERT_PHASES, default=defaults.get(CONF_INVERT_PHASES, False)
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
            _optional_entity_field(CONF_MAX_IMPORT_POWER_ENTITY_ID, defaults),
            _entity_sel(
                hass, {None, "power"}, _POWER_UNITS, ["sensor", "input_number"]
            ),
        ),
        _num(
            CONF_PHASE_VOLTAGE, defaults, DEFAULT_PHASE_VOLTAGE,
            100, 400, 1, "V", required=True,
        ),
        # The ONE export number: the site's physical/contract ceiling.
        # The Excess trigger derives from it (limit − trigger margin)
        # and the PV clipping forecast integrates above it. 0 = no
        # limit: grid-side Excess never triggers, forecast off.
        _num(CONF_GRID_EXPORT_LIMIT, defaults, DEFAULT_GRID_EXPORT_LIMIT, 0, 50000, 100, "W"),
        _num(
            CONF_EXCESS_TRIGGER_MARGIN, defaults, DEFAULT_EXCESS_TRIGGER_MARGIN,
            0, 5000, 50, "W",
        ),
        # The release band, not the trigger point: once Excess is
        # engaged the surplus may fall this far below the trigger
        # before an engaged load lets go.
        _num(CONF_EXCESS_HYSTERESIS, defaults, DEFAULT_EXCESS_HYSTERESIS, 0, 5000, 50, "W"),
        _num(
            CONF_SITE_UPDATE_FREQUENCY, defaults, DEFAULT_SITE_UPDATE_FREQUENCY,
            1, 60, 1, "s",
        ),
        (
            vol.Required(
                CONF_AUTO_DETECT_PHASE_MAPPING,
                default=defaults.get(CONF_AUTO_DETECT_PHASE_MAPPING, True),
            ),
            bool,
        ),
        _num(
            CONF_SOLAR_GRACE_PERIOD, defaults, DEFAULT_SOLAR_GRACE_PERIOD,
            0, 30, 1, "min", required=True,
        ),
        # --- Site policy ---
        # Hardware (inverters, batteries, PV arrays and their forecasts)
        # lives on the inverter entries; what stays here is site-wide
        # policy applied to whatever fleet those entries form.
        _num(
            CONF_BATTERY_SOC_HYSTERESIS, defaults, DEFAULT_BATTERY_SOC_HYSTERESIS,
            1, 10, 1, "%", mode="slider",
        ),
        _num(
            CONF_BATTERY_SOC_FREEZE_FLOOR, defaults, DEFAULT_BATTERY_SOC_FREEZE_FLOOR,
            0, 50, 1, "%", mode="slider",
        ),
        _num(CONF_BASE_CONSUMPTION, defaults, DEFAULT_BASE_CONSUMPTION, 0, 10000, 50, "W"),
        _num(
            CONF_FORECAST_SOC_FLOOR, defaults, DEFAULT_FORECAST_SOC_FLOOR,
            0, 90, 1, "%", mode="slider",
        ),
    ]


def _build_inverter_solar_schema(hass, defaults: dict | None = None) -> list[tuple]:
    """PV fields for an INVERTER entry - the array behind this inverter.

    Its production sensor and its Open-Meteo forecast device(s) belong to
    the inverter, not the site: a hybrid and an AC-coupled string inverter
    each have their own array. The engine sums the fleet's production and
    merges the fleet's forecast devices into ONE site forecast, because
    clipping is decided by the site-wide export limit.
    """
    defaults = defaults or {}
    return [
        (
            _optional_entity_field(CONF_SOLAR_PRODUCTION_ENTITY_ID, defaults),
            _entity_sel(hass, {None, "power"}, _POWER_UNITS),
        ),
        (
            # One forecast DEVICE per PV array - the Open-Meteo Solar
            # Forecast integration creates one device per array, and
            # several of its sensors carry the same watts series, so
            # letting the user pick sensors risks double-counting.
            vol.Optional(
                CONF_SOLAR_FORECAST_DEVICE_IDS,
                # suggested_value, NOT default - same clearing rule as
                # CONF_SOC_LIMIT_ENTITY_IDS (_normalize_list).
                description={
                    "suggested_value": defaults.get(CONF_SOLAR_FORECAST_DEVICE_IDS)
                    or []
                },
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


def _build_hub_inverter_schema(hass, defaults: dict | None = None) -> list[tuple]:
    """Build inverter configuration fields as a reusable list."""
    defaults = defaults or {}
    current_power = _entity_sel(
        hass, {None, "current", "power"}, _CURRENT_UNITS | _POWER_UNITS
    )
    # "0 means not configured" is STORED as None (_normalize_inverter_power_caps),
    # and dict.get's fallback does not cover a key that exists holding None -
    # while voluptuous validates defaults, so a None default fails the
    # NumberSelector the moment the field is left empty. `or 0` restores the
    # None↔0 round-trip.
    caps = {
        key: defaults.get(key) or 0
        for key in (CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE)
    }
    topology_options = [
        {
            "value": WIRING_TOPOLOGY_PARALLEL,
            "label": "Parallel (AC-coupled / no battery)",
        },
        {"value": WIRING_TOPOLOGY_SERIES, "label": "Series (Hybrid / battery)"},
    ]
    return [
        _num(CONF_INVERTER_MAX_POWER, caps, 0, 0, 50000, 100, "W"),
        _num(CONF_INVERTER_MAX_POWER_PER_PHASE, caps, 0, 0, 20000, 100, "W"),
        (
            vol.Required(
                CONF_INVERTER_SUPPORTS_ASYMMETRIC,
                default=defaults.get(CONF_INVERTER_SUPPORTS_ASYMMETRIC, False),
            ),
            bool,
        ),
        *(
            (_optional_entity_field(key, defaults), current_power)
            for key in (
                CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
                CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
                CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
            )
        ),
        (
            vol.Required(
                CONF_WIRING_TOPOLOGY,
                default=defaults.get(CONF_WIRING_TOPOLOGY, DEFAULT_WIRING_TOPOLOGY),
            ),
            selector({"select": {"options": topology_options, "mode": "dropdown"}}),
        ),
    ]


def _build_inverter_battery_schema(hass, defaults: dict | None = None) -> list[tuple]:
    """Battery fields for an INVERTER entry - the battery physically behind
    this inverter. Reuses the hub-level key names (see ENTRY_TYPE_INVERTER
    in const/common.py), but deliberately excludes the hub-policy fields
    (SOC target/min sliders, hysteresis) and the hub-scoped solar
    production / forecast inputs."""
    defaults = defaults or {}
    return [
        (
            _optional_entity_field(CONF_BATTERY_SOC_ENTITY_ID, defaults),
            _entity_sel(hass, {None, "battery"}, _SOC_UNITS),
        ),
        (
            _optional_entity_field(CONF_BATTERY_POWER_ENTITY_ID, defaults),
            _entity_sel(hass, {None, "power"}, _POWER_UNITS),
        ),
        _num(
            CONF_BATTERY_MAX_CHARGE_POWER, defaults, DEFAULT_BATTERY_MAX_POWER,
            0, 50000, 100, "W",
        ),
        _num(
            CONF_BATTERY_MAX_DISCHARGE_POWER, defaults, DEFAULT_BATTERY_MAX_POWER,
            0, 50000, 100, "W",
        ),
        _num(
            CONF_BATTERY_CAPACITY_KWH, defaults, DEFAULT_BATTERY_CAPACITY_KWH,
            0, 1000, 0.1, "kWh",
        ),
        (
            _optional_entity_field(CONF_BATTERY_CAPACITY_ENTITY_ID, defaults),
            _entity_sel(
                hass, {None, "energy_storage"}, _ENERGY_UNITS, ["sensor", "input_number"]
            ),
        ),
    ]


def _build_inverter_control_schema(hass, defaults: dict | None = None) -> list[tuple]:
    """Write-control fields for an INVERTER entry - optional throughout.

    With no target entity the inverter stays advisory: the forecast's
    recommended charge limit is published as a sensor and nothing is
    written. Naming a register adds the opt-in switch that starts writes.

    Two independent controls share this page - the charge RATE (one register)
    and the SOC CEILING (a list of time-of-use slot entities). Each gets its
    own switch, and configuring one does not imply the other.
    """
    defaults = defaults or {}
    return [
        (
            _optional_entity_field(CONF_CHARGE_LIMIT_ENTITY_ID, defaults),
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
            _optional_entity_field(CONF_BATTERY_VOLTAGE_ENTITY_ID, defaults),
            _entity_sel(hass, {None, "voltage"}, _VOLTAGE_UNITS),
        ),
        _num(
            CONF_BATTERY_NOMINAL_VOLTAGE, defaults, DEFAULT_BATTERY_NOMINAL_VOLTAGE,
            12, 1000, 0.1, "V",
        ),
        _num(
            CONF_CHARGE_LIMIT_NORMAL, defaults, DEFAULT_CHARGE_LIMIT_NORMAL,
            0, 1000, 1, None,
        ),
        # The floor under the ENGAGED limit, in the same own-units
        # convention as the normal above: a plain number, not an entity, so
        # no ENTITY_UNIT_CONTRACTS entry applies. 0 is "no floor" and is
        # exactly the behaviour before this field existed.
        _num(
            CONF_CHARGE_LIMIT_MINIMUM, defaults, DEFAULT_CHARGE_LIMIT_MINIMUM,
            0, 1000, 1, None,
        ),
        _num(
            CONF_CHARGE_CONTROL_INTERVAL, defaults, DEFAULT_CHARGE_CONTROL_INTERVAL,
            30, 3600, 30, "s",
        ),
        _num(
            CONF_CHARGE_CONTROL_DEADBAND_W, defaults, DEFAULT_CHARGE_CONTROL_DEADBAND_W,
            0, 2000, 10, "W",
        ),
        (
            # MULTI-select on purpose: on a Deye the SOC ceiling is one
            # entity per time-of-use slot, so controlling the ceiling means
            # writing every slot the battery may charge in. input_number is
            # offered beside number because a user may front the slots with
            # their own helper.
            vol.Optional(
                CONF_SOC_LIMIT_ENTITY_IDS,
                # suggested_value, NOT default: a default is injected at
                # validation time, so a cleared multi-select (the frontend
                # omits the key entirely) would resurrect the stored list
                # and clearing would be impossible. The save paths map the
                # absent key to [] (_normalize_list).
                description={
                    "suggested_value": defaults.get(CONF_SOC_LIMIT_ENTITY_IDS)
                    or []
                },
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
            # whatever already owns the slots keeps owning them - we only
            # ever push below it. sensor is allowed too: a template sensor
            # deriving the ceiling from a schedule is a normal way to do it.
            _optional_entity_field(CONF_SOC_LIMIT_NORMAL_ENTITY_ID, defaults),
            selector(
                {"entity": {"domain": ["input_number", "number", "sensor"]}}
            ),
        ),
        (
            # What a WRITE to the entities above means on this hardware - the
            # flag the floor-aware SOC fan-out keys on. It never changes what
            # is READ: the destination always comes from the ceiling source
            # when that is set (a floor whose value is not the target leaves
            # the source unset instead). See const/inverter.py.
            vol.Required(
                CONF_SOC_LIMIT_SEMANTICS,
                default=defaults.get(
                    CONF_SOC_LIMIT_SEMANTICS, DEFAULT_SOC_LIMIT_SEMANTICS
                ),
            ),
            selector(
                {
                    "select": {
                        "options": [
                            {
                                "value": SOC_LIMIT_SEMANTICS_CEILING,
                                "label": "Charge ceiling - the battery stops charging there",
                            },
                            {
                                "value": SOC_LIMIT_SEMANTICS_FLOOR,
                                "label": "Discharge floor - grid-defense level (Deye TOU slot)",
                            },
                        ],
                        "mode": "dropdown",
                    }
                }
            ),
        ),
    ]


def _inverter_features_schema(defaults: dict | None = None) -> vol.Schema:
    """The first inverter page: what this inverter HAS.

    A multi-select of feature slugs (labels via the ``inverter_features``
    selector translation). ``suggested_value`` rather than ``default``: a list
    the user empties is omitted by the frontend, and a default would silently
    put the old list back (the same clearing rule as the SOC slots).
    """
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_INVERTER_FEATURES,
                description={
                    "suggested_value": list(defaults.get(CONF_INVERTER_FEATURES) or [])
                },
            ): selector(
                {
                    "select": {
                        "options": list(INVERTER_FEATURES),
                        "multiple": True,
                        "mode": "list",
                        "translation_key": "inverter_features",
                    }
                }
            ),
        }
    )


def _inverter_config_schema(hass, defaults: dict | None, features):
    """The inverter's own page on setup: the AC side, plus the PV section when
    the solar feature is declared. Returns the field list (the create step
    adds name and entity id in front of it)."""
    fields = _build_hub_inverter_schema(hass, defaults)
    if INVERTER_FEATURE_SOLAR in features:
        fields.extend(_build_inverter_solar_schema(hass, defaults))
    return fields


def _hub_grid_schema(hass, defaults: dict | None = None) -> vol.Schema:
    """The hub setup page: grid connection and site policy."""
    return vol.Schema(dict(_build_hub_grid_schema(hass, defaults)))


# The hub's options menu splits the grid page into four pages, one question
# each: how the site is wired to the grid, where the export wall and the
# Excess trigger sit, the fleet-wide battery/forecast policy, and the engine's
# timing. The setup wizard keeps the one page (_hub_grid_schema).
HUB_CONNECTION_KEYS = (
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_INVERT_PHASES,
    CONF_MAIN_BREAKER_RATING,
    CONF_PHASE_VOLTAGE,
    CONF_ENABLE_MAX_IMPORT_POWER,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
)
HUB_EXPORT_KEYS = (
    CONF_GRID_EXPORT_LIMIT,
    CONF_EXCESS_TRIGGER_MARGIN,
    CONF_EXCESS_HYSTERESIS,
)
HUB_POLICY_KEYS = (
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_BATTERY_SOC_FREEZE_FLOOR,
    CONF_BASE_CONSUMPTION,
    CONF_FORECAST_SOC_FLOOR,
)
HUB_TIMING_KEYS = (
    CONF_SITE_UPDATE_FREQUENCY,
    CONF_AUTO_DETECT_PHASE_MAPPING,
    CONF_SOLAR_GRACE_PERIOD,
)


def _hub_section_schema(hass, defaults, keys) -> vol.Schema:
    """One hub options page: the grid page's fields restricted to ``keys``,
    in the grid page's own order, so the two never drift apart."""
    wanted = set(keys)
    fields = [
        (marker, validator)
        for marker, validator in _build_hub_grid_schema(hass, defaults)
        if getattr(marker, "schema", None) in wanted
    ]
    assert len(fields) == len(wanted), "hub section keys drifted from the grid page"
    return vol.Schema(dict(fields))


def _hub_filters_schema(defaults: dict | None = None) -> vol.Schema:
    """The Filters page: the control pipeline's time constants and slews.

    Deliberately NOT drawn from ``_build_hub_grid_schema`` like the other hub
    pages: that list also builds the hub SETUP form (``_hub_grid_schema``), and
    eight filter dials have no business in front of someone wiring up their
    first hub. Options-only, reachable from the hub menu.

    Every default is the engine constant the dial overrides, so a hub that has
    never opened this page runs the constants exactly. The fast filter's step
    is "any": its default is the calibrated 1.2427 s, and a 0.1 grid would
    either flag that as invalid or nudge it to 1.2 on an open-and-save.
    """
    defaults = defaults or {}
    return vol.Schema(
        dict(
            [
                _num(CONF_FILTER_INPUT_TAU_S, defaults, EMA_TAU_S, 1, 60, 0.1, "s"),
                _num(CONF_FILTER_PERMIT_TAU_S, defaults, PERMIT_TAU_S, 1, 60, 0.1, "s"),
                _num(CONF_FILTER_RAMP_TAU_S, defaults, RAMP_TAU_S, 1, 60, 0.1, "s"),
                _num(
                    CONF_FILTER_CTRL_FAST_TAU_S, defaults, CTRL_FAST_TAU_S,
                    0.5, 60, "any", "s",
                ),
                _num(
                    CONF_FILTER_SETTLE_SECONDS, defaults, SETTLE_DRAW_SECONDS,
                    5, 300, 1, "s",
                ),
                _num(CONF_FILTER_DEAD_BAND, defaults, DEAD_BAND, 0, 5, 0.1, "A"),
                _num(
                    CONF_FILTER_RAMP_UP_RATE, defaults, RAMP_UP_RATE,
                    0.05, 5, 0.05, "A/s",
                ),
                _num(
                    CONF_FILTER_RAMP_DOWN_RATE, defaults, RAMP_DOWN_RATE,
                    0.05, 5, 0.05, "A/s",
                ),
            ]
        )
    )


def validate_hub_filters(data: dict, errors: dict) -> None:
    """The battery controller's fast filter must be FASTER than the site
    reading filter it pairs with. A longer tau on the "fast" half inverts the
    matched pair, and the feedback law then reads every transition backwards
    (see readers._smooth_directional)."""
    fast = data.get(CONF_FILTER_CTRL_FAST_TAU_S, CTRL_FAST_TAU_S)
    slow = data.get(CONF_FILTER_INPUT_TAU_S, EMA_TAU_S)
    if fast is not None and slow is not None and float(fast) >= float(slow):
        errors[CONF_FILTER_CTRL_FAST_TAU_S] = "fast_filter_not_below_input"


def _ocpp_device_field(defaults: dict) -> tuple:
    """The OCPP device picker both charger pages offer.

    A device picker rather than the free-text charge point id it replaces:
    the flow resolves the picked device back to the charge point id AND to the
    charger's sensor entities through the same derivation the discovery scan
    uses, so a charger matched to the wrong OCPP device is fixed by pointing
    at the right one instead of by typing an id nobody can check. Optional,
    and pre-filled through suggested_value (a default would silently re-fill
    the picker after the user clears it). Filtered to the ocpp integration,
    so it is empty (and skippable) when that integration is not the source -
    OCPP-shaped template sensors have no device to offer.
    """
    return (
        _optional_entity_field(FIELD_OCPP_DEVICE, defaults),
        selector({"device": {"integration": OCPP_INTEGRATION_DOMAIN}}),
    )


def _charger_info_schema(defaults: dict | None = None) -> vol.Schema:
    """Build schema for charger info step (name, entity ID, priority, OCPP device)."""
    defaults = defaults or {}
    return vol.Schema(
        dict(
            [
                *_identity_fields(defaults, "", ""),
                _num(
                    CONF_LOAD_PRIORITY, defaults, DEFAULT_LOAD_PRIORITY,
                    1, 10, 1, None, required=True,
                ),
                _ocpp_device_field(defaults),
            ]
        )
    )


def _charger_current_schema(
    defaults: dict | None = None, hub_phases: int = 3
) -> vol.Schema:
    """Build schema for charger current limits and phase mapping.

    Only shows L2/L3 phase mapping fields when the hub has 2+/3+ phases.
    """
    defaults = defaults or {}
    # One site phase per charger LEG - not a phase mask. L1/L2/L3 each land on
    # exactly one phase, so the multi-phase combinations that the load pickers
    # offer (PHASE_MASK_OPTIONS) would be meaningless here.
    leg_phase = selector(
        {
            "select": {
                "options": [
                    {"value": phase, "label": f"Phase {phase}"}
                    for phase in ("A", "B", "C")
                ],
                "mode": "dropdown",
            }
        }
    )
    legs = (
        (CONF_CHARGER_L1_PHASE, "A"),
        (CONF_CHARGER_L2_PHASE, "B"),
        (CONF_CHARGER_L3_PHASE, "C"),
    )
    return vol.Schema(
        dict(
            [
                _num(
                    CONF_EVSE_MINIMUM_CHARGE_CURRENT, defaults, DEFAULT_MIN_CHARGE_CURRENT,
                    6, 80, 1, "A", required=True,
                ),
                _num(
                    CONF_EVSE_MAXIMUM_CHARGE_CURRENT, defaults, DEFAULT_MAX_CHARGE_CURRENT,
                    6, 80, 1, "A", required=True,
                ),
                *(
                    (vol.Required(key, default=defaults.get(key, phase)), leg_phase)
                    for key, phase in legs[: max(hub_phases, 1)]
                ),
            ]
        )
    )


def _charger_timing_schema(
    defaults: dict | None = None, detected_unit: str | None = None
) -> vol.Schema:
    """Build schema for charger timing and unit configuration."""
    defaults = defaults or {}
    unit_options = [
        {"value": CHARGE_RATE_UNIT_AMPS, "label": "Amperes (A)"},
        {"value": CHARGE_RATE_UNIT_WATTS, "label": "Watts (W)"},
    ]

    # A stored unit wins over the detected one; with neither, no default.
    stored_unit = defaults.get(CONF_CHARGE_RATE_UNIT)
    if stored_unit in (CHARGE_RATE_UNIT_AMPS, CHARGE_RATE_UNIT_WATTS):
        unit_default = stored_unit
    else:
        unit_default = detected_unit
    charge_rate_field = (
        vol.Required(CONF_CHARGE_RATE_UNIT, default=unit_default)
        if unit_default
        else vol.Required(CONF_CHARGE_RATE_UNIT)
    )

    return vol.Schema(
        dict(
            [
                (
                    charge_rate_field,
                    selector({"select": {"options": unit_options, "mode": "dropdown"}}),
                ),
                (
                    vol.Required(
                        CONF_PROFILE_VALIDITY_MODE,
                        default=defaults.get(
                            CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE
                        ),
                    ),
                    selector(
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
                ),
                _num(
                    CONF_UPDATE_FREQUENCY, defaults, DEFAULT_UPDATE_FREQUENCY,
                    5, 300, 1, "s", required=True,
                ),
                _num(
                    CONF_OCPP_PROFILE_TIMEOUT, defaults, DEFAULT_OCPP_PROFILE_TIMEOUT,
                    30, 600, 1, "s", required=True,
                ),
                _num(
                    CONF_CHARGE_PAUSE_DURATION, defaults, DEFAULT_CHARGE_PAUSE_DURATION,
                    0, 10, 1, "min", required=True,
                ),
                _num(
                    CONF_STACK_LEVEL, defaults, DEFAULT_STACK_LEVEL,
                    0, 10, 1, None, required=True,
                ),
                _num(
                    CONF_SOLAR_GRACE_PERIOD, defaults, DEFAULT_SOLAR_GRACE_PERIOD,
                    0, 30, 1, "min", required=True,
                ),
            ]
        )
    )


def _plug_schema(defaults: dict | None = None) -> vol.Schema:
    """Build schema for smart load configuration."""
    defaults = defaults or {}
    return vol.Schema(
        dict(
            [
                (
                    vol.Required(
                        CONF_PLUG_SWITCH_ENTITY_ID,
                        default=defaults.get(CONF_PLUG_SWITCH_ENTITY_ID),
                    ),
                    selector({"entity": {"domain": "switch"}}),
                ),
                _num(
                    CONF_PLUG_POWER_RATING, defaults, DEFAULT_PLUG_POWER_RATING,
                    100, 25000, 100, "W", required=True,
                ),
                _num(
                    CONF_PLUG_MAX_CURRENT, defaults, DEFAULT_PLUG_MAX_CURRENT,
                    6, 63, 1, "A", required=True,
                ),
                (
                    vol.Required(
                        CONF_CONNECTED_TO_PHASE,
                        default=defaults.get(CONF_CONNECTED_TO_PHASE, "A"),
                    ),
                    selector({"select": {"options": PHASE_MASK_OPTIONS, "mode": "dropdown"}}),
                ),
                _num(
                    CONF_LOAD_PRIORITY, defaults, DEFAULT_LOAD_PRIORITY,
                    1, 10, 1, None, required=True,
                ),
                _num(
                    CONF_BINARY_MIN_OFF_TIME, defaults, DEFAULT_BINARY_MIN_OFF_TIME,
                    0, 60, 1, "min", required=True,
                ),
                (
                    _optional_entity_field(CONF_PLUG_POWER_MONITOR_ENTITY_ID, defaults),
                    selector({"entity": {"domain": ["sensor", "input_number"]}}),
                ),
                (
                    vol.Required(
                        CONF_PLUG_RESTART_ON_NO_DRAW,
                        default=defaults.get(
                            CONF_PLUG_RESTART_ON_NO_DRAW,
                            DEFAULT_PLUG_RESTART_ON_NO_DRAW,
                        ),
                    ),
                    selector({"boolean": {}}),
                ),
                _num(
                    CONF_PLUG_RESTART_AFTER, defaults, DEFAULT_PLUG_RESTART_AFTER,
                    1, 120, 1, "min", required=True,
                ),
                _num(
                    CONF_PLUG_FINISH_BELOW_W, defaults, DEFAULT_PLUG_FINISH_BELOW_W,
                    0, 5000, 1, "W", required=True,
                ),
                _num(
                    CONF_PLUG_FINISH_FOR, defaults, DEFAULT_PLUG_FINISH_FOR,
                    1, 120, 1, "min", required=True,
                ),
                _num(
                    CONF_UPDATE_FREQUENCY, defaults, DEFAULT_UPDATE_FREQUENCY,
                    5, 300, 1, "s", required=True,
                ),
                _num(
                    CONF_SOLAR_GRACE_PERIOD, defaults, DEFAULT_SOLAR_GRACE_PERIOD,
                    0, 30, 1, "min", required=True,
                ),
            ]
        )
    )


def _hot_water_tank_schema(defaults: dict | None = None) -> vol.Schema:
    """Build schema for hot water tank configuration."""
    defaults = defaults or {}
    return vol.Schema(
        dict(
            [
                (
                    vol.Required(
                        CONF_CLIMATE_ENTITY_ID,
                        default=defaults.get(CONF_CLIMATE_ENTITY_ID),
                    ),
                    selector({"entity": {"domain": ["climate", "water_heater"]}}),
                ),
                _num(
                    CONF_HEATING_ELEMENT_POWER, defaults, DEFAULT_HEATING_ELEMENT_POWER,
                    100, 25000, 100, "W", required=True,
                ),
                (
                    vol.Required(
                        CONF_TANK_PRIORITIZE_BELOW_NORMAL,
                        default=defaults.get(
                            CONF_TANK_PRIORITIZE_BELOW_NORMAL,
                            DEFAULT_TANK_PRIORITIZE_BELOW_NORMAL,
                        ),
                    ),
                    selector({"boolean": {}}),
                ),
                (
                    vol.Required(
                        CONF_CONNECTED_TO_PHASE,
                        default=defaults.get(CONF_CONNECTED_TO_PHASE, "A"),
                    ),
                    selector({"select": {"options": PHASE_MASK_OPTIONS, "mode": "dropdown"}}),
                ),
                _num(
                    CONF_LOAD_PRIORITY, defaults, DEFAULT_LOAD_PRIORITY,
                    1, 10, 1, None, required=True,
                ),
                _num(
                    CONF_BINARY_MIN_OFF_TIME, defaults, DEFAULT_BINARY_MIN_OFF_TIME,
                    0, 60, 1, "min", required=True,
                ),
                (
                    # Power-class sensors only (e.g. the smart relay feeding the
                    # element), plus any input_number standing in for one.
                    _optional_entity_field(CONF_TANK_POWER_ENTITY_ID, defaults),
                    selector(
                        {
                            "entity": {
                                "domain": ["sensor", "input_number"],
                                "filter": [
                                    {"domain": "sensor", "device_class": "power"},
                                    {"domain": "input_number"},
                                ],
                            }
                        }
                    ),
                ),
                (
                    # A water heater's own word for "off" (Vaillant: stand_by) -
                    # every integration has its own, so it is typed, not picked.
                    vol.Optional(
                        CONF_TANK_OFF_OPERATION_MODE,
                        description={"suggested_value": defaults.get(CONF_TANK_OFF_OPERATION_MODE, "")},
                    ),
                    selector({"text": {}}),
                ),
                _num(
                    CONF_UPDATE_FREQUENCY, defaults, DEFAULT_UPDATE_FREQUENCY,
                    5, 300, 1, "s", required=True,
                ),
                _num(
                    CONF_SOLAR_GRACE_PERIOD, defaults, DEFAULT_SOLAR_GRACE_PERIOD,
                    0, 30, 1, "min", required=True,
                ),
            ]
        )
    )


def _power_station_schema(defaults: dict | None = None) -> vol.Schema:
    """Build schema for portable power station configuration.

    The charge bounds are configured rather than read from the device, so a
    station whose hardware accepts more can be held below that. The reserve
    is the station's on/off gate - dropped below its current battery level it
    stops drawing from the wall - so both the day-to-day and the storm level
    are set here.
    """
    defaults = defaults or {}
    return vol.Schema(
        dict(
            [
                (
                    vol.Required(
                        CONF_STATION_CHARGE_SPEED_ENTITY_ID,
                        default=defaults.get(CONF_STATION_CHARGE_SPEED_ENTITY_ID),
                    ),
                    selector({"entity": {"domain": "number"}}),
                ),
                (
                    vol.Required(
                        CONF_STATION_RESERVE_ENTITY_ID,
                        default=defaults.get(CONF_STATION_RESERVE_ENTITY_ID),
                    ),
                    selector({"entity": {"domain": "number"}}),
                ),
                (
                    vol.Required(
                        CONF_STATION_BATTERY_LEVEL_ENTITY_ID,
                        default=defaults.get(CONF_STATION_BATTERY_LEVEL_ENTITY_ID),
                    ),
                    selector({"entity": {"domain": ["sensor", "input_number"]}}),
                ),
                (
                    _optional_entity_field(CONF_STATION_CHARGE_LIMIT_ENTITY_ID, defaults),
                    selector({"entity": {"domain": ["number", "sensor"]}}),
                ),
                (
                    _optional_entity_field(CONF_STATION_AC_INPUT_ENTITY_ID, defaults),
                    selector({"entity": {"domain": ["sensor", "input_number"]}}),
                ),
                (
                    _optional_entity_field(CONF_STATION_AC_OUTPUT_ENTITY_ID, defaults),
                    selector({"entity": {"domain": ["sensor", "input_number"]}}),
                ),
                _num(
                    CONF_STATION_MIN_CHARGE_POWER, defaults, DEFAULT_STATION_MIN_CHARGE_POWER,
                    0, STATION_CHARGE_POWER_MAX, STATION_CHARGE_POWER_STEP, "W",
                    required=True,
                ),
                _num(
                    CONF_STATION_MAX_CHARGE_POWER, defaults, DEFAULT_STATION_MAX_CHARGE_POWER,
                    0, STATION_CHARGE_POWER_MAX, STATION_CHARGE_POWER_STEP, "W",
                    required=True,
                ),
                _num(
                    CONF_STATION_NORMAL_RESERVE, defaults, DEFAULT_STATION_NORMAL_RESERVE,
                    0, 100, 1, "%", mode="slider", required=True,
                ),
                _num(
                    CONF_STATION_STORM_RESERVE, defaults, DEFAULT_STATION_STORM_RESERVE,
                    0, 100, 1, "%", mode="slider", required=True,
                ),
                (
                    vol.Required(
                        CONF_CONNECTED_TO_PHASE,
                        default=defaults.get(CONF_CONNECTED_TO_PHASE, "A"),
                    ),
                    selector({"select": {"options": PHASE_MASK_OPTIONS, "mode": "dropdown"}}),
                ),
                _num(
                    CONF_LOAD_PRIORITY, defaults, DEFAULT_LOAD_PRIORITY,
                    1, 10, 1, None, required=True,
                ),
                _num(
                    CONF_UPDATE_FREQUENCY, defaults, DEFAULT_UPDATE_FREQUENCY,
                    5, 300, 1, "s", required=True,
                ),
                _num(
                    CONF_SOLAR_GRACE_PERIOD, defaults, DEFAULT_SOLAR_GRACE_PERIOD,
                    0, 30, 1, "min", required=True,
                ),
            ]
        )
    )
