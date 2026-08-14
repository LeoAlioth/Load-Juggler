import re
import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector
from homeassistant.helpers.entity_registry import (
    async_get as async_get_entity_registry,
    async_entries_for_device as er_async_entries_for_device,
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

_LOGGER = logging.getLogger(__name__)
_POWER_FACTOR = 0.9  # 90% of detected limit for safe headroom

_CURRENT_UNITS = frozenset({"A", "mA"})
_POWER_UNITS = frozenset({"W", "kW"})
_SOC_UNITS = frozenset({"%"})


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
        if not state or state.state in ("unavailable", "unknown"):
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


# --- Device-priority reordering (shared by the options and reconfigure flows) ---

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
        ]

    def _build_hub_battery_schema(
        self, defaults: dict | None = None, include_battery_hardware: bool = True
    ) -> list[tuple]:
        """Build battery fields as a reusable list (includes solar entity selector).

        ``include_battery_hardware=False`` keeps only the hub-scoped fields
        (solar production entity, SOC hysteresis, forecast inputs) — used once
        a hub's legacy battery hardware has been auto-imported onto an
        inverter entry, where it is edited from then on.
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
        if include_battery_hardware:
            return fields
        battery_hardware = {
            CONF_BATTERY_SOC_ENTITY_ID,
            CONF_BATTERY_POWER_ENTITY_ID,
            CONF_BATTERY_MAX_CHARGE_POWER,
            CONF_BATTERY_MAX_DISCHARGE_POWER,
            CONF_BATTERY_SOC_FULL,
            CONF_BATTERY_CAPACITY_KWH,
        }
        return [(marker, sel) for marker, sel in fields if marker.schema not in battery_hardware]

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

    def _inverter_battery_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Schema for the inverter-entry battery step."""
        return vol.Schema(dict(self._build_inverter_battery_schema(defaults)))

    def _inverter_combined_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Inverter + battery fields on one page (reconfigure/options flows)."""
        fields = self._build_hub_inverter_schema(defaults)
        fields.extend(self._build_inverter_battery_schema(defaults))
        return vol.Schema(dict(fields))

    def _hub_schema(
        self,
        defaults: dict | None = None,
        include_grid: bool = True,
        include_battery: bool = True,
        include_inverter: bool = False,
        include_battery_hardware: bool = True,
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
            fields_list.extend(
                self._build_hub_battery_schema(defaults, include_battery_hardware)
            )
        if include_inverter:
            fields_list.extend(self._build_hub_inverter_schema(defaults))

        return vol.Schema(dict(fields_list))

    def _hub_grid_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build schema with only grid/electrical fields."""
        return self._hub_schema(defaults, include_grid=True, include_battery=False)

    def _hub_battery_schema(
        self, defaults: dict | None = None, include_battery_hardware: bool = True
    ) -> vol.Schema:
        """Build schema with only battery fields."""
        return self._hub_schema(
            defaults,
            include_grid=False,
            include_battery=True,
            include_battery_hardware=include_battery_hardware,
        )

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
        if defaults.get("ocpp_device_id"):
            fields[
                vol.Optional(
                    "ocpp_device_id",
                    default=defaults.get("ocpp_device_id", ""),
                )
            ] = str

        return vol.Schema(fields)

    def _get_hub_phase_count(self, hub_entry_id: str | None = None) -> int:
        """Get the number of phases configured on the hub."""
        entry_id = hub_entry_id or self._data.get(CONF_HUB_ENTRY_ID)
        if not entry_id:
            return 3  # Default to 3 if unknown
        hub_entry = self.hass.config_entries.async_get_entry(entry_id)
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
        # Off-grid fallback: infer from inverter output entities
        count = sum(
            1
            for key in (
                CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
                CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
                CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
            )
            if opts.get(key)
        )
        return max(count, 1)

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

    def _create_entry_and_seed_options(
        self, title: str, static_data: dict, options_data: dict
    ) -> config_entries.FlowResult:
        """Create a config entry with options set directly."""
        return self.async_create_entry(
            title=title, data=static_data, options=options_data
        )

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
        """Hub step 2: Grid/electrical configuration."""
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
                # New hubs carry no inverter hardware — inverters are added
                # as their own entries after the hub exists.
                return await self.async_step_hub_battery()
            return self.async_show_form(
                step_id="hub_grid",
                data_schema=self._hub_grid_schema(user_input),
                errors=errors,
                last_step=False,
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
                    CONF_AUTO_DETECT_PHASE_MAPPING: True,
                }
            )

        except Exception as e:
            _LOGGER.error("Error in async_step_hub_grid: %s", e, exc_info=True)
            errors["base"] = "unknown"
            data_schema = vol.Schema({})

        return self.async_show_form(
            step_id="hub_grid", data_schema=data_schema, errors=errors, last_step=False
        )

    async def async_step_hub_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 3: Solar & site settings (final step — creates entry).

        A NEW hub carries no battery/inverter hardware of its own — that lives
        on Inverter entries added afterwards ("Add Inverter / Home Battery").
        This page keeps only the hub-scoped fields (solar production sensor,
        SOC hysteresis, forecast inputs), and the entry is flagged as
        imported from birth so the legacy pages never appear for it.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, [CONF_SOLAR_PRODUCTION_ENTITY_ID]
            )
            user_input = self._normalize_forecast_list(user_input)
            _validate_entity_units(
                self.hass,
                user_input,
                {
                    CONF_SOLAR_PRODUCTION_ENTITY_ID: _POWER_UNITS,
                },
                errors,
            )
            bad_forecast_entity = _validate_forecast_devices(
                self.hass, user_input, errors
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

                return self._create_entry_and_seed_options(
                    static_data[CONF_NAME], static_data, options_data
                )
            return self.async_show_form(
                step_id="hub_battery",
                data_schema=self._hub_battery_schema(
                    user_input, include_battery_hardware=False
                ),
                errors=errors,
                description_placeholders=(
                    {"entity": bad_forecast_entity} if bad_forecast_entity else None
                ),
                last_step=True,
            )

        # Auto-detect the solar production entity only — battery hardware is
        # configured on Inverter entries, which have their own detection.
        data_schema = self._hub_battery_schema(
            {
                CONF_SOLAR_PRODUCTION_ENTITY_ID: self._auto_detect_entity(
                    SOLAR_PRODUCTION_PATTERNS
                ),
                CONF_BATTERY_SOC_HYSTERESIS: DEFAULT_BATTERY_SOC_HYSTERESIS,
            },
            include_battery_hardware=False,
        )

        return self.async_show_form(
            step_id="hub_battery",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    async def async_step_hub_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub step 3: Inverter configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._INVERTER_ENTITY_KEYS
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
                self._normalize_inverter_powers()
                return await self.async_step_hub_battery()
            battery_hint = self._auto_detect_entity_value(
                BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR
            )
            hint_text = f"{battery_hint}W detected" if battery_hint else "not detected"
            return self.async_show_form(
                step_id="hub_inverter",
                data_schema=self._hub_inverter_schema(user_input),
                errors=errors,
                last_step=False,
                description_placeholders={"battery_power_hint": hint_text},
            )

        # Auto-detect per-phase inverter output entities
        inv_detected = self._auto_detect_phase_entities(INVERTER_OUTPUT_PATTERNS)
        default_inv_a = inv_detected["phase_a"]
        default_inv_b = inv_detected["phase_b"]
        default_inv_c = inv_detected["phase_c"]

        # Auto-detect wiring topology: series if battery entities are detected
        default_topology = DEFAULT_WIRING_TOPOLOGY
        if self._auto_detect_entity(BATTERY_SOC_PATTERNS):
            default_topology = WIRING_TOPOLOGY_SERIES

        data_schema = self._hub_inverter_schema(
            {
                CONF_INVERTER_MAX_POWER: 0,
                CONF_INVERTER_MAX_POWER_PER_PHASE: 0,
                CONF_INVERTER_SUPPORTS_ASYMMETRIC: False,
                CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: default_inv_a,
                CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: default_inv_b,
                CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: default_inv_c,
                CONF_WIRING_TOPOLOGY: default_topology,
            }
        )

        # Auto-detect battery discharge power for description hint
        battery_hint = self._auto_detect_entity_value(
            BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR
        )
        hint_text = f"{battery_hint}W detected" if battery_hint else "not detected"

        return self.async_show_form(
            step_id="hub_inverter",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
            description_placeholders={"battery_power_hint": hint_text},
        )

    # ==================== CHARGER CONFIGURATION STEPS ====================

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Handle integration discovery of OCPP chargers."""
        # Store discovery info
        self._data[CONF_HUB_ENTRY_ID] = discovery_info["hub_entry_id"]
        self._selected_charger = {
            "id": discovery_info["charger_id"],
            "name": discovery_info["charger_name"],
            "device_id": discovery_info.get("device_id"),
            "current_import_entity": discovery_info["current_import_entity"],
            "current_offered_entity": discovery_info["current_offered_entity"],
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

                return self._create_entry_and_seed_options(
                    _compose_entry_title(plug_name, "Smart Load"),
                    static_data, options_data
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
                return self._create_entry_and_seed_options(
                    _compose_entry_title(tank_name, "Hot Water Tank"),
                    static_data, options_data
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
                return self._create_entry_and_seed_options(
                    _compose_entry_title(station_name, "Power Station"),
                    static_data, options_data
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

                return self._create_entry_and_seed_options(
                    f"{group_name}", static_data, options_data
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
        """Inverter step 1: name + capacity, topology and output sensors."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._INVERTER_ENTITY_KEYS
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
            }
        )

        return self.async_show_form(
            step_id="inverter_config",
            data_schema=data_schema,
            errors=errors,
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

                return self._create_entry_and_seed_options(
                    _compose_entry_title(name, "Inverter"), static_data, options_data
                )

        data_schema = self._inverter_battery_schema({**self._data, **(user_input or {})})
        return self.async_show_form(
            step_id="inverter_battery",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    # The legacy hub-level fields the auto-import moves onto an inverter entry.
    # Hub-scoped settings stay behind: SOC hysteresis, the SOC target/min
    # sliders, solar production entity, forecast sources, base consumption,
    # forecast SOC floor and the grid export limit.
    _HUB_INVERTER_IMPORT_FIELDS = (
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

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        """One-time auto-import of a hub's legacy inverter/battery fields into
        a standalone inverter entry (spawned from _setup_hub_entry).

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

        await self.async_set_unique_id(f"{hub_entry_id}_inverter_import")

        # Snapshot the legacy fields, then blank the hub — in this order, and
        # before the duplicate check, so every path leaves the hub clean.
        imported = {
            key: get_entry_value(hub_entry, key, None)
            for key in self._HUB_INVERTER_IMPORT_FIELDS
        }
        imported = {k: v for k, v in imported.items() if v is not None}
        self._blank_hub_legacy_inverter_fields(hub_entry)
        self._abort_if_unique_id_configured()

        hub_name = hub_entry.data.get(CONF_NAME, hub_entry.title)
        hub_prefix = hub_entry.data.get(CONF_ENTITY_ID, "lj_hub")
        name = f"{hub_name} Inverter"
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
            "inverter entry (%s)",
            hub_name,
            ", ".join(sorted(imported)) or "no fields",
        )
        return self.async_create_entry(
            title=name, data=static_data, options=imported
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
        """Discover OCPP chargers from the OCPP integration."""
        chargers = []

        entity_registry = async_get_entity_registry(self.hass)
        device_registry = async_get_device_registry(self.hass)

        # Get already configured charger entity IDs to exclude them
        configured_charger_imports = set()
        for entry in self._get_charger_entries():
            configured_charger_imports.add(
                entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
            )

        # Find entities with current_import suffix (OCPP chargers)
        for entity_id, entity in entity_registry.entities.items():
            if entity_id.endswith(
                OCPP_ENTITY_SUFFIX_CURRENT_IMPORT
            ) and entity_id.startswith("sensor."):
                # Skip already configured chargers
                if entity_id in configured_charger_imports:
                    continue

                # Extract charger base name
                base_name = entity_id.replace("sensor.", "").replace(
                    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT, ""
                )

                # Check if corresponding current_offered entity exists
                current_offered_id = (
                    f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_OFFERED}"
                )
                current_offered_entity = (
                    current_offered_id
                    if current_offered_id in entity_registry.entities
                    else None
                )

                # Fallback: check for power_offered entity if current_offered not available
                power_offered_id = (
                    f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_POWER_OFFERED}"
                )
                power_offered_entity = (
                    power_offered_id
                    if power_offered_id in entity_registry.entities
                    else None
                )

                # Skip chargers without current_offered OR power_offered
                if not current_offered_entity and not power_offered_entity:
                    continue

                # Check for per-phase current import entities (fallback 1)
                current_import_l1_id = (
                    f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L1}"
                )
                current_import_l1_entity = (
                    current_import_l1_id
                    if current_import_l1_id in entity_registry.entities
                    else None
                )
                current_import_l2_id = (
                    f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L2}"
                )
                current_import_l2_entity = (
                    current_import_l2_id
                    if current_import_l2_id in entity_registry.entities
                    else None
                )
                current_import_l3_id = (
                    f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L3}"
                )
                current_import_l3_entity = (
                    current_import_l3_id
                    if current_import_l3_id in entity_registry.entities
                    else None
                )

                # Check for power_import entity (fallback 2)
                power_import_id = f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_POWER_IMPORT}"
                power_import_entity = (
                    power_import_id
                    if power_import_id in entity_registry.entities
                    else None
                )

                # Get device info if available
                device_name = prettify_name(base_name)
                # Use the entity base_name as OCPP device ID (e.g., "evbox_elvy"), not the internal HA UUID
                ocpp_device_id = base_name

                if entity.device_id:
                    device = device_registry.async_get(entity.device_id)
                    if device:
                        # Use device name if available, otherwise fall back to base_name
                        if device.name:
                            device_name = prettify_name(device.name)

                chargers.append(
                    {
                        "id": base_name,
                        "name": device_name,
                        "device_id": ocpp_device_id,
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
                "ocpp_device_id": self._data.get(
                    "ocpp_device_id", self._selected_charger.get("device_id")
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

            return self._create_entry_and_seed_options(
                _compose_entry_title(charger_name, "Charger"),
                static_data, options_data
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

    # ==================== RECONFIGURE STEPS ====================

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure entry point — a menu that routes to focused setup pages.

        Multi-section devices (hub, EVSE) show a menu; each page saves on submit
        and returns here so several sections can be edited in one sitting, then
        "Done" closes. Single-page devices (plug, tank) go straight to their
        form.
        """
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        if not entry:
            return self.async_abort(reason="entry_not_found")

        self._data = dict(entry.data)
        entry_type = entry.data.get(ENTRY_TYPE)

        # Legacy entries without entry_type are hubs
        if not entry_type or entry_type == ENTRY_TYPE_HUB:
            # Once the legacy inverter/battery fields have been auto-imported
            # onto an inverter entry, that hardware is edited on the inverter
            # entry — the hub's inverter page disappears and its battery page
            # keeps only the hub-scoped fields (solar entity, hysteresis,
            # forecast inputs).
            menu_options = ["reconfigure_hub_grid"]
            if not entry.data.get(MIGRATE_HUB_INVERTER_IMPORTED_FLAG):
                menu_options.append("reconfigure_hub_inverter")
            menu_options += [
                "reconfigure_hub_battery",
                "reconfigure_priority",
                "reconfigure_finish",
            ]
            return self.async_show_menu(
                step_id="reconfigure",
                menu_options=menu_options,
            )
        if entry_type == ENTRY_TYPE_CHARGER:
            device_type = entry.data.get(CONF_DEVICE_TYPE)
            if device_type == DEVICE_TYPE_PLUG:
                return await self.async_step_reconfigure_plug()
            if device_type == DEVICE_TYPE_HOT_WATER_TANK:
                return await self.async_step_reconfigure_hot_water_tank()
            if device_type == DEVICE_TYPE_POWER_STATION:
                return await self.async_step_reconfigure_power_station()
            return self.async_show_menu(
                step_id="reconfigure",
                menu_options=[
                    "reconfigure_charger",
                    "reconfigure_charger_current",
                    "reconfigure_charger_timing",
                    "reconfigure_finish",
                ],
            )
        if entry_type == ENTRY_TYPE_INVERTER:
            return await self.async_step_reconfigure_inverter()
        return await self.async_step_reconfigure_charger()

    async def async_step_reconfigure_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure an inverter entry — one page, inverter + battery fields."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input,
                self._INVERTER_ENTITY_KEYS
                + [CONF_BATTERY_SOC_ENTITY_ID, CONF_BATTERY_POWER_ENTITY_ID],
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
                    CONF_BATTERY_POWER_ENTITY_ID: _POWER_UNITS,
                    CONF_BATTERY_SOC_ENTITY_ID: _SOC_UNITS,
                },
                errors,
            )
            if not errors:
                for key in (CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE):
                    if user_input.get(key) == 0:
                        user_input[key] = None
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, **user_input}
                )
                return self.async_abort(reason="reconfigure_successful")
            return self.async_show_form(
                step_id="reconfigure_inverter",
                data_schema=self._inverter_combined_schema(user_input),
                errors=errors,
                last_step=True,
            )

        return self.async_show_form(
            step_id="reconfigure_inverter",
            data_schema=self._inverter_combined_schema(defaults),
            errors=errors,
            last_step=True,
        )

    async def async_step_reconfigure_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Close the reconfigure menu — each section was saved when submitted."""
        return self.async_abort(reason="reconfigure_successful")

    async def async_step_reconfigure_priority(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reorder all controlled devices by priority, then return to the menu."""
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        devices = _controlled_devices(self.hass, entry.entry_id)

        # Nothing to order yet — drop straight back to the menu.
        if not devices:
            return await self.async_step_reconfigure()

        if user_input is not None:
            _apply_priority_order(
                self.hass, devices, list(user_input.get(CONF_PRIORITY_ORDER, []))
            )
            return await self.async_step_reconfigure()

        return self.async_show_form(
            step_id="reconfigure_priority",
            data_schema=_priority_order_schema(devices),
            last_step=False,
        )

    async def async_step_reconfigure_hub_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure hub grid settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

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
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, **user_input}
                )
                return await self.async_step_reconfigure()
            return self.async_show_form(
                step_id="reconfigure_hub_grid",
                data_schema=self._hub_grid_schema(user_input),
                errors=errors,
                last_step=False,
            )

        try:
            data_schema = self._hub_grid_schema(defaults)
        except Exception as e:
            _LOGGER.error("Error in reconfigure_hub_grid: %s", e, exc_info=True)
            errors["base"] = "unknown"
            data_schema = vol.Schema({})

        return self.async_show_form(
            step_id="reconfigure_hub_grid",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_reconfigure_hub_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure hub battery settings (final step — saves)."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}
        # Post-import, battery hardware lives on the inverter entry — this
        # page keeps only the hub-scoped fields.
        battery_hw = not entry.data.get(MIGRATE_HUB_INVERTER_IMPORTED_FLAG)
        step_keys = (
            self._BATTERY_ENTITY_KEYS
            if battery_hw
            else [CONF_SOLAR_PRODUCTION_ENTITY_ID]
        )

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, step_keys
            )
            user_input = self._normalize_forecast_list(user_input)
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
                defaults, user_input, errors,
                hass=self.hass, hub_entry_id=entry.entry_id,
            )
            if not errors:
                self.hass.config_entries.async_update_entry(
                    entry,
                    options={**entry.options, **user_input},
                )
                return await self.async_step_reconfigure()
            return self.async_show_form(
                step_id="reconfigure_hub_battery",
                data_schema=self._hub_battery_schema(
                    user_input, include_battery_hardware=battery_hw
                ),
                errors=errors,
                description_placeholders=(
                    {"entity": bad_forecast_entity} if bad_forecast_entity else None
                ),
                last_step=False,
            )

        data_schema = self._hub_battery_schema(
            defaults, include_battery_hardware=battery_hw
        )

        return self.async_show_form(
            step_id="reconfigure_hub_battery",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_reconfigure_hub_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure hub inverter settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._INVERTER_ENTITY_KEYS
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
                self._data = dict(user_input)
                self._normalize_inverter_powers()
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, **self._data}
                )
                return await self.async_step_reconfigure()
            battery_hint = self._auto_detect_entity_value(
                BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR
            )
            hint_text = f"{battery_hint}W detected" if battery_hint else "not detected"
            return self.async_show_form(
                step_id="reconfigure_hub_inverter",
                data_schema=self._hub_inverter_schema(user_input),
                errors=errors,
                last_step=False,
                description_placeholders={"battery_power_hint": hint_text},
            )

        # Show existing values, defaulting 0 for None (user sees 0 = "not set")
        inverter_defaults = dict(defaults)
        for key in [CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE]:
            if inverter_defaults.get(key) is None:
                inverter_defaults[key] = 0

        data_schema = self._hub_inverter_schema(inverter_defaults)

        # Auto-detect battery discharge power for description hint
        battery_hint = self._auto_detect_entity_value(
            BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR
        )
        hint_text = f"{battery_hint}W detected" if battery_hint else "not detected"

        return self.async_show_form(
            step_id="reconfigure_hub_inverter",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
            description_placeholders={"battery_power_hint": hint_text},
        )

    async def async_step_reconfigure_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure charger: priority and OCPP device ID."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                entry, options={**entry.options, **user_input}
            )
            return await self.async_step_reconfigure()

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
            step_id="reconfigure_charger",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_reconfigure_charger_current(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure charger step 2: Current limits and phase mapping."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}
        hub_entry_id = defaults.get(CONF_HUB_ENTRY_ID)
        hub_phases = self._get_hub_phase_count(hub_entry_id)

        if user_input is not None:
            data = dict(user_input)
            # Auto-fill hidden phase mappings to match L1
            l1 = data.get(CONF_CHARGER_L1_PHASE, "A")
            if hub_phases < 2:
                data[CONF_CHARGER_L2_PHASE] = l1
            if hub_phases < 3:
                data[CONF_CHARGER_L3_PHASE] = l1

            validate_charger_settings(data, errors)
            if errors:
                return self.async_show_form(
                    step_id="reconfigure_charger_current",
                    data_schema=self._charger_current_schema(
                        data, hub_phases=hub_phases
                    ),
                    errors=errors,
                    last_step=False,
                )
            self.hass.config_entries.async_update_entry(
                entry, options={**entry.options, **data}
            )
            return await self.async_step_reconfigure()

        data_schema = self._charger_current_schema(defaults, hub_phases=hub_phases)

        return self.async_show_form(
            step_id="reconfigure_charger_current",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_reconfigure_charger_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure charger: units and timing."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        ocpp_device_id = entry.data.get(CONF_OCPP_DEVICE_ID)
        detected_unit = await self._detect_charge_rate_unit(ocpp_device_id)

        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, **user_input},
            )
            return await self.async_step_reconfigure()

        data_schema = self._charger_timing_schema(defaults, detected_unit=detected_unit)

        return self.async_show_form(
            step_id="reconfigure_charger_timing",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_reconfigure_plug(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure smart load settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._PLUG_ENTITY_KEYS
            )
            self._data.update(user_input)
            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, **self._data},
            )
            return self.async_abort(reason="reconfigure_successful")

        data_schema = self._plug_schema(defaults)

        return self.async_show_form(
            step_id="reconfigure_plug",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    async def async_step_reconfigure_hot_water_tank(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure hot water tank settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._TANK_ENTITY_KEYS
            )
            self._data.update(user_input)

            device_id = self._data.pop(CONF_TANK_POWER_DEVICE_ID, None)
            if device_id and not self._data.get(CONF_TANK_POWER_ENTITY_ID):
                resolved = self._resolve_device_power_entity(device_id)
                if resolved:
                    self._data[CONF_TANK_POWER_ENTITY_ID] = resolved

            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, **self._data},
            )
            return self.async_abort(reason="reconfigure_successful")

        data_schema = self._hot_water_tank_schema(defaults)

        return self.async_show_form(
            step_id="reconfigure_hot_water_tank",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    async def async_step_reconfigure_power_station(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Reconfigure portable power station settings."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            user_input = self._normalize_optional_inputs(
                user_input, self._STATION_ENTITY_KEYS
            )
            self._data.update(user_input)
            if self._data.get(
                CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
            ) < self._data.get(
                CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER
            ):
                errors[CONF_STATION_MAX_CHARGE_POWER] = "station_max_below_min"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    options={**entry.options, **self._data},
                )
                return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure_power_station",
            data_schema=self._power_station_schema({**defaults, **self._data}),
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
        entry_type = self.config_entry.data.get(ENTRY_TYPE)

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
        """Options for an inverter entry — one page, inverter + battery fields."""
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(
                user_input,
                f._INVERTER_ENTITY_KEYS
                + [CONF_BATTERY_SOC_ENTITY_ID, CONF_BATTERY_POWER_ENTITY_ID],
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
                    CONF_BATTERY_POWER_ENTITY_ID: _POWER_UNITS,
                    CONF_BATTERY_SOC_ENTITY_ID: _SOC_UNITS,
                },
                errors,
            )
            if not errors:
                for key in (CONF_INVERTER_MAX_POWER, CONF_INVERTER_MAX_POWER_PER_PHASE):
                    if user_input.get(key) == 0:
                        user_input[key] = None
                return self.async_create_entry(
                    title="",
                    data={**self.config_entry.options, **user_input},
                )

        return self.async_show_form(
            step_id="inverter",
            data_schema=f._inverter_combined_schema(defaults),
            errors=errors,
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
            if not errors:
                self._data.update(user_input)
                # Post-import the inverter hardware is edited on its own
                # entry — skip the legacy hub inverter page.
                if self.config_entry.data.get(MIGRATE_HUB_INVERTER_IMPORTED_FLAG):
                    return await self.async_step_hub()
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
        errors: dict[str, str] = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        f = self._schema_helper
        # Post-import, battery hardware lives on the inverter entry — this
        # page keeps only the hub-scoped fields.
        battery_hw = not self.config_entry.data.get(
            MIGRATE_HUB_INVERTER_IMPORTED_FLAG
        )
        step_keys = (
            f._BATTERY_ENTITY_KEYS if battery_hw else [CONF_SOLAR_PRODUCTION_ENTITY_ID]
        )

        if user_input is not None:
            user_input = f._normalize_optional_inputs(
                user_input, step_keys
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
                data_schema=f._hub_battery_schema(
                    user_input, include_battery_hardware=battery_hw
                ),
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
            data_schema=f._hub_battery_schema(
                defaults, include_battery_hardware=battery_hw
            ),
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

        ocpp_device_id = self.config_entry.data.get(CONF_OCPP_DEVICE_ID)
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
