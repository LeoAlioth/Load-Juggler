"""A hybrid read through its output AND input sensors, through the real engine.

Machine-authored tests - not yet human-reviewed.

The household is ``grid + Σ (output - input) - managed draw``
(``calculations/utils.household_per_phase``). This rig is the arrangement the
old wiring-topology setting could not name: a hybrid the grid flows through
with 40 % of the house on the grid-meter bus AHEAD of it (a non-backed-up
panel). The grid meter reads the whole house; the hybrid's load port carries
the backed 60 % and its input sensors read what it draws from the grid - the
same 60 % - so what it passes through is not counted twice.

Physical site, one phase, 230 V, no solar sensor and no managed load:

    house 1000 W  =  400 W on the meter bus  +  600 W behind the hybrid
    grid meter (W)         1000 W  (import)
    hybrid output (A)      600 W / 230 V
    hybrid input (W)       600 W

With no solar sensor the published household is the engine's per-phase sum,
not the solar identity, so it must land on the physical house.
"""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    CONF_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_INPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
)
from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
    run_hub_calculation,
)

V = 230.0
HOUSE_W = 1000.0
BACKED_W = 0.6 * HOUSE_W

GRID = "sensor.in_grid_a"
OUT_A = "sensor.in_hybrid_out_a"
IN_A = "sensor.in_hybrid_in_a"


def _rig(hass):
    hub = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=9, title="Input rig",
        data={CONF_NAME: "Input rig", CONF_ENTITY_ID: "in_rig",
              ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={CONF_MAIN_BREAKER_RATING: 40, CONF_PHASE_VOLTAGE: int(V),
                 CONF_PHASE_A_CURRENT_ENTITY_ID: GRID},
    )
    hub.add_to_hass(hass)
    inverter = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=9, title="Hybrid",
        data={CONF_NAME: "Hybrid", CONF_ENTITY_ID: "in_hybrid",
              ENTRY_TYPE: ENTRY_TYPE_INVERTER, CONF_HUB_ENTRY_ID: hub.entry_id},
        options={
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: OUT_A,
            CONF_INVERTER_INPUT_PHASE_A_ENTITY_ID: IN_A,
        },
    )
    inverter.add_to_hass(hass)
    hass.data[DOMAIN] = {"hubs": {hub.entry_id: {"loads": []}}, "loads": {},
                         "load_allocations": {}, "inverters": {}}
    hass.states.async_set(
        GRID, f"{HOUSE_W:.1f}", {"device_class": "power", "unit_of_measurement": "W"})
    hass.states.async_set(
        OUT_A, f"{BACKED_W / V:.4f}",
        {"device_class": "current", "unit_of_measurement": "A"})
    hass.states.async_set(
        IN_A, f"{BACKED_W:.1f}", {"device_class": "power", "unit_of_measurement": "W"})
    return hub


async def test_a_hybrid_with_a_panel_ahead_of_it_gives_the_physical_household(hass):
    """Grid 1000 W + output 600 W - input 600 W = 1000 W: the house, with the
    grid the hybrid passes on counted once. Read through W and A sensors."""
    hub = _rig(hass)

    for _ in range(3):
        result = run_hub_calculation(hass, hub)

    assert result["household_power"] == pytest.approx(HOUSE_W, abs=1.0)


async def test_an_unavailable_input_is_held_and_named_on_the_hub_status(hass):
    """The input is read like the output: a dropout is held by the stale guard
    (the household must not jump by the whole input), and the hub status names
    the sensor so the owner sees which feed went."""
    hub = _rig(hass)
    for _ in range(3):
        run_hub_calculation(hass, hub)

    hass.states.async_set(
        IN_A, "unavailable",
        {"device_class": "power", "unit_of_measurement": "W"})
    result = run_hub_calculation(hass, hub)

    assert result["household_power"] == pytest.approx(HOUSE_W, abs=1.0)
    assert result["hub_status"].startswith("Sensor unavailable:")
    assert "Inverter Hybrid input (L1)" in result["hub_status"]
    assert any(IN_A in warning for warning in result["hub_warnings"])
