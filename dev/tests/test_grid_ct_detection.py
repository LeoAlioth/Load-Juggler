"""Grid CT auto-detection on each brand's own entity ids.

Each brand row is the entities one meter publishes - the ids its GRID_CT
entry is meant to match, with the voltage / energy / current / power
siblings beside them - in registration order, which is the order detection
scans them in.
"""

import pytest

from custom_components.dynamic_ocpp_evse.config_flow.helpers import _auto_detect_phase_entities
from custom_components.dynamic_ocpp_evse.detection_patterns import PHASE_PATTERNS


def _ph(template, unit, names="123"):
    """One reading per phase: ``template`` formatted with each of ``names``."""
    return [(template.format(n), unit) for n in names]


# brand: ({device name: [(object_id, unit), ...]}, (template, names) the
# patterns propose, or None for nothing)
BRANDS = {
    "SolarEdge": (
        {"SolarEdge M1": _ph("solaredge_m1_ac_voltage_{}n", "V", "abc")
            + _ph("solaredge_m1_ac_voltage_{}", "V", ("ab", "bc", "ca"))
            + _ph("solaredge_m1_exported_{}_kwh", "kWh", "abc")
            + _ph("solaredge_m1_ac_current_{}", "A", "abc")
            + _ph("solaredge_m1_ac_power_{}", "W", "abc")},
        ("solaredge_m1_ac_current_{}", "abc"),
    ),
    # ha-solarman deye_p3: the CTs' power first
    "Deye": (
        {"Deye": _ph("deye_grid_l{}_voltage", "V")
            + _ph("deye_internal_ct{}_power", "W") + _ph("deye_internal_ct{}_current", "A")
            + _ph("deye_external_ct{}_current", "A") + _ph("deye_external_ct{}_power", "W")
            + _ph("deye_grid_l{}_power", "W")},
        ("deye_external_ct{}_power", "123"),
    ),
    # deye_p3 with its CT sensors disabled: the grid's own power, not the
    # voltage defined before it
    "Deye, grid only": (
        {"Deye": _ph("deye_grid_l{}_voltage", "V") + _ph("deye_grid_l{}_power", "W")},
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
    ),
    "Huawei": (
        {"Power meter": _ph("power_meter_phase_{}_voltage", "V", "abc")
            + _ph("power_meter_phase_{}_current", "A", "abc")
            + _ph("power_meter_phase_{}_active_power", "W", "abc")
            + [("power_meter_active_power", "W"), ("power_meter_consumption", "kWh")]},
        ("power_meter_phase_{}_current", "abc"),
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
    ),
    # voltage registered first
    "Victron": (
        {"Grid": _ph("victron_grid_l{}_voltage", "V") + _ph("victron_grid_l{}_current", "A")
            + _ph("victron_grid_l{}_power", "W") + _ph("victron_grid_l{}_energy_forward", "kWh")},
        ("victron_grid_l{}_power", "123"),
    ),
    "Sofar": (
        {"Sofar": _ph("sofar_voltage_grid_l{}", "V") + _ph("sofar_current_grid_l{}", "A")
            + _ph("sofar_active_power_grid_l{}", "W")},
        ("sofar_current_grid_l{}", "123"),
    ),
    # mkaiser's Modbus YAML: registered, but no device
    "Sungrow": (
        {None: _ph("meter_phase_{}_voltage", "V", "abc") + _ph("meter_phase_{}_current", "A", "abc")
            + _ph("meter_phase_{}_active_power", "W", "abc")},
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
    devices, expected = BRANDS[brand]
    ids = [f"sensor.{object_id}" for rows in devices.values() for object_id, _ in rows]
    assert _auto_detect_phase_entities(ids, PHASE_PATTERNS) == _triple(expected)
