"""The feedback loop's managed-draw smoothing, closed through the production cycle.

Machine-authored tests - not yet human-reviewed.

The feedback loop reconstructs the household as ``grid − Σ managed draw`` and
the engine sizes every permit on what that leaves. Both halves are SMOOTHED on
the input EMA (``hub_calculation._managed_phase_draws``): a smoothed grid
reading minus a RAW draw moves the reconstruction by a load's whole step before
the grid term has caught up, which hunted a station 299-828 W around a 600 W
target on the rig (2026-09-08). The subtraction is only exact while the two
EMAs advance in lockstep - once per site cycle, at the same weight - and until
2026-09-24 the draw's advanced TWICE a cycle, once for the site view and again
for the charge controller's view, so it ran ahead of the grid term.

This rig closes the loop through ``run_hub_calculation`` and the real permit
pipeline (``control.smoothing.apply_smoothing``) against a plant: a charger on
a breaker-limited phase that slews its draw toward whatever it is commanded,
and a CT that reads household plus that draw, both updating in the same cycle.
The household never moves, so the right permit never moves either - any
excursion above the breaker's allowance is the reconstruction reading the
household low while the charger ramps.
"""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    CONF_ENTITY_ID,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
)

from .closed_loop import AMPS, DT, V, close_loop, evse_entry, over_w, slew

BREAKER_A = 25.0
HOUSE_A = 8.0
ALLOWANCE_A = BREAKER_A - HOUSE_A     # 17 A: all the charger may take
START = 30                            # cycles on the household alone first
STATUS = "sensor.ramp_evse_status_connector"


def _hub(slug):
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=f"Ramp Hub {slug}",
        data={CONF_NAME: f"Ramp Hub {slug}",
              CONF_ENTITY_ID: f"ramp_hub_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.ramp_phase_a",
            CONF_MAIN_BREAKER_RATING: int(BREAKER_A),
            CONF_PHASE_VOLTAGE: int(V),
        },
    )


async def _ramp(hass, slug, car_ramp_a_s, cycles=120, each_cycle=None):
    """A 1-phase 6→32 A Standard EVSE on the breaker-limited phase.

    The connector reads Available (no car) for ``START`` cycles, so every input
    EMA is settled on the household alone. Then the car plugs in and slews its
    draw toward its last command at ``car_ramp_a_s``; the connector reports
    Charging on the cycle current starts flowing, as a real charger does.
    ``each_cycle(i)`` runs as cycle ``i`` starts. One row per cycle.
    """
    hub = _hub(slug)
    hass.states.async_set(STATUS, "Available")
    draw = 0.0

    def plant(i, command):
        nonlocal draw
        if each_cycle is not None:
            each_cycle(i)
        if i == START:
            hass.states.async_set(STATUS, "Charging")
        if i >= START:
            draw = slew(draw, command, car_ramp_a_s * DT)
        hass.states.async_set("sensor.ramp_phase_a", f"{HOUSE_A + draw:.3f}", AMPS)
        hass.states.async_set("sensor.ramp_evse_current", f"{draw:.3f}", AMPS)
        return {"draw": draw, "import": HOUSE_A + draw}

    return await close_loop(hass, hub, evse_entry(hub, "ramp_evse"), cycles,
                            plant, at="2026-09-24 10:00:00+00:00")


@pytest.mark.parametrize("car_ramp_a_s", [1.0, 100.0], ids=["car-1A/s", "step"])
async def test_a_ramping_charger_is_never_permitted_past_the_breaker(
    hass, car_ramp_a_s
):
    """The permit holds the allowance while the charger ramps up to it.

    Measured against this rig, as W over the allowance at peak:

    ====================================  ===========  ===========
    managed draw                          1 A/s car    step
    ====================================  ===========  ===========
    smoothed, advanced twice a cycle      permit 414   permit 690
                                          import 345   import 414
    raw (no smoothing at all)             permit 1035  permit 2737
                                          import 897   import 1380
    smoothed, once a cycle (production)   0 / 0        0 / 0
    ====================================  ===========  ===========

    So this is the test for the smoothing's reason to exist as much as for the
    double advance: remove the draw's EMA and it fails by a kilowatt. The budget
    is one step of the permit's own 0.1 A rounding.
    """
    trace = await _ramp(hass, f"r{car_ramp_a_s:g}", car_ramp_a_s)

    permit_over = over_w(trace, "permit", ALLOWANCE_A)
    import_over = over_w(trace, "import", BREAKER_A)
    assert permit_over <= 0.1 * V, f"permit {permit_over:.0f} W over the allowance"
    assert import_over <= 0.1 * V, f"site {import_over:.0f} W over the breaker"
    # And not by holding the car back: it ramped all the way to the allowance.
    assert trace[-1]["draw"] >= ALLOWANCE_A - 0.1


async def test_both_views_subtract_one_smoothed_draw_per_cycle(hass, monkeypatch):
    """The site view and the charge controller's view subtract the SAME draw.

    Each cycle with a managed draw subtracts it twice - once from the symmetric
    grid phases, once from the charge controller's directional ones - and the
    two must agree on what our loads draw. They did not while the draw's EMA
    was advanced once per subtraction: the second view read a filter one
    extra step ahead, and the next cycle's first view inherited it.
    """
    from custom_components.dynamic_ocpp_evse.engine import hub_calculation

    real = hub_calculation.grid_without_managed_draws
    now = {}
    per_cycle = {}

    def spy(consumption, export, draws):
        per_cycle.setdefault(now["i"], []).append(list(draws))
        return real(consumption, export, draws)

    monkeypatch.setattr(hub_calculation, "grid_without_managed_draws", spy)
    await _ramp(hass, "views", 1.0, cycles=START + 20,
                each_cycle=lambda i: now.update(i=i))

    ramping = [per_cycle[i] for i in range(START, START + 20)]
    assert all(len(calls) == 2 for calls in ramping)
    for calls in ramping:
        assert calls[0] == calls[1], f"site view {calls[0]} vs control view {calls[1]}"
