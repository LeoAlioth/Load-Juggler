"""The initial setup forms open with what the Energy dashboard already names.

The preferences are trimmed copies of the live sites' (HA_Configs snapshots of
2026-10-12): Home - one PV array, a grid meter declared inverted; Kozolec - two
PV arrays and one battery. Suggestions fill empty fields of the create forms
only (hub grid, the first inverter's three pages), never an options page, and
the dashboard is read, never written.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.config_flow.sections import flat_fields
from custom_components.dynamic_ocpp_evse.const import (
    CONF_BATTERY_CAPACITY_ENTITY_ID,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_ENTITY_ID,
    CONF_INVERT_PHASES,
    CONF_INVERTER_FEATURES,
    CONF_INVERTER_MAX_POWER_ENTITY_ID,
    CONF_NAME,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
)

from .conftest import add_inverter

HELPERS = "custom_components.dynamic_ocpp_evse.config_flow.helpers"
OPEN_METEO = "open_meteo_solar_forecast"
POWER = {"device_class": "power", "unit_of_measurement": "W"}

HOME = {
    "energy_sources": [
        {"type": "solar", "stat_energy_from": "sensor.solaredge_se17k_i1_ac_energy",
         "config_entry_solar_forecast": ["HOMEFORECAST0000000000000000"],
         "stat_rate": "sensor.solaredge_se17k_i1_ac_power"},
        {"type": "grid", "stat_energy_from": "sensor.solaredge_se17k_m1_imported",
         "stat_energy_to": "sensor.solaredge_se17k_m1_exported",
         "power_config": {"stat_rate_inverted": "sensor.solaredge_se17k_m1_ac_power"},
         "stat_rate": "sensor.solaredge_se17k_m1_ac_power_inverted"},
    ],
}
KOZOLEC = {
    "energy_sources": [
        {"type": "battery", "stat_energy_from": "sensor.gx_device_dc_battery_discharge_energy",
         "stat_energy_to": "sensor.gx_device_dc_battery_charge_energy",
         "power_config": {"stat_rate_inverted": "sensor.gx_device_dc_battery_power"},
         "stat_soc": "sensor.jk_bms_id_512_charge", "capacity": 62,
         "stat_rate": "sensor.gx_device_dc_battery_power_inverted"},
        {"type": "water", "stat_energy_from": "sensor.water_usage"},
        {"type": "solar", "stat_energy_from": "sensor.mppt_150_70_total_yield",
         "config_entry_solar_forecast": ["KOZFORECASTONE00000000000000"],
         "stat_rate": "sensor.mppt_150_70_pv_yield_power"},
        {"type": "solar", "stat_energy_from": "sensor.mppt_150_85_total_yield",
         "config_entry_solar_forecast": ["KOZFORECASTTWO00000000000000"],
         "stat_rate": "sensor.mppt_150_85_pv_yield_power"},
        {"type": "grid", "stat_energy_from": "sensor.multiplus_energy_from_ac_in_1_to_inverter",
         "stat_energy_to": None},
    ],
}


@pytest.fixture
def energy():
    """The dashboard's preferences: set ``["prefs"]`` (or ``["raises"]``)
    before opening a form. Fails the test if anything ever wrote them."""
    update = AsyncMock()
    state = {"prefs": None, "raises": None}

    async def get_manager(hass):
        if state["raises"]:
            raise state["raises"]
        return SimpleNamespace(data=state["prefs"], async_update=update)

    with patch(f"{HELPERS}.async_get_manager", get_manager):
        yield state
    update.assert_not_awaited()


def _hub(hass, options=None):
    hub = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=9, title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options=options or {},
    )
    hub.add_to_hass(hass)
    return hub


def _forecast_device(hass, entry_id, domain=OPEN_METEO):
    """A forecast config entry with the given id and its one device."""
    source = MockConfigEntry(domain=domain, entry_id=entry_id, title=entry_id)
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry_id, identifiers={(domain, entry_id)}, name=entry_id
    )
    return device.id


def _states(hass, *entity_ids, attrs=POWER):
    for entity_id in entity_ids:
        hass.states.async_set(entity_id, "1", attrs)


def _suggested(result, key):
    for marker in flat_fields(result["data_schema"]):
        if getattr(marker, "schema", None) == key:
            return (getattr(marker, "description", None) or {}).get("suggested_value")
    raise AssertionError(f"{key} not in the form")


def _default(result, key):
    for marker in flat_fields(result["data_schema"]):
        if getattr(marker, "schema", None) == key:
            default = getattr(marker, "default", None)
            return default() if callable(default) else default
    raise AssertionError(f"{key} not in the form")


async def _inverter_features(hass):
    """Open 'Add Inverter' on a hub-only site: the features page."""
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"setup_type": "inverter"})
    assert result["step_id"] == "inverter_features"
    return result


async def _to_config(hass, result, features):
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INVERTER_FEATURES: features}
    )
    assert result["step_id"] == "inverter_config"
    return result


def _config_input(**extra):
    return {CONF_NAME: "Inv", CONF_ENTITY_ID: "lj_inv", **extra}


async def _to_battery(hass, result, **config):
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _config_input(**config)
    )
    assert result["step_id"] == "inverter_battery", result
    return result


# ── The first inverter ──────────────────────────────────────────────────


async def test_kozolec_inverter_offers_features_forecasts_and_the_one_battery(
    hass: HomeAssistant, energy
):
    energy["prefs"] = KOZOLEC
    _hub(hass)
    first = _forecast_device(hass, "KOZFORECASTONE00000000000000")
    second = _forecast_device(hass, "KOZFORECASTTWO00000000000000")
    _states(hass, "sensor.mppt_150_70_pv_yield_power", "sensor.mppt_150_85_pv_yield_power",
            "sensor.gx_device_dc_battery_power_inverted")
    _states(hass, "sensor.jk_bms_id_512_charge", attrs={"device_class": "battery", "unit_of_measurement": "%"})

    result = await _inverter_features(hass)
    assert _suggested(result, CONF_INVERTER_FEATURES) == ["solar", "battery"]

    result = await _to_config(hass, result, ["solar", "battery"])
    # two arrays: LJ takes one total sensor, so none is offered
    assert _suggested(result, CONF_SOLAR_PRODUCTION_ENTITY_ID) is None
    assert sorted(_suggested(result, CONF_SOLAR_FORECAST_DEVICE_IDS)) == sorted([first, second])

    result = await _to_battery(hass, result)
    # the dashboard's own signed sensor: no inversion needed
    assert _suggested(result, CONF_BATTERY_POWER_ENTITY_ID) == "sensor.gx_device_dc_battery_power_inverted"
    assert _suggested(result, CONF_BATTERY_SOC_ENTITY_ID) == "sensor.jk_bms_id_512_charge"
    assert _default(result, CONF_BATTERY_CAPACITY_KWH) == 62
    # the dashboard has a number, not a sensor
    assert _suggested(result, CONF_BATTERY_CAPACITY_ENTITY_ID) is None


async def test_home_inverter_offers_the_one_array_and_no_battery(hass: HomeAssistant, energy):
    energy["prefs"] = HOME
    _hub(hass)
    device = _forecast_device(hass, "HOMEFORECAST0000000000000000")
    _forecast_device(hass, "OTHERPROVIDER000000000000000", domain="forecast_solar")
    _states(hass, "sensor.solaredge_se17k_i1_ac_power")

    result = await _inverter_features(hass)
    assert _suggested(result, CONF_INVERTER_FEATURES) == ["solar"]
    result = await _to_config(hass, result, ["solar"])
    assert _suggested(result, CONF_SOLAR_PRODUCTION_ENTITY_ID) == "sensor.solaredge_se17k_i1_ac_power"
    assert _suggested(result, CONF_SOLAR_FORECAST_DEVICE_IDS) == [device]


async def test_only_the_first_inverter_of_a_hub_is_offered_anything(hass: HomeAssistant, energy):
    energy["prefs"] = KOZOLEC
    hub = _hub(hass)
    add_inverter(hass, hub, {}, slug="first")
    _forecast_device(hass, "KOZFORECASTONE00000000000000")
    _states(hass, "sensor.gx_device_dc_battery_power_inverted")

    result = await _inverter_features(hass)
    assert not _suggested(result, CONF_INVERTER_FEATURES)
    result = await _to_config(hass, result, ["solar", "battery"])
    assert not _suggested(result, CONF_SOLAR_FORECAST_DEVICE_IDS)
    result = await _to_battery(hass, result)
    assert _suggested(result, CONF_BATTERY_POWER_ENTITY_ID) is None
    assert _suggested(result, CONF_BATTERY_SOC_ENTITY_ID) is None
    assert _default(result, CONF_BATTERY_CAPACITY_KWH) != 62


async def test_a_sensor_with_no_state_is_not_offered(hass: HomeAssistant, energy):
    energy["prefs"] = HOME
    _hub(hass)  # sensor.solaredge_se17k_i1_ac_power has no state
    result = await _to_config(hass, await _inverter_features(hass), ["solar"])
    assert _suggested(result, CONF_SOLAR_PRODUCTION_ENTITY_ID) is None


async def test_typed_values_win_and_a_cleared_field_stays_cleared(hass: HomeAssistant, energy):
    """A failed submit shows what was typed, with nothing filled back in."""
    energy["prefs"] = KOZOLEC
    _hub(hass)
    _states(hass, "sensor.gx_device_dc_battery_power_inverted", "sensor.my_battery_power")
    _states(hass, "sensor.jk_bms_id_512_charge", "sensor.my_soc",
            attrs={"device_class": "battery", "unit_of_measurement": "%"})
    # a capacity sensor in W: the one error the page can raise
    _states(hass, "sensor.bad_capacity",
            attrs={"device_class": "energy_storage", "unit_of_measurement": "W"})

    result = await _to_config(hass, await _inverter_features(hass), ["solar", "battery"])
    result = await _to_battery(hass, result)
    assert _suggested(result, CONF_BATTERY_POWER_ENTITY_ID) == "sensor.gx_device_dc_battery_power_inverted"

    # SOC typed, power cleared (omitted), and the unit error
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BATTERY_SOC_ENTITY_ID: "sensor.my_soc", CONF_BATTERY_CAPACITY_ENTITY_ID: "sensor.bad_capacity"},
    )
    assert result["step_id"] == "inverter_battery" and result["errors"]
    assert _suggested(result, CONF_BATTERY_SOC_ENTITY_ID) == "sensor.my_soc"
    assert _suggested(result, CONF_BATTERY_POWER_ENTITY_ID) is None


async def test_a_cleared_production_field_stays_cleared_after_an_error(hass: HomeAssistant, energy):
    energy["prefs"] = HOME
    _hub(hass)
    _states(hass, "sensor.solaredge_se17k_i1_ac_power")
    # a max-power sensor reading amps: the unit error
    _states(hass, "sensor.wrong_unit", attrs={"device_class": "power", "unit_of_measurement": "A"})

    result = await _to_config(hass, await _inverter_features(hass), ["solar"])
    assert _suggested(result, CONF_SOLAR_PRODUCTION_ENTITY_ID) == "sensor.solaredge_se17k_i1_ac_power"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _config_input(**{CONF_INVERTER_MAX_POWER_ENTITY_ID: "sensor.wrong_unit"})
    )
    assert result["step_id"] == "inverter_config" and result["errors"]
    assert _suggested(result, CONF_SOLAR_PRODUCTION_ENTITY_ID) is None


# ── The hub's grid ──────────────────────────────────────────────────────


async def _hub_grid(hass):
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"setup_type": "hub"})
    result = await flow.async_configure(result["flow_id"], {CONF_NAME: "New hub", CONF_ENTITY_ID: "new_hub"})
    assert result["step_id"] == "hub_grid"
    return result


def _meter(hass, name, readings):
    """A meter device with sensors ``(object_id, unit)``; returns the entity
    ids by object id."""
    source = MockConfigEntry(domain="meter")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, name=name, identifiers={("meter", name)}
    )
    classes = {"W": "power", "A": "current"}
    for object_id, unit in readings:
        er.async_get(hass).async_get_or_create(
            "sensor", "meter", object_id, suggested_object_id=object_id, config_entry=source,
            device_id=device.id, original_device_class=classes[unit], unit_of_measurement=unit,
        )
        hass.states.async_set(f"sensor.{object_id}", "1", {"unit_of_measurement": unit})


def _phases(prefix, unit, kind):
    return [(f"{prefix}_{kind}_{p}", unit) for p in "abc"]


def _grid_prefs(raw):
    return {"energy_sources": [{
        "type": "grid", "stat_energy_from": "sensor.imported",
        "power_config": {"stat_rate_inverted": raw}, "stat_rate": f"{raw}_inverted"}]}


def _ct(result):
    return [_suggested(result, key) for key in (
        CONF_PHASE_A_CURRENT_ENTITY_ID, CONF_PHASE_B_CURRENT_ENTITY_ID, CONF_PHASE_C_CURRENT_ENTITY_ID)]


async def test_hub_grid_takes_the_meters_watts_and_inverts_when_the_dashboard_says_so(
    hass: HomeAssistant, energy
):
    energy["prefs"] = _grid_prefs("sensor.m1_ac_power")
    _meter(hass, "M1", [("m1_ac_power", "W"), *_phases("m1", "A", "current"), *_phases("m1", "W", "power")])
    result = await _hub_grid(hass)
    assert _ct(result) == ["sensor.m1_power_a", "sensor.m1_power_b", "sensor.m1_power_c"]
    assert _default(result, CONF_INVERT_PHASES) is True


async def test_hub_grid_with_amps_only_does_not_invert(hass: HomeAssistant, energy):
    energy["prefs"] = _grid_prefs("sensor.m1_ac_power")
    _meter(hass, "M1", [("m1_ac_power", "W"), *_phases("m1", "A", "current")])
    result = await _hub_grid(hass)
    assert _ct(result) == ["sensor.m1_current_a", "sensor.m1_current_b", "sensor.m1_current_c"]
    assert _default(result, CONF_INVERT_PHASES) is False


async def test_hub_grid_without_a_dashboard_grid_meter_keeps_the_detection(hass: HomeAssistant, energy):
    energy["prefs"] = KOZOLEC  # a grid with no power sensor at all
    _meter(hass, "SolarEdge M1", [(f"solaredge_m1_ac_current_{p}", "A") for p in "abc"]
           + [(f"solaredge_m1_ac_power_{p}", "W") for p in "abc"])
    result = await _hub_grid(hass)
    assert _ct(result) == [f"sensor.solaredge_m1_ac_power_{p}" for p in "abc"]
    assert _default(result, CONF_INVERT_PHASES) is False


# ── Never on a Configure page; never when the dashboard cannot be read ──


async def test_configure_pages_never_prefill(hass: HomeAssistant, energy, mock_setup):
    from custom_components.dynamic_ocpp_evse.const import INVERTER_FEATURE_BATTERY, INVERTER_FEATURE_SOLAR

    energy["prefs"] = KOZOLEC
    _states(hass, "sensor.mppt_150_70_pv_yield_power", "sensor.gx_device_dc_battery_power_inverted")
    _states(hass, "sensor.jk_bms_id_512_charge", attrs={"device_class": "battery", "unit_of_measurement": "%"})
    hub = _hub(hass)
    inverter = add_inverter(
        hass, hub, {CONF_INVERTER_FEATURES: [INVERTER_FEATURE_SOLAR, INVERTER_FEATURE_BATTERY]}
    )

    async def page(entry, step):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})

    solar = await page(inverter, "inverter_solar")
    assert _suggested(solar, CONF_SOLAR_PRODUCTION_ENTITY_ID) is None
    assert not _suggested(solar, CONF_SOLAR_FORECAST_DEVICE_IDS)
    battery = await page(inverter, "inverter_battery")
    assert _suggested(battery, CONF_BATTERY_POWER_ENTITY_ID) is None
    assert _suggested(battery, CONF_BATTERY_SOC_ENTITY_ID) is None
    assert _default(battery, CONF_BATTERY_CAPACITY_KWH) != 62
    connection = await page(hub, "hub_connection")
    assert _ct(connection) == [None, None, None]


async def test_an_unreadable_dashboard_leaves_the_forms_as_they_were(hass: HomeAssistant, energy):
    energy["raises"] = RuntimeError("store not loaded")
    _hub(hass)
    result = await _inverter_features(hass)
    assert result["type"] == FlowResultType.FORM and not _suggested(result, CONF_INVERTER_FEATURES)
    result = await _to_config(hass, result, ["solar", "battery"])
    assert _suggested(result, CONF_SOLAR_PRODUCTION_ENTITY_ID) is None
    assert (await _hub_grid(hass))["type"] == FlowResultType.FORM
