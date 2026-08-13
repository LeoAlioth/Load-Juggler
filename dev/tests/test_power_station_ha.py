"""HA-layer tests for the portable power station device type.

Machine-authored tests — not yet human-reviewed.

Covers the two halves that need Home Assistant to exercise: the engine builder
(_build_power_station_charger — bounds, managed draw, status) and the command
module (send_power_station_command — what gets written to which entity). The pure
resolvers are covered in test_power_station.py.

Run under WSL/Linux with pytest-homeassistant-custom-component; HA core needs
fcntl, so these do not collect on macOS.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_CHARGER,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_POWER_STATION,
    CONF_CHARGER_PRIORITY,
    CONF_CONNECTED_TO_PHASE,
    CONF_PHASE_VOLTAGE,
    CONF_MAIN_BREAKER_RATING,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_UPDATE_FREQUENCY,
    CONF_STATION_CHARGE_SPEED_ENTITY_ID,
    CONF_STATION_RESERVE_ENTITY_ID,
    CONF_STATION_BATTERY_LEVEL_ENTITY_ID,
    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
    CONF_STATION_AC_INPUT_ENTITY_ID,
    CONF_STATION_AC_OUTPUT_ENTITY_ID,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_NORMAL_RESERVE,
    CONF_STATION_STORM_RESERVE,
    BEHAVIOR_EXCESS,
    BEHAVIOR_FULL_POWER,
)

SPEED = "number.ef_test_ac_charging_speed"
RESERVE = "number.ef_test_backup_reserve"
SOC = "sensor.ef_test_battery_level"
LIMIT = "number.ef_test_battery_charge_limit_max"
AC_IN = "sensor.ef_test_ac_input_power"
AC_OUT = "sensor.ef_test_ac_output_power"


@pytest.fixture
def hub_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Station Hub",
        data={
            CONF_NAME: "Station Hub",
            CONF_ENTITY_ID: "station_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_phase_a",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_PHASE_VOLTAGE: 230,
        },
    )


@pytest.fixture
def station_entry(hub_entry: MockConfigEntry) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Power Station",
        data={
            CONF_ENTITY_ID: "lj_power_station",
            CONF_NAME: "Power Station",
            ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_DEVICE_TYPE: DEVICE_TYPE_POWER_STATION,
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
            CONF_STATION_CHARGE_SPEED_ENTITY_ID: SPEED,
            CONF_STATION_RESERVE_ENTITY_ID: RESERVE,
        },
        options={
            CONF_CHARGER_PRIORITY: 1,
            CONF_STATION_BATTERY_LEVEL_ENTITY_ID: SOC,
            CONF_STATION_CHARGE_LIMIT_ENTITY_ID: LIMIT,
            CONF_STATION_AC_INPUT_ENTITY_ID: AC_IN,
            CONF_STATION_AC_OUTPUT_ENTITY_ID: AC_OUT,
            CONF_STATION_MIN_CHARGE_POWER: 200,
            CONF_STATION_MAX_CHARGE_POWER: 2400,
            CONF_STATION_NORMAL_RESERVE: 30,
            CONF_STATION_STORM_RESERVE: 80,
            CONF_CONNECTED_TO_PHASE: "A",
            CONF_UPDATE_FREQUENCY: 15,
        },
    )


@pytest.fixture
def domain_data(hass: HomeAssistant, hub_entry, station_entry):
    hass.data[DOMAIN] = {
        "hubs": {
            hub_entry.entry_id: {
                "entry": hub_entry,
                "chargers": [station_entry.entry_id],
                "distribution_mode": "Priority",
            }
        },
        "chargers": {
            station_entry.entry_id: {
                "entry": station_entry,
                "hub_entry_id": hub_entry.entry_id,
                "dynamic_control": True,
            }
        },
        "charger_allocations": {station_entry.entry_id: 0},
    }
    return hass.data[DOMAIN]["chargers"][station_entry.entry_id]


def _set_states(
    hass, speed="200", soc="60", limit="90", ac_in="0", ac_out="0", reserve="30"
):
    """Populate the station's entities. Values mirror the real integration."""
    hass.states.async_set(SPEED, speed, {"unit_of_measurement": "W"})
    hass.states.async_set(RESERVE, reserve, {"unit_of_measurement": "%"})
    hass.states.async_set(SOC, soc, {"device_class": "battery", "unit_of_measurement": "%"})
    hass.states.async_set(LIMIT, limit, {"unit_of_measurement": "%"})
    hass.states.async_set(AC_IN, ac_in, {"device_class": "power", "unit_of_measurement": "W"})
    hass.states.async_set(AC_OUT, ac_out, {"device_class": "power", "unit_of_measurement": "W"})


def _build(hass, station_entry):
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        _build_power_station_charger,
    )

    return _build_power_station_charger(
        hass, station_entry, 230, "lj_power_station", 1
    )


# ── Builder: bounds and behavior ──────────────────────────────────────


async def test_builder_derives_current_bounds_from_configured_watts(
    hass, hub_entry, station_entry, domain_data
):
    _set_states(hass)
    charger = _build(hass, station_entry)
    # 200 W and 2400 W at 230 V, single phase.
    assert charger.min_current == pytest.approx(200 / 230, abs=0.01)
    assert charger.max_current == pytest.approx(2400 / 230, abs=0.01)
    assert charger.device_type == DEVICE_TYPE_POWER_STATION


async def test_builder_prefers_runtime_sliders_over_config(
    hass, hub_entry, station_entry, domain_data
):
    # The sliders exist so bounds can be tuned without a reconfigure.
    _set_states(hass)
    domain_data["station_min_charge_power"] = 400
    domain_data["station_max_charge_power"] = 1200
    charger = _build(hass, station_entry)
    assert charger.min_current == pytest.approx(400 / 230, abs=0.01)
    assert charger.max_current == pytest.approx(1200 / 230, abs=0.01)


async def test_builder_clamps_max_below_min(
    hass, hub_entry, station_entry, domain_data
):
    _set_states(hass)
    domain_data["station_min_charge_power"] = 1000
    domain_data["station_max_charge_power"] = 500
    charger = _build(hass, station_entry)
    assert charger.max_current == charger.min_current


async def test_builder_defaults_to_excess_behavior(
    hass, hub_entry, station_entry, domain_data
):
    _set_states(hass)
    charger = _build(hass, station_entry)
    assert charger.operating_mode == "Excess"
    assert charger.mode_behavior == BEHAVIOR_EXCESS
    assert charger.mode_priority == 4


async def test_storm_reserve_makes_the_station_must_run(
    hass, hub_entry, station_entry, domain_data
):
    # A reserve that may only be filled from surplus is not a reserve, so the
    # storm switch overrides whatever mode is selected.
    _set_states(hass)
    domain_data["operating_mode"] = "Excess"
    domain_data["station_storm_reserve"] = True
    charger = _build(hass, station_entry)
    assert charger.operating_mode == "Standard"
    assert charger.mode_behavior == BEHAVIOR_FULL_POWER
    assert charger.mode_priority == 1


# ── Builder: managed draw ─────────────────────────────────────────────


async def test_managed_draw_is_the_charging_component_only(
    hass, hub_entry, station_entry, domain_data
):
    # 1163 W in, 963 W out: 200 W of that wall draw is charging, the rest is
    # pass-through and belongs to the household, not this load.
    _set_states(hass, ac_in="1163", ac_out="963")
    charger = _build(hass, station_entry)
    total = charger.l1_current + charger.l2_current + charger.l3_current
    assert total == pytest.approx(200 / 230, abs=0.01)


async def test_pure_pass_through_is_not_our_draw(
    hass, hub_entry, station_entry, domain_data
):
    # Input equals output — the station is only passing power through.
    _set_states(hass, ac_in="163", ac_out="163")
    charger = _build(hass, station_entry)
    assert charger.l1_current == 0


async def test_negative_ac_output_convention_is_handled(
    hass, hub_entry, station_entry, domain_data
):
    # Some integrations report per-port output as negative.
    _set_states(hass, ac_in="1163", ac_out="-963")
    charger = _build(hass, station_entry)
    assert charger.l1_current == pytest.approx(200 / 230, abs=0.01)


async def test_draw_falls_back_to_commanded_speed_without_sensors(
    hass, hub_entry, station_entry, domain_data
):
    # No AC sensors configured: trust what we last told the station, but only
    # while we actually asked it to charge.
    entry = _entry_variant(
        station_entry,
        drop_options=(CONF_STATION_AC_INPUT_ENTITY_ID, CONF_STATION_AC_OUTPUT_ENTITY_ID),
    )
    _set_states(hass, speed="800")

    domain_data["station_charging"] = True
    charging = _build(hass, entry)
    assert charging.l1_current == pytest.approx(800 / 230, abs=0.01)

    domain_data["station_charging"] = False
    idle = _build(hass, entry)
    assert idle.l1_current == 0


# ── Builder: status ───────────────────────────────────────────────────


async def test_station_at_its_charge_limit_frees_its_power(
    hass, hub_entry, station_entry, domain_data
):
    _set_states(hass, soc="90", limit="90")
    charger = _build(hass, station_entry)
    # "Available" is the engine's inactive marker — the allocation goes to
    # other loads instead.
    assert charger.connector_status == "Available"


async def test_station_below_its_charge_limit_is_active(
    hass, hub_entry, station_entry, domain_data
):
    _set_states(hass, soc="60", limit="90")
    charger = _build(hass, station_entry)
    assert charger.connector_status == "Charging"


async def test_unavailable_speed_entity_marks_the_station_unavailable(
    hass, hub_entry, station_entry, domain_data
):
    # BLE allows one connection at a time — the vendor app taking over looks
    # exactly like this, and we must stop allocating power we can't command.
    _set_states(hass)
    hass.states.async_set(SPEED, "unavailable")
    charger = _build(hass, station_entry)
    assert charger.connector_status == "Unavailable"


# ── Command module ────────────────────────────────────────────────────


def _entry_variant(station_entry, drop_data=(), drop_options=()):
    """A copy of the station entry with some keys removed.

    The entries in these tests are never added to hass, so
    hass.config_entries.async_update_entry cannot be used on them.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        # Same entry_id: the builder and command module look their runtime state
        # up by it in hass.data[DOMAIN]["chargers"].
        entry_id=station_entry.entry_id,
        version=station_entry.version,
        minor_version=station_entry.minor_version,
        title=station_entry.title,
        data={k: v for k, v in station_entry.data.items() if k not in drop_data},
        options={
            k: v for k, v in station_entry.options.items() if k not in drop_options
        },
    )


def _sensor(hass, station_entry):
    sensor = MagicMock()
    sensor.hass = hass
    sensor.config_entry = station_entry
    sensor._attr_name = "Power Station"
    return sensor


async def _send(hass, hub_entry, station_entry, limit):
    from custom_components.dynamic_ocpp_evse.control.power_station import (
        send_power_station_command,
    )

    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock
    ) as call:
        await send_power_station_command(
            _sensor(hass, station_entry), limit, hub_entry, time.monotonic()
        )
    # Class-level patch, so call args are (domain, service, data) with no self.
    return {
        c[0][2]["entity_id"]: c[0][2]["value"]
        for c in call.call_args_list
        if c[0][0] == "number" and c[0][1] == "set_value"
    }


async def test_allocation_writes_speed_and_raises_the_reserve(
    hass, hub_entry, station_entry, domain_data
):
    _set_states(hass, speed="200", soc="60", limit="90")
    # 900 W allocated on one phase at 230 V.
    written = await _send(hass, hub_entry, station_entry, 900 / 230)
    assert written[SPEED] == 900
    assert written[RESERVE] == 90
    assert domain_data["station_charging"] is True


async def test_allocation_below_the_minimum_drops_the_reserve(
    hass, hub_entry, station_entry, domain_data
):
    # 150 W is below the station's 200 W floor, so there is no speed to write —
    # dropping the reserve is what stops the charge. Start from a raised reserve,
    # as if the station had been charging: writes are deadbanded against the
    # entity's current value, so dropping to a reserve it already holds is a no-op.
    _set_states(hass, speed="200", soc="60", limit="90", reserve="90")
    written = await _send(hass, hub_entry, station_entry, 150 / 230)
    assert SPEED not in written
    assert written[RESERVE] == 30
    assert domain_data["station_charging"] is False


async def test_reserve_write_is_skipped_when_already_at_the_target(
    hass, hub_entry, station_entry, domain_data
):
    # Idle station already sitting at its normal reserve — nothing to write.
    _set_states(hass, speed="200", soc="60", limit="90", reserve="30")
    written = await _send(hass, hub_entry, station_entry, 150 / 230)
    assert written == {}
    assert domain_data["station_reserve_label"] == "normal"


async def test_speed_is_floored_to_the_device_step(
    hass, hub_entry, station_entry, domain_data
):
    _set_states(hass, speed="200", soc="60", limit="90")
    written = await _send(hass, hub_entry, station_entry, 1290 / 230)
    assert written[SPEED] == 1200


async def test_speed_write_is_skipped_within_the_deadband(
    hass, hub_entry, station_entry, domain_data
):
    # Already at 900 W; a 950 W allocation floors to 900 and needs no write.
    _set_states(hass, speed="900", soc="60", limit="90")
    written = await _send(hass, hub_entry, station_entry, 950 / 230)
    assert SPEED not in written


async def test_storm_reserve_writes_the_storm_level_at_full_speed(
    hass, hub_entry, station_entry, domain_data
):
    _set_states(hass, speed="200", soc="60", limit="90")
    domain_data["station_storm_reserve"] = True
    # Allocation irrelevant: a storm reserve fills as fast as the station allows.
    written = await _send(hass, hub_entry, station_entry, 0)
    assert written[SPEED] == 2400
    assert written[RESERVE] == 80


async def test_reserve_never_exceeds_the_stations_charge_limit(
    hass, hub_entry, station_entry, domain_data
):
    # The device's own limit is the user's battery-health cap.
    _set_states(hass, speed="200", soc="50", limit="70")
    written = await _send(hass, hub_entry, station_entry, 900 / 230)
    assert written[RESERVE] == 70


async def test_runtime_reserve_sliders_are_honoured(
    hass, hub_entry, station_entry, domain_data
):
    _set_states(hass, speed="200", soc="60", limit="90")
    domain_data["station_normal_reserve"] = 45
    written = await _send(hass, hub_entry, station_entry, 0)
    assert written[RESERVE] == 45


async def test_published_state_feeds_the_status_sensor(
    hass, hub_entry, station_entry, domain_data
):
    _set_states(hass, speed="200", soc="60", limit="90")
    await _send(hass, hub_entry, station_entry, 900 / 230)
    assert domain_data["station_charge_speed"] == 900
    assert domain_data["station_reserve"] == 90
    assert domain_data["station_reserve_label"] == "charging"


async def test_missing_control_entities_write_nothing(
    hass, hub_entry, station_entry, domain_data
):
    entry = _entry_variant(
        station_entry, drop_data=(CONF_STATION_RESERVE_ENTITY_ID,)
    )
    _set_states(hass)
    written = await _send(hass, hub_entry, entry, 900 / 230)
    assert written == {}
