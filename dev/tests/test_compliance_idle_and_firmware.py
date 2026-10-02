"""The compliance check leaves a charger alone while the car is not drawing
and while the charger is updating its firmware.

Machine-authored tests - not yet human-reviewed.

Home's EVBox Elvi, 2026-09-28/29: a car plugged in but suspended (SuspendedEV
and SuspendedEVSE, drawing 0 A) and the charger's offered current reported
only now and then, at 6-8 A, against 16 A commanded. Judged as a mismatch it
drew a profile reset every few minutes and a hard reset every fourteen, all
night, for nine nights. Its firmware status meanwhile read "Downloading" -
the last one the charger ever sent, on 21 Sep - which must not stop the
check for good.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from custom_components.dynamic_ocpp_evse.const import (
    CONF_CHARGER_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
)
from custom_components.dynamic_ocpp_evse.control.compliance import check_profile_compliance

NOW = datetime.now(timezone.utc)


class FakeState:
    def __init__(self, state, changed=None):
        self.state = state
        self.attributes = {}
        self.last_changed = changed or NOW


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service))


class FakeHass:
    def __init__(self, states):
        self.states = self
        self._states = states
        self.services = FakeServices()
        self.data = {}

    def get(self, entity_id):
        return self._states.get(entity_id)


class FakeEntry:
    entry_id = "elvi"
    data = {CONF_CHARGER_ID: "evbox_elvi"}
    options = {CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.evbox_elvi_current_offered"}


class FakeSensor:
    _attr_name = "Evbox Elvi Available Current"

    def __init__(self, hass):
        self.hass = hass
        self.config_entry = FakeEntry()
        self.hub_entry = None
        self._connector_status_entity = "sensor.evbox_elvi_status_connector"
        self._last_commanded_limit = 16.0
        self._last_compliance_limit = 16.0
        self._last_hard_reset_at = None
        self._last_auto_reset_at = None
        self._mismatch_count = 0
        self._mismatch_since = time.monotonic() - 120.0     # already disagreeing for two minutes
        self._profile_reset_count = 0
        self._phases = 3
        self._car_active_phases = 3


def _run(connector, firmware=None, firmware_changed=None):
    states = {"sensor.evbox_elvi_status_connector": FakeState(connector),
              "sensor.evbox_elvi_current_offered": FakeState("8.0")}
    if firmware:
        states["sensor.evbox_elvi_status_firmware"] = FakeState(firmware, firmware_changed)
    hass = FakeHass(states)
    sensor = FakeSensor(hass)
    asyncio.run(check_profile_compliance(sensor, 16.0, True))
    return hass.services.calls, sensor


def test_a_charging_car_offered_half_is_still_reset():
    calls, _ = _run("Charging")
    assert calls, "8 A offered against 16 A while charging is the mismatch the check exists for"


def test_a_suspended_car_is_left_alone():
    for status in ("SuspendedEV", "SuspendedEVSE"):
        calls, sensor = _run(status)
        assert calls == [] and sensor._mismatch_since is None, (status, calls)


def test_a_charger_updating_its_firmware_is_not_reset():
    calls, _ = _run("Charging", "Downloading", NOW - timedelta(minutes=10))
    assert calls == []


def test_a_firmware_status_left_over_from_days_ago_does_not_stop_the_check():
    calls, _ = _run("Charging", "Downloading", NOW - timedelta(days=8))
    assert calls, "the Elvi's 'Downloading' is from 21 Sep; it must not switch compliance off for good"
