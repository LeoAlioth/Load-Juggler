"""Load Juggler - the config-flow schema builders, as one mixin.

Every voluptuous schema the flow shows the user is built here: the hub, grid,
battery and inverter forms (and the field-list builders they compose), the
per-device-type forms for EVSE / plug / hot water tank / power station, and the
entity-selector plumbing behind them.

A mixin rather than a module of functions so the builders keep reading the
handler state they always read — ``self.hass`` for the entity registry,
``self._data`` for what earlier steps collected — and so both flows keep
reaching them through the same handler instance.

Moved verbatim out of LoadJugglerConfigFlow.
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
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_SOC_FULL,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_CHARGER_PRIORITY,
    CONF_CHARGE_CONTROL_DEADBAND,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_CHARGE_LIMIT_ENTITY_ID,
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
    CONF_OCPP_DEVICE_ID,
    CONF_OCPP_PROFILE_TIMEOUT,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_PLUG_MAX_CURRENT,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
    CONF_PLUG_POWER_RATING,
    CONF_PLUG_SWITCH_ENTITY_ID,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_SITE_UPDATE_FREQUENCY,
    CONF_SOC_LIMIT_ENTITY_IDS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
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
    CONF_TANK_AWAY_TEMPERATURE,
    CONF_TANK_BOOST_TEMPERATURE,
    CONF_TANK_NORMAL_TEMPERATURE,
    CONF_TANK_POWER_DEVICE_ID,
    CONF_TANK_POWER_ENTITY_ID,
    CONF_TANK_PRIORITIZE_BELOW_NORMAL,
    CONF_UPDATE_FREQUENCY,
    CONF_WIRING_TOPOLOGY,
    DEFAULT_BASE_CONSUMPTION,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_BATTERY_MAX_POWER,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_BATTERY_SOC_FULL,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_CHARGER_PRIORITY,
    DEFAULT_CHARGE_CONTROL_DEADBAND,
    DEFAULT_CHARGE_CONTROL_INTERVAL,
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
    DEFAULT_PLUG_MAX_CURRENT,
    DEFAULT_PLUG_POWER_RATING,
    DEFAULT_PROFILE_VALIDITY_MODE,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DEFAULT_SOLAR_GRACE_PERIOD,
    DEFAULT_STACK_LEVEL,
    DEFAULT_STATION_MAX_CHARGE_POWER,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DEFAULT_STATION_NORMAL_RESERVE,
    DEFAULT_STATION_STORM_RESERVE,
    DEFAULT_TANK_AWAY_TEMPERATURE,
    DEFAULT_TANK_BOOST_TEMPERATURE,
    DEFAULT_TANK_NORMAL_TEMPERATURE,
    DEFAULT_TANK_PRIORITIZE_BELOW_NORMAL,
    DEFAULT_UPDATE_FREQUENCY,
    DEFAULT_WIRING_TOPOLOGY,
    PROFILE_VALIDITY_MODE_ABSOLUTE,
    PROFILE_VALIDITY_MODE_RELATIVE,
    STATION_CHARGE_POWER_STEP,
    WIRING_TOPOLOGY_PARALLEL,
    WIRING_TOPOLOGY_SERIES,
)
from ..helpers import normalize_optional_entity
from .helpers import (
    _CURRENT_UNITS,
    _POWER_UNITS,
    _SOC_UNITS,
    _VOLTAGE_UNITS,
)


class SchemaBuilderMixin:
    """The schema builders shared by the config flow and the options flow."""

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
                    # suggested_value, NOT default — same clearing rule as
                    # CONF_SOC_LIMIT_ENTITY_IDS (_normalize_forecast_list).
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
                    # suggested_value, NOT default — same clearing rule as
                    # CONF_SOC_LIMIT_ENTITY_IDS (_normalize_forecast_list).
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
                    # "0 means not configured" is STORED as None
                    # (_normalize_inverter_power_caps), and dict.get's fallback
                    # does not cover a key that exists holding None — while
                    # voluptuous validates defaults, so a None default fails
                    # the NumberSelector the moment the field is left empty.
                    # `or 0` restores the None↔0 round-trip.
                    default=defaults.get(CONF_INVERTER_MAX_POWER) or 0,
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
                    # Same None↔0 round-trip as CONF_INVERTER_MAX_POWER above.
                    default=defaults.get(CONF_INVERTER_MAX_POWER_PER_PHASE) or 0,
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
                    # suggested_value, NOT default: a default is injected at
                    # validation time, so a cleared multi-select (the frontend
                    # omits the key entirely) would resurrect the stored list
                    # and clearing would be impossible. The save paths map the
                    # absent key to [] (_normalize_soc_limit_list).
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
