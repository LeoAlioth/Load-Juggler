"""Grid CT auto-detection on each brand's own entity ids.

Each brand row is the entities one meter publishes - the ids its GRID_CT
entry is meant to match, with the voltage / energy / current / power
siblings beside them - in registration order, which is the order detection
scans them in. The patterns propose a triple (_auto_detect_phase_entities);
the hub setup page then offers the meter's own watts in place of its amps
(_power_beside), or, with no complete triple, the phases one meter has
(_same_device_fill) - leaving out ids a pattern's device test turns down,
which only the device registry tells (_DEVICE_INFO).
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
    # StephanJoubert's Solarman, deye_sg04lp3: "External CT L1 Power"
    "Deye, old Solarman": (
        {"Solarman": [("solarman_total_grid_power", "W")]
            + _ph("solarman_grid_voltage_l{}", "V") + _ph("solarman_internal_ct_l{}_power", "W")
            + _ph("solarman_external_ct_l{}_power", "W") + _ph("solarman_load_l{}_power", "W")
            + _ph("solarman_current_l{}", "A") + _ph("solarman_inverter_l{}_power", "W")},
        ("solarman_external_ct_l{}_power", "123"),
        ("solarman_external_ct_l{}_power", "123"),
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
    # HA core names them from its translations today ("Current phase 1")
    "Fronius, translated names": (
        {"Smart Meter TS 65A-3": _ph("smart_meter_ts_65a_3_voltage_phase_{}", "V")
            + _ph("smart_meter_ts_65a_3_voltage_phase_{}", "V", ("1_2", "2_3", "3_1"))
            + [("smart_meter_ts_65a_3_real_energy_consumed", "kWh")]
            + _ph("smart_meter_ts_65a_3_current_phase_{}", "A")
            + _ph("smart_meter_ts_65a_3_real_power_phase_{}", "W")
            + _ph("smart_meter_ts_65a_3_apparent_power_phase_{}", "VA")
            + _ph("smart_meter_ts_65a_3_reactive_power_phase_{}", "var")},
        ("smart_meter_ts_65a_3_current_phase_{}", "123"),
        ("smart_meter_ts_65a_3_real_power_phase_{}", "123"),
    ),
    # a Zaptec charger's "Current phase 1" is not a Fronius meter's
    "Huawei, Zaptec charger beside": (
        {"Zaptec Go": _ph("zaptec_go_available_current_phase_{}", "A")
            + _ph("zaptec_go_current_phase_{}", "A"),
         "Power meter": _ph("power_meter_phase_{}_current", "A", "abc")
            + _ph("power_meter_phase_{}_active_power", "W", "abc")},
        ("power_meter_phase_{}_current", "abc"),
        ("power_meter_phase_{}_active_power", "abc"),
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
    # sfstar/hass-victron names a unit 0/100/225 device victron_<register>;
    # voltage registered first
    "Victron": (
        {"Grid": _ph("victron_grid_l{}_voltage", "V") + _ph("victron_grid_l{}_current", "A")
            + _ph("victron_grid_l{}_power", "W") + _ph("victron_grid_l{}_energy_forward", "kWh")},
        ("victron_grid_l{}_power", "123"),
        ("victron_grid_l{}_power", "123"),
    ),
    # ... any other unit victron<register><unit> - a grid meter on unit 30
    "Victron, grid meter on unit 30": (
        {"grid": _ph("victrongrid_l{}_power30", "W") + _ph("victrongrid_l{}_energy_forward30", "kWh")
            + _ph("victrongrid_l{}_voltage30", "V") + _ph("victrongrid_l{}_current30", "A")},
        ("victrongrid_l{}_current30", "123"),
        ("victrongrid_l{}_power30", "123"),
    ),
    # ... and victron_<register>_<unit> before February 2026
    "Victron, unit 30, older entity ids": (
        {"grid": _ph("victron_grid_l{}_power_30", "W") + _ph("victron_grid_l{}_voltage_30", "V")
            + _ph("victron_grid_l{}_current_30", "A")},
        ("victron_grid_l{}_current_30", "123"),
        ("victron_grid_l{}_power_30", "123"),
    ),
    # HA core's victron_gx (and ha-victron-mqtt): a grid meter's device is
    # named after its product and instance, "Power on L1" signed
    "Victron GX grid meter": (
        {"ET340 Energy Meter (ID: 30)": _ph("et340_energy_meter_id_30_voltage_on_l{}", "V")
            + _ph("et340_energy_meter_id_30_current_on_l{}", "A")
            + _ph("et340_energy_meter_id_30_power_on_l{}", "W")
            + _ph("et340_energy_meter_id_30_grid_consumption_on_l{}", "kWh")
            + _ph("et340_energy_meter_id_30_feed_in_on_l{}", "kWh")
            + _ph("et340_energy_meter_id_30_voltage_l{}", "V", ("1_to_l2", "2_to_l3", "3_to_l1"))
            + [("et340_energy_meter_id_30_current", "A"), ("et340_energy_meter_id_30_power", "W"),
               ("et340_energy_meter_id_30_frequency", "Hz")]},
        ("et340_energy_meter_id_30_power_on_l{}", "123"),
        ("et340_energy_meter_id_30_power_on_l{}", "123"),
    ),
    # ... ha-victron-mqtt, a grid meter given a custom name on the GX
    "Victron GX grid meter, ha-victron-mqtt": (
        {"Grid meter": _ph("grid_meter_voltage_on_l{}", "V") + _ph("grid_meter_current_on_l{}", "A")
            + _ph("grid_meter_power_on_l{}", "W")},
        ("grid_meter_power_on_l{}", "123"),
        ("grid_meter_power_on_l{}", "123"),
    ),
    # ... the GX's own device, "Victron Venus": "Grid power L1"
    "Victron GX system": (
        {"Victron Venus": _ph("victron_venus_consumption_current_l{}", "A")
            + _ph("victron_venus_consumption_power_l{}", "W")
            + _ph("victron_venus_grid_current_l{}", "A") + _ph("victron_venus_grid_power_l{}", "W")
            + _ph("victron_venus_pv_on_grid_power_l{}", "W") + [("victron_venus_grid_phases", "phases")]},
        ("victron_venus_grid_power_l{}", "123"),
        ("victron_venus_grid_power_l{}", "123"),
    ),
    # ... both: the meter's own
    "Victron GX grid meter and system": (
        {"Victron Venus": _ph("victron_venus_grid_current_l{}", "A") + _ph("victron_venus_grid_power_l{}", "W"),
         "ET340 Energy Meter (ID: 30)": _ph("et340_energy_meter_id_30_current_on_l{}", "A")
            + _ph("et340_energy_meter_id_30_power_on_l{}", "W")},
        ("et340_energy_meter_id_30_power_on_l{}", "123"),
        ("et340_energy_meter_id_30_power_on_l{}", "123"),
    ),
    # ... an AC load meter of the same product says the same, on its own
    # instance: only the device's identifier (acload_31) tells
    "Victron GX grid meter, AC load meter beside": (
        {"ET340 Energy Meter (ID: 31)": _ph("et340_energy_meter_id_31_current_on_l{}", "A")
            + _ph("et340_energy_meter_id_31_power_on_l{}", "W"),
         "ET340 Energy Meter (ID: 30)": _ph("et340_energy_meter_id_30_current_on_l{}", "A")
            + _ph("et340_energy_meter_id_30_power_on_l{}", "W")},
        ("et340_energy_meter_id_31_power_on_l{}", "123"),
        ("et340_energy_meter_id_30_power_on_l{}", "123"),
    ),
    # another integration's charger in the same words is not a Victron meter
    "Charger saying Power on L1": (
        {"Wallbox": _ph("wallbox_current_on_l{}", "A") + _ph("wallbox_power_on_l{}", "W")},
        ("wallbox_power_on_l{}", "123"),
        None,
    ),
    # ha-solarman deye_string, its default device name: "Grid L1 Current" is
    # the inverter's output (0x004C), spelled as Victron's; no per-phase meter
    "Deye string inverter": (
        {"Inverter": [("inverter_pv_power", "W"), ("inverter_pv1_power", "W"),
                      ("inverter_pv1_voltage", "V"), ("inverter_pv1_current", "A"),
                      ("inverter_today_production", "kWh"), ("inverter_total_production", "kWh")]
            + _ph("inverter_grid_l{}_voltage", "V", ("12", "23", "31"))
            + _ph("inverter_grid_l{}_voltage", "V") + _ph("inverter_grid_l{}_current", "A")
            + [("inverter_output_ac_power", "W"), ("inverter_input_power", "W"),
               ("inverter_output_apparent_power", "VA"), ("inverter_power", "W"),
               ("inverter_output_reactive_power", "var"), ("inverter_load_power", "W"),
               ("inverter_grid_power", "W")]},
        None,
        None,
    ),
    # the same inverter on a Victron's AC output: the Victron's grid meter
    "Deye string inverter, Victron grid meter beside": (
        {"Inverter": _ph("inverter_grid_l{}_voltage", "V") + _ph("inverter_grid_l{}_current", "A")
            + [("inverter_output_ac_power", "W"), ("inverter_grid_power", "W")],
         "grid": _ph("victrongrid_l{}_power30", "W") + _ph("victrongrid_l{}_current30", "A")},
        ("victrongrid_l{}_current30", "123"),
        ("victrongrid_l{}_power30", "123"),
    ),
    # Solarman sofar_g3hyd (both integrations): the grid meter is the PCC;
    # StephanJoubert's publishes the reactive power as W
    "Sofar G3 hybrid": (
        {"Sofar": _ph("sofar_voltage_phase_{}", "V", "rst") + _ph("sofar_current_output_{}", "A", "rst")
            + _ph("sofar_activepower_output_{}", "W", "rst") + _ph("sofar_reactivepower_pcc_{}", "W", "rst")
            + _ph("sofar_current_pcc_{}", "A", "rst") + _ph("sofar_activepower_pcc_{}", "W", "rst")
            + [("sofar_activepower_pcc_total", "W")]},
        ("sofar_activepower_pcc_{}", "rst"),
        ("sofar_activepower_pcc_{}", "rst"),
    ),
    # HA core's Sofar: the same readings, translated names, kW
    "Sofar, HA core": (
        {"Sofar": _ph("sofar_voltage_l{}", "V") + _ph("sofar_current_output_l{}", "A")
            + _ph("sofar_active_power_output_l{}", "kW") + _ph("sofar_reactive_power_pcc_l{}", "kvar")
            + _ph("sofar_active_power_pcc_l{}n", "kW", "12") + _ph("sofar_current_pcc_l{}", "A")
            + _ph("sofar_active_power_pcc_l{}", "kW")},
        ("sofar_active_power_pcc_l{}", "123"),
        ("sofar_active_power_pcc_l{}", "123"),
    ),
    # ha-solarman sofar_hybrid (HYD-ES, single phase): "Grid L1 Current" is
    # the inverter's own (0x0207, Sofar's "Grid A current"; L2/L3 read
    # registers Sofar marks reserved), spelled as Victron's; the meter is the
    # one "Grid power" - no per-phase grid reading
    "Sofar HYD-ES": (
        {"Sofar": _ph("sofar_grid_l{}_voltage", "V") + _ph("sofar_grid_l{}_current", "A")
            + [("sofar_grid_power", "W")]},
        None,
        None,
    ),
    # ha-solarman kstar_hybrid: "Grid L1 Power" is KSTAR's "R Phase Meter
    # Power" (3100); the inverter's own (3126) is "L1 Power"
    "KStar hybrid": (
        {"KStar": [("kstar_today_energy_import", "kWh")]
            + _ph("kstar_grid_l{}_voltage", "V") + _ph("kstar_grid_l{}_frequency", "Hz")
            + _ph("kstar_grid_l{}_current", "A") + _ph("kstar_grid_l{}_power", "W")
            + [("kstar_grid_power", "W")] + _ph("kstar_l{}_voltage", "V") + _ph("kstar_l{}_current", "A")
            + _ph("kstar_l{}_power", "W") + _ph("kstar_load_ups_l{}_power", "W")
            + _ph("kstar_load_l{}_power", "W")},
        ("kstar_grid_l{}_power", "123"),
        ("kstar_grid_l{}_power", "123"),
    ),
    # ha-solarman megarevo_r-3h: "Grid L1 Power" is Megarevo's "Grid_A Power"
    # (0x3112, its CT's); the inverter's own "INV_A Power" (0x3192) is not read
    "MegaRevo R-3H": (
        {"MegaRevo": [("megarevo_today_energy_export", "kWh"), ("megarevo_today_energy_import", "kWh")]
            + _ph("megarevo_grid_l{}_voltage", "V") + _ph("megarevo_grid_l{}_current", "A")
            + _ph("megarevo_grid_l{}_power", "W") + [("megarevo_grid_frequency", "Hz")]
            + _ph("megarevo_load_l{}_voltage", "V") + _ph("megarevo_load_l{}_current", "A")
            + _ph("megarevo_load_l{}_power", "W") + _ph("megarevo_load_l{}", "%")},
        ("megarevo_grid_l{}_power", "123"),
        ("megarevo_grid_l{}_power", "123"),
    ),
    # ha-solarman afore_hybrid: "Grid L1 Power" is Afore's "R-phase
    # grid-connected power (meter)" (529); the inverter's is "Output L1 Power"
    # (516). Its L3 output current is a second "Output L2 Current".
    "Afore hybrid": (
        {"Afore": _ph("afore_grid_l{}_voltage", "V") + _ph("afore_output_l{}_current", "A", "12")
            + [("afore_output_l2_current_2", "A")] + _ph("afore_grid_l{}_frequency", "Hz")
            + _ph("afore_output_l{}_power", "W") + [("afore_power", "W")]
            + _ph("afore_grid_l{}_power", "W") + [("afore_grid_power", "W")]
            + _ph("afore_load_l{}_power", "W") + [("afore_today_energy_import", "kWh")]},
        ("afore_grid_l{}_power", "123"),
        ("afore_grid_l{}_power", "123"),
    ),
    # ha-solarman afore_BNTxxxKTL-2mppt (Afore BNT T4 string inverter, no
    # meter): its "Grid L1 Power" is line voltage x its own output current
    # (input registers 1 and 4). The ids are a meter's; only the device's
    # model tells them apart, so the patterns propose them and the page not.
    "Afore BNT string inverter": (
        {"Afore BNT": _ph("afore_bnt_grid_l{}_voltage", "V") + _ph("afore_bnt_grid_l{}_current", "A")
            + _ph("afore_bnt_grid_l{}_power", "W") + [("afore_bnt_grid_frequency", "Hz")]
            + [("afore_bnt_pv_power", "W"), ("afore_bnt_pv1_power", "W"), ("afore_bnt_pv1_voltage", "V"),
               ("afore_bnt_pv1_current", "A"), ("afore_bnt_power", "W"),
               ("afore_bnt_today_production", "kWh"), ("afore_bnt_total_production", "kWh")]},
        ("afore_bnt_grid_l{}_power", "123"),
        None,
    ),
    # ... on a Victron's AC output: the Victron's grid meter
    "Afore BNT string inverter, Victron grid meter beside": (
        {"Afore BNT": _ph("afore_bnt_grid_l{}_voltage", "V") + _ph("afore_bnt_grid_l{}_current", "A")
            + _ph("afore_bnt_grid_l{}_power", "W"),
         "grid": _ph("victrongrid_l{}_power30", "W") + _ph("victrongrid_l{}_current30", "A")},
        ("afore_bnt_grid_l{}_power", "123"),
        ("victrongrid_l{}_power30", "123"),
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


_DEVICE_CLASS = {"W": "power", "kW": "power", "A": "current", "V": "voltage", "kWh": "energy",
                 "VA": "apparent_power", "var": "reactive_power", "kvar": "reactive_power"}

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
    # ha-solarman deye_hybrid (SG0*LP1, split phase): "Grid L1 Power" is
    # Deye's "Grid side L1 power" (167); the inverter's is "Output L1 Power"
    "Deye hybrid, split phase": (
        {"Deye LP1": [("deye_lp1_grid_frequency", "Hz")] + _ph("deye_lp1_grid_l{}_voltage", "V", "12")
            + [("deye_lp1_grid_voltage", "V")] + _ph("deye_lp1_grid_l{}_current", "A", "12")
            + _ph("deye_lp1_grid_l{}_power", "W", "12") + [("deye_lp1_grid_power", "W")]
            + _ph("deye_lp1_external_ct{}_current", "A", "12")
            + _ph("deye_lp1_external_ct{}_power", "W", "12") + [("deye_lp1_external_power", "W")]
            + _ph("deye_lp1_load_l{}_voltage", "V", "12") + _ph("deye_lp1_load_l{}_power", "W", "12")
            + _ph("deye_lp1_output_l{}_power", "W", "12")
            + [("deye_lp1_today_energy_import", "kWh")]},
        ("deye_lp1_grid_l1_power", "deye_lp1_grid_l2_power", None),
    ),
}

# What the device registry holds beside a device's name: ha-solarman's model
# is its definition's (info, else the file name's); victron_gx identifies a
# device as <installation>_<device type>_<instance>
_DEVICE_INFO = {
    "ET340 Energy Meter (ID: 30)": {"identifiers": {("victron_gx", "c0619ab1a2b3_grid_30")}},
    "ET340 Energy Meter (ID: 31)": {"identifiers": {("victron_gx", "c0619ab1a2b3_acload_31")}},
    "Victron Venus": {"identifiers": {("victron_gx", "c0619ab1a2b3_system_0")}},
    "Grid meter": {"identifiers": {("victron_mqtt", "c0619ab1a2b3_grid_30")}},
    "KStar": {"model": "Hybrid Inverter"},
    "MegaRevo": {"model": "R-3H"},
    "Afore": {"model": "HYBRID"},
    "Afore BNT": {"model": "BNTXXXKTL-2MPPT"},
    "Deye LP1": {"model": "SG0*LP1"},
}


async def _hub_grid_offers(hass: HomeAssistant, devices: dict, device_classes: bool = True) -> dict:
    """Register the meters, open the hub setup page, read the CT suggestions."""
    source = MockConfigEntry(domain="meter")
    source.add_to_hass(hass)
    for name, rows in devices.items():
        device = name and dr.async_get(hass).async_get_or_create(
            config_entry_id=source.entry_id, name=name,
            **{"identifiers": {("meter", name)}} | _DEVICE_INFO.get(name, {}),
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
