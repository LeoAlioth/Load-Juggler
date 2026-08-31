"""The battery charge control, as a CLOSED LOOP through the production code.

Machine-authored tests — not yet human-reviewed.

Everything else about this controller is tested a piece at a time: the value in
``test_forecast_clipping.py``, the engine wiring in ``test_sensor_update.py``,
the register rules in ``test_inverter_control.py``. What none of those can show
is the behaviour that only exists when the loop is closed — the advice moving
the register, the register moving the battery, the battery moving the meter, and
the meter deciding the next advice.

So this rig runs the real cycle against a plant:

    battery = min(register, production − house − load draw, room left)
    measured = min(potential, export limit + house + battery + draw)

The second line is the site the design exists for: an inverter that HARD-ENFORCES
the export limit by curtailing its own PV, so production is masked while export
sits on the wall and ``curtailed`` is the energy the site threw away. Every
figure below is measured off that — no monkeypatches, no stand-in for
``recommended_charge_limit`` or ``send_inverter_charge_limit``, the same
``run_hub_calculation`` → per-inverter advice → real ``number.set_value`` path
production takes once per site cycle.

The rows here are the ones the design was chosen on (six rounds of head-to-head
experiments, worktree-only): a cloudy household day, a burst train, a kettle, a
cloud, an evening ramp-down, an Excess load taking the surplus, and the gate
engaging.
"""

import math

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.control import inverter as control
from custom_components.dynamic_ocpp_evse.const import (
    CONF_BASE_CONSUMPTION,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_CHARGER_ID,
    CONF_FORECAST_SOC_FLOOR,
    CONF_GRID_EXPORT_LIMIT,
    CONF_HUB_ENTRY_ID,
    CONF_LOAD_PRIORITY,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PHASES,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
    ENTRY_TYPE_LOAD,
    INVERTER_RT_CONTROL_ENABLED,
)

LIMIT = 5000.0          # export limit, and the wall the inverter enforces
MARGIN = 500.0          # Excess trigger margin — the register's slew step too
BASE = 300.0            # configured base consumption (the ENERGY threshold's)
SETPOINT = LIMIT - MARGIN          # 4500 W at the meter
FULL_RATE = 4500.0                 # the battery's charge rating
PACK_V = 51.2                      # the register counts DC amps at this voltage
REGISTER = "number.loop_charge_limit"
NORMAL_A = FULL_RATE / PACK_V      # 87.9 A
DT = 2                             # site cycle, seconds (the default), so the
#                                    engine's input smoothing behaves as it does
#                                    in production rather than five times slower


def _hub(slug):
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=f"Loop Hub {slug}",
        data={CONF_NAME: f"Loop Hub {slug}",
              CONF_ENTITY_ID: f"loop_hub_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.loop_phase_a",
            CONF_MAIN_BREAKER_RATING: 40,
            CONF_PHASE_VOLTAGE: 230,
            CONF_GRID_EXPORT_LIMIT: int(LIMIT),
            CONF_BASE_CONSUMPTION: int(BASE),
            CONF_FORECAST_SOC_FLOOR: 30,
            CONF_SOLAR_FORECAST_ENTITY_IDS: ["sensor.loop_forecast"],
        },
    )


def _inverter(hub, slug, interval=None):
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4,
        title=f"Loop Inverter {slug}",
        data={CONF_NAME: f"Loop Inverter {slug}",
              CONF_ENTITY_ID: f"loop_inverter_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_INVERTER,
              CONF_HUB_ENTRY_ID: hub.entry_id},
        options={
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.loop_battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.loop_battery_power",
            CONF_BATTERY_MAX_CHARGE_POWER: int(FULL_RATE),
            CONF_BATTERY_MAX_DISCHARGE_POWER: int(FULL_RATE),
            CONF_BATTERY_CAPACITY_KWH: 20,
            CONF_BATTERY_NOMINAL_VOLTAGE: PACK_V,
            CONF_SOC_LIMIT_NORMAL_ENTITY_ID: "number.loop_normal",
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.loop_solar",
            CONF_CHARGE_LIMIT_ENTITY_ID: REGISTER,
            CONF_CHARGE_LIMIT_UNIT: "A",
            **({} if interval is None
               else {CONF_CHARGE_CONTROL_INTERVAL: interval}),
        },
    )


def _evse(hub):
    """A 1-phase 6→16 A Excess EVSE, for the yield row."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title="Loop EVSE",
        data={CONF_NAME: "Loop EVSE",
              CONF_ENTITY_ID: "loop_evse",
              ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_CHARGER_ID: "loop_evse",
              CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.loop_evse_current",
              CONF_HUB_ENTRY_ID: hub.entry_id},
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_PHASES: 1,
        },
    )


async def _rig(hass, slug, soc, clip=False, interval=None, writes=None,
               with_evse=False):
    hub = _hub(slug)
    inverter = _inverter(hub, slug, interval=interval)
    inverter.add_to_hass(hass)
    load = _evse(hub) if with_evse else None
    loads = {}
    if load is not None:
        load.add_to_hass(hass)
        loads[load.entry_id] = {
            "entry": load,
            "hub_entry_id": hub.entry_id,
            "operating_mode": "Excess",
            "dynamic_control": True,
        }
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": list(loads)}},
        "loads": loads,
        "load_allocations": {entry_id: 0 for entry_id in loads},
        "inverters": {inverter.entry_id: {INVERTER_RT_CONTROL_ENABLED: True}},
    }

    async def _set_value(call):
        if writes is not None:
            writes.append(call.data["value"])
        hass.states.async_set(
            call.data["entity_id"], str(call.data["value"]),
            {"max": NORMAL_A, "min": 0.0, "unit_of_measurement": "A"},
        )

    hass.services.async_register("number", "set_value", _set_value)
    hass.states.async_set(REGISTER, str(NORMAL_A),
                          {"max": NORMAL_A, "min": 0.0,
                           "unit_of_measurement": "A"})
    hass.states.async_set("number.loop_normal", "95",
                          {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.loop_battery_soc", str(soc),
                          {"device_class": "battery",
                           "unit_of_measurement": "%"})
    hass.states.async_set("sensor.loop_evse_current", "0",
                          {"device_class": "current",
                           "unit_of_measurement": "A"})
    # With a clip forecast the reservation gate is in play; without one the only
    # gate is the destination hold (absorbable_kwh == 0 → full rate below it).
    watts = ({"2026-08-14T10:00:00+00:00": 7300,
              "2026-08-14T11:00:00+00:00": 0} if clip else
             {"2026-08-14T10:00:00+00:00": 1000,
              "2026-08-14T11:00:00+00:00": 0})
    hass.states.async_set("sensor.loop_forecast", "1.0", {"watts": watts})
    return hub, inverter, load


async def _loop(hass, slug, potential, house, soc0, cycles, clip=False,
                load=None, plant_wh=None, interval=None, writes=None,
                with_evse=False):
    """Close the loop through the real engine and the real control layer.

    ``potential(t)`` → W the array could make. ``load(t, verdict)`` → W an
    Excess load draws behind the meter (credited back onto the CT reading the way
    the feedback loop does). ``plant_wh`` is the PLANT's pack size, so a day can
    be run without the SOC walking out of the gate's band.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, evse = await _rig(
        hass, slug, soc0, clip=clip, interval=interval, writes=writes,
        with_evse=with_evse or load is not None,
    )
    trace = []
    soc = float(soc0)
    pack_wh = plant_wh or 20_000.0
    register_w = FULL_RATE
    draw = 0.0
    verdict = False
    prev_limiting = None
    with freeze_time("2026-08-14 08:00:00+00:00") as frozen:
        for i in range(cycles):
            frozen.tick(DT)
            pot = potential(i * DT)
            hs = house(i * DT) if callable(house) else house
            # The battery takes what the register permits, of what is there, and
            # never more than the room it has left.
            room_w = (100.0 - soc) / 100.0 * pack_wh * 3600.0 / DT
            battery = max(0.0, min(register_w, pot - hs - draw, room_w))
            measured = min(pot, LIMIT + hs + battery + draw)
            physical = measured - hs - battery - draw
            curtailed = pot - measured
            soc = min(100.0, soc + battery * DT / 3600.0 / pack_wh * 100.0)
            hass.states.async_set(
                "sensor.loop_solar", str(measured),
                {"device_class": "power", "unit_of_measurement": "W"})
            # The CT reads the PHYSICAL meter figure. Crediting the managed
            # draw back is the ENGINE's job (``_apply_feedback_loop`` plus the
            # reconstruction), and doing it here as well would double-count it.
            hass.states.async_set(
                "sensor.loop_phase_a", str(-physical / 230.0),
                {"device_class": "current", "unit_of_measurement": "A"})
            hass.states.async_set(
                "sensor.loop_battery_power", str(-battery),
                {"device_class": "power", "unit_of_measurement": "W"})
            hass.states.async_set(
                "sensor.loop_battery_soc", f"{soc:.2f}",
                {"device_class": "battery", "unit_of_measurement": "%"})
            if evse is not None:
                hass.states.async_set(
                    "sensor.loop_evse_current", f"{draw / 230.0:.3f}",
                    {"device_class": "current", "unit_of_measurement": "A"})
            result = run_hub_calculation(hass, hub)
            section = result["inverters"][inverter.entry_id]
            advice = section["forecast_charge_limit_w"]
            limiting = section["forecast_charge_limiting"]
            # The production path, verbatim: the site-cycle worker hands the
            # published advice and gate to the control with a monotonic stamp.
            await control.send_inverter_charge_limit(
                hass, inverter, hub, advice, i * DT, limiting
            )
            register_w = float(hass.states.get(REGISTER).state) * PACK_V
            verdict = bool(result["excess_available"])
            draw = load(i * DT, verdict) if load else 0.0
            trace.append({
                "i": i, "soc": soc, "potential": pot, "house": hs,
                "battery": battery, "advice": advice, "limiting": limiting,
                "engaging": bool(limiting) and not prev_limiting,
                "register_w": register_w, "export": physical,
                "curtailed": curtailed, "draw": draw, "verdict": verdict,
                "margin": result["excess_margin_power"],
            })
            prev_limiting = limiting
    return trace


def _wh(trace, key):
    return sum(row[key] for row in trace) * DT / 3600.0


def _reversals(writes):
    moves = [b - a for a, b in zip(writes, writes[1:])]
    moves = [m for m in moves if abs(m) > 0.01]
    return sum(1 for a, b in zip(moves, moves[1:]) if a * b < 0)


def _ideal(row):
    """What a perfect controller would have permitted this cycle."""
    return min(FULL_RATE,
               max(0.0, row["potential"] - row["house"] - row["draw"] - SETPOINT))


# ── the decision row: a cloudy household day ──────────────────────────


def _cloudy_day(seed=20260826, hours=8, events=40, clouds=4):
    """A solar arc with 40 household events and four thick passing clouds.

    A clear-sky day flatters a feedforward controller (its whole cost is a
    standing offset, which a smooth arc never disturbs) and a cloudy one flatters
    a memoryless controller (it has no stale correction to carry out of a cloud).
    A real day has both, which is why this is the row the design rests on.
    """
    rnd = __import__("random").Random(seed)
    span = hours * 3600
    events_list = [
        (start, start + rnd.uniform(120, 900), rnd.uniform(500, 3000))
        for start in (rnd.uniform(0, span) for _ in range(events))
    ]
    cloud_rnd = __import__("random").Random(seed + 1)
    windows = [
        (start, start + cloud_rnd.uniform(600, 900))
        for start in (cloud_rnd.uniform(0.15 * span, 0.85 * span)
                      for _ in range(clouds))
    ]

    def house(t):
        return BASE + sum(w for s, e, w in events_list if s <= t < e)

    def potential(t):
        clear = max(0.0, 7200.0 * math.sin(math.pi * t / span))
        for s, e in windows:
            if s <= t < e:
                return clear * 0.08        # a thick cloud, ~8 % of clear sky
        return clear

    return potential, house


async def test_a_cloudy_household_day_curtails_almost_nothing(hass):
    """The decision row. 8 h, 40 household events, 4 clouds, gate engaged.

    The experiments put the design this replaced (production feedforward anchored
    a margin low, plus a bounded integral trim) at ~370 Wh of curtailment on this
    day. Production measures ~51 Wh here, and the budget below is 100 Wh —
    generous enough not to be a fingerprint of the seed, tight enough that a
    return to a stateful value could not pass it.

    The whole price is register traffic, and that is bounded here too: the
    downward window is what keeps a day of household events from becoming a day
    of Modbus writes (~77 measured for the eight hours, against the thousands a
    controller that wrote every move it saw would have made).
    """
    potential, house = _cloudy_day()
    writes = []
    trace = await _loop(hass, "cday", potential, house, soc0=96.0, cycles=14400,
                        plant_wh=400_000.0, writes=writes)

    assert all(row["limiting"] for row in trace)   # the destination hold, all day
    curtailed = _wh(trace, "curtailed")
    assert curtailed <= 100.0, f"curtailed {curtailed:.1f} Wh"
    # 145, not the 120 this held when the write deadband was 5 % of the normal
    # value. The deadband became an absolute 100 W (2026-08-31), which is ~5×
    # tighter at this register's scale, so the approach to a moving setpoint
    # costs more writes: 132 measured here against 120 before. The curtailment
    # budget above is what the trade buys and must not move.
    assert len(writes) <= 145, f"{len(writes)} writes in 8 h"
    # And the battery really did absorb the surplus rather than the site export
    # it: the ideal permit and what the pack took agree to within a few percent.
    ideal_wh = sum(_ideal(row) for row in trace) * DT / 3600.0
    assert _wh(trace, "battery") >= 0.9 * ideal_wh


# ── the protective transition ─────────────────────────────────────────


async def test_the_gate_engaging_lands_its_write_on_the_gate_cycle(hass):
    """The exemption, end to end: the pack crosses its destination at full rate.

    Without it the register would sit at full rate for a whole persistence
    window with the pack already above where its owner sends it — the window is
    for steady-state corrections, and this is not one.
    """
    writes = []
    trace = await _loop(hass, "engage", lambda t: 6800.0, BASE, soc0=94.4,
                        cycles=600, writes=writes)

    gate = next(row["i"] for row in trace if row["limiting"])
    assert trace[gate]["engaging"] is True
    # The register is down on the gate cycle itself, not a window later.
    assert trace[gate]["register_w"] < FULL_RATE - MARGIN
    assert len(writes) >= 1
    # And it is genuinely the protective direction: the pack stops taking full
    # rate within a cycle of the crossing.
    assert trace[gate + 1]["battery"] < FULL_RATE


# ── the yield: the surplus goes to the car, not to the pack ───────────


async def test_an_engaged_excess_load_takes_the_surplus_from_the_pack(hass):
    """Above the destination the battery is the absorber of LAST resort.

    A 3 kW Excess load runs behind the meter while the pack is parked above its
    destination. The pack must give up its permit watt for watt — "theft" here is
    the battery taking what the car could have had — and the reconstruction is
    what keeps the load's own draw from being read as an export shortfall.
    """
    def load(t, verdict):
        return 3000.0

    trace = await _loop(hass, "yield", lambda t: 8000.0, BASE, soc0=96.0,
                        cycles=900, plant_wh=400_000.0, load=load)

    settled = trace[600:]
    surplus = 8000.0 - BASE - SETPOINT            # 3200 W the site cannot place
    theft = max(row["battery"] - max(0.0, surplus - 3000.0)
                for row in settled)
    assert theft <= 250.0, f"the pack took {theft:.0f} W of the load's surplus"
    # The car kept drawing throughout: the yield did not cost it the verdict.
    assert all(row["draw"] == 3000.0 for row in settled)


# ── the evening: the register follows production down ─────────────────


async def test_the_register_follows_the_evening_ramp_down(hass):
    """Production falls 6800 → 0 over an hour with the gate engaged.

    The worry a peak-hold filter would have had here: a register held at the
    day's maximum while the sun goes. Directional pacing has no such memory —
    every window's maximum is a fresh measurement — so the register walks down
    with the sun, one window at a time.
    """
    writes = []
    trace = await _loop(
        hass, "rampdown", lambda t: max(0.0, 6800.0 * (1 - t / 3600.0)), BASE,
        soc0=96.0, cycles=2400, plant_wh=400_000.0, writes=writes)

    at_5 = trace[150]["register_w"]
    at_30 = trace[900]["register_w"]
    at_60 = trace[1799]["register_w"]
    assert at_30 < at_5 / 2.0, "the register stayed high as the sun went"
    assert at_60 <= 500.0
    # One direction only: every write in the ramp-down is a step down, so the
    # register tracks the sun rather than hunting after it.
    assert _reversals(writes) == 0
    # And nothing is curtailed on the way: the register is always at or above
    # what the sun can actually deliver into it.
    assert _wh(trace, "curtailed") <= 20.0


# ── the household noise the window exists for ─────────────────────────


async def test_a_kettle_costs_no_register_write_at_all(hass):
    """A 2 kW household step for 60 s: the window eats it whole.

    This is the guard the deleted integral trim's time constant used to provide,
    and the reason the value is allowed to be memoryless. The advice DOES move —
    the meter really did drop by 2 kW, and pretending otherwise is what a
    feedforward controller did — but a reduction that lasts one minute of a five
    minute window never reaches the register.
    """
    writes = []
    trace = await _loop(
        hass, "kettle", lambda t: 6800.0,
        lambda t: BASE + (2000.0 if 1200 <= t < 1260 else 0.0),
        soc0=96.0, cycles=1500, plant_wh=400_000.0, writes=writes)

    settled = [row for row in trace if 300 <= row["i"] < 600]
    kettle_rows = [row for row in trace if 600 <= row["i"] < 630]
    # The advice saw it — a bite as much as an assertion: a controller that
    # could not see a kettle at all would pass the register half of this test.
    assert min(row["advice"] for row in kettle_rows) <= (
        min(row["advice"] for row in settled) - 1500
    )
    # The register did not move a step, and the whole run cost the ONE write
    # the gate's own engagement made.
    during = [row["register_w"] for row in kettle_rows]
    assert max(during) - min(during) == 0.0
    assert len(writes) == 1


async def test_a_burst_train_is_almost_free(hass):
    """3 kW of household load, 3 minutes on and 3 minutes off, for two hours.

    The pathological input for a stateful controller: round 2 of the experiments
    wound an unbounded trim to −2 kW on this and curtailed ~1.4 kW continuously.
    Memoryless, each burst is answered honestly and forgotten, and the 300 s
    window means no burst shorter than itself reaches the register at all.

    At a 60 s window the same train costs several times the writes for a few
    watt-hours less curtailment — which is why the setting's default is 300 s and
    why lowering it buys nothing. Not a test of its own: a configuration we
    advise against is not a contract, and the experiments' 60 s rows are the
    record of it.
    """
    writes = []
    trace = await _loop(
        hass, "burst", lambda t: 6800.0,
        lambda t: 3000.0 if t % 360 < 180 else BASE,
        soc0=96.0, cycles=3600, plant_wh=400_000.0, writes=writes)

    curtailed = _wh(trace, "curtailed")
    assert curtailed <= 10.0, f"curtailed {curtailed:.1f} Wh"
    # 16, not 10, for the same reason as the cloudy day above: an absolute
    # 100 W deadband tracks a moving setpoint more closely and pays for it in
    # writes (14 measured). Curtailment is unchanged at ≤10 Wh.
    assert len(writes) <= 16, f"{len(writes)} writes in two hours"


# ── the cloud ─────────────────────────────────────────────────────────


async def test_a_cloud_costs_little_and_leaves_nothing_behind(hass):
    """15 minutes at 500 W, gate engaged, then the sun returns.

    Two things at once: the cloud itself is cheap, and the recovery is clean —
    the register climbs back at one margin per cycle with no correction earned in
    the dark riding on top of it, and the site ends where it started.
    """
    writes = []
    trace = await _loop(
        hass, "cloud",
        lambda t: 500.0 if 1800 <= t < 2700 else 6800.0,
        BASE, soc0=96.0, cycles=3000, plant_wh=400_000.0, writes=writes)

    assert _wh(trace, "curtailed") <= 20.0
    after = [row for row in trace if row["i"] >= 2100]
    before = [row for row in trace if 600 <= row["i"] < 900]
    # Same plant, same place: no stale state can survive the cloud, because
    # there is no state.
    assert abs(after[-1]["register_w"] - before[-1]["register_w"]) <= 250.0
    assert abs(after[-1]["export"] - before[-1]["export"]) <= 250.0


async def test_a_steady_plant_settles_and_stops_writing(hass):
    """H1 and the limit cycle, in one row: the loop lands on the setpoint and
    then leaves the register alone.

    Export inside 50 W of (limit − margin) is the accuracy the whole controller
    was chosen for, and zero reversals after settling is what says the deadband
    and the window are not fighting each other.
    """
    writes = []
    trace = await _loop(hass, "steady", lambda t: 6800.0, BASE, soc0=96.0,
                        cycles=3000, plant_wh=400_000.0, writes=writes)

    settled = trace[1500:]
    assert abs(settled[-1]["export"] - SETPOINT) <= 50.0
    # Nothing written in the second half: the plant is still, so the register is.
    written_late = [
        row for row in settled
        if abs(row["register_w"] - settled[0]["register_w"]) > 1.0
    ]
    assert written_late == []
    assert _reversals(writes) == 0
