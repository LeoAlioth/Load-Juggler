"""Load Juggler - the options flow: the single edit path after setup.

``LoadJugglerOptionsFlow`` is what "Configure" opens on an existing entry -
a small menu that branches to the settings steps for whatever the entry is
(hub, inverter, group or one of the load types) and to the two read-only pages.
It owns no schemas of its own: every form it shows comes from ``schemas.py``,
the same builders the create flow uses.

Two executors carry the shape the steps share - ``_async_edit_page`` for a
page that saves on submit, ``_async_wizard_page`` for one that routes on to
the next - so each step is left declaring only what makes it different. The
priority and circuit-group steps stay hand-written; see their docstrings.
"""
import voluptuous as vol
from typing import Any
from homeassistant import config_entries
from homeassistant.helpers.selector import selector
from ..const import (
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_LOAD_PRIORITY,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
    CONF_CIRCUIT_GROUP_MEMBERS,
    CONF_DEVICE_TYPE,
    CONF_HUB_ENTRY_ID,
    CONF_OCPP_DEVICE_ID,
    CONF_PRIORITY_ORDER,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_SOC_LIMIT_ENTITY_IDS,
    DEFAULT_LOAD_PRIORITY,
    DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_POWER_STATION,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
    FIELD_OCPP_DEVICE,
    CONF_INVERTER_FEATURES,
    INVERTER_FEATURE_BATTERY,
    INVERTER_FEATURE_BATTERY_CONTROL,
    INVERTER_FEATURE_SOLAR,
)
from ..helpers import (
    get_entry_value,
    validate_charger_settings,
    validate_offgrid_battery_requirement,
)
from ..helpers import hub_has_battery, inverter_features, strip_unfeatured_inverter_options
from .helpers import (
    _BATTERY_UNIT_MAP,
    _GRID_ENTITY_KEYS,
    _GRID_UNIT_MAP,
    _INVERTER_ENTITY_KEYS,
    _INVERTER_OUTPUT_UNIT_MAP,
    _LOGGER,
    _PLUG_ENTITY_KEYS,
    _SOLAR_UNIT_MAP,
    _STATION_ENTITY_KEYS,
    _TANK_ENTITY_KEYS,
    _apply_priority_order,
    _controlled_devices,
    _detect_charge_rate_unit,
    _check_power_window,
    _fill_hidden_legs,
    _hub_phase_count,
    _load_options,
    _normalize_list,
    _normalize_inverter_power_caps,
    _normalize_optional_inputs,
    _priority_order_schema,
    _validate_entity_units,
    _write_control_unit_map,
    _validate_forecast_devices,
    _validate_inverter_features,
)
from ..ocpp_discovery import (
    ocpp_charger_for_device,
    ocpp_device_for_charge_point,
    ocpp_entry_fields,
)
from .pages import _overview_text, _summary_text
from .schemas import (
    _charger_current_schema,
    _charger_timing_schema,
    _hot_water_tank_schema,
    _plug_schema,
    _power_station_schema,
    _inverter_features_schema,
    _build_hub_inverter_schema,
    _build_inverter_solar_schema,
    _build_inverter_battery_schema,
    _build_inverter_control_schema,
    _hub_section_schema,
    _num,
    _ocpp_device_field,
    _hub_filters_schema,
    validate_hub_filters,
    HUB_CONNECTION_KEYS,
    HUB_EXPORT_KEYS,
    HUB_POLICY_KEYS,
    HUB_TIMING_KEYS,
)


class LoadJugglerOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Load Juggler."""

    def __init__(self):
        self._data = {}

    @property
    def _defaults(self) -> dict[str, Any]:
        """The stored config as every form here shows it: data, options on top."""
        return {**self.config_entry.data, **self.config_entry.options}

    def _save(self) -> config_entries.FlowResult:
        """Write what the steps collected in ``self._data`` back to the entry.

        Options only - the static ``data`` half is never edited after setup, so
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
        list_keys: tuple = (),
        unit_map: dict | None = None,
        validate=None,
        finalize=None,
    ) -> config_entries.FlowResult:
        """Run one self-contained edit page: normalize → validate → save.

        The shape every single-page settings step shares. The stored config is
        the form's defaults; a submit normalizes the page's entity fields
        (``entity_keys`` - omitted ones were cleared) and any multi-select
        lists (``list_keys``), validates units and whatever else the page demands, and saves.
        A failed validation re-shows the form over what the user just typed.

        Hooks, all optional:
            unit_map: field→accepted-units map for _validate_entity_units.
            validate: extra check(data, errors); may return an entity name to
                pass to the form as the ``entity`` placeholder.
            finalize: last-moment rewrite of the data about to be stored.

        The hub and charger wizards do NOT use this - their submit branch
        routes to the next step instead of saving, so they stay hand-written.
        """
        errors: dict[str, str] = {}
        placeholder = None

        if user_input is not None:
            user_input = _normalize_optional_inputs(user_input, entity_keys)
            for key in list_keys:
                _normalize_list(user_input, key)
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
            last_step=True,
        )

    async def _async_wizard_page(
        self,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
        schema,
        next_step,
        validate=None,
        placeholders=None,
    ) -> config_entries.FlowResult:
        """Run one page of the charger edit wizard: validate → on.

        The same skeleton as _async_edit_page, except a clean submit routes to
        ``next_step`` instead of saving - the input piles up in ``self._data``
        until the wizard's final step calls _save(). A failed validation
        re-shows the form over the submitted input alone; the first show uses
        the stored config.

        Hooks:
            validate: check(data, errors), may rewrite ``data``.
            placeholders: form placeholders every show needs.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = dict(user_input)
            if validate is not None:
                validate(user_input, errors)
            if not errors:
                self._data.update(user_input)
                return await next_step()
            form_defaults = user_input
        else:
            form_defaults = self._defaults

        return self.async_show_form(
            step_id=step_id,
            data_schema=schema(form_defaults),
            errors=errors,
            description_placeholders=(
                placeholders() if placeholders is not None else None
            ),
            last_step=False,
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """The one entry point for editing an entry - a small menu.

        "Configure" is the single edit path (there is no reconfigure flow), so
        this menu also hosts the two read-only pages: a live Overview for every
        entry type, and "How it decides" for the hub.
        """
        entry_type = self.config_entry.data.get(ENTRY_TYPE, ENTRY_TYPE_HUB)
        if entry_type == ENTRY_TYPE_INVERTER:
            # An inverter's menu lists its pages directly: the features first,
            # then one page per declared feature - each saves on its own.
            features = inverter_features(self.config_entry)
            menu_options = ["inverter_features", "inverter_core"]
            if INVERTER_FEATURE_SOLAR in features:
                menu_options.append("inverter_solar")
            if INVERTER_FEATURE_BATTERY in features:
                menu_options.append("inverter_battery")
            if INVERTER_FEATURE_BATTERY_CONTROL in features:
                menu_options.append("inverter_control")
            menu_options.append("overview")
            return self.async_show_menu(step_id="init", menu_options=menu_options)
        if entry_type == ENTRY_TYPE_HUB:
            # A hub edits its own settings one question per page - its
            # hardware lives on inverter entries (a hub still carrying legacy
            # hardware fields has them moved there on its next setup). The
            # battery/forecast policy is offered only while some inverter on
            # it declares a battery; the priority order only while it has
            # loads to order.
            menu_options = ["hub_connection", "hub_export"]
            if hub_has_battery(self.hass, self.config_entry):
                menu_options.append("hub_policy")
            menu_options.append("hub_timing")
            menu_options.append("hub_filters")
            if _controlled_devices(self.hass, self.config_entry.entry_id):
                menu_options.append("priority")
            menu_options += ["overview", "summary"]
            return self.async_show_menu(step_id="init", menu_options=menu_options)
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "overview"]
        )

    async def async_step_overview(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Read-only live overview, scoped to this entry.

        Rendered as a MENU, not a form: a form's submit button is fixed to
        "Next"/"Submit" by HA, which reads as if something gets saved. Menu
        options give real, labeled buttons instead - "Refresh" re-enters this
        step (rebuilding the text from live data), "Back" returns to init.
        """
        try:
            text = _overview_text(self.hass, self.config_entry.entry_id)
        except Exception:  # pragma: no cover - a display page must not break
            _LOGGER.exception("Could not build the overview page")
            text = "⚠️ Could not read the live data - see the Home Assistant log."
        return self.async_show_menu(
            step_id="overview",
            menu_options=["overview", "init"],
            description_placeholders={"overview": text},
        )

    async def async_step_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Read-only "how it decides" page (hub only).

        A menu for the same reason as async_step_overview - the only action
        here is going back, and a form's "Next" button would misname it.
        """
        try:
            text = _summary_text(self.hass, self.config_entry.entry_id)
        except Exception:  # pragma: no cover - a display page must not break
            _LOGGER.exception("Could not build the summary page")
            text = "⚠️ Could not read the configuration - see the Home Assistant log."
        return self.async_show_menu(
            step_id="summary",
            menu_options=["init"],
            description_placeholders={"summary": text},
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Route to the editable pages for this entry type."""
        entry_type = self.config_entry.data.get(ENTRY_TYPE)
        if entry_type == ENTRY_TYPE_LOAD:
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
        """Options for an inverter entry: the features page."""
        return await self.async_step_inverter_features(user_input)

    async def async_step_inverter_features(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """What this inverter has. Saves on its own: the menu then offers a
        page per declared feature, and the keys of every feature unticked
        here are cleared so nothing outlives the section it belonged to."""

        def _finalize(data: dict) -> None:
            strip_unfeatured_inverter_options(
                data, data.get(CONF_INVERTER_FEATURES) or [], clear_all=True
            )

        return await self._async_edit_page(
            user_input,
            step_id="inverter_features",
            schema=lambda defaults: _inverter_features_schema(
                {
                    **defaults,
                    CONF_INVERTER_FEATURES: inverter_features(self.config_entry),
                }
            ),
            list_keys=(CONF_INVERTER_FEATURES,),
            validate=_validate_inverter_features,
            finalize=_finalize,
        )

    async def async_step_inverter_core(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """The inverter's AC side: capacity, topology, per-phase output."""
        return await self._async_edit_page(
            user_input,
            step_id="inverter_core",
            schema=lambda defaults: vol.Schema(
                dict(_build_hub_inverter_schema(self.hass, defaults))
            ),
            entity_keys=_INVERTER_ENTITY_KEYS,
            unit_map=_INVERTER_OUTPUT_UNIT_MAP,
            finalize=_normalize_inverter_power_caps,
        )

    async def async_step_inverter_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """The PV array behind this inverter: production sensor, forecast."""
        return await self._async_edit_page(
            user_input,
            step_id="inverter_solar",
            schema=lambda defaults: vol.Schema(
                dict(_build_inverter_solar_schema(self.hass, defaults))
            ),
            entity_keys=[CONF_SOLAR_PRODUCTION_ENTITY_ID],
            list_keys=(CONF_SOLAR_FORECAST_DEVICE_IDS,),
            unit_map=_SOLAR_UNIT_MAP,
            # Returns a bad forecast device's name for the ``entity`` placeholder.
            validate=lambda data, errors: _validate_forecast_devices(
                self.hass, data, errors
            ),
            # The device selection replaces any legacy sensor list.
            finalize=lambda data: data.update({CONF_SOLAR_FORECAST_ENTITY_IDS: []}),
        )

    async def async_step_inverter_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """The battery behind this inverter."""
        return await self._async_edit_page(
            user_input,
            step_id="inverter_battery",
            schema=lambda defaults: vol.Schema(
                dict(_build_inverter_battery_schema(self.hass, defaults))
            ),
            entity_keys=[CONF_BATTERY_SOC_ENTITY_ID, CONF_BATTERY_POWER_ENTITY_ID],
            unit_map=_BATTERY_UNIT_MAP,
        )

    async def async_step_inverter_control(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Battery charge management: the charge register and SOC slots."""
        return await self._async_edit_page(
            user_input,
            step_id="inverter_control",
            schema=lambda defaults: vol.Schema(
                dict(_build_inverter_control_schema(self.hass, defaults))
            ),
            entity_keys=[
                CONF_CHARGE_LIMIT_ENTITY_ID,
                CONF_BATTERY_VOLTAGE_ENTITY_ID,
                CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
            ],
            list_keys=(CONF_SOC_LIMIT_ENTITY_IDS,),
            validate=lambda data, errors: _validate_entity_units(
                self.hass, data, _write_control_unit_map(data), errors
            ),
        )

    async def async_step_hub_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """How the site is wired to the grid: CTs, breaker, voltage, import cap.
        Dropping every CT makes the hub off-grid, so the battery requirement is
        checked here."""

        def _require_battery_when_offgrid(data, errors) -> None:
            validate_offgrid_battery_requirement(
                data, self._defaults, errors,
                hass=self.hass, hub_entry_id=self.config_entry.entry_id,
            )

        return await self._async_edit_page(
            user_input,
            step_id="hub_connection",
            schema=lambda defaults: _hub_section_schema(
                self.hass, defaults, HUB_CONNECTION_KEYS
            ),
            entity_keys=_GRID_ENTITY_KEYS,
            unit_map=_GRID_UNIT_MAP,
            validate=_require_battery_when_offgrid,
        )

    async def async_step_hub_export(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """The export wall and the Excess trigger under it."""
        return await self._async_edit_page(
            user_input,
            step_id="hub_export",
            schema=lambda defaults: _hub_section_schema(
                self.hass, defaults, HUB_EXPORT_KEYS
            ),
        )

    async def async_step_hub_policy(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Fleet-wide battery and forecast policy."""
        return await self._async_edit_page(
            user_input,
            step_id="hub_policy",
            schema=lambda defaults: _hub_section_schema(
                self.hass, defaults, HUB_POLICY_KEYS
            ),
        )

    async def async_step_hub_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """The engine's timing: refresh interval, phase detection, solar grace."""
        return await self._async_edit_page(
            user_input,
            step_id="hub_timing",
            schema=lambda defaults: _hub_section_schema(
                self.hass, defaults, HUB_TIMING_KEYS
            ),
        )

    async def async_step_hub_filters(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """The control pipeline's filters: time constants, dead band, slews."""
        return await self._async_edit_page(
            user_input,
            step_id="hub_filters",
            schema=lambda defaults: _hub_filters_schema(defaults),
            validate=validate_hub_filters,
        )

    async def async_step_priority(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Hub options: reorder all controlled devices by priority.

        Presents one ordered multi-select listing every load (EVSE, smart plug,
        hot-water tank) linked to this hub. The selection order becomes the
        served-first order: the first chip is priority 1, the next is 2, and so
        on. This is the single place to set relative priority - the per-device
        number is written back to each child entry from the chosen order.
        """
        devices = _controlled_devices(self.hass, self.config_entry.entry_id)

        # No loads to order yet - just persist the hub settings and finish.
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
        """Options charger step 1: priority and the OCPP device behind it.

        The same device picker the create wizard's charger_info step offers,
        instead of the free-text charge point id it replaces - an id nobody can
        check, typed against a device the registry already knows by name.

        Pre-selected to whatever device claims the stored charge point id. When
        nothing does (an OCPP-shaped template-sensor site has no device at all)
        the picker simply opens empty and the stored charge point id is named in
        the page text, rather than falling back to a second, free-text field:
        the picker is Optional either way, so submitting the page untouched
        keeps the working config on both paths, and one field beats two that
        contradict each other.
        """

        def _schema(defaults: dict[str, Any]) -> vol.Schema:
            picked = defaults.get(FIELD_OCPP_DEVICE) or ocpp_device_for_charge_point(
                self.hass, get_entry_value(self.config_entry, CONF_OCPP_DEVICE_ID, None)
            )
            return vol.Schema(
                dict(
                    [
                        _num(
                            CONF_LOAD_PRIORITY, defaults, DEFAULT_LOAD_PRIORITY,
                            1, 10, 1, None, required=True,
                        ),
                        _ocpp_device_field({FIELD_OCPP_DEVICE: picked}),
                    ]
                )
            )

        def _charge_point_hint() -> dict[str, str]:
            """The stored charge point id, named in the page text.

            The one thing the picker cannot show: it renders device names, and
            on a site with no OCPP device it renders nothing at all.
            """
            return {
                "charge_point_id": get_entry_value(
                    self.config_entry, CONF_OCPP_DEVICE_ID, None
                )
                or "-"
            }

        def _apply_picked_device(
            data: dict[str, Any], errors: dict[str, str]
        ) -> None:
            """A picked device rewrites the charger's whole OCPP side.

            Through the very resolver and mapping the create wizard uses, so
            both edit paths derive the charge point id and every sensor entity
            once. An untouched (or cleared) picker changes nothing.
            """
            picked = data.get(FIELD_OCPP_DEVICE)
            if not picked:
                data.pop(FIELD_OCPP_DEVICE, None)
                return
            resolved = ocpp_charger_for_device(self.hass, picked)
            if resolved is None:
                # Left in ``data`` on purpose: the re-shown form is built from
                # it, so the bad pick stays visible next to its error.
                errors[FIELD_OCPP_DEVICE] = "ocpp_device_not_usable"
                return
            data.pop(FIELD_OCPP_DEVICE)
            data.update(ocpp_entry_fields(resolved))

        return await self._async_wizard_page(
            user_input,
            step_id="charger",
            schema=_schema,
            next_step=self.async_step_charger_current,
            validate=_apply_picked_device,
            placeholders=_charge_point_hint,
        )

    async def async_step_charger_current(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Options charger step 2: Current limits and phase mapping."""
        hub_phases = _hub_phase_count(self.hass, self._defaults.get(CONF_HUB_ENTRY_ID))

        def _validate(data: dict[str, Any], errors: dict[str, str]) -> None:
            _fill_hidden_legs(data, hub_phases)
            validate_charger_settings(data, errors)

        return await self._async_wizard_page(
            user_input,
            step_id="charger_current",
            schema=lambda defaults: _charger_current_schema(
                defaults, hub_phases=hub_phases
            ),
            next_step=self.async_step_charger_timing,
            validate=_validate,
        )

    async def async_step_charger_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Options charger step 3: Units and timing (final - saves)."""

        # A device picked on the charger page is not stored yet, so the pending
        # charge point id wins - the detected-unit hint has to describe the
        # charger the user just pointed at, not the one being replaced. Then
        # options-first, since a previous edit lives in entry.options.
        ocpp_device_id = self._data.get(CONF_OCPP_DEVICE_ID) or get_entry_value(
            self.config_entry, CONF_OCPP_DEVICE_ID, None
        )
        detected_unit = await _detect_charge_rate_unit(self.hass, ocpp_device_id)

        return await self._async_edit_page(
            user_input,
            step_id="charger_timing",
            schema=lambda defaults: _charger_timing_schema(
                defaults, detected_unit=detected_unit
            ),
        )

    async def async_step_plug(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        return await self._async_edit_page(
            user_input,
            step_id="plug",
            schema=_plug_schema,
            entity_keys=_PLUG_ENTITY_KEYS,
        )

    async def async_step_hot_water_tank(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        return await self._async_edit_page(
            user_input,
            step_id="hot_water_tank",
            schema=_hot_water_tank_schema,
            entity_keys=_TANK_ENTITY_KEYS,
        )

    async def async_step_power_station(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        return await self._async_edit_page(
            user_input,
            step_id="power_station",
            schema=_power_station_schema,
            entity_keys=_STATION_ENTITY_KEYS,
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

        data_schema = vol.Schema(
            dict(
                [
                    _num(
                        CONF_CIRCUIT_GROUP_CURRENT_LIMIT, defaults,
                        DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT, 1, 100, 1, "A",
                        required=True,
                    ),
                    (
                        vol.Required(
                            CONF_CIRCUIT_GROUP_MEMBERS,
                            default=defaults.get(CONF_CIRCUIT_GROUP_MEMBERS, []),
                        ),
                        selector(
                            {
                                "select": {
                                    "options": _load_options(
                                        self.hass,
                                        self.config_entry.data.get(CONF_HUB_ENTRY_ID),
                                    ),
                                    "multiple": True,
                                    "mode": "list",
                                }
                            }
                        ),
                    ),
                ]
            )
        )

        return self.async_show_form(
            step_id="group",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )
