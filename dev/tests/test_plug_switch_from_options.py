"""A smart load's switch is read from its options, where its settings page
saves a changed one.

Machine-authored tests - not yet human-reviewed.

The switch was read from the entry's setup data alone, so a switch picked
again on the settings page (after its entity was renamed, 2026-09-28) was
saved but never used: the load kept commanding and reading an entity that no
longer existed.
"""

import asyncio

from custom_components.dynamic_ocpp_evse.const import (
    DOMAIN,
    CONF_PLUG_SWITCH_ENTITY_ID,
)
from custom_components.dynamic_ocpp_evse.control.plug import send_plug_command
from custom_components.dynamic_ocpp_evse.engine.load_builders import (
    _build_plug_load,
)

OLD, NEW = "switch.shellypro4pm_kozolec_switch_0", "switch.power_strip"


class FakeState:
    def __init__(self, state):
        self.state = state
        self.attributes = {}


class FakeStates:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))


class FakeHass:
    def __init__(self, mapping):
        self.states = FakeStates(mapping)
        self.services = FakeServices()
        self.data = {DOMAIN: {"loads": {"plug": {}}}}


class FakeEntry:
    def __init__(self, data, options):
        self.entry_id = "plug"
        self.data = data
        self.options = options


class FakeSensor:
    _attr_name = "Power Strip"

    def __init__(self, hass, entry):
        self.hass = hass
        self.config_entry = entry


def _renamed():
    # Set up on the old switch; the settings page then saved the new one.
    return FakeEntry({CONF_PLUG_SWITCH_ENTITY_ID: OLD}, {CONF_PLUG_SWITCH_ENTITY_ID: NEW})


def test_the_switch_from_the_settings_page_is_commanded():
    hass = FakeHass({NEW: FakeState("off")})
    asyncio.run(send_plug_command(FakeSensor(hass, _renamed()), 10, 0.0))
    assert hass.services.calls == [("switch", "turn_on", {"entity_id": NEW})]


def test_the_switch_from_the_settings_page_is_read():
    # Off, so it cannot pass by accident: a switch that is not found at all
    # makes the load assume it is on.
    hass = FakeHass({NEW: FakeState("off")})
    load = _build_plug_load(hass, _renamed(), 230.0, "plug_1", 1)
    assert load.connector_status == "Available"


def test_an_entry_never_edited_still_uses_its_setup_switch():
    hass = FakeHass({OLD: FakeState("off")})
    entry = FakeEntry({CONF_PLUG_SWITCH_ENTITY_ID: OLD}, {})
    asyncio.run(send_plug_command(FakeSensor(hass, entry), 0, 0.0))
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": OLD})]
