"""Tests for the hot water tank device type — setpoint resolution.

Machine-authored tests — not yet human-reviewed.

resolve_tank_setpoint is the core new logic: given the tank's operating mode,
the three setpoints, the element power and the hub state, it picks which
setpoint (away / normal / boost) the climate entity should target.
"""

from custom_components.dynamic_ocpp_evse.hot_water_tank import resolve_tank_setpoint
from custom_components.dynamic_ocpp_evse.const import (
    OPERATING_MODE_FREEZE_PROTECTION,
    OPERATING_MODE_NORMAL,
    OPERATING_MODE_SOLAR_ONLY,
)

AWAY, NORMAL, BOOST = 30.0, 45.0, 65.0
ELEMENT_POWER = 2000.0


def _hub(soc=None, soc_min=20, soc_target=80, export=0):
    """Build a hub_data dict for resolve_tank_setpoint."""
    return {
        "battery_soc": soc,
        "battery_soc_min": soc_min,
        "battery_soc_target": soc_target,
        "total_export_power": export,
    }


# --- Freeze Protection: always the away setpoint ---

def test_freeze_protection_always_away():
    for hub in (_hub(), _hub(soc=10), _hub(soc=95, export=9999)):
        result = resolve_tank_setpoint(
            OPERATING_MODE_FREEZE_PROTECTION, AWAY, NORMAL, BOOST, ELEMENT_POWER, hub
        )
        assert result == (AWAY, "away")


# --- Solar Only: setpoint follows the battery SOC band ---

def test_solar_only_below_min_soc_is_away():
    result = resolve_tank_setpoint(
        OPERATING_MODE_SOLAR_ONLY, AWAY, NORMAL, BOOST, ELEMENT_POWER, _hub(soc=15)
    )
    assert result == (AWAY, "away")


def test_solar_only_between_min_and_target_is_normal():
    result = resolve_tank_setpoint(
        OPERATING_MODE_SOLAR_ONLY, AWAY, NORMAL, BOOST, ELEMENT_POWER, _hub(soc=50)
    )
    assert result == (NORMAL, "normal")


def test_solar_only_at_or_above_target_is_boost():
    for soc in (80, 95):
        result = resolve_tank_setpoint(
            OPERATING_MODE_SOLAR_ONLY, AWAY, NORMAL, BOOST, ELEMENT_POWER,
            _hub(soc=soc),
        )
        assert result == (BOOST, "boost")


def test_solar_only_no_battery_defaults_normal():
    result = resolve_tank_setpoint(
        OPERATING_MODE_SOLAR_ONLY, AWAY, NORMAL, BOOST, ELEMENT_POWER, _hub(soc=None)
    )
    assert result == (NORMAL, "normal")


# --- Normal: normal setpoint, raised to boost on surplus ---

def test_normal_no_surplus_is_normal():
    result = resolve_tank_setpoint(
        OPERATING_MODE_NORMAL, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=50, export=0),
    )
    assert result == (NORMAL, "normal")


def test_normal_export_above_element_power_is_boost():
    result = resolve_tank_setpoint(
        OPERATING_MODE_NORMAL, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=50, export=ELEMENT_POWER + 500),
    )
    assert result == (BOOST, "boost")


def test_normal_export_below_element_power_is_normal():
    result = resolve_tank_setpoint(
        OPERATING_MODE_NORMAL, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=50, export=ELEMENT_POWER - 500),
    )
    assert result == (NORMAL, "normal")


def test_normal_soc_over_target_is_boost():
    result = resolve_tank_setpoint(
        OPERATING_MODE_NORMAL, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=85, soc_target=80, export=0),
    )
    assert result == (BOOST, "boost")


def test_normal_no_battery_low_export_is_normal():
    result = resolve_tank_setpoint(
        OPERATING_MODE_NORMAL, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=None, export=0),
    )
    assert result == (NORMAL, "normal")


def test_normal_no_battery_high_export_is_boost():
    result = resolve_tank_setpoint(
        OPERATING_MODE_NORMAL, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=None, export=9999),
    )
    assert result == (BOOST, "boost")


def test_normal_offgrid_full_battery_boosts_without_export():
    """Off-grid: export is always ~0; the SOC > target clause carries boost."""
    result = resolve_tank_setpoint(
        OPERATING_MODE_NORMAL, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=90, soc_target=80, export=0),
    )
    assert result == (BOOST, "boost")
