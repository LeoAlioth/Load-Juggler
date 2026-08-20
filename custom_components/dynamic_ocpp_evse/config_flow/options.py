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
    CONF_OCPP_DEVICE_ID,
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
    _BATTERY_UNIT_MAP,
    _GRID_UNIT_MAP,
    _INVERTER_OUTPUT_UNIT_MAP,
    _LOGGER,
    _POWER_FACTOR,
    _SOLAR_UNIT_MAP,
    _apply_priority_order,
    _controlled_devices,
    _normalize_inverter_power_caps,
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

    @property
    def _defaults(self) -> dict[str, Any]:
        """The stored config as every form here shows it: data, options on top."""
        return {**self.config_entry.data, **self.config_entry.options}

    def _save(self) -> config_entries.FlowResult:
        """Write what the steps collected in ``self._data`` back to the entry.

        Options only — the static ``data`` half is never edited after setup, so
        the previous options are the base every step merges onto.
        """
        return self.async_create_entry(
            title="", data={**self.config_entry.options, **self._data}
        )

    async def _async_edit_page(
        self,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
        schema,
        entity_keys: list[str] | None = None,
        list_normalizers: tuple = (),
        unit_map: dict | None = None,
        validate=None,
        finalize=None,
        last_step: bool | None = True,
    ) -> config_entries.FlowResult:
        """Run one self-contained edit page: normalize → validate → save.

        The shape every single-page settings step shares. The stored config is
        the form's defaults; a submit normalizes the page's entity fields
        (``entity_keys`` — omitted ones were cleared) and any multi-select
        lists, validates units and whatever else the page demands, and saves.
        A failed validation re-shows the form over what the user just typed.

        Hooks, all optional:
            unit_map: field→accepted-units map for _validate_entity_units.
            validate: extra check(data, errors); may return an entity name to
                pass to the form as the ``entity`` placeholder.
            finalize: last-moment rewrite of the data about to be stored.

        The hub and charger wizards do NOT use this — their submit branch
        routes to the next step instead of saving, so they stay hand-written.
        """
        errors: dict[str, str] = {}
        placeholder = None
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(user_input, entity_keys)
            for normalize_list in list_normalizers:
                user_input = normalize_list(user_input)
            self._data.update(user_input)
            if unit_map:
                _validate_entity_units(self.hass, self._data, unit_map, errors)
            if validate is not None:
                placeholder = validate(self._data, errors)
            if not errors:
                if finalize is not None:
                    finalize(self._data)
                return self._save()

        return self.async_show_form(
            step_id=step_id,
            data_schema=schema({**self._defaults, **self._data}),
            errors=errors,
            description_placeholders=({"entity": placeholder} if placeholder else None),
            last_step=last_step,
        )

    async def _async_wizard_page(
        self,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
        schema,
        next_step,
        entity_keys: list[str] | None = None,
        list_normalizers: tuple = (),
        unit_map: dict | None = None,
        validate=None,
        finalize=None,
        show_defaults=None,
        placeholders=None,
    ) -> config_entries.FlowResult:
        """Run one page of a multi-step edit wizard: normalize → validate → on.

        The same skeleton as _async_edit_page, except a clean submit routes to
        ``next_step`` instead of saving — the input piles up in ``self._data``
        until the wizard's final step calls _save(). A failed validation
        re-shows the form over the submitted input alone; the first show uses
        the stored config, or ``show_defaults`` where a page has to massage it.

        Hooks beyond _async_edit_page's:
            show_defaults: replaces the stored config on the first show.
            placeholders: form placeholders every show needs (a detected-value
                hint); ``validate`` may add more, for the error re-show only.
        """
        errors: dict[str, str] = {}
        extra_placeholders = None
        f = self._schema_helper

        if user_input is not None:
            user_input = f._normalize_optional_inputs(user_input, entity_keys)
            for normalize_list in list_normalizers:
                user_input = normalize_list(user_input)
            if unit_map:
                _validate_entity_units(self.hass, user_input, unit_map, errors)
            if validate is not None:
                extra_placeholders = validate(user_input, errors)
            if not errors:
                self._data.update(user_input)
                if finalize is not None:
                    finalize(self._data)
                return await next_step()
            form_defaults = user_input
        elif show_defaults is not None:
            form_defaults = show_defaults()
        else:
            form_defaults = self._defaults

        shown = {
            **(placeholders() if placeholders is not None else {}),
            **(extra_placeholders or {}),
        }
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema(form_defaults),
            errors=errors,
            description_placeholders=shown or None,
            last_step=False,
        )

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
        f = self._schema_helper
        return await self._async_edit_page(
            user_input,
            step_id="inverter",
            schema=f._inverter_combined_schema,
            entity_keys=f._INVERTER_ENTITY_KEYS
            + [
                CONF_SOLAR_PRODUCTION_ENTITY_ID,
                CONF_BATTERY_SOC_ENTITY_ID,
                CONF_BATTERY_POWER_ENTITY_ID,
                CONF_CHARGE_LIMIT_ENTITY_ID,
                CONF_BATTERY_VOLTAGE_ENTITY_ID,
                CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
            ],
            list_normalizers=(f._normalize_forecast_list, f._normalize_soc_limit_list),
            unit_map=_INVERTER_OUTPUT_UNIT_MAP | _SOLAR_UNIT_MAP | _BATTERY_UNIT_MAP,
            validate=lambda data, errors: _validate_forecast_devices(
                self.hass, data, errors
            ),
            finalize=_normalize_inverter_power_caps,
            last_step=None,
        )

    async def async_step_hub_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub settings step 1: the grid connection and the site policy.

        No auto-detection when editing an existing hub — only the initial
        install scans for entities. Re-detecting here can grab entities from an
        unrelated system (e.g. a second inverter in another building), silently
        adding phantom phases. The stored values are shown as-is.
        """
        f = self._schema_helper

        def _require_battery_when_offgrid(data, errors) -> None:
            # Dropping the grid CTs here is what makes a hub off-grid, so this
            # is where the battery requirement belongs now that the battery
            # itself lives on an inverter entry.
            validate_offgrid_battery_requirement(
                data, self._defaults, errors,
                hass=self.hass, hub_entry_id=self.config_entry.entry_id,
            )

        async def _next() -> config_entries.FlowResult:
            # Post-import the hardware (inverters, batteries, PV sensors and
            # forecast sources) is edited on the inverter entries — the legacy
            # hub pages are skipped entirely.
            if self.config_entry.data.get(MIGRATE_HUB_INVERTER_IMPORTED_FLAG):
                return await self.async_step_priority()
            return await self.async_step_hub_inverter()

        return await self._async_wizard_page(
            user_input,
            step_id="hub_grid",
            schema=f._hub_grid_schema,
            next_step=_next,
            entity_keys=f._GRID_ENTITY_KEYS,
            unit_map=_GRID_UNIT_MAP,
            validate=_require_battery_when_offgrid,
        )

    async def async_step_hub_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """LEGACY hub inverter page — reachable only while the hub still
        carries those fields (i.e. before the one-time auto-import).

        No auto-detection when editing an existing hub: re-detecting the
        inverter output phases here can grab a different inverter's per-phase
        sensors (e.g. a 3-phase system in another building), creating phantom
        L2/L3 phases that split the available power across phases that don't
        exist on this site. The stored values are shown as-is.
        """
        f = self._schema_helper

        def _battery_power_hint() -> dict[str, str]:
            """Detected battery discharge power — form text only, sets nothing."""
            hint = f._auto_detect_entity_value(
                BATTERY_MAX_DISCHARGE_POWER_PATTERNS, _POWER_FACTOR
            )
            return {
                "battery_power_hint": f"{hint}W detected" if hint else "not detected"
            }

        def _stored_with_zeroed_caps() -> dict[str, Any]:
            """Stored values, with 0 standing in for an unset power cap."""
            defaults = self._defaults
            return {
                **defaults,
                **{
                    key: 0
                    for key in (
                        CONF_INVERTER_MAX_POWER,
                        CONF_INVERTER_MAX_POWER_PER_PHASE,
                    )
                    if defaults.get(key) is None
                },
            }

        return await self._async_wizard_page(
            user_input,
            step_id="hub_inverter",
            schema=f._hub_inverter_schema,
            next_step=self.async_step_hub,
            entity_keys=f._INVERTER_ENTITY_KEYS,
            unit_map=_INVERTER_OUTPUT_UNIT_MAP,
            finalize=_normalize_inverter_power_caps,
            show_defaults=_stored_with_zeroed_caps,
            placeholders=_battery_power_hint,
        )

    async def async_step_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """LEGACY hub solar/battery page — reachable only while the hub still
        carries those fields (i.e. before the one-time auto-import).

        No auto-detection here either: re-detecting can grab battery/solar
        entities from an unrelated system. The stored values are shown as-is.
        """
        f = self._schema_helper

        def _validate(data, errors) -> dict[str, str] | None:
            bad_forecast_entity = _validate_forecast_devices(self.hass, data, errors)
            validate_offgrid_battery_requirement(
                {**self._defaults, **self._data}, data, errors,
                hass=self.hass, hub_entry_id=self.config_entry.entry_id,
            )
            return {"entity": bad_forecast_entity} if bad_forecast_entity else None

        return await self._async_wizard_page(
            user_input,
            step_id="hub",
            schema=f._hub_battery_schema,
            next_step=self.async_step_priority,
            entity_keys=f._BATTERY_ENTITY_KEYS,
            list_normalizers=(f._normalize_forecast_list,),
            unit_map=_SOLAR_UNIT_MAP | _BATTERY_UNIT_MAP,
            validate=_validate,
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

        # No loads to order yet — just persist the hub settings and finish.
        if not devices:
            return self._save()

        if user_input is not None:
            _apply_priority_order(
                self.hass, devices, list(user_input.get(CONF_PRIORITY_ORDER, []))
            )
            return self._save()

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
        defaults = self._defaults

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
        defaults = self._defaults
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
        defaults = self._defaults
        f = self._schema_helper

        # Options-first: an edited device ID lives in entry.options.
        ocpp_device_id = get_entry_value(self.config_entry, CONF_OCPP_DEVICE_ID, None)
        detected_unit = await f._detect_charge_rate_unit(ocpp_device_id)

        if user_input is not None:
            self._data.update(user_input)
            return self._save()

        return self.async_show_form(
            step_id="charger_timing",
            data_schema=f._charger_timing_schema(defaults, detected_unit=detected_unit),
            errors=errors,
            last_step=True,
        )

    async def async_step_plug(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        f = self._schema_helper
        return await self._async_edit_page(
            user_input,
            step_id="plug",
            schema=f._plug_schema,
            entity_keys=f._PLUG_ENTITY_KEYS,
        )

    async def async_step_hot_water_tank(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        f = self._schema_helper

        def _resolve_power_device(data: dict[str, Any]) -> None:
            """A picked power device becomes its power-sensor entity, so runtime
            only ever deals with CONF_TANK_POWER_ENTITY_ID."""
            device_id = data.pop(CONF_TANK_POWER_DEVICE_ID, None)
            if device_id and not data.get(CONF_TANK_POWER_ENTITY_ID):
                resolved = f._resolve_device_power_entity(device_id)
                if resolved:
                    data[CONF_TANK_POWER_ENTITY_ID] = resolved

        return await self._async_edit_page(
            user_input,
            step_id="hot_water_tank",
            schema=f._hot_water_tank_schema,
            entity_keys=f._TANK_ENTITY_KEYS,
            finalize=_resolve_power_device,
        )

    async def async_step_power_station(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        f = self._schema_helper

        def _check_power_window(data: dict[str, Any], errors: dict[str, str]) -> None:
            if data.get(
                CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
            ) < data.get(
                CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER
            ):
                errors[CONF_STATION_MAX_CHARGE_POWER] = "station_max_below_min"

        return await self._async_edit_page(
            user_input,
            step_id="power_station",
            schema=f._power_station_schema,
            entity_keys=f._STATION_ENTITY_KEYS,
            validate=_check_power_window,
        )

    async def async_step_group(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Options flow for circuit group: current limit + member selection."""
        errors: dict[str, str] = {}
        defaults = self._defaults

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
