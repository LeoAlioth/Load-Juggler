"""A smart load that stops drawing with its relay on is power-cycled.

Machine-authored tests - not yet human-reviewed.

Kozolec's Pond EVSE sits behind switch.pond_evse and cuts out on high
temperature. The switch still reads on, a turn_on to it does nothing, and
Load Juggler never acted. The owner's stand-in automation cycled the plug every
15 min whenever it drew under 10 W - also with the car full or the load not
permitted, ten times in a row on 3 Oct. "Restart if it stops drawing"
(control/plug.py) cycles it only while permitted and on, and only if the load drew lately (JUG-33): the first
restart after 10 min of no draw, then 1 h, 3 h, 6 h, 12 h, 24 h, 24 h apart,
until the load draws for 5 min.
"""

import asyncio
import logging

from custom_components.dynamic_ocpp_evse.const import (
    CONF_EARLY_RATE_ENTITY_ID,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
    CONF_PLUG_POWER_RATING,
    CONF_PLUG_RESTART_ON_NO_DRAW,
    CONF_PLUG_SWITCH_ENTITY_ID,
    DOMAIN,
)
from custom_components.dynamic_ocpp_evse.control.plug import send_plug_command
from custom_components.dynamic_ocpp_evse.entities.load_sensors import (
    LoadJugglerPlugStatusSensor,
)

SWITCH, MONITOR = "switch.pond_evse", "sensor.pond_evse_power"
PERMIT = 8.7  # the plug's rated current, granted
SITE_S, INTERVAL_S = 5, 15  # site cycle, the plug's update frequency


class FakeState:
    def __init__(self, state, unit=None):
        self.state = state
        self.attributes = {"unit_of_measurement": unit} if unit else {}


class FakeStates:
    def __init__(self):
        self.mapping = {}

    def get(self, entity_id):
        return self.mapping.get(entity_id)


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False, context=None):
        self.calls.append(service)


class FakeHass:
    def __init__(self):
        self.states = FakeStates()
        self.services = FakeServices()
        self.data = {DOMAIN: {"loads": {"pond": {}}}}


class FakeEntry:
    entry_id = "pond"

    def __init__(self, options):
        self.data = {CONF_PLUG_SWITCH_ENTITY_ID: SWITCH}
        self.options = options


class FakeSensor:
    _attr_name = "Pond EVSE"

    def __init__(self, hass, entry):
        self.hass = hass
        self.config_entry = entry
        self._last_command_time = -float("inf")

    def _runtime(self):
        return self.hass.data[DOMAIN]["loads"]["pond"]


# The load drew for 5 min, then stopped: what a charger that cut out looks like.
CUT_OUT = lambda t: 2000.0 if t < 300 else 0.0  # noqa: E731

ENABLED = {CONF_PLUG_POWER_MONITOR_ENTITY_ID: MONITOR, CONF_PLUG_RESTART_ON_NO_DRAW: True}


def _run(seconds, power, permit=lambda t: PERMIT, options=ENABLED):
    """Drive the plug through ``seconds`` of site cycles, the command gate as
    entities/load.py runs it. ``power(t)`` is what the load behind the plug
    would draw at ``t`` while its switch is on. Returns the switch commands
    as ``[(t, service)]`` and the load's runtime dict."""
    hass = FakeHass()
    sensor = FakeSensor(hass, FakeEntry(options))
    on = True
    commands = []
    for t in range(0, seconds, SITE_S):
        hass.states.mapping[SWITCH] = FakeState("on" if on else "off")
        hass.states.mapping[MONITOR] = FakeState(str(power(t) if on else 0.0), "W")
        if t - sensor._last_command_time < INTERVAL_S:
            continue
        hass.services.calls.clear()
        asyncio.run(send_plug_command(sensor, permit(t), float(t)))
        for service in hass.services.calls:
            commands.append((t, service))
            on = service == "turn_on"
    return commands, sensor._runtime()


def _restarts(commands, permit=lambda t: PERMIT):
    """Times the plug was switched off while it was permitted."""
    return [t for t, s in commands if s == "turn_off" and permit(t) > 0]


def _status(rt, switch="on"):
    """What the plug's Status sensor shows over this runtime dict."""
    hass = FakeHass()
    hass.data[DOMAIN]["loads"]["pond"] = rt
    hass.states.mapping[SWITCH] = FakeState(switch)
    sensor = LoadJugglerPlugStatusSensor.__new__(LoadJugglerPlugStatusSensor)
    sensor.hass = hass
    sensor.config_entry = FakeEntry(ENABLED)
    sensor._switch_entity = SWITCH
    sensor._read_site_data()
    return sensor.native_value, sensor.extra_state_attributes


def test_a_permitted_plug_that_stopped_drawing_for_10_min_is_cycled_once():
    commands, rt = _run(25 * 60, power=CUT_OUT)
    assert _restarts(commands) == [900]
    # Off at 900 s and nothing sent until the 30 s are up, then back on.
    after = [(t, s) for t, s in commands if t >= 900]
    assert after[0] == (900, "turn_off")
    assert after[1][1] == "turn_on" and 930 <= after[1][0] < 930 + SITE_S
    assert rt["plug_restart_count"] == 1
    assert _status(rt) == ("Restarted (no draw 10 min)", {"restart_count": 1})


def test_a_plug_that_never_drew_is_never_cycled():
    commands, rt = _run(3 * 3600, power=lambda t: 0.0)
    assert _restarts(commands) == []
    assert rt["plug_restart_count"] == 0


def test_a_plug_that_drew_long_ago_is_not_cycled():
    # It drew until 300 s, was shed from 300 s to 4000 s, and is permitted
    # again with nothing drawn: the last draw is over 30 min before the span.
    permit = lambda t: 0.0 if 300 <= t < 4000 else PERMIT  # noqa: E731
    commands, rt = _run(3 * 3600, power=CUT_OUT, permit=permit)
    assert _restarts(commands, permit) == []


def test_a_plug_drawing_normally_is_never_cycled():
    commands, rt = _run(3 * 3600, power=lambda t: 2000.0)
    assert _restarts(commands) == []
    assert _status(rt) == ("On", {"restart_count": 0})


def test_a_plug_not_permitted_is_never_cycled():
    # Load Juggler switches it off on its first command and keeps it off.
    commands, rt = _run(3 * 3600, power=lambda t: 0.0, permit=lambda t: 0.0)
    assert {s for _, s in commands} == {"turn_off"}
    assert rt["plug_restart_count"] == 0
    assert _status(rt, switch="off") == ("Off", {"restart_count": 0})


def test_a_plug_switched_off_by_load_juggler_is_never_cycled():
    # Permitted and drawing nothing for 5 min, then shed for the rest: the
    # watch's 10 min never complete with the switch on.
    permit = lambda t: PERMIT if t < 300 else 0.0  # noqa: E731
    commands, rt = _run(3 * 3600, power=lambda t: 0.0, permit=permit)
    assert _restarts(commands, permit) == []
    assert rt["plug_restart_count"] == 0


def test_without_the_option_or_a_monitor_the_plug_is_untouched():
    for options in ({CONF_PLUG_POWER_MONITOR_ENTITY_ID: MONITOR},
                    {CONF_PLUG_RESTART_ON_NO_DRAW: True}):
        commands, rt = _run(3 * 3600, power=lambda t: 0.0, options=options)
        assert {s for _, s in commands} == {"turn_on"}
        assert "plug_restart_count" not in rt
        assert _status(rt) == ("On", {})


def test_restarts_back_off_1_3_6_12_24_h_and_stay_at_24_h(caplog):
    with caplog.at_level(logging.INFO):
        commands, rt = _run(3 * 86400, power=CUT_OUT)
    h = 3600
    assert _restarts(commands) == [900, 900 + h, 900 + 4 * h, 900 + 10 * h,
                                   900 + 22 * h, 900 + 46 * h, 900 + 70 * h]
    assert rt["plug_restart_count"] == 7
    assert _status(rt) == ("Restarted (no draw 10 min)", {"restart_count": 7})
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_a_load_that_draws_again_resets_the_back_off():
    # Two restarts, then the car charges for 400 s from 5000 s and stops.
    power = lambda t: 2000.0 if t < 300 or 5000 <= t < 5400 else 0.0  # noqa: E731
    commands, rt = _run(7000, power=power)
    # Back at the first step: the next restart is 10 min after it stopped.
    assert _restarts(commands) == [900, 4500, 6000]
    assert rt["plug_restart_count"] == 1


def test_a_permit_that_goes_away_and_returns_keeps_the_back_off():
    permit = lambda t: 0.0 if 5000 <= t < 5600 else PERMIT  # noqa: E731
    commands, rt = _run(5 * 3600, power=CUT_OUT, permit=permit)
    assert _restarts(commands, permit) == [900, 4500, 4500 + 3 * 3600]
    assert rt["plug_restart_count"] == 3


def test_drawing_counts_from_a_tenth_of_the_rated_power():
    # Rated 3.5 kW: 200 W is not drawing, 400 W is.
    options = {**ENABLED, CONF_PLUG_POWER_RATING: 3500}
    low = lambda t: 200.0 if t < 300 else 0.0  # noqa: E731
    high = lambda t: 400.0 if t < 300 else 0.0  # noqa: E731
    assert _restarts(_run(3600, power=low, options=options)[0]) == []
    assert _restarts(_run(3600, power=high, options=options)[0]) == [900]


def test_drawing_counts_from_a_tenth_of_the_absorption_rate():
    # Rate sensor 1 kW (in kW): 50 W is not drawing, 150 W is.
    options = {**ENABLED, CONF_EARLY_RATE_ENTITY_ID: "sensor.rate", CONF_PLUG_POWER_RATING: 9000}
    for watts, expected in ((50.0, []), (150.0, [900])):
        hass = FakeHass()
        hass.states.mapping["sensor.rate"] = FakeState("1.0", "kW")
        sensor = FakeSensor(hass, FakeEntry(options))
        on, restarts = True, []
        for t in range(0, 3600, SITE_S):
            hass.states.mapping[SWITCH] = FakeState("on" if on else "off")
            hass.states.mapping[MONITOR] = FakeState(str(watts if t < 300 and on else 0.0), "W")
            if t - sensor._last_command_time < INTERVAL_S:
                continue
            hass.services.calls.clear()
            asyncio.run(send_plug_command(sensor, PERMIT, float(t)))
            for service in hass.services.calls:
                on = service == "turn_on"
                if service == "turn_off":
                    restarts.append(t)
        assert restarts == expected, watts


def test_the_count_survives_a_restart_of_home_assistant():
    _, rt = _run(5 * 3600, power=CUT_OUT)
    assert rt["plug_restart_count"] == 3
    sensor = LoadJugglerPlugStatusSensor.__new__(LoadJugglerPlugStatusSensor)
    sensor._runtime = lambda: rt
    saved = sensor.extra_restore_state_data.as_dict()
    assert saved == {"count": 3}

    fresh = {}
    sensor._runtime = lambda: fresh
    sensor._restore(saved)
    assert fresh == {"plug_restart_count": 3}
    # Nothing to keep while the plug is fine.
    sensor._runtime = lambda: {}
    assert sensor.extra_restore_state_data is None


def test_a_restored_count_waits_a_full_step_from_the_first_look():
    # HA restarted with 2 restarts behind it: the load has been quiet since, but the
    # next restart is 3 h away, not at once.
    hass = FakeHass()
    hass.data[DOMAIN]["loads"]["pond"] = {"plug_restart_count": 2}
    sensor = FakeSensor(hass, FakeEntry(ENABLED))
    hass.states.mapping[SWITCH] = FakeState("on")
    hass.states.mapping[MONITOR] = FakeState("0.0", "W")
    for t in range(0, 3 * 3600 + 600, INTERVAL_S):
        hass.services.calls.clear()
        asyncio.run(send_plug_command(sensor, PERMIT, float(t)))
        if "turn_off" in hass.services.calls:
            break
    assert t == 3 * 3600
