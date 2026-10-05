"""A smart load that stops drawing with its relay on is power-cycled.

Machine-authored tests - not yet human-reviewed.

Kozolec's Pond EVSE sits behind switch.pond_evse and cuts out on high
temperature. The switch still reads on, a turn_on to it does nothing, and
Load Juggler never acted. Anze's stand-in automation cycled the plug every
15 min whenever it drew under 10 W - also with the car full or the load not
permitted, ten times in a row on 3 Oct. "Restart if it stops drawing"
(control/plug.py) cycles it only while permitted and on, at most once per
30 min, and gives up after 3 in a row until the load draws again.
"""

import asyncio
import logging

from custom_components.dynamic_ocpp_evse.const import (
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
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

    async def async_call(self, domain, service, data, blocking=False):
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


def test_a_permitted_plug_drawing_nothing_for_10_min_is_cycled_once():
    commands, rt = _run(25 * 60, power=lambda t: 0.0)
    assert _restarts(commands) == [600]
    # Off at 600 s and nothing sent until the 30 s are up, then back on.
    after = [(t, s) for t, s in commands if t >= 600]
    assert after[0] == (600, "turn_off")
    assert after[1][1] == "turn_on" and 630 <= after[1][0] < 630 + SITE_S
    assert rt["plug_restart_count"] == 1
    assert _status(rt) == ("Restarted (no draw 10 min)", {"restart_count": 1})


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


def test_restarts_are_30_min_apart_and_give_up_after_3(caplog):
    with caplog.at_level(logging.INFO):
        commands, rt = _run(4 * 3600, power=lambda t: 0.0)
    assert _restarts(commands) == [600, 600 + 1800, 600 + 3600]
    assert rt["plug_restart_count"] == 3
    assert _status(rt) == ("No draw (gave up after 3 restarts)", {"restart_count": 3})
    infos = [r for r in caplog.records if "power-cycling" in r.getMessage()]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.levelno for r in infos] == [logging.INFO] * 3
    assert len(warnings) == 1 and "after 3 restarts" in warnings[0].getMessage()


def test_a_load_that_draws_again_resets_the_count():
    # Two restarts, then the car charges for 10 min from 2500 s and stops.
    power = lambda t: 2000.0 if 2500 <= t < 3100 else 0.0  # noqa: E731
    commands, rt = _run(4 * 3600, power=power)
    restarts = _restarts(commands)
    assert restarts[:2] == [600, 2400]
    # Reset: three more restarts before it gives up, five in all.
    assert len(restarts) == 5
    assert all(b - a >= 1800 for a, b in zip(restarts, restarts[1:]))
    assert rt["plug_restart_count"] == 3


def test_a_permit_that_goes_away_and_returns_starts_over_after_a_give_up():
    # Given up by 1.5 h; shed for 10 min at 2 h; permitted again after.
    permit = lambda t: 0.0 if 7200 <= t < 7800 else PERMIT  # noqa: E731
    commands, rt = _run(4 * 3600, power=lambda t: 0.0, permit=permit)
    restarts = _restarts(commands, permit)
    assert restarts[:3] == [600, 2400, 4200]
    assert restarts[3] > 7800
    assert rt["plug_restart_count"] >= 1
