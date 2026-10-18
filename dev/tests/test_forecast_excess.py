"""Forecast-driven Excess verdict (owner, 2026-10-17).

Machine-authored tests - not yet human-reviewed.

The pure latch and top-up (calculations/forecast_excess.py), where the calculator
reads them (the verdict, the Excess pool, the per-phase importing guard, the
Solar behaviours left alone), the engine wiring (``_apply_forecast_excess``) and
the published verdict through ``run_hub_calculation``.
"""

from types import SimpleNamespace

import pytest

from custom_components.dynamic_ocpp_evse.calculations.forecast_excess import (
    FORECAST_EXCESS_MIN_KWH,
    forecast_excess_gate,
    forecast_excess_verdict,
    starts_early,
)
from custom_components.dynamic_ocpp_evse.calculations.pacing import pace_watts
from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext,
    PhaseConstraints,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    _calculate_excess_available,
    _excess_permits,
    _excess_phase_is_importing,
    _excess_verdict,
    _source_limit,
    excess_figures,
    excess_margin,
)
from custom_components.dynamic_ocpp_evse.const import (
    BEHAVIOR_BINARY_EXCESS,
    BEHAVIOR_SOLAR_ONLY,
    BEHAVIOR_SOLAR_PRIORITY,
    CONF_FORECAST_EXCESS_ENABLED,
    CONF_FORECAST_EXCESS_SOC,
    EXCESS_URGENCY_TIER,
    CONF_FORECAST_EXCESS_SOC_SOURCE,
    FORECAST_EXCESS_SOURCE_FIXED,
    FORECAST_EXCESS_SOURCE_MIN,
    FORECAST_EXCESS_SOURCE_TARGET,
    FORECAST_SOC_HYSTERESIS,
)
from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
    _apply_forecast_excess,
    _publish_early_start,
)
from custom_components.dynamic_ocpp_evse.control.status import with_early_start

MIN, TARGET = 20.0, 80.0


# --- the gate ----------------------------------------------------------------

def test_gate_is_the_chosen_source():
    assert forecast_excess_gate(FORECAST_EXCESS_SOURCE_MIN, MIN, TARGET, 60) == (MIN, 0.0)
    assert forecast_excess_gate(FORECAST_EXCESS_SOURCE_TARGET, MIN, TARGET, 60) == (TARGET, 0.0)
    assert forecast_excess_gate(FORECAST_EXCESS_SOURCE_FIXED, MIN, TARGET, 60) == (
        60, float(FORECAST_SOC_HYSTERESIS)
    )
    assert forecast_excess_gate("nonsense", MIN, TARGET, 60)[0] is None


# --- the verdict latch ---------------------------------------------------------

def _on(soc=85, gate=80, band=0.0, planned=3.0, was_on=False, enabled=True):
    return forecast_excess_verdict(enabled, soc, gate, band, planned, was_on)


def test_on_when_everything_holds():
    assert _on() is True


@pytest.mark.parametrize(
    "kw",
    [
        {"enabled": False},
        {"soc": None},  # no battery / no SOC: inert
        {"planned": None},  # sensors unreadable past the stale timeout
        {"soc": 79.9},  # under the gate
        {"planned": 0.0},
        {"planned": FORECAST_EXCESS_MIN_KWH},  # must EXCEED the margin
    ],
)
def test_each_condition_is_needed(kw):
    assert _on(**kw) is False


def test_gate_none_is_off():
    assert forecast_excess_verdict(True, 85, None, 0, 3.0) is False


def test_the_option_off_drops_a_latched_verdict():
    assert _on(was_on=True, enabled=False) is False


def test_fixed_value_holds_inside_its_band_and_releases_below():
    band = float(FORECAST_SOC_HYSTERESIS)
    assert _on(soc=60, gate=60, band=band) is True
    assert _on(soc=60 - band, gate=60, band=band, was_on=True) is True  # wobble: held
    assert _on(soc=60 - band - 0.1, gate=60, band=band, was_on=True) is False
    # off again until the gate itself is reached, not the band
    assert _on(soc=59, gate=60, band=band, was_on=False) is False


def test_planned_energy_crossing_zero_does_not_chatter():
    # starts above 0.2 kWh, holds down to 0.1, releases at or under it
    assert _on(planned=0.25) is True
    assert _on(planned=0.15, was_on=True) is True
    assert _on(planned=0.15, was_on=False) is False
    assert _on(planned=0.1, was_on=True) is False
    assert _on(planned=-1.0, was_on=True) is False


def test_latched_gate_with_no_band_releases_at_the_gate():
    # minimum / target arrive already latched (battery SOC hysteresis in them)
    assert _on(soc=80, gate=80, band=0.0) is True
    assert _on(soc=79, gate=80, band=0.0, was_on=True) is False


# --- the pace and the top-up -------------------------------------------------------

def test_pace_is_energy_over_hours_floored_at_one():
    assert pace_watts(4.6, 2) == pytest.approx(2300)
    assert pace_watts(4.6, 0.2) == pytest.approx(4600)
    assert pace_watts(-1, 2) == 0.0


# --- the calculator ------------------------------------------------------------------

def _site(charge_w=3000.0, *, forecast=True, pace_w=2000.0, phases=1,
          asymmetric=True, threshold=13000.0, soc=85.0, charge_rating=5000.0,
          export=0.0, consumption=0.0):
    def pv(v):
        return PhaseValues(*(v if i < phases else None for i in range(3)))

    return SiteContext(
        voltage=230,
        main_breaker_rating=35,
        consumption=pv(consumption),
        export_current=pv(export),
        battery_soc=soc,
        battery_soc_min=MIN,
        battery_soc_target=TARGET,
        battery_soc_full=97.0,
        battery_power=-charge_w,
        battery_max_charge_power=charge_rating,
        excess_export_threshold=threshold,
        inverter_supports_asymmetric=asymmetric,
        forecast_excess=forecast,
        forecast_excess_pace_w=pace_w,
    )


def _tank(**kw):
    """A binary element of 8.3 A on one phase: 1.9 kW at 230 V."""
    return LoadContext(
        load_id="tank", entity_id="tank", min_current=8.3, max_current=8.3, phases=1,
        mode_behavior=BEHAVIOR_BINARY_EXCESS, active_phases_mask="A", **kw,
    )


def test_pool_is_the_paced_part_of_the_battery_charge():
    pool = _calculate_excess_available(_site(3000, pace_w=2000))
    assert pool.get_available("A") == pytest.approx(2000 / 230)


def test_pool_is_capped_by_the_charge_when_the_pace_is_higher():
    pool = _calculate_excess_available(_site(3000, pace_w=9000))
    assert pool.get_available("A") == pytest.approx(3000 / 230)


def test_no_planned_energy_no_top_up_but_the_verdict_stands():
    site = _site(3000, pace_w=0.0)
    assert _excess_verdict(site, _tank()) is True
    assert _calculate_excess_available(site).get_available("A") == 0


def test_without_the_forecast_the_pool_is_empty():
    site = _site(3000, forecast=False)
    assert _excess_verdict(site, _tank()) is False
    assert _calculate_excess_available(site).get_available("A") == 0


def test_a_battery_not_charging_has_nothing_to_give():
    site = _site(0.0, pace_w=2000)
    assert _calculate_excess_available(site).get_available("A") == 0


def test_the_top_up_adds_to_a_surplus_verdict_of_its_own():
    # export 8 A over a 1000 W trigger + a battery saturated at its 3000 W rating
    site = _site(3000, pace_w=2000, threshold=1000.0, charge_rating=3000.0, export=8.0)
    margin = excess_margin(site, 0.0)
    assert margin > 0
    pool = _calculate_excess_available(site)
    assert pool.get_available("A") * 230 == pytest.approx(margin + 2000)


def test_the_pool_never_exceeds_what_the_site_places():
    # charge above the rating our control holds it to: the margin already
    # counts the excess charge, the top-up must not count it again
    site = _site(3000, pace_w=9000, threshold=1000.0, charge_rating=1000.0, export=8.0)
    _, absorbed, _ = excess_figures(site, 0.0)
    pool = _calculate_excess_available(site)
    assert pool.get_available("A") * 230 == pytest.approx(absorbed)


def test_a_load_that_starts_does_not_shrink_its_own_top_up():
    # An Excess-tier load takes 1500 W out of the battery's charge (3000 -> 1500 W);
    # the feedback loop has put its draw back on the export side (6.52 A) and the
    # placement hands it back to the battery. The top-up is the same either way.
    load = LoadContext(
        load_id="l", entity_id="l", min_current=6, max_current=16, phases=1,
        l1_current=1500 / 230, active_phases_mask="A", dynamic_control=True,
        mode_priority=EXCESS_URGENCY_TIER,
    )
    idle = _calculate_excess_available(_site(3000, pace_w=2000))
    site = _site(1500, pace_w=2000, export=1500 / 230)
    site.loads = [load]
    running = _calculate_excess_available(site)
    assert running.get_available("A") == pytest.approx(2000 / 230)
    assert running.get_available("A") == pytest.approx(idle.get_available("A"))


def test_symmetric_pool_is_spread_over_the_phases_like_the_charge():
    pool = _calculate_excess_available(
        _site(6900, pace_w=6000, phases=3, asymmetric=False, charge_rating=9000)
    )
    # each phase's own flow is 0, the top-up is 2000 W per phase
    assert pool.get_available("A") == pytest.approx(2000 / 230)
    assert pool.get_available("B") == pytest.approx(2000 / 230)


def test_the_importing_guard_sees_the_topped_up_phase():
    load = LoadContext(
        load_id="l", entity_id="l", min_current=6, max_current=16, phases=1,
        active_phases_mask="A",
    )
    # phase A imports 1 A (the battery is eating the sun)
    flat = _site(3000, pace_w=2000, consumption=1.0, forecast=False)
    assert _excess_phase_is_importing(load, flat) is True
    topped = _site(3000, pace_w=2000, consumption=1.0)
    assert _excess_phase_is_importing(load, topped) is False


def _plug(behavior):
    return LoadContext(
        load_id="p", entity_id="p", min_current=8.7, max_current=8.7, phases=1,
        device_type="plug", mode_behavior=behavior, active_phases_mask="A",
    )


def test_every_binary_excess_load_follows_the_verdict():
    full = PhaseConstraints.from_pool(30, 30, 30, 30)
    site = _site(3000, pace_w=2000)
    pool = _calculate_excess_available(site)
    assert _source_limit(_plug(BEHAVIOR_BINARY_EXCESS), site, full, pool) == pytest.approx(8.7)
    off = _site(3000, forecast=False)
    assert _source_limit(
        _plug(BEHAVIOR_BINARY_EXCESS), off, full, _calculate_excess_available(off)
    ) == 0


@pytest.mark.parametrize("behavior", [BEHAVIOR_SOLAR_ONLY, BEHAVIOR_SOLAR_PRIORITY])
def test_solar_behaviours_ignore_the_forecast_verdict(behavior):
    # battery under its target: Solar Only 0, Solar Priority its minimum - as
    # before (docs/design-decisions.md, 2026-09-09)
    solar = PhaseConstraints.from_pool(30, 30, 30, 30)
    plug = LoadContext(
        load_id="p", entity_id="p", min_current=6, max_current=16, phases=1,
        mode_behavior=behavior, active_phases_mask="A",
    )
    on, off = _site(3000, soc=70), _site(3000, soc=70, forecast=False)
    got = [
        _source_limit(plug, s, solar, _calculate_excess_available(s)) for s in (on, off)
    ]
    assert got[0] == got[1] == (0 if behavior == BEHAVIOR_SOLAR_ONLY else 6)


# --- the engine wiring -----------------------------------------------------------------

def _entry(**options):
    return SimpleNamespace(entry_id="hub", data={}, options=options)


def _apply(entry, runtime, soc=85.0, pacing=(3.0, 2.0)):
    site = _site()
    site.battery_soc = soc
    site.forecast_excess = False
    site.forecast_excess_pace_w = 0.0
    return _apply_forecast_excess(runtime, entry, site, pacing, pacing[1] if pacing else None), site


def test_option_off_by_default():
    on, site = _apply(_entry(), {})
    assert on is False and site.forecast_excess is False
    assert site.forecast_excess_pace_w == 0.0


def test_engine_sets_the_verdict_and_the_pace():
    runtime = {}
    on, site = _apply(_entry(**{CONF_FORECAST_EXCESS_ENABLED: True}), runtime)
    assert on is True and site.forecast_excess is True
    assert site.forecast_excess_pace_w == pytest.approx(1500.0)  # 3 kWh / 2 h
    assert runtime["_excess_by_forecast_on"] is True


def test_engine_default_source_is_the_target():
    entry = _entry(**{CONF_FORECAST_EXCESS_ENABLED: True})
    assert _apply(entry, {}, soc=79)[0] is False
    assert _apply(entry, {}, soc=80)[0] is True


def test_engine_minimum_source_and_fixed_value():
    entry = _entry(**{
        CONF_FORECAST_EXCESS_ENABLED: True,
        CONF_FORECAST_EXCESS_SOC_SOURCE: FORECAST_EXCESS_SOURCE_MIN,
    })
    assert _apply(entry, {}, soc=25)[0] is True
    fixed = _entry(**{
        CONF_FORECAST_EXCESS_ENABLED: True,
        CONF_FORECAST_EXCESS_SOC_SOURCE: FORECAST_EXCESS_SOURCE_FIXED,
        CONF_FORECAST_EXCESS_SOC: 50,
    })
    runtime = {}
    assert _apply(fixed, runtime, soc=50)[0] is True
    assert _apply(fixed, runtime, soc=48.5)[0] is True  # inside the band
    assert _apply(fixed, runtime, soc=47)[0] is False


def test_engine_unreadable_pacing_switches_it_off():
    runtime = {}
    entry = _entry(**{CONF_FORECAST_EXCESS_ENABLED: True})
    assert _apply(entry, runtime)[0] is True
    assert _apply(entry, runtime, pacing=None)[0] is False
    assert runtime["_excess_by_forecast_on"] is False


def test_engine_no_battery_is_inert():
    entry = _entry(**{CONF_FORECAST_EXCESS_ENABLED: True})
    assert _apply(entry, {}, soc=None)[0] is False


# --- the published verdict, through the whole hub calculation -------------------------

def _rig(hass, **extra):
    """The destination rig plus the pacing sensors and the option, and the two
    helpers that set its states and run one cycle at 08:00 UTC."""
    from freezegun import freeze_time
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.dynamic_ocpp_evse.const import (
        CONF_PACING_ENERGY_ENTITY_ID,
        CONF_PACING_HORIZON_ENTITY_ID,
        DOMAIN,
    )
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    from .test_sensor_update import (
        _destination_hub,
        _destination_inverter,
        _set_destination_states,
    )

    base = _destination_hub("fx")
    hub = MockConfigEntry(
        domain=DOMAIN, entry_id=base.entry_id, version=2, minor_version=4,
        title=base.title, data=dict(base.data),
        options={
            **base.options,
            CONF_PACING_ENERGY_ENTITY_ID: "sensor.fx_energy",
            CONF_PACING_HORIZON_ENTITY_ID: "sensor.fx_horizon",
            **extra,
        },
    )
    _destination_inverter(hass, hub, "fx").add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }

    def states(soc=85, energy="3.45", power=-3000):
        _set_destination_states(hass, soc=soc)
        hass.states.async_set(
            "sensor.dst_battery_soc", str(soc),
            {"device_class": "battery", "unit_of_measurement": "%"},
        )
        hass.states.async_set(
            "sensor.dst_battery_power", str(power),
            {"device_class": "power", "unit_of_measurement": "W"},
        )
        hass.states.async_set(
            "sensor.fx_energy", energy,
            {"device_class": "energy", "unit_of_measurement": "kWh"},
        )
        hass.states.async_set(
            "sensor.fx_horizon", "2026-08-14T10:00:00+00:00",
            {"device_class": "timestamp"},
        )

    def run():
        with freeze_time("2026-08-14 08:00:00+00:00"):
            return run_hub_calculation(hass, hub)

    return states, run


async def test_hub_publishes_the_forecast_verdict(hass):
    """Battery 85 % (target 80 %) charging at 3 kW under a 5 kW rating: the surplus
    verdict is off. With the option on and 3.45 kWh planned over two hours, the
    hub's ``excess_available`` (the one verdict the tank and every Excess load
    read) is on and says it was the forecast; each condition then switches it
    off again."""
    states, run = _rig(hass, **{CONF_FORECAST_EXCESS_ENABLED: True})

    states()
    result = run()
    assert result["excess_available"] is True
    assert result["forecast_excess"] is True
    assert result["excess_margin_power"] < 0  # the surplus itself says no

    states(soc=70)  # under the target gate
    assert run()["excess_available"] is False
    states()
    assert run()["excess_available"] is True
    states(energy="0")  # nothing left to use
    assert run()["excess_available"] is False


async def test_hub_without_the_option_is_unchanged(hass):
    states, run = _rig(hass)
    states()
    result = run()
    assert result["excess_available"] is False
    assert result["forecast_excess"] is False


async def test_hub_without_a_battery_reading_is_inert(hass):
    states, run = _rig(hass, **{CONF_FORECAST_EXCESS_ENABLED: True})
    states(soc="unavailable")
    assert run()["forecast_excess"] is False


# --- early start per load: counting back from the next full charge ---------------------

def test_starts_early_is_the_latest_start_before_the_next_full_charge():
    # 16.1 kWh at a 1.9 kW element is 8.5 h of heating: from 8.4 h before full
    assert starts_early(16.1, 8.4, 1900) is True
    assert starts_early(16.1, 9.0, 1900) is False
    # 2.4 kWh is 1.3 h: nowhere near, the boiler waits for the full battery
    assert starts_early(2.4, 8.4, 1900) is False
    assert starts_early(2.4, 1.2, 1900) is True
    # full now (0 h left): any energy goes
    assert starts_early(0.5, 0.0, 1900) is True


def test_starts_early_holds_inside_a_band_and_is_off_when_unreadable():
    assert starts_early(16.1, 9.0, 1900) is False
    assert starts_early(16.1, 9.0, 1900, was_on=True) is True  # inside the 20 % band
    assert starts_early(16.1, 11.0, 1900, was_on=True) is False
    assert starts_early(None, 2.0, 1900) is False
    assert starts_early(3.0, None, 1900) is False
    assert starts_early(0.1, 5.0, 0.0) is True  # a load with no minimum is never held back


def test_a_load_not_yet_due_does_not_take_the_forecast_verdict():
    site = _site(3000, forecast=True)
    site.battery_power = 0  # nothing absorbed: the pool is empty, only the verdict is left
    assert _excess_permits(_tank(early_start_ok=True), site, 0.0) is True
    assert _excess_permits(_tank(early_start_ok=False), site, 0.0) is False


def test_the_engine_sends_each_load_when_its_own_minimum_can_use_the_energy():
    entry = _entry(**{CONF_FORECAST_EXCESS_ENABLED: True})
    boiler = _tank()
    small = LoadContext(
        load_id="small", entity_id="small", min_current=1.3, max_current=1.3, phases=1,
        mode_behavior=BEHAVIOR_BINARY_EXCESS, active_phases_mask="A",
    )

    def run(planned, hours, loads):
        site = _site()
        site.battery_soc = 85.0
        site.loads = loads
        on = _apply_forecast_excess({}, entry, site, (planned, 8.0), hours)
        return on, site

    # midnight: 16.1 kWh, 15 h to full - only a 300 W load fits yet
    on, site = run(16.1, 15.0, [boiler, small])
    assert (boiler.early_start_ok, small.early_start_ok) == (False, True)
    assert on is True and site.forecast_excess is True
    # 8.4 h before full the boiler joins
    run(16.1, 8.4, [boiler, small])
    assert boiler.early_start_ok is True
    # today: 2.4 kWh, 8 h to full - the boiler waits
    run(2.4, 8.0, [boiler, small])
    assert (boiler.early_start_ok, small.early_start_ok) == (False, True)
    # the boiler alone, not due: no site verdict at all, so no pool top-up either
    on, site = run(2.4, 8.0, [boiler])
    assert on is False and site.forecast_excess is False and site.forecast_excess_pace_w == 0.0


def test_a_waiting_load_says_when_it_will_start_and_at_what_power():
    from custom_components.dynamic_ocpp_evse.const import DOMAIN

    waiting, due = _tank(early_start_ok=False), _tank(early_start_ok=True)
    due.load_id = "due"
    solar = _tank(early_start_ok=False)
    solar.load_id, solar.mode_behavior = "solar", BEHAVIOR_SOLAR_ONLY
    site = _site()
    site.loads = [waiting, due, solar]
    hass = SimpleNamespace(data={DOMAIN: {"loads": {"tank": {}, "due": {}, "solar": {}}}})
    _publish_early_start(hass, {"_excess_by_forecast_on": True}, site, 2.4, 8.0)
    note = hass.data[DOMAIN]["loads"]["tank"]["early_start_note"]
    assert note.startswith("Early start ") and note.endswith(" at 1.9 kW")
    assert hass.data[DOMAIN]["loads"]["due"]["early_start_note"] is None      # already its turn
    assert hass.data[DOMAIN]["loads"]["solar"]["early_start_note"] is None    # not an Excess load
    _publish_early_start(hass, {"_excess_by_forecast_on": False}, site, 2.4, 8.0)
    assert hass.data[DOMAIN]["loads"]["tank"]["early_start_note"] is None     # verdict off
    assert with_early_start("Off", {"early_start_note": note}) == f"Off - {note}"
    assert with_early_start("Off", {}) == "Off"
