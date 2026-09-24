"""The draw-settle rule at the moment a car starts charging.

Machine-authored tests - not yet human-reviewed.

An EVSE's footprint on the shared pools is its PERMIT until its measured draw
has held steady for the settle time (SETTLE_DRAW_SECONDS, 15 s) at least
SETTLE_PERMIT_MARGIN below that permit; from then on it is the draw, and the
gap is handed to other loads (engine/load_builders._build_evse_load,
calculations/target_calculator._pool_deduction). That is right for a car that
limits itself below what it is offered - and wrong for a 0 A that held steady
because no energy was flowing yet: plugged in and waiting (Preparing), or
paused by the car (SuspendedEV). Carried across into Charging, that 0 A counted
as settled on the very cycle charging began, while the meter still read 0.

This rig closes the loop through ``run_hub_calculation`` and the real permit
pipeline (``control.smoothing.apply_smoothing``) against a plant, like
dev/tests/test_managed_draw_smoothing.py: chargers on one breaker-limited phase
whose cars slew their draw toward what they are commanded, and a CT that reads
the household plus every draw, all updating in the same cycle.
"""

from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    CONF_CHARGER_ID,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_HUB_ENTRY_ID,
    CONF_LOAD_PRIORITY,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PHASES,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
)

V = 230.0
BREAKER_A = 25.0
HOUSE_A = 8.0
ALLOWANCE_A = BREAKER_A - HOUSE_A     # 17 A: all the chargers may take together
DT = 2                                # the default site cycle, seconds
CAR_RAMP_A_S = 1.0                    # how fast a car follows its command
# A connector status held this long at 0 A outlasts the settle time (15 s),
# the case that used to count as settled.
WAIT = 30 // DT                       # cycles


def _hub(slug):
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=f"Start Hub {slug}",
        data={CONF_NAME: f"Start Hub {slug}",
              CONF_ENTITY_ID: f"start_hub_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: f"sensor.start_{slug}_phase_a",
            CONF_MAIN_BREAKER_RATING: int(BREAKER_A),
            CONF_PHASE_VOLTAGE: int(V),
        },
    )


def _evse(hub, name, priority):
    """A 1-phase 6→32 A Standard EVSE on phase A: the breaker binds, not it."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=name,
        data={CONF_NAME: name,
              CONF_ENTITY_ID: name,
              ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_CHARGER_ID: name,
              CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: f"sensor.{name}_current",
              CONF_HUB_ENTRY_ID: hub.entry_id},
        options={
            CONF_LOAD_PRIORITY: priority,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 32,
            CONF_PHASES: 1,
        },
    )


async def _site(hass, slug, cars, cycles):
    """Close the loop: engine permit → permit pipeline → car → meters → engine.

    ``cars`` is one dict per charger, in priority order: ``status(i)`` is the
    connector status on cycle ``i``, ``flows(i)`` whether the car takes current
    then (it slews toward its command at CAR_RAMP_A_S; it drops to 0 at once
    when it stops), and an optional ``cap`` - the most the car will take
    whatever it is offered. One row per cycle, per-charger values in lists.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.control.smoothing import (
        apply_smoothing,
    )
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _hub(slug)
    hub.add_to_hass(hass)
    names = [f"start_{slug}_evse{n}" for n in range(len(cars))]
    entries = [_evse(hub, name, n + 1) for n, name in enumerate(names)]
    for entry in entries:
        entry.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": [e.entry_id for e in entries]}},
        "loads": {e.entry_id: {
            "entry": e, "hub_entry_id": hub.entry_id, "dynamic_control": True,
        } for e in entries},
        "load_allocations": {e.entry_id: 0 for e in entries},
        "inverters": {},
    }
    # apply_smoothing keeps its state on the load entity and touches only these.
    permit_states = [
        SimpleNamespace(
            _attr_name=name, _ema_current=None, _schmitt_current=None,
            _schmitt_state="rising", _rate_limited_current=0.0,
        )
        for name in names
    ]

    trace = []
    draws = [0.0] * len(cars)
    commands = [0.0] * len(cars)
    step = CAR_RAMP_A_S * DT
    with freeze_time("2026-09-24 10:00:00+00:00") as frozen:
        for i in range(cycles):
            frozen.tick(DT)
            statuses = [car["status"](i) for car in cars]
            for n, car in enumerate(cars):
                if car["flows"](i):
                    want = min(commands[n], car.get("cap", commands[n]))
                    draws[n] += max(-step, min(step, want - draws[n]))
                else:
                    draws[n] = 0.0
                hass.states.async_set(
                    f"sensor.{names[n]}_status_connector", statuses[n])
                hass.states.async_set(
                    f"sensor.{names[n]}_current", f"{draws[n]:.3f}",
                    {"device_class": "current", "unit_of_measurement": "A"})
            hass.states.async_set(
                f"sensor.start_{slug}_phase_a", f"{HOUSE_A + sum(draws):.3f}",
                {"device_class": "current", "unit_of_measurement": "A"})
            result = run_hub_calculation(hass, hub)
            # The load processor's own rounding (entities/load.py).
            permits = [
                round(result["load_available"][e.entry_id], 1) for e in entries
            ]
            commands = [
                apply_smoothing(state, permit, False, hub)
                for state, permit in zip(permit_states, permits)
            ]
            trace.append({
                "i": i, "status": statuses, "draw": list(draws),
                "permit": permits, "command": list(commands),
                "import": HOUSE_A + sum(draws),
            })
    return trace


def _worst_permit(trace, n=0):
    """The row where charger ``n``'s permit stands highest over the allowance."""
    return max(trace, key=lambda row: row["permit"][n])


def _starts(*waits):
    """A car that waits at 0 A through ``waits`` - (status, cycles) pairs, in
    order - and then starts: the connector reports Charging from the next
    cycle, one cycle before the meter reports any current, the order a real
    charger's MeterValues arrive in."""
    ends, end = [], 0
    for status, cycles in waits:
        end += cycles
        ends.append((end, status))

    def status(i):
        return next((st for until, st in ends if i < until), "Charging")

    return {"status": status, "flows": lambda i: i > end}


@pytest.mark.parametrize(
    "waits",
    [
        [("Available", WAIT)],
        [("Available", WAIT), ("Preparing", 3)],
        [("Preparing", WAIT)],
    ],
    ids=["no-car-then-charging", "plugged-in", "waited-in-preparing"],
)
async def test_a_car_starting_is_never_permitted_past_the_allowance(
    hass, waits
):
    """A 0 A held before charging is not a settled draw once charging begins.

    The connector reads 0 A for 30 s or more - no car (Available), a car just
    plugged in (Available, then 6 s of Preparing), or a car waiting to start
    (Preparing) - then reports Charging one cycle before the meter reads any
    current. Before the fix the steady 0 A counted as settled: the car's
    pass-1 footprint was its 0 A draw instead of its 6 A minimum, so pass 2
    handed it the whole 17 A pool ON TOP of the minimum. Measured on this rig,
    with the site's peak import against its 25 A breaker:

    ==================  ==========================================  =======
    case                permit (17 A allowed)                       import
    ==================  ==========================================  =======
    no car, charging    23.0 A on the first Charging cycle only;    25.3 A
                        command reached 18.5 A
    plugged in          23.0 A from the first Preparing cycle to    25.6 A
                        the first Charging one (4 cycles); command
                        reached 21.1 A
    waited, Preparing   23.0 A for 7 cycles, the last 12 s of the   25.5 A
                        wait and the first Charging cycle; command
                        reached 22.2 A
    fixed, all three    17.0 A throughout                           25.0 A
    ==================  ==========================================  =======

    23 A is 6 A (1380 W) over. The first row is the case as first seen: an
    inactive connector has no footprint, so the flag bit only once it turned
    active. The budget is one step of the permit's own 0.1 A rounding.
    """
    slug = "".join(status[0] for status, _ in waits).lower()
    trace = await _site(hass, slug, [_starts(*waits)], cycles=WAIT + 30)

    worst = _worst_permit(trace)
    over_a = worst["permit"][0] - ALLOWANCE_A
    assert over_a <= 0.1, (
        f"permit {worst['permit'][0]:.1f} A against a {ALLOWANCE_A:.0f} A "
        f"allowance - {over_a:.1f} A ({over_a * V:.0f} W) over, on cycle "
        f"{worst['i']} ({worst['status'][0]}, meter {worst['draw'][0]:.1f} A)"
    )
    import_over = max(row["import"] for row in trace) - BREAKER_A
    assert import_over <= 0.1, f"site {import_over * V:.0f} W over the breaker"
    # And not by holding the car back: it ramped all the way to the allowance.
    assert trace[-1]["draw"][0] >= ALLOWANCE_A - 0.1


async def test_a_car_resuming_from_suspended_ev_is_never_permitted_past_it(hass):
    """The same carry-over from a pause: the car charges at the allowance,
    suspends itself for 30 s (SuspendedEV, 0 A - inside the 60 s idle grace,
    so the session is still live), then resumes one cycle before the meter
    reads current again.

    Before the fix the steady 0 A settled 15 s into the pause: the permit stood
    at 23.0 A (+6 A, 1380 W) for 7 cycles, the rest of the pause and the first
    Charging cycle after it; the command reached 22.2 A and the site drew
    25.5 A through the 25 A breaker as the car resumed. Fixed: 17.0 A
    throughout, the breaker never passed.
    """
    pause_from, resume_at = 20, 20 + WAIT
    car = {
        "status": lambda i: (
            "SuspendedEV" if pause_from <= i < resume_at else "Charging"
        ),
        "flows": lambda i: not (pause_from <= i <= resume_at),
    }
    trace = await _site(hass, "resume", [car], cycles=resume_at + 30)

    worst = _worst_permit(trace)
    over_a = worst["permit"][0] - ALLOWANCE_A
    assert over_a <= 0.1, (
        f"permit {worst['permit'][0]:.1f} A against a {ALLOWANCE_A:.0f} A "
        f"allowance - {over_a:.1f} A ({over_a * V:.0f} W) over, on cycle "
        f"{worst['i']} ({worst['status'][0]}, meter {worst['draw'][0]:.1f} A)"
    )
    import_over = max(row["import"] for row in trace) - BREAKER_A
    assert import_over <= 0.1, f"site {import_over * V:.0f} W over the breaker"
    assert trace[-1]["draw"][0] >= ALLOWANCE_A - 0.1


async def test_a_charging_car_that_holds_below_its_permit_frees_the_gap(hass):
    """The settle rule's purpose, through the real site cycle: a car CHARGING
    that holds at 8 A of an 11 A permit hands the unused gap to the charger
    behind it, which rises from its 6 A minimum to 9 A - and the two together
    fill the 17 A allowance without passing it.

    Both cars wait in Preparing first, so this also pins that a settle window
    opened only by charging still opens: the gap moves once the first car's
    draw has held for the settle time while Charging, and not before. Before
    the fix the 30 s wait settled both chargers at 0 A: each was permitted
    23 A for 7 cycles, both started on commands above 20 A (21.4 and 20.7 A),
    and the site drew 26.0 A through the 25 A breaker (230 W over) as the cars
    ramped. Fixed: 25.0 A at most, and the gap still reaches the second car.
    """
    capped = dict(_starts(("Preparing", WAIT)), cap=8.0)
    second = _starts(("Preparing", WAIT))
    trace = await _site(hass, "gap", [capped, second], cycles=WAIT + 45)

    import_over = max(row["import"] for row in trace) - BREAKER_A
    assert import_over <= 0.1, f"site {import_over * V:.0f} W over the breaker"
    end = trace[-1]
    assert end["draw"][0] == pytest.approx(8.0, abs=0.1)
    assert end["permit"][1] == pytest.approx(9.0, abs=0.1), end["permit"]
    assert sum(end["draw"]) == pytest.approx(ALLOWANCE_A, abs=0.2)
    # Not before the first car's draw had held through the settle time while
    # Charging: until then its permit is its footprint, and the second charger
    # has its minimum and no more.
    held_from = next(
        row["i"] for row in trace
        if row["i"] > WAIT and row["draw"][0] == pytest.approx(8.0, abs=0.01)
    )
    early = [row for row in trace if row["i"] < held_from + 15 // DT]
    assert all(row["permit"][1] <= 6.0 for row in early), [
        (row["i"], row["permit"]) for row in early
    ]
