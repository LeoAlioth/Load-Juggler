"""A car drawing below its minimum is never permitted past the breaker.

Machine-authored tests - not yet human-reviewed.

A car can be Charging and take less than its minimum current: it pauses at
0 A mid-session, or tapers near full. Once that draw has held for the settle
window the engine books only the draw against the pools, while the car's
permit base is still its whole minimum. The fill pass then measured the permit
from that base, so the unbooked part of the minimum was counted twice - as
reserved, and again as still in the pool - and the permit came out at
minimum + everything left. On the site below (a 25 A breaker, an 8 A house,
17 A for the car) a car paused at 0 A was permitted 23 A in Priority mode and
its whole 32 A in Shared, and its command climbed toward that for as long as
it stayed paused. When the car resumed, it followed the command past the
breaker until the engine caught up.

This closes the loop through the real site cycle - ``run_hub_calculation`` and
the real permit pipeline (``control.smoothing.apply_smoothing``) - against a
plant: a car that slews its draw toward the lesser of its command and what it
wants, and a CT that reads household plus that draw. The connector reports
Charging the whole session, pause included: the car really is charging, it is
just taking nothing, which is exactly the case the settle rule books at its
draw.

Strict and Optimized size a load once, against the pool as it stands, and
never had the fault; they are here so they keep not having it. The measured
figures are in each test's docstring.
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
    DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
    DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    DISTRIBUTION_MODE_SHARED,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
)

V = 230.0
BREAKER_A = 25.0
HOUSE_A = 8.0
ALLOWANCE_A = BREAKER_A - HOUSE_A     # 17 A: all the car may take
MIN_A = 6.0
MAX_A = 32.0
DT = 2                                # the default site cycle, seconds
PLUG_IN = 30                          # cycles on the household alone first
PAUSE = 90                            # the car stops taking current here...
RESUME = 150                          # ...for 120 s, well past the settle window
CYCLES = 240
STATUS = "sensor.pbm_evse_status_connector"
# One step of the permit's own 0.1 A rounding - the budget the neighbouring
# closed-loop rig (test_managed_draw_smoothing.py) allows too.
BUDGET_A = 0.1

MODES = [
    DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SHARED,
    DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
]


def _hub(slug):
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=f"PBM Hub {slug}",
        data={CONF_NAME: f"PBM Hub {slug}",
              CONF_ENTITY_ID: f"pbm_hub_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.pbm_phase_a",
            CONF_MAIN_BREAKER_RATING: int(BREAKER_A),
            CONF_PHASE_VOLTAGE: int(V),
        },
    )


def _evse(hub):
    """A 1-phase 6→32 A Standard EVSE: the breaker binds, not the car."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title="PBM EVSE",
        data={CONF_NAME: "PBM EVSE",
              CONF_ENTITY_ID: "pbm_evse",
              ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_CHARGER_ID: "pbm_evse",
              CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.pbm_evse_current",
              CONF_HUB_ENTRY_ID: hub.entry_id},
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: int(MIN_A),
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: int(MAX_A),
            CONF_PHASES: 1,
        },
    )


async def _session(hass, mode, car_ramp_a_s, paused_draw_a):
    """Close the loop: engine permit → permit pipeline → car → meters → engine.

    The connector reads Available for ``PLUG_IN`` cycles so every input EMA is
    settled on the household alone. Then the car charges, and from ``PAUSE``
    to ``RESUME`` it wants only ``paused_draw_a`` (0 = paused, 3 = tapering)
    while the connector keeps reporting Charging. The car slews its draw toward
    the lesser of its command and what it wants at ``car_ramp_a_s``.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.control.smoothing import (
        apply_smoothing,
    )
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    slug = f"{mode[:4].lower()}{car_ramp_a_s:g}d{paused_draw_a:g}".replace(".", "")
    hub = _hub(slug)
    hub.add_to_hass(hass)
    evse = _evse(hub)
    evse.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {
            "loads": [evse.entry_id], "distribution_mode": mode,
        }},
        "loads": {evse.entry_id: {
            "entry": evse, "hub_entry_id": hub.entry_id, "dynamic_control": True,
        }},
        "load_allocations": {evse.entry_id: 0},
        "inverters": {},
    }
    hass.states.async_set(STATUS, "Available")
    # apply_smoothing keeps its state on the load entity and touches only these.
    permit_state = SimpleNamespace(
        _attr_name="pbm_evse", _ema_current=None, _schmitt_current=None,
        _schmitt_state="rising", _rate_limited_current=0.0,
    )

    trace = []
    draw = command = 0.0
    step = car_ramp_a_s * DT
    with freeze_time("2026-09-24 10:00:00+00:00") as frozen:
        for i in range(CYCLES):
            frozen.tick(DT)
            if i == PLUG_IN:
                hass.states.async_set(STATUS, "Charging")
            if i >= PLUG_IN:
                wants = paused_draw_a if PAUSE <= i < RESUME else MAX_A
                target = min(command, wants)
                draw += max(-step, min(step, target - draw))
            hass.states.async_set(
                "sensor.pbm_phase_a", f"{HOUSE_A + draw:.3f}",
                {"device_class": "current", "unit_of_measurement": "A"})
            hass.states.async_set(
                "sensor.pbm_evse_current", f"{draw:.3f}",
                {"device_class": "current", "unit_of_measurement": "A"})
            result = run_hub_calculation(hass, hub)
            # The load processor's own rounding (entities/load.py).
            permit = round(result["load_available"][evse.entry_id], 1)
            command = apply_smoothing(permit_state, permit, False, hub)
            trace.append({"i": i, "draw": draw, "permit": permit,
                          "command": command, "import": HOUSE_A + draw})
    return trace


def _window(trace, start, end):
    return [row for row in trace if start <= row["i"] < end]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("car_ramp_a_s", [1.0, 100.0], ids=["car-1A/s", "step"])
async def test_a_paused_car_is_never_permitted_past_the_breaker(
    hass, mode, car_ramp_a_s
):
    """A car paused at 0 A - still Charging - is permitted the allowance, and
    resuming does not take the site past the breaker.

    Measured against this rig before the fix, as the peak permit while paused
    and the peak import over the breaker once the car resumed:

    ======================  ==================  ==================
    mode                    1 A/s car           step
    ======================  ==================  ==================
    Priority                23.0 A / 0.70 A     23.0 A / 6.00 A
    Shared                  32.0 A / 1.10 A     32.0 A / 15.00 A
    Strict, Optimized       17.0 A / 0          17.0 A / 0
    ======================  ==================  ==================

    and 17.0 A / 0 everywhere after it. The paused car is still offered at
    least its minimum throughout, so it can start again on its own.
    """
    trace = await _session(hass, mode, car_ramp_a_s, paused_draw_a=0.0)

    paused = _window(trace, PAUSE, RESUME)
    resumed = _window(trace, RESUME, CYCLES)
    peak_permit = max(row["permit"] for row in paused)
    over = max(row["import"] for row in resumed) - BREAKER_A

    assert over <= BUDGET_A, (
        f"{mode}: the site went {over:.2f} A over the {BREAKER_A:.0f} A "
        f"breaker when the paused car resumed - it had been permitted "
        f"{peak_permit:.1f} A against a {ALLOWANCE_A:.0f} A allowance"
    )
    assert peak_permit <= ALLOWANCE_A + BUDGET_A, (
        f"{mode}: a car paused at 0 A was permitted {peak_permit:.1f} A "
        f"against a {ALLOWANCE_A:.0f} A allowance"
    )
    # Still offered its minimum while paused, so it can start on its own...
    assert min(row["permit"] for row in paused) >= MIN_A
    # ...and it did: back up to the whole allowance, not held back.
    assert trace[-1]["draw"] >= ALLOWANCE_A - BUDGET_A


@pytest.mark.parametrize("mode", MODES)
async def test_a_tapering_car_is_never_permitted_past_the_breaker(hass, mode):
    """A car tapering to 3 A books 3 A; its permit is the allowance.

    Measured before the fix, as the peak permit while tapering and the peak
    import over the breaker: Priority 20.0 A / 0.50 A, Shared 32.0 A / 2.00 A,
    Strict and Optimized 17.0 A / 0. After it, 17.0 A / 0 in every mode.
    """
    trace = await _session(hass, mode, 1.0, paused_draw_a=3.0)

    tapering = _window(trace, PAUSE, RESUME)
    peak_permit = max(row["permit"] for row in tapering)
    over = max(row["import"] for row in trace) - BREAKER_A

    assert over <= BUDGET_A, (
        f"{mode}: the site went {over:.2f} A over the {BREAKER_A:.0f} A "
        f"breaker - the tapering car had been permitted {peak_permit:.1f} A "
        f"against a {ALLOWANCE_A:.0f} A allowance"
    )
    assert peak_permit <= ALLOWANCE_A + BUDGET_A, (
        f"{mode}: a car tapering to 3 A was permitted {peak_permit:.1f} A "
        f"against a {ALLOWANCE_A:.0f} A allowance"
    )
    # The permit stays above the 3 A draw, so the draw stays settled and the
    # gap it leaves is still free for anything behind it.
    assert tapering[-1]["permit"] > 3.0 + 1.0
