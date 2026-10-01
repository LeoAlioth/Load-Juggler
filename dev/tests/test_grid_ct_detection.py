"""Grid CT auto-detection on each brand's own entity ids.

Each brand row is the entities one meter publishes - the ids its GRID_CT
entry is meant to match, with the voltage / energy / current / power
siblings beside them - in registration order, which is the order detection
scans them in. The patterns propose a triple (_auto_detect_phase_entities);
the hub setup page then offers the meter's own watts in place of its amps
(_power_beside), or, with no complete triple, the phases one meter has
(_same_device_fill).
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.config_flow.helpers import _auto_detect_phase_entities
from custom_components.dynamic_ocpp_evse.const import (
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    DOMAIN,
)
from custom_components.dynamic_ocpp_evse.detection_patterns import PHASE_PATTERNS


def _ph(template, unit, names="123"):
    """One reading per phase: ``template`` formatted with each of ``names``."""
    return [(template.format(n), unit) for n in names]


# brand: ({device name, None for none: [(object_id, unit), ...]},
#         (template, names) the patterns propose, ... the setup page offers;
#         None for nothing)
BRANDS = {
    "SolarEdge": (
        {"SolarEdge M1": _ph("solaredge_m1_ac_voltage_{}n", "V", "abc")
            + _ph("solaredge_m1_ac_voltage_{}", "V", ("ab", "bc", "ca"))
            + _ph("solaredge_m1_exported_{}_kwh", "kWh", "abc")
            + _ph("solaredge_m1_ac_current_{}", "A", "abc")
            + _ph("solaredge_m1_ac_power_{}", "W", "abc")},
        ("solaredge_m1_ac_current_{}", "abc"),
        ("solaredge_m1_ac_power_{}", "abc"),
    ),
    # ha-solarman deye_p3: the CTs' power first
    "Deye": (
        {"Deye": _ph("deye_grid_l{}_voltage", "V")
            + _ph("deye_internal_ct{}_power", "W") + _ph("deye_internal_ct{}_current", "A")
            + _ph("deye_external_ct{}_current", "A") + _ph("deye_external_ct{}_power", "W")
            + _ph("deye_grid_l{}_power", "W")},
        ("deye_external_ct{}_power", "123"),
        ("deye_external_ct{}_power", "123"),
    ),
    # deye_p3 with its CT sensors disabled: the grid's own power, not the
    # voltage defined before it
    "Deye, grid only": (
        {"Deye": _ph("deye_grid_l{}_voltage", "V") + _ph("deye_grid_l{}_power", "W")},
        ("deye_grid_l{}_power", "123"),
        ("deye_grid_l{}_power", "123"),
    ),
    "Fronius": (
        {"Smart Meter 63A": _ph("smart_meter_63a_voltage_ac_phase_{}", "V")
            + [("smart_meter_63a_energy_real_consumed", "kWh")]
            + _ph("smart_meter_63a_current_ac_phase_{}", "A")
            + _ph("smart_meter_63a_power_real_phase_{}", "W")
            + _ph("smart_meter_63a_power_apparent_phase_{}", "VA")
            + _ph("smart_meter_63a_power_reactive_phase_{}", "var")},
        ("smart_meter_63a_current_ac_phase_{}", "123"),
        ("smart_meter_63a_power_real_phase_{}", "123"),
    ),
    "Huawei": (
        {"Power meter": _ph("power_meter_phase_{}_voltage", "V", "abc")
            + _ph("power_meter_phase_{}_current", "A", "abc")
            + _ph("power_meter_phase_{}_active_power", "W", "abc")
            + [("power_meter_active_power", "W"), ("power_meter_consumption", "kWh")]},
        ("power_meter_phase_{}_current", "abc"),
        ("power_meter_phase_{}_active_power", "abc"),
    ),
    # HA core names the phases l1..l3, and the NET reading is the grid's
    "Enphase": (
        {"Envoy 122233344455": _ph("envoy_122233344455_current_power_consumption_l{}", "W")
            + _ph("envoy_122233344455_current_net_power_consumption_l{}", "W")
            + _ph("envoy_122233344455_lifetime_energy_consumption_l{}", "kWh")
            + _ph("envoy_122233344455_lifetime_net_energy_consumption_l{}", "kWh")
            + _ph("envoy_122233344455_net_consumption_ct_current_l{}", "A")
            + _ph("envoy_122233344455_voltage_net_consumption_ct_l{}", "V")},
        ("envoy_122233344455_current_net_power_consumption_l{}", "123"),
        ("envoy_122233344455_current_net_power_consumption_l{}", "123"),
    ),
    # voltage registered first
    "Victron": (
        {"Grid": _ph("victron_grid_l{}_voltage", "V") + _ph("victron_grid_l{}_current", "A")
            + _ph("victron_grid_l{}_power", "W") + _ph("victron_grid_l{}_energy_forward", "kWh")},
        ("victron_grid_l{}_power", "123"),
        ("victron_grid_l{}_power", "123"),
    ),
    "Sofar": (
        {"Sofar": _ph("sofar_voltage_grid_l{}", "V") + _ph("sofar_current_grid_l{}", "A")
            + _ph("sofar_active_power_grid_l{}", "W")},
        ("sofar_current_grid_l{}", "123"),
        ("sofar_active_power_grid_l{}", "123"),
    ),
    # mkaiser's Modbus YAML: registered, but no device
    "Sungrow": (
        {None: _ph("meter_phase_{}_voltage", "V", "abc") + _ph("meter_phase_{}_current", "A", "abc")
            + _ph("meter_phase_{}_active_power", "W", "abc")},
        ("meter_phase_{}_current", "abc"),
        ("meter_phase_{}_current", "abc"),
    ),
}


def _triple(expected):
    if expected is None:
        return {"phase_a": None, "phase_b": None, "phase_c": None}
    template, names = expected
    return {slot: f"sensor.{template.format(n)}"
            for slot, n in zip(("phase_a", "phase_b", "phase_c"), names)}


@pytest.mark.parametrize("brand", BRANDS)
def test_patterns_find_each_brands_meter(brand):
    devices, expected, _ = BRANDS[brand]
    ids = [f"sensor.{object_id}" for rows in devices.values() for object_id, _ in rows]
    assert _auto_detect_phase_entities(ids, PHASE_PATTERNS) == _triple(expected)


_DEVICE_CLASS = {"W": "power", "A": "current", "V": "voltage", "kWh": "energy",
                 "VA": "apparent_power", "var": "reactive_power"}

# Only the setup page tells these apart from "nothing": no pattern set
# matches all three phases. (object ids the page offers for A, B, C)
PARTIAL = {
    # a single-phase Victron: its one grid CT, in watts
    "Victron, one phase": (
        {"Grid": [("victron_grid_l1_voltage", "V"), ("victron_grid_l1_current", "A"),
                  ("victron_grid_l1_power", "W")]},
        ("victron_grid_l1_power", None, None),
    ),
    # a meter whose B and C are not named as its pattern expects: that
    # device's own B and C, never the other meter's that the pattern of
    # another brand matches
    "Huawei, B and C named apart": (
        {"Power meter": [("power_meter_phase_a_current", "A"), ("power_meter_phase_a_active_power", "W")]
            + _ph("power_meter_l{}_active_power", "W", "23"),
         "Garage": _ph("garage_current_grid_l{}", "A", "23")},
        ("power_meter_phase_a_active_power", "power_meter_l2_active_power",
         "power_meter_l3_active_power"),
    ),
}


async def _hub_grid_offers(hass: HomeAssistant, devices: dict, device_classes: bool = True) -> dict:
    """Register the meters, open the hub setup page, read the CT suggestions."""
    source = MockConfigEntry(domain="meter")
    source.add_to_hass(hass)
    for name, rows in devices.items():
        device = name and dr.async_get(hass).async_get_or_create(
            config_entry_id=source.entry_id, identifiers={("meter", name)}, name=name,
        )
        for object_id, unit in rows:
            dc = _DEVICE_CLASS.get(unit) if device_classes else None
            er.async_get(hass).async_get_or_create(
                "sensor", "meter", object_id, suggested_object_id=object_id,
                config_entry=source, device_id=device.id if device else None,
                original_device_class=dc, unit_of_measurement=unit,
            )
            hass.states.async_set(f"sensor.{object_id}", "1", {"unit_of_measurement": unit})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "hub"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub"})
    assert result["step_id"] == "hub_grid"
    suggested = {getattr(m, "schema", None): (getattr(m, "description", None) or {}).get("suggested_value")
                 for m in result["data_schema"].schema}
    return {"phase_a": suggested[CONF_PHASE_A_CURRENT_ENTITY_ID],
            "phase_b": suggested[CONF_PHASE_B_CURRENT_ENTITY_ID],
            "phase_c": suggested[CONF_PHASE_C_CURRENT_ENTITY_ID]}


@pytest.mark.parametrize("brand", BRANDS)
async def test_setup_offers_each_brands_meter(hass: HomeAssistant, brand):
    devices, _, expected = BRANDS[brand]
    assert await _hub_grid_offers(hass, devices) == _triple(expected)


@pytest.mark.parametrize("case", PARTIAL)
async def test_setup_fills_from_the_one_meter(hass: HomeAssistant, case):
    devices, expected = PARTIAL[case]
    assert await _hub_grid_offers(hass, devices) == {
        slot: object_id and f"sensor.{object_id}"
        for slot, object_id in zip(("phase_a", "phase_b", "phase_c"), expected)}


async def test_setup_offers_watts_published_without_a_device_class(hass: HomeAssistant):
    """A meter's sensors with a unit but no device class: kind by unit."""
    devices, _, expected = BRANDS["Fronius"]
    assert await _hub_grid_offers(hass, devices, device_classes=False) == _triple(expected)
