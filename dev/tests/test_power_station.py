"""Tests for the portable power station device type.

Machine-authored tests — not yet human-reviewed.

Two pure resolvers carry the device type's logic. The station's charge-speed knob
has no zero (200 W floor), so the engine's allocation decides *how fast* and the
backup reserve decides *whether*: dropped below the current battery level, the
station stops drawing from the wall and serves its own loads from its battery.
"""

from custom_components.dynamic_ocpp_evse.const import (
    DEFAULT_OPERATING_MODE_POWER_STATION,
    OPERATING_MODES_POWER_STATION,
    STATION_CHARGE_POWER_STEP,
    resolve_station_charge_speed,
    resolve_station_reserve,
)

MIN_POWER, MAX_POWER = 200.0, 2400.0
NORMAL, STORM, LIMIT = 30, 80, 90


# --- Charge speed: quantised down, never below the floor --------------------

def test_allocation_below_the_floor_cannot_charge():
    # No speed the station accepts, so the caller drops the reserve instead.
    assert resolve_station_charge_speed(150, MIN_POWER, MAX_POWER) is None
    assert resolve_station_charge_speed(0, MIN_POWER, MAX_POWER) is None


def test_allocation_at_the_floor_charges_at_the_floor():
    assert resolve_station_charge_speed(200, MIN_POWER, MAX_POWER) == 200


def test_allocation_floors_to_the_device_step():
    # 250 W of surplus must not become a 300 W draw — that would overdraw the
    # pool by rounding up.
    assert resolve_station_charge_speed(250, MIN_POWER, MAX_POWER) == 200
    assert resolve_station_charge_speed(1290, MIN_POWER, MAX_POWER) == 1200
    assert resolve_station_charge_speed(1300, MIN_POWER, MAX_POWER) == 1300


def test_allocation_is_capped_at_the_configured_max():
    # The configured max, not the hardware's — a station may be held lower.
    assert resolve_station_charge_speed(5000, MIN_POWER, MAX_POWER) == 2400
    assert resolve_station_charge_speed(5000, MIN_POWER, 1500) == 1500


def test_none_allocation_is_not_charging():
    assert resolve_station_charge_speed(None, MIN_POWER, MAX_POWER) is None


def test_step_is_the_devices_granularity():
    # The write deadband uses the same constant, so they can't drift apart.
    assert STATION_CHARGE_POWER_STEP == 100


# --- Reserve: the real on/off gate ------------------------------------------

def test_not_charging_drops_to_the_normal_reserve():
    # Below the station's SOC this stops the wall draw entirely and lets it
    # spend what it stored on its own loads.
    assert resolve_station_reserve(
        charging=False, normal_reserve=NORMAL, storm_reserve=STORM,
        charge_limit=LIMIT, storm_on=False,
    ) == (NORMAL, "normal")


def test_charging_raises_the_reserve_to_the_charge_limit():
    assert resolve_station_reserve(
        charging=True, normal_reserve=NORMAL, storm_reserve=STORM,
        charge_limit=LIMIT, storm_on=False,
    ) == (LIMIT, "charging")


def test_reserve_never_exceeds_the_stations_own_charge_limit():
    # The limit is the user's battery-health cap, read from the device.
    reserve, _ = resolve_station_reserve(
        charging=True, normal_reserve=NORMAL, storm_reserve=STORM,
        charge_limit=70, storm_on=False,
    )
    assert reserve == 70


def test_storm_reserve_wins_over_not_charging():
    # A reserve that yields to a cloudy afternoon is not a reserve.
    assert resolve_station_reserve(
        charging=False, normal_reserve=NORMAL, storm_reserve=STORM,
        charge_limit=LIMIT, storm_on=True,
    ) == (STORM, "storm")


def test_storm_reserve_wins_over_charging_too():
    assert resolve_station_reserve(
        charging=True, normal_reserve=NORMAL, storm_reserve=STORM,
        charge_limit=LIMIT, storm_on=True,
    ) == (STORM, "storm")


# --- Mode catalog -----------------------------------------------------------

def test_absorbing_surplus_is_the_default_mode():
    assert DEFAULT_OPERATING_MODE_POWER_STATION.key == "Excess"


def test_station_modulates_so_it_uses_the_evse_mode_set():
    assert [m.key for m in OPERATING_MODES_POWER_STATION] == [
        "Standard",
        "Solar Priority",
        "Solar Only",
        "Excess",
    ]


def test_station_modes_are_distinct_objects_from_the_evse_ones():
    # OperatingMode uses identity equality, so modes that coincide on every
    # display field still map to their own engine behavior.
    from custom_components.dynamic_ocpp_evse.const import (
        EVSE_MODE_EXCESS,
        STATION_MODE_EXCESS,
    )
    assert STATION_MODE_EXCESS is not EVSE_MODE_EXCESS
    assert STATION_MODE_EXCESS != EVSE_MODE_EXCESS


def test_every_station_mode_maps_to_a_behavior():
    from custom_components.dynamic_ocpp_evse.const import behavior_for

    for mode in OPERATING_MODES_POWER_STATION:
        assert behavior_for(mode)


def test_excess_mode_competes_at_the_lowest_urgency():
    tiers = {m.key: m.priority for m in OPERATING_MODES_POWER_STATION}
    assert tiers["Excess"] == 4
    assert tiers["Standard"] == 1
