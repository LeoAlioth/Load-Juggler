"""A smart load finishes its cycle before Load Juggler switches it off.

Kozolec's washing machine on a Solar Priority plug (Anze, 2026-10-09): once
the battery drops below its minimum the plug would be cut mid-wash. With
"Finish its cycle" set, a denied plug stays on until its power has stayed
under the threshold for the set time; a pause mid-cycle starts that over.
"""

import asyncio

from custom_components.dynamic_ocpp_evse.const import (
    CONF_PLUG_FINISH_BELOW_W,
    CONF_PLUG_FINISH_FOR,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
)
from custom_components.dynamic_ocpp_evse.control.plug import send_plug_command

from .test_plug_restart_on_no_draw import (
    MONITOR,
    SWITCH,
    FakeEntry,
    FakeHass,
    FakeSensor,
    FakeState,
)


def _plug(**options):
    hass = FakeHass()
    hass.states.mapping[SWITCH] = FakeState("on")
    sensor = FakeSensor(hass, FakeEntry({CONF_PLUG_POWER_MONITOR_ENTITY_ID: MONITOR, **options}))
    return hass, sensor


def _deny(hass, sensor, watts, now):
    """One command with the permit gone, the monitor reading ``watts``;
    returns the switch services called."""
    hass.states.mapping[MONITOR] = FakeState(str(watts), "W")
    hass.services.calls.clear()
    asyncio.run(send_plug_command(sensor, 0.0, now))
    return list(hass.services.calls)


def test_a_running_cycle_is_finished_before_the_plug_goes_off():
    hass, sensor = _plug(**{CONF_PLUG_FINISH_BELOW_W: 5, CONF_PLUG_FINISH_FOR: 10})
    assert _deny(hass, sensor, 2000, 0) == []  # heating: stays on
    assert sensor._runtime()["plug_finish_status"] == "Finishing its cycle"
    assert _deny(hass, sensor, 2, 60) == []  # quiet 0 min
    assert _deny(hass, sensor, 2, 360) == []  # quiet 5 min
    assert _deny(hass, sensor, 300, 420) == []  # a spin: the quiet starts over
    assert _deny(hass, sensor, 2, 480) == []
    assert _deny(hass, sensor, 2, 1020) == []  # quiet 9 min
    assert _deny(hass, sensor, 2, 1080) == ["turn_off"]  # quiet 10 min: done
    assert sensor._runtime()["plug_finish_status"] is None


def test_a_cycle_that_ended_long_ago_lets_the_plug_off_at_once():
    hass, sensor = _plug(**{CONF_PLUG_FINISH_BELOW_W: 5, CONF_PLUG_FINISH_FOR: 10})
    hass.states.mapping[MONITOR] = FakeState("1", "W")
    asyncio.run(send_plug_command(sensor, 8.7, 0))  # permitted, idle since 0
    assert _deny(hass, sensor, 1, 3600) == ["turn_off"]


def test_without_the_setting_the_plug_goes_off_at_once():
    hass, sensor = _plug()
    assert _deny(hass, sensor, 2000, 0) == ["turn_off"]


def test_an_unreadable_monitor_does_not_cut_a_cycle():
    hass, sensor = _plug(**{CONF_PLUG_FINISH_BELOW_W: 5, CONF_PLUG_FINISH_FOR: 10})
    assert _deny(hass, sensor, "unavailable", 0) == []
