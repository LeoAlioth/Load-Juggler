"""Manual override: someone operating the device outside Load Juggler hands
the load to the user for its Manual override timeout, with the Dynamic Control
switch still on; Load Juggler takes it back after. The switch is the user's
alone: off stays off, and switching it off cancels an override (the owner,
2026-10-14).

Machine-authored tests - not yet human-reviewed.

A hub and one load set up for real (every platform), the device a bare state
the test writes: with the context of Load Juggler's own call to play the device
answering it, with a fresh one to play a hand on the device.
"""

from datetime import timedelta

import pytest
import voluptuous as vol
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
    mock_restore_cache,
)

from custom_components.dynamic_ocpp_evse.config_flow.schemas import _plug_schema
from custom_components.dynamic_ocpp_evse.const import (
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_DEVICE_TYPE,
    CONF_CONNECTED_TO_PHASE,
    CONF_LOAD_PRIORITY,
    CONF_PLUG_SWITCH_ENTITY_ID,
    CONF_PLUG_POWER_RATING,
    CONF_CLIMATE_ENTITY_ID,
    CONF_MANUAL_OVERRIDE_TIMEOUT,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_HOT_WATER_TANK,
)
from custom_components.dynamic_ocpp_evse.control import call_device
from custom_components.dynamic_ocpp_evse.control.status import hands_off_status

PLUG = "switch.washer_relay"
TANK = "climate.boiler"
OURS = "switch.load_dynamic_control"


def _load_entry(hub, device_type, timeout=30, **data):
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=8,
        title="Load",
        data={
            CONF_NAME: "Load",
            CONF_ENTITY_ID: "lj_load",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_DEVICE_TYPE: device_type,
            CONF_HUB_ENTRY_ID: hub.entry_id,
            **data,
        },
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_CONNECTED_TO_PHASE: "A",
            CONF_PLUG_POWER_RATING: 2000,
            CONF_MANUAL_OVERRIDE_TIMEOUT: timeout,
        },
    )


async def _setup(hass, hub, load):
    hub.add_to_hass(hass)
    await hass.config_entries.async_setup(hub.entry_id)
    load.add_to_hass(hass)
    await hass.config_entries.async_setup(load.entry_id)
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, "lj_load_dynamic_control"
    ) == OURS
    rt = hass.data[DOMAIN]["loads"][load.entry_id]
    # Whatever the first site cycle asked of the device is no part of a test.
    rt.pop("_commanded", None)
    return rt


async def _plug(hass, hub, **kw):
    hass.states.async_set(PLUG, "off")
    return await _setup(
        hass, hub, _load_entry(hub, DEVICE_TYPE_PLUG, **kw, **{CONF_PLUG_SWITCH_ENTITY_ID: PLUG})
    )


async def _lj(hass, rt, calls, domain, service, data, field, value):
    """Load Juggler commands the device; the context its call went out with."""
    await call_device(hass, rt, domain, service, data, field, value)
    await hass.async_block_till_done()
    return calls[-1].context


def _resumes_at(hass):
    return dt_util.parse_datetime(hass.states.get(OURS).attributes["resumes_at"])


def _on_without_override(hass, rt):
    assert hass.states.get(OURS).state == "on"
    assert "resumes_at" not in hass.states.get(OURS).attributes
    assert rt["dynamic_control"] is True


def _overridden(hass, rt):
    """In a manual override: the switch still on, the load not Load Juggler's."""
    assert hass.states.get(OURS).state == "on"
    assert "resumes_at" in hass.states.get(OURS).attributes
    assert rt["dynamic_control"] is False


async def _later(hass, minutes):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done()


async def _operated_by_hand(hass, rt):
    """Load Juggler switches the plug on, the relay answers, then its button
    is pressed."""
    calls = async_mock_service(hass, "switch", "turn_on")
    ctx = await _lj(hass, rt, calls, "switch", "turn_on", {"entity_id": PLUG}, "state", "on")
    hass.states.async_set(PLUG, "on", context=ctx)
    await hass.async_block_till_done()
    _on_without_override(hass, rt)
    hass.states.async_set(PLUG, "off")
    await hass.async_block_till_done()


async def test_switched_off_by_hand_it_stays_off_past_the_timeout(
    hass: HomeAssistant, mock_hub_entry
):
    rt = await _plug(hass, mock_hub_entry)
    await hass.services.async_call("switch", "turn_off", {"entity_id": OURS}, blocking=True)

    assert hass.states.get(OURS).state == "off"
    assert "resumes_at" not in hass.states.get(OURS).attributes
    assert rt["dynamic_control"] is False
    assert hands_off_status(rt) == "Dynamic Control Off"

    await _later(hass, 24 * 60)
    assert hass.states.get(OURS).state == "off"
    assert rt["dynamic_control"] is False


async def test_a_plug_switched_outside_load_juggler_is_manual_until_the_timeout(
    hass: HomeAssistant, mock_hub_entry
):
    rt = await _plug(hass, mock_hub_entry)
    await _operated_by_hand(hass, rt)

    _overridden(hass, rt)
    until = _resumes_at(hass)
    left = until - dt_util.utcnow()
    assert timedelta(minutes=29) < left <= timedelta(minutes=30)
    assert hands_off_status(rt) == f"Manual until {dt_util.as_local(until):%H:%M}"

    await _later(hass, 29)
    _overridden(hass, rt)
    await _later(hass, 31)
    _on_without_override(hass, rt)


async def test_load_juggler_leaves_an_overridden_load_alone(
    hass: HomeAssistant, mock_hub_entry
):
    """A site cycle in an override sends the plug nothing; once it times out,
    the same cycle does."""
    rt = await _plug(hass, mock_hub_entry)
    await _operated_by_hand(hass, rt)
    hass.states.async_set(PLUG, "on")  # the user wants it on, against the site
    await hass.async_block_till_done()
    _overridden(hass, rt)
    coordinator = hass.data[DOMAIN]["hub_coordinators"][mock_hub_entry.entry_id]
    sent = async_mock_service(hass, "switch", "turn_off")

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not sent

    await _later(hass, 31)
    _on_without_override(hass, rt)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in sent] == [PLUG]


async def test_load_juggler_s_own_commands_do_not_start_it(
    hass: HomeAssistant, mock_hub_entry
):
    rt = await _plug(hass, mock_hub_entry)
    calls = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    ctx = await _lj(hass, rt, calls, "switch", "turn_on", {"entity_id": PLUG}, "state", "on")
    hass.states.async_set(PLUG, "on", context=ctx)
    await _lj(hass, rt, calls, "switch", "turn_off", {"entity_id": PLUG}, "state", "off")
    # A slow device: the report comes after its context lapsed, matching.
    hass.states.async_set(PLUG, "off")
    # The late report of the command before, while the next is in force.
    hass.states.async_set(PLUG, "on")
    hass.states.async_set(PLUG, "off")
    # Dropping out and coming back is no one's hand.
    hass.states.async_set(PLUG, "unavailable")
    hass.states.async_set(PLUG, "on")
    await hass.async_block_till_done()
    _on_without_override(hass, rt)


async def test_nothing_commanded_yet_nothing_to_override(
    hass: HomeAssistant, mock_hub_entry
):
    """After a start, before Load Juggler has asked the device anything, a
    change is no departure from a command."""
    rt = await _plug(hass, mock_hub_entry)
    hass.states.async_set(PLUG, "on")
    await hass.async_block_till_done()
    _on_without_override(hass, rt)


async def _tank(hass, hub):
    hass.states.async_set(TANK, "heat", {"temperature": 50, "current_temperature": 48})
    rt = await _setup(
        hass, hub, _load_entry(hub, DEVICE_TYPE_HOT_WATER_TANK, **{CONF_CLIMATE_ENTITY_ID: TANK})
    )
    calls = async_mock_service(hass, "climate", "set_temperature")
    ctx = await _lj(
        hass, rt, calls, "climate", "set_temperature",
        {"entity_id": TANK, "temperature": 60}, "temperature", 60,
    )
    hass.states.async_set(TANK, "heat", {"temperature": 60, "current_temperature": 48}, context=ctx)
    await hass.async_block_till_done()
    _on_without_override(hass, rt)
    return rt


async def test_a_tank_s_target_raised_on_its_thermostat_starts_the_override(
    hass: HomeAssistant, mock_hub_entry
):
    rt = await _tank(hass, mock_hub_entry)
    # Its own readings move: nothing Load Juggler commands changed.
    hass.states.async_set(TANK, "heat", {"temperature": 60, "current_temperature": 52})
    await hass.async_block_till_done()
    _on_without_override(hass, rt)

    hass.states.async_set(TANK, "heat", {"temperature": 70, "current_temperature": 52})
    await hass.async_block_till_done()
    _overridden(hass, rt)


async def test_a_tank_turned_off_on_its_thermostat_starts_the_override(
    hass: HomeAssistant, mock_hub_entry
):
    rt = await _tank(hass, mock_hub_entry)
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    ctx = await _lj(
        hass, rt, calls, "climate", "set_hvac_mode",
        {"entity_id": TANK, "hvac_mode": "heat"}, "state", "heat",
    )
    hass.states.async_set(TANK, "heat", {"temperature": 60, "current_temperature": 48}, context=ctx)
    hass.states.async_set(TANK, "off", {"temperature": 60, "current_temperature": 48})
    await hass.async_block_till_done()
    _overridden(hass, rt)


async def test_a_lower_target_starts_it_only_when_a_user_set_it(
    hass: HomeAssistant, mock_hub_entry
):
    """A device that keeps a lower target than asked (a MELCloud poll reading
    back 55 for 60) is no hand on the dial; an HA user lowering it is."""
    rt = await _tank(hass, mock_hub_entry)
    hass.states.async_set(TANK, "heat", {"temperature": 55, "current_temperature": 48})
    await hass.async_block_till_done()
    _on_without_override(hass, rt)

    hass.states.async_set(
        TANK, "heat", {"temperature": 45, "current_temperature": 48},
        context=Context(user_id="<user>"),
    )
    await hass.async_block_till_done()
    _overridden(hass, rt)


async def _tank_by_hand(hass, hub):
    """A tank in an override: its target raised on the thermostat. (A tank,
    not the plug: the plug's mocked switch.turn_on would stand in for the
    Dynamic Control switch's own.)"""
    rt = await _tank(hass, hub)
    hass.states.async_set(TANK, "heat", {"temperature": 70, "current_temperature": 52})
    await hass.async_block_till_done()
    _overridden(hass, rt)
    return rt


async def test_switching_dynamic_control_off_cancels_an_override(
    hass: HomeAssistant, mock_hub_entry
):
    rt = await _tank_by_hand(hass, mock_hub_entry)

    await hass.services.async_call("switch", "turn_off", {"entity_id": OURS}, blocking=True)
    assert hass.states.get(OURS).state == "off"
    assert "resumes_at" not in hass.states.get(OURS).attributes
    assert hands_off_status(rt) == "Dynamic Control Off"
    # Its timer is gone: the off is not ended by it.
    await _later(hass, 31)
    assert hass.states.get(OURS).state == "off"
    assert rt["dynamic_control"] is False

    await hass.services.async_call("switch", "turn_on", {"entity_id": OURS}, blocking=True)
    _on_without_override(hass, rt)


async def test_switched_back_on_before_the_timeout_there_is_no_override(
    hass: HomeAssistant, mock_hub_entry
):
    rt = await _tank_by_hand(hass, mock_hub_entry)
    await hass.services.async_call("switch", "turn_off", {"entity_id": OURS}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": OURS}, blocking=True)
    _on_without_override(hass, rt)
    await _later(hass, 31)
    _on_without_override(hass, rt)


async def test_an_override_survives_a_restart_unchanged(hass: HomeAssistant, mock_hub_entry):
    until = dt_util.utcnow() + timedelta(minutes=17)
    mock_restore_cache(hass, [State(OURS, "on", {"resumes_at": until.isoformat()})])
    rt = await _plug(hass, mock_hub_entry)

    _overridden(hass, rt)
    assert _resumes_at(hass) == until

    async_fire_time_changed(hass, until + timedelta(seconds=1))
    await hass.async_block_till_done()
    _on_without_override(hass, rt)


async def test_an_override_that_ran_out_while_down_ends_on_start(
    hass: HomeAssistant, mock_hub_entry
):
    until = dt_util.utcnow() - timedelta(minutes=1)
    mock_restore_cache(hass, [State(OURS, "on", {"resumes_at": until.isoformat()})])
    rt = await _plug(hass, mock_hub_entry)
    _on_without_override(hass, rt)


async def test_a_pre_release_timed_off_becomes_an_override(
    hass: HomeAssistant, mock_hub_entry
):
    """The version before kept an override as the switch off with resumes_at:
    it comes back on, in an override until then - not off for good."""
    until = dt_util.utcnow() + timedelta(minutes=17)
    mock_restore_cache(hass, [State(OURS, "off", {"resumes_at": until.isoformat()})])
    rt = await _plug(hass, mock_hub_entry)

    _overridden(hass, rt)
    assert _resumes_at(hass) == until
    async_fire_time_changed(hass, until + timedelta(seconds=1))
    await hass.async_block_till_done()
    _on_without_override(hass, rt)


async def test_a_pre_release_timed_off_that_ran_out_comes_back_on(
    hass: HomeAssistant, mock_hub_entry
):
    until = dt_util.utcnow() - timedelta(minutes=1)
    mock_restore_cache(hass, [State(OURS, "off", {"resumes_at": until.isoformat()})])
    rt = await _plug(hass, mock_hub_entry)
    _on_without_override(hass, rt)


async def test_an_off_without_an_end_stays_off(hass: HomeAssistant, mock_hub_entry):
    mock_restore_cache(hass, [State(OURS, "off")])
    rt = await _plug(hass, mock_hub_entry)
    assert hass.states.get(OURS).state == "off"
    assert "resumes_at" not in hass.states.get(OURS).attributes
    assert rt["dynamic_control"] is False
    await _later(hass, 24 * 60)
    assert hass.states.get(OURS).state == "off"


async def test_the_timeout_is_never_under_five_minutes(hass: HomeAssistant, mock_hub_entry):
    schema = _plug_schema()
    field = next(v for k, v in schema.schema.items() if k == CONF_MANUAL_OVERRIDE_TIMEOUT)
    for minutes in (0, 4):
        with pytest.raises(vol.Invalid):
            field(minutes)
    assert field(5) == 5

    # A stored value under it (none the form takes) is held at five.
    rt = await _plug(hass, mock_hub_entry, timeout=1)
    await _operated_by_hand(hass, rt)
    left = _resumes_at(hass) - dt_util.utcnow()
    assert timedelta(minutes=4) < left <= timedelta(minutes=5)
