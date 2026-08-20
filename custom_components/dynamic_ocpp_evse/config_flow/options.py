"""Load Juggler - the options flow: the single edit path after setup.

``LoadJugglerOptionsFlow`` is what "Configure" opens on an existing entry —
a small menu that branches to the settings steps for whatever the entry is
(hub, inverter, group or one of the load types) and to the two read-only pages.
It owns no schemas of its own: every form it shows comes from the create-flow
builders, reached through a cached ``LoadJugglerConfigFlow`` instance.

Moved verbatim out of flow.py.
"""
import voluptuous as vol
from typing import Any
from homeassistant import config_entries
from homeassistant.helpers.selector import selector
from ..const import (
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_CHARGER_PRIORITY,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
    CONF_CIRCUIT_GROUP_MEMBERS,
    CONF_DEVICE_TYPE,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_OCPP_DEVICE_ID,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PRIORITY_ORDER,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_TANK_POWER_DEVICE_ID,
    CONF_TANK_POWER_ENTITY_ID,
    DEFAULT_CHARGER_PRIORITY,
    DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
    DEFAULT_STATION_MAX_CHARGE_POWER,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_POWER_STATION,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_CHARGER,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
    MIGRATE_HUB_INVERTER_IMPORTED_FLAG,
)
from ..detection_patterns import BATTERY_MAX_DISCHARGE_POWER_PATTERNS
from ..helpers import (
    get_entry_value,
    validate_charger_settings,
    validate_offgrid_battery_requirement,
)
from .flow import LoadJugglerConfigFlow
from .helpers import (
    _CURRENT_UNITS,
    _LOGGER,
    _POWER_FACTOR,
    _POWER_UNITS,
    _SOC_UNITS,
    _apply_priority_order,
    _controlled_devices,
    _priority_order_schema,
    _validate_entity_units,
    _validate_forecast_devices,
)
from .pages import _overview_text, _summary_text


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
