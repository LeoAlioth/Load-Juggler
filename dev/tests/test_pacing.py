"""Pacing of an Excess-mode load over the planning horizon (GitHub issue 22).

Machine-authored tests - not yet human-reviewed.

The pure term (calculations/pacing.py), where the calculator applies it
(``_source_limit``, both Excess behaviors, running vs off), and the reader of
the hub's two entities (engine/readers.py ``_read_pacing``): unavailable
sentinel, holdover inside the stale timeout, fall back to unpaced after it.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext,
    PhaseConstraints,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.pacing import (
    PACE_START_MARGIN,
    pace_cap_current,
    pace_limit,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    _source_limit,
)
from custom_components.dynamic_ocpp_evse.const import (
    BEHAVIOR_BINARY_EXCESS,
    BEHAVIOR_EXCESS,
    BEHAVIOR_SOLAR_ONLY,
    CONF_PACING_ENERGY_ENTITY_ID,
    CONF_PACING_HORIZON_ENTITY_ID,
    INPUT_STALE_TIMEOUT,
)
from custom_components.dynamic_ocpp_evse.engine import readers


# --- the pure term ---------------------------------------------------------

def test_cap_is_energy_over_hours_per_phase():
    # 4.6 kWh over 2 h = 2300 W = 10 A on one phase, 3.33 A on each of three
    assert pace_cap_current(4.6, 2, 230, 1) == pytest.approx(10.0)
    assert pace_cap_current(4.6, 2, 230, 3) == pytest.approx(10.0 / 3)


def test_hours_left_floors_at_one_hour():
    assert pace_cap_current(2.3, 0.1, 230, 1) == pace_cap_current(2.3, 1, 230, 1)
    assert pace_cap_current(2.3, 0, 230, 1) == pytest.approx(10.0)


def test_negative_energy_caps_at_zero():
    assert pace_cap_current(-3, 5, 230, 1) == 0.0


def test_no_voltage_caps_at_zero():
    assert pace_cap_current(5, 5, 0, 1) == 0.0


def test_limit_off_under_minimum_even_when_running():
    assert pace_limit(16, 5.9, 6, running=True) == 0.0


def test_running_load_rides_down_to_the_minimum():
    assert pace_limit(16, 6.0, 6, running=True) == 6.0
    assert pace_limit(16, 6.5, 6, running=True) == 6.5


def test_off_load_starts_only_past_the_margin():
    start = 6 * (1 + PACE_START_MARGIN)
    assert pace_limit(16, start - 0.01, 6, running=False) == 0.0
    assert pace_limit(16, start, 6, running=False) == pytest.approx(start)


def test_cap_never_raises_the_limit():
    assert pace_limit(8, 12, 6, running=False) == 8
    assert pace_limit(0, 12, 6, running=False) == 0


# --- where the calculator applies it ---------------------------------------

def _site():
    return SiteContext(
        voltage=230,
        main_breaker_rating=35,
        consumption=PhaseValues(2.0, None, None),
        export_current=PhaseValues(20.0, None, None),
    )


def _load(behavior, cap, draw=0.0, min_current=6, **kw):
    return LoadContext(
        load_id="l", entity_id="l", min_current=min_current, max_current=16, phases=1,
        mode_behavior=behavior, l1_current=draw, pace_cap_current=cap, **kw
    )


def _limit(load):
    pool = PhaseConstraints.from_pool(30, 30, 30, 30)
    return _source_limit(load, _site(), pool, pool)


def test_excess_load_is_held_to_its_cap_when_running():
    assert _limit(_load(BEHAVIOR_EXCESS, 7.5, draw=7.0)) == pytest.approx(7.5)


def test_excess_load_off_in_the_start_band_until_it_runs():
    assert _limit(_load(BEHAVIOR_EXCESS, 6.5, draw=0.0)) == 0.0
    assert _limit(_load(BEHAVIOR_EXCESS, 6.5, draw=6.0)) == pytest.approx(6.5)


def test_a_small_load_counts_as_running_at_half_its_minimum():
    # a 0.8 A minimum plug drawing 0.5 A is running (it would never reach the 1 A
    # threshold): the restart margin must not hold it off while the cap rides down to its minimum
    small = lambda draw: _load(BEHAVIOR_EXCESS, 0.85, draw=draw, min_current=0.8)  # noqa: E731
    assert _limit(small(0.5)) == pytest.approx(0.85)
    assert _limit(small(0.0)) == 0.0          # off: the 20 % start margin applies (0.96 A)


def test_not_paced_is_untouched():
    assert _limit(_load(BEHAVIOR_EXCESS, None)) == pytest.approx(30.0)


def test_other_modes_ignore_the_cap():
    assert _limit(_load(BEHAVIOR_SOLAR_ONLY, 1.0)) == _limit(
        _load(BEHAVIOR_SOLAR_ONLY, None)
    )


def test_binary_excess_load_needs_its_rating_under_the_cap():
    def plug(cap, draw=0.0):
        return LoadContext(
            load_id="p", entity_id="p", min_current=8.7, max_current=8.7,
            phases=1, device_type="plug", mode_behavior=BEHAVIOR_BINARY_EXCESS,
            l1_current=draw, pace_cap_current=cap,
        )

    assert _limit(plug(None)) == pytest.approx(8.7)
    assert _limit(plug(8.0)) == 0.0
    assert _limit(plug(9.0)) == 0.0  # past the rating but not the start margin
    assert _limit(plug(9.0, draw=8.7)) == pytest.approx(8.7)
    assert _limit(plug(11.0)) == pytest.approx(8.7)


# --- the reader --------------------------------------------------------------

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


class _States:
    def __init__(self, **states):
        self.states = states

    def get(self, entity_id):
        return self.states.get(entity_id)


def _hass(energy="4.6", horizon=None, unit="kWh"):
    horizon = horizon or (NOW + timedelta(hours=2)).isoformat()
    return SimpleNamespace(
        states=_States(**{
            "sensor.e": SimpleNamespace(
                entity_id="sensor.e", state=energy,
                attributes={"unit_of_measurement": unit},
            ),
            "sensor.h": SimpleNamespace(
                entity_id="sensor.h", state=horizon, attributes={}
            ),
        })
    )


def _entry(energy="sensor.e", horizon="sensor.h"):
    return SimpleNamespace(
        entry_id="hub",
        data={CONF_PACING_ENERGY_ENTITY_ID: energy, CONF_PACING_HORIZON_ENTITY_ID: horizon},
        options={},
    )


def test_reads_energy_and_hours_left():
    got = readers._read_pacing(_hass(), _entry(), {}, now=NOW)
    assert got == (pytest.approx(4.6), pytest.approx(2.0))


def test_wh_is_converted():
    got = readers._read_pacing(_hass(energy="4600", unit="Wh"), _entry(), {}, now=NOW)
    assert got[0] == pytest.approx(4.6)


def test_passed_horizon_gives_zero_hours():
    past = (NOW - timedelta(hours=1)).isoformat()
    assert readers._read_pacing(_hass(horizon=past), _entry(), {}, now=NOW)[1] == 0.0


@pytest.mark.parametrize("missing", ["energy", "horizon"])
def test_one_entity_unset_means_no_pacing(missing):
    entry = _entry(**{missing: None})
    assert readers._read_pacing(_hass(), entry, {}, now=NOW) is None
    assert readers._read_pacing(_hass(), _entry(None, None), {}, now=NOW) is None


def test_dropout_holds_then_falls_back(monkeypatch):
    runtime = {}
    assert readers._read_pacing(_hass(), _entry(), runtime, now=NOW) is not None
    gone = _hass(energy="unavailable")
    assert readers._read_pacing(gone, _entry(), runtime, now=NOW) == (
        pytest.approx(4.6), pytest.approx(2.0)
    )
    clock = readers.time.monotonic() + INPUT_STALE_TIMEOUT + 1
    monkeypatch.setattr(readers.time, "monotonic", lambda: clock)
    assert readers._read_pacing(gone, _entry(), runtime, now=NOW) is None


def test_unreadable_timestamp_with_nothing_held_means_no_pacing():
    assert readers._read_pacing(_hass(horizon="not a time"), _entry(), {}, now=NOW) is None


# --- the wiring: a load's own option decides --------------------------------

def _plug_entry(pace, phase="A"):
    from custom_components.dynamic_ocpp_evse.const import (
        CONF_CONNECTED_TO_PHASE, CONF_DEVICE_TYPE, CONF_ENTITY_ID,
        CONF_PACE_OVER_HORIZON, CONF_PLUG_POWER_RATING, DEVICE_TYPE_PLUG,
    )

    options = {} if pace is None else {CONF_PACE_OVER_HORIZON: pace}
    return SimpleNamespace(
        entry_id="plug1",
        data={
            CONF_DEVICE_TYPE: DEVICE_TYPE_PLUG,
            CONF_ENTITY_ID: "plug",
            CONF_PLUG_POWER_RATING: 2300,
            CONF_CONNECTED_TO_PHASE: phase,
        },
        options=options,
    )


def _built_cap(pace, pacing, phase="A"):
    from custom_components.dynamic_ocpp_evse.const import DOMAIN
    from custom_components.dynamic_ocpp_evse.engine.load_builders import (
        _add_loads_to_site,
    )

    hass = SimpleNamespace(
        states=_States(),
        data={DOMAIN: {"loads": {"plug1": {"operating_mode": "Excess"}}}},
    )
    site = _site()
    _add_loads_to_site(hass, site, "hub", [_plug_entry(pace, phase)], pacing=pacing)
    assert site.loads[0].mode_behavior == BEHAVIOR_BINARY_EXCESS
    return site.loads[0].pace_cap_current


def test_option_on_sets_the_load_cap():
    # 4.6 kWh over 2 h = 2300 W = 10 A on the plug's one phase
    assert _built_cap(True, (4.6, 2.0)) == pytest.approx(10.0)


def test_cap_is_per_phase_the_load_really_sits_on():
    # a plug wired across A and B on a site that has only A: its mask is clamped to A,
    # so the cap is 2300 W on one phase (10 A), not on the two phases it was wired for (5 A)
    assert _built_cap(True, (4.6, 2.0), phase="AB") == pytest.approx(10.0)


def test_option_off_or_unset_leaves_the_load_unpaced():
    assert _built_cap(False, (4.6, 2.0)) is None
    assert _built_cap(None, (4.6, 2.0)) is None


def test_no_pacing_reading_leaves_every_load_unpaced():
    assert _built_cap(True, None) is None
