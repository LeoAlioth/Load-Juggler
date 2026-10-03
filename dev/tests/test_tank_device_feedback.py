"""What a tank's own device says back: the target it keeps, and whether it heats.

Machine-authored tests - not yet human-reviewed.

A user's site, 2-3 Oct 2026: a Mitsubishi Ecodan's hot water tank through
MELCloud (water_heater.tc, 40-60 C, a "status" of idle / heat_water), Normal
mode with a 60 C boost. Load Juggler wrote 60; at every 15-minute cloud poll the
target read back 55 until it wrote 60 again - four cloud writes an hour - and
the unit heated to 53.5 C and went idle for hours, while Load Juggler showed
the tank heating and held its element power for it.
"""

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.dynamic_ocpp_evse.const import (
    CONF_CLIMATE_ENTITY_ID,
    CONF_CONNECTED_TO_PHASE,
    CONF_ENTITY_ID,
    CONF_HEATING_ELEMENT_POWER,
    CONF_NAME,
    CONF_TANK_AWAY_TEMPERATURE,
    CONF_TANK_BOOST_TEMPERATURE,
    CONF_TANK_NORMAL_TEMPERATURE,
    DOMAIN,
    TANK_MODE_NORMAL,
)
from custom_components.dynamic_ocpp_evse.control.hot_water_tank import (
    KEPT_SETTLE_S,
    judge_kept_target,
    send_hot_water_tank_command,
)
from custom_components.dynamic_ocpp_evse.engine.load_builders import (
    _build_hot_water_tank_load,
)

from .test_water_heater_tank import FakeEntry, FakeHass, FakeSensor, FakeState

WH = "water_heater.tc"
POLL_S = 15 * 60.0  # MELCloud's cloud poll
CYCLE_S = 15.0


# --- the rule: the same lower value, on separate settled readbacks ---


def _judge(readbacks, asked=60.0):
    """``readbacks``: ``(t, value)``, each a new readback. Returns the first
    ``(t, kept)`` judged, or None. The ask starts at 0."""
    rec, _ = judge_kept_target(None, asked, None, None, 0.0)
    for i, (t, value) in enumerate(readbacks):
        rec, kept = judge_kept_target(rec, asked, value, i, t)
        if kept is not None:
            return t, kept
    return None


def test_a_value_kept_at_three_polls_is_the_devices_own():
    assert _judge([(POLL_S, 55), (2 * POLL_S, 55), (3 * POLL_S, 55)]) == (3 * POLL_S, 55)


def test_a_readback_that_lags_once_then_takes_the_write_is_no_ceiling():
    # The previous target once, a poll not yet through; then the asked one.
    assert _judge([(KEPT_SETTLE_S + 10, 50)] + [
        (KEPT_SETTLE_S + 10 + n * POLL_S, 60) for n in range(1, 8)
    ]) is None


def test_the_echo_of_a_write_counts_for_nothing():
    # Inside the settle time a readback says nothing, however often it comes.
    assert _judge([(t, 55) for t in range(0, int(KEPT_SETTLE_S), 15)]) is None


def test_one_readback_seen_again_is_one_answer():
    rec = None
    for t in (400, 800, 1200, 1600):
        rec, kept = judge_kept_target(rec, 60.0, 55.0, "same poll", t)
        assert kept is None
    assert rec["seen"] == 1


def test_readbacks_closer_than_the_settle_time_count_once():
    # A local device answering every cycle is one answer per settle time.
    assert _judge([(KEPT_SETTLE_S + t, 55) for t in range(0, 600, 15)]) is None
    assert _judge([(KEPT_SETTLE_S + t, 55) for t in range(0, 615, 15)]) is not None


def test_a_higher_value_is_no_ceiling():
    assert _judge([(n * POLL_S, 65) for n in range(1, 6)]) is None


def test_a_different_value_starts_the_count_over():
    assert _judge([(POLL_S, 50), (2 * POLL_S, 55), (3 * POLL_S, 55)]) is None


def test_a_new_ask_starts_the_count_over():
    rec = None
    for n, asked in ((1, 60.0), (2, 60.0), (3, 58.0)):
        rec, kept = judge_kept_target(rec, asked, 55.0, n, n * POLL_S)
    assert kept is None and rec["seen"] == 0


# --- driven: the tank against a device that keeps its own target ---


def _rig(hass, entity_id, state, **settings):
    hass.data[DOMAIN] = {"loads": {"tank": {"operating_mode": TANK_MODE_NORMAL.key}}}
    entry = FakeEntry({
        CONF_CLIMATE_ENTITY_ID: entity_id,
        CONF_ENTITY_ID: "tank",
        CONF_NAME: "Tank",
        CONF_TANK_AWAY_TEMPERATURE: settings.get("away", 40),
        CONF_TANK_NORMAL_TEMPERATURE: settings.get("normal", 50),
        CONF_TANK_BOOST_TEMPERATURE: settings.get("boost", 60),
    })
    registry = er.async_get(hass)
    for name in ("away", "normal", "boost"):
        registry.async_get_or_create(
            "number", DOMAIN, f"tank_tank_{name}_temperature",
            suggested_object_id=f"tank_{name}_temperature",
        )
    hass.states.async_set(entity_id, state, {
        "temperature": 45, "current_temperature": 45.5,
        "min_temp": 40, "max_temp": 60,
    })
    domain = entity_id.split(".")[0]
    async_mock_service(hass, domain, "set_hvac_mode")
    return FakeSensor(hass, entry), {
        "set": async_mock_service(hass, domain, "set_temperature"),
        "number": async_mock_service(hass, "number", "set_value"),
        "notify": async_mock_service(hass, "persistent_notification", "create"),
    }


def _report(hass, entity_id, target):
    """The device's state written again - a poll, or its answer to a write."""
    state = hass.states.get(entity_id)
    hass.states.async_set(
        entity_id, state.state, {**state.attributes, "temperature": target}
    )


async def _drive(hass, sensor, calls, entity_id, answer, hours=1.5):
    """Boost asked for ``hours``, one cycle every 15 s. A water heater echoes a
    write at once and shows the unit's ``answer(n)`` at its n-th cloud poll; a
    climate answers each write itself."""
    water_heater = entity_id.startswith("water_heater.")
    polls = 0
    for i in range(int(hours * 3600 / CYCLE_S)):
        t = i * CYCLE_S
        if water_heater and t and t % POLL_S == 0:
            polls += 1
            _report(hass, entity_id, answer(polls))
        written = len(calls["set"])
        await send_hot_water_tank_command(sensor, 4.3, {"excess_available": True}, t)
        await hass.async_block_till_done()
        if len(calls["set"]) > written:
            asked = calls["set"][-1].data["temperature"]
            _report(hass, entity_id, asked if water_heater else min(asked, answer(0)))
    return [c.data["temperature"] for c in calls["set"]]


async def test_a_water_heater_keeping_55_has_its_boost_lowered_to_55(hass):
    sensor, calls = _rig(hass, WH, "auto")
    written = await _drive(hass, sensor, calls, WH, lambda n: 55)

    # 60 at the start and again after each of the three polls that read 55 -
    # then the boost setting is 55 and the device already holds it.
    assert written == [60, 60, 60]
    rt = hass.data[DOMAIN]["loads"]["tank"]
    assert rt["tank_boost_temperature"] == 55
    assert rt["tank_setpoint"] == 55
    assert rt.get("tank_normal_temperature") is None  # 50 is under 55: untouched
    assert [c.data for c in calls["number"]] == [
        {"entity_id": "number.tank_boost_temperature", "value": 55},
    ]
    assert len(calls["notify"]) == 1
    note = calls["notify"][0].data
    assert note["notification_id"] == f"{DOMAIN}_tank_target_not_kept_tank"
    for text in ("Tank", WH, "60 °C", "55 °C", "Boost Temperature"):
        assert text in note["title"] + note["message"], text


async def test_a_water_heater_that_lags_once_keeps_its_boost(hass):
    sensor, calls = _rig(hass, WH, "auto")
    # The first poll still shows the target from before the boost.
    written = await _drive(hass, sensor, calls, WH, lambda n: 45 if n == 1 else 60)
    assert written == [60, 60]
    assert hass.data[DOMAIN]["loads"]["tank"].get("tank_boost_temperature") is None
    assert not calls["number"] and not calls["notify"]


async def test_a_thermostat_keeping_55_has_its_boost_lowered_to_55(hass):
    climate = "climate.tank"
    sensor, calls = _rig(hass, climate, "heat")
    written = await _drive(hass, sensor, calls, climate, lambda n: 55)

    # A climate is re-asserted every cycle: 60 until the device's answer has
    # been the same three times, five minutes apart, then 55 for good.
    adopted = written.index(55)
    assert set(written[:adopted]) == {60} and set(written[adopted:]) == {55}
    assert adopted * CYCLE_S <= 3 * KEPT_SETTLE_S + CYCLE_S
    assert [c.data["value"] for c in calls["number"]] == [55]
    assert len(calls["notify"]) == 1


async def test_a_ceiling_below_normal_brings_normal_down_with_boost(hass):
    sensor, calls = _rig(hass, WH, "auto", away=40, normal=50, boost=60)
    await _drive(hass, sensor, calls, WH, lambda n: 45)

    rt = hass.data[DOMAIN]["loads"]["tank"]
    assert (rt.get("tank_away_temperature"), rt["tank_normal_temperature"],
            rt["tank_boost_temperature"]) == (None, 45, 45)  # away 40 untouched
    assert {c.data["entity_id"]: c.data["value"] for c in calls["number"]} == {
        "number.tank_normal_temperature": 45,
        "number.tank_boost_temperature": 45,
    }
    assert "Normal Temperature, Boost Temperature" in calls["notify"][0].data["message"]


def test_the_notification_reads_the_same_in_both_languages():
    import json
    from pathlib import Path
    import re

    base = Path("custom_components/dynamic_ocpp_evse/translations")
    fields = None
    for name in ("en.json", "sl.json"):
        note = json.loads((base / name).read_text(encoding="utf-8"))[
            "issues"]["tank_target_not_kept"]
        found = set(re.findall(r"{(\w+)}", note["title"] + note["description"]))
        assert fields in (None, found), name
        fields = found
    assert fields == {"tank", "device", "asked", "kept", "value", "setting", "lowered"}


# --- the device's own word on whether it heats ---


def _tank(entity_id, state, **attrs):
    hass = FakeHass({entity_id: FakeState(state, **attrs)})
    data = {
        CONF_CLIMATE_ENTITY_ID: entity_id,
        CONF_CONNECTED_TO_PHASE: "A",
        CONF_HEATING_ELEMENT_POWER: 1000,
    }
    load = _build_hot_water_tank_load(hass, FakeEntry(data), 230.0, "tank_1", 1)
    return load, hass.data[DOMAIN]["loads"]["tank"]["tank_hvac_action"]


def test_an_idle_heat_pump_holds_no_power():
    # The user's tank: 53 C under a 60 C target, the heat pump idle.
    load, action = _tank(
        WH, "auto", status="idle", current_temperature=53, temperature=60,
    )
    assert action == "idle"
    assert load.connector_status == "Available"
    assert load.l1_current == 0


def test_a_heat_pump_heating_water_draws_the_element_power():
    load, action = _tank(
        WH, "auto", status="heat_water", current_temperature=53, temperature=60,
    )
    assert action == "heating"
    assert abs(load.l1_current - 1000 / 230.0) < 0.01


def test_a_heat_pump_heating_the_house_holds_no_tank_power():
    _, action = _tank(
        WH, "auto", status="heat_zones", current_temperature=53, temperature=60,
    )
    assert action == "idle"


def test_a_status_it_does_not_know_leaves_the_reading_as_it_was():
    # A defrost, or another integration's word: colder than target is heating.
    _, action = _tank(
        WH, "auto", status="defrost", current_temperature=53, temperature=60,
    )
    assert action == "heating"


def test_a_thermostat_idle_or_heating_is_read_off_its_hvac_action():
    load, _ = _tank("climate.tank", "heat", hvac_action="idle", current_temperature=53)
    assert load.connector_status == "Available" and load.l1_current == 0
    load, _ = _tank("climate.tank", "heat", hvac_action="heating", current_temperature=53)
    assert abs(load.l1_current - 1000 / 230.0) < 0.01
