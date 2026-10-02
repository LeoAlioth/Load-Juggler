"""The compliance check judges a charger no faster than the charger reports.

Machine-authored tests - not yet human-reviewed.

A go-eCharger V4 with no OCPP transaction running, 2026-10-02: its meter
values - the offered current among them - came clock-aligned every 15 minutes,
so for up to a quarter of an hour after every command the reported figure was
the old one. Judged in 60 s, the stale figure drew three profile resets and a
hard reset every 12.5 minutes: 44 reboots in ten days. The mismatch now has to
outlast the readout's own longest gap, as the stuck-readout watch learned it.
"""

import asyncio
import time

from custom_components.dynamic_ocpp_evse.const import (
    CONF_CHARGER_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    EVSE_RT_READOUT_WATCH,
)
from custom_components.dynamic_ocpp_evse.control.compliance import check_profile_compliance


class FakeState:
    def __init__(self, state):
        self.state = state
        self.attributes = {}


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
    entry_id = "stara"
    data = {CONF_CHARGER_ID: "charger"}
    options = {CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.charger_current_offered"}


class FakeSensor:
    _attr_name = "Charger Available Current"

    def __init__(self, hass, mismatched_for_s, readout_gap_s=None):
        self.hass = hass
        self.config_entry = FakeEntry()
        self.hub_entry = None
        self._connector_status_entity = "sensor.charger_status_connector"
        self._last_commanded_limit = 16.0
        self._last_compliance_limit = 16.0
        self._last_hard_reset_at = None
        self._last_auto_reset_at = None
        self._mismatch_count = 0
        self._mismatch_since = time.monotonic() - mismatched_for_s
        self._profile_reset_count = 0
        self._phases = 3
        self._car_active_phases = 3
        self._gap = readout_gap_s

    def _runtime(self):
        # the stuck-readout watch's state, as engine/readout_watch.py keeps it:
        # MIN_GAPS gaps of its own learned
        return {EVSE_RT_READOUT_WATCH: {"gaps": [self._gap] * 3}} if self._gap else {}


def _run(mismatched_for_s, readout_gap_s=None):
    hass = FakeHass({"sensor.charger_status_connector": FakeState("Charging"),
                     "sensor.charger_current_offered": FakeState("12.0")})     # 12 A offered against 16 A
    sensor = FakeSensor(hass, mismatched_for_s, readout_gap_s)
    asyncio.run(check_profile_compliance(sensor, 16.0, True))
    return hass.services.calls


def test_a_charger_reporting_every_few_seconds_is_judged_in_a_minute():
    assert _run(120.0, readout_gap_s=10.0), "a live readout disagreeing for two minutes is the mismatch"
    assert _run(120.0), "and so without a readout watch at all - today's behaviour"


def test_a_charger_reporting_every_quarter_hour_is_not_judged_on_a_stale_figure():
    assert _run(120.0, readout_gap_s=900.0) == [], "two minutes of disagreement is within one 15-minute readout"
    assert _run(1000.0, readout_gap_s=900.0), "outlasting a full readout gap, the disagreement is real"
