"""Per-load catch-up (owner, 2026-10-17).

Machine-authored tests - not yet human-reviewed.

The pure can't-keep-up test and latch (calculations/catch_up.py), the SOC floor,
where the calculator reads the verdict (Solar Only, Solar Priority, the plug's
binary Solar Only; tanks left alone), the engine wiring (``_apply_catch_up``,
the builder's option) and the three settings pages.
"""

from types import SimpleNamespace

import pytest

from custom_components.dynamic_ocpp_evse.calculations.catch_up import (
    CATCH_UP_RELEASE_RATIO,
    cant_keep_up,
    catch_up_verdict,
    max_power_w,
)
from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext,
    PhaseConstraints,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    _source_limit,
)
from custom_components.dynamic_ocpp_evse.config_flow.schemas import (
    _charger_timing_schema,
    _plug_schema,
    _power_station_schema,
)
from custom_components.dynamic_ocpp_evse.const import (
    BEHAVIOR_BINARY_ABOVE_MIN,
    BEHAVIOR_BINARY_ABOVE_TARGET,
    BEHAVIOR_SOLAR_ONLY,
    BEHAVIOR_SOLAR_PRIORITY,
    CONF_CATCH_UP_ENABLED,
    CONF_FORECAST_EXCESS_ENABLED,
    CONF_FORECAST_EXCESS_SOC,
    CONF_FORECAST_EXCESS_SOC_SOURCE,
    DEFAULT_CATCH_UP_ENABLED,
    DEVICE_TYPE_HOT_WATER_TANK,
    FORECAST_EXCESS_SOURCE_FIXED,
    FORECAST_EXCESS_SOURCE_MIN,
    FORECAST_EXCESS_SOURCE_TARGET,
    FORECAST_SOC_HYSTERESIS,
)
from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
    _apply_catch_up,
)

MIN, TARGET = 20.0, 80.0
MAX_W = 3680.0  # 16 A x 230 V x 1 phase


# --- the can't-keep-up test --------------------------------------------------

def test_max_power_is_current_voltage_phases():
    assert max_power_w(16, 230, 1) == 3680
    assert max_power_w(16, 230, 3) == 11040
    assert max_power_w(16, 230, None) == 3680  # phases unknown: one


def test_rate_at_the_maximum_cant_keep_up():
    # 3.68 kWh in 1 h = exactly 3680 W
    assert cant_keep_up(3.68, 1.0, MAX_W) is True
    assert cant_keep_up(3.67, 1.0, MAX_W) is False


def test_hours_left_is_floored_at_one():
    # 3.68 kWh with 0.2 h left is still spent over one hour, not 18 h-equivalents
    assert cant_keep_up(3.0, 0.2, MAX_W) is False
    assert cant_keep_up(3.68, 0.0, MAX_W) is True


def test_more_hours_make_it_keep_up():
    assert cant_keep_up(7.36, 2.0, MAX_W) is True
    assert cant_keep_up(7.36, 4.0, MAX_W) is False


@pytest.mark.parametrize(
    "planned, hours, power",
    [(None, 1.0, MAX_W), (3.68, None, MAX_W), (3.68, 1.0, 0.0), (0.0, 1.0, MAX_W)],
)
def test_unreadable_or_empty_is_false(planned, hours, power):
    assert cant_keep_up(planned, hours, power) is False


def test_a_tiny_load_needs_more_than_the_minimum_energy():
    # 100 W plug: 0.15 kWh/h would be over its maximum but under the 0.2 kWh start
    assert cant_keep_up(0.15, 1.0, 100.0) is False
    assert cant_keep_up(0.25, 1.0, 100.0) is True


# --- the latch -----------------------------------------------------------------

def test_latch_holds_inside_the_band_and_releases_below():
    held = CATCH_UP_RELEASE_RATIO * MAX_W / 1000  # 2.944 kWh in 1 h
    assert cant_keep_up(held + 0.01, 1.0, MAX_W, was_on=True) is True
    assert cant_keep_up(held - 0.01, 1.0, MAX_W, was_on=True) is False
    # off again until the start ratio itself, not the release one
    assert cant_keep_up(held + 0.01, 1.0, MAX_W, was_on=False) is False


def test_planned_energy_wobbling_at_the_boundary_does_not_chatter():
    state, seen = False, []
    for planned in (3.70, 3.60, 3.68, 3.50, 3.70, 3.40, 3.70):
        state = cant_keep_up(planned, 1.0, MAX_W, state)
        seen.append(state)
    assert seen == [True] * 7


def test_planned_energy_going_to_zero_releases():
    assert cant_keep_up(0.1, 1.0, 50.0, was_on=True) is False
    assert cant_keep_up(0.0, 1.0, MAX_W, was_on=True) is False
    assert cant_keep_up(-2.0, 1.0, MAX_W, was_on=True) is False


# --- the verdict: option, behaviour, SOC floor -------------------------------------

def _v(soc=60.0, floor=MIN, band=0.0, planned=3.68, hours=1.0, was_on=False,
       enabled=True, behavior=BEHAVIOR_SOLAR_ONLY):
    return catch_up_verdict(
        enabled, behavior, soc, floor, band, planned, hours, MAX_W, was_on
    )


def test_verdict_on_when_everything_holds():
    assert _v() is True


@pytest.mark.parametrize(
    "kw",
    [
        {"enabled": False},
        {"soc": None},  # no battery: inert
        {"floor": None},
        {"soc": 19.9},  # under the minimum floor
        {"planned": None},
        {"planned": 3.0},  # can keep up
        {"behavior": "full_power"},
        {"behavior": "excess"},
        {"behavior": BEHAVIOR_BINARY_ABOVE_MIN},  # runs above the minimum already
    ],
)
def test_each_condition_is_needed(kw):
    assert _v(**kw) is False


@pytest.mark.parametrize(
    "behavior",
    [BEHAVIOR_SOLAR_ONLY, BEHAVIOR_SOLAR_PRIORITY, BEHAVIOR_BINARY_ABOVE_TARGET],
)
def test_the_target_gated_behaviours_are_eligible(behavior):
    assert _v(behavior=behavior) is True


def test_floor_holds_inside_its_band_and_releases_below():
    assert _v(soc=50, floor=50, band=3.0) is True
    assert _v(soc=47, floor=50, band=3.0, was_on=True) is True
    assert _v(soc=46.9, floor=50, band=3.0, was_on=True) is False
    assert _v(soc=49, floor=50, band=3.0, was_on=False) is False


# --- the calculator ----------------------------------------------------------------

def _site(soc=60.0, charge_w=3450.0, export=18.0):
    return SiteContext(
        voltage=230,
        main_breaker_rating=35,
        consumption=PhaseValues(2.0, None, None),
        export_current=PhaseValues(export, None, None),
        battery_soc=soc,
        battery_soc_min=MIN,
        battery_soc_target=TARGET,
        battery_soc_full=97.0,
        battery_power=-charge_w,
        battery_max_charge_power=3450,
        battery_max_discharge_power=5000,
    )


def _load(behavior, active, *, device_type="evse", min_current=6, max_current=16):
    return LoadContext(
        load_id="l", entity_id="l", min_current=min_current, max_current=max_current,
        phases=1, device_type=device_type, mode_behavior=behavior,
        active_phases_mask="A", catch_up_enabled=True, catch_up_active=active,
    )


POOL = PhaseConstraints.from_pool(33, 33, 33, 33)  # 33 A of sun incl. the charge
NONE = PhaseConstraints.from_pool(0, 0, 0, 0)


def test_solar_only_below_target_waits_without_catch_up():
    assert _source_limit(_load(BEHAVIOR_SOLAR_ONLY, False), _site(), POOL, NONE) == 0


def test_solar_only_below_target_takes_the_solar_pool_with_catch_up():
    # the pool is 33 A, the load's maximum 16 A
    assert _source_limit(_load(BEHAVIOR_SOLAR_ONLY, True), _site(), POOL, NONE) == 16


def test_solar_only_catch_up_is_the_pool_not_more():
    small = PhaseConstraints.from_pool(9.5, 9.5, 9.5, 9.5)
    assert _source_limit(_load(BEHAVIOR_SOLAR_ONLY, True), _site(), small, NONE) == 9.5
    assert _source_limit(
        _load(BEHAVIOR_SOLAR_ONLY, True), _site(), small, NONE, base=2.0
    ) == 11.5  # what it already holds counts, as above target


def test_solar_priority_below_target_minimum_vs_pool():
    assert _source_limit(_load(BEHAVIOR_SOLAR_PRIORITY, False), _site(), POOL, NONE) == 6
    assert _source_limit(_load(BEHAVIOR_SOLAR_PRIORITY, True), _site(), POOL, NONE) == 16


def test_solar_priority_catch_up_keeps_the_grid_backed_minimum():
    nothing = PhaseConstraints.from_pool(0, 0, 0, 0)
    assert _source_limit(_load(BEHAVIOR_SOLAR_PRIORITY, True), _site(), nothing, NONE) == 6


def test_above_target_is_unchanged_by_catch_up():
    site = _site(soc=85)
    for on in (False, True):
        assert _source_limit(_load(BEHAVIOR_SOLAR_ONLY, on), site, POOL, NONE) == 33


def test_binary_solar_only_plug_runs_below_target_only_with_catch_up():
    plug = lambda on: _load(  # noqa: E731
        BEHAVIOR_BINARY_ABOVE_TARGET, on, device_type="plug", min_current=8.7, max_current=8.7
    )
    assert _source_limit(plug(False), _site(), POOL, NONE) == 0
    assert _source_limit(plug(True), _site(), POOL, NONE) == 8.7


def test_a_tank_never_catches_up():
    # a tank in Solar Priority: even a stray flag changes nothing it reads from
    # the option; the builder never sets catch_up_enabled for a tank (below)
    tank = _load(BEHAVIOR_SOLAR_PRIORITY, False, device_type=DEVICE_TYPE_HOT_WATER_TANK)
    assert _source_limit(tank, _site(), POOL, NONE) == 6


# --- the engine --------------------------------------------------------------------

def _entry(**options):
    return SimpleNamespace(entry_id="hub", data={}, options=options)


def _engine(entry, runtime, *, soc=60.0, pacing=(3.68, 1.0), loads=None):
    site = _site(soc=soc)
    site.loads = loads or [_load(BEHAVIOR_SOLAR_ONLY, False)]
    _apply_catch_up(runtime, entry, site, pacing)
    return site


def test_engine_latches_a_load_over_the_minimum_floor_by_default():
    runtime = {}
    site = _engine(_entry(), runtime)
    assert site.loads[0].catch_up_active is True
    assert runtime["_catch_up_on"] == {"l": True}


def test_engine_option_off_on_the_load_is_inert():
    load = _load(BEHAVIOR_SOLAR_ONLY, False)
    load.catch_up_enabled = False
    site = _engine(_entry(), {}, loads=[load])
    assert site.loads[0].catch_up_active is False


def test_engine_without_a_battery_or_pacing_is_inert():
    assert _engine(_entry(), {}, soc=None).loads[0].catch_up_active is False
    assert _engine(_entry(), {}, pacing=None).loads[0].catch_up_active is False


def test_engine_floor_is_the_minimum_while_the_hub_option_is_off():
    assert _engine(_entry(), {}, soc=19).loads[0].catch_up_active is False
    assert _engine(_entry(), {}, soc=21).loads[0].catch_up_active is True


def test_engine_floor_follows_the_early_excess_gate_when_that_is_on():
    on = {CONF_FORECAST_EXCESS_ENABLED: True}
    # default source is the target: a battery at 60 % is under the floor
    assert _engine(_entry(**on), {}).loads[0].catch_up_active is False
    assert _engine(_entry(**on), {}, soc=80).loads[0].catch_up_active is True
    minimum = _entry(**on, **{CONF_FORECAST_EXCESS_SOC_SOURCE: FORECAST_EXCESS_SOURCE_MIN})
    assert _engine(minimum, {}).loads[0].catch_up_active is True
    assert _engine(
        _entry(**on, **{CONF_FORECAST_EXCESS_SOC_SOURCE: FORECAST_EXCESS_SOURCE_TARGET}), {}
    ).loads[0].catch_up_active is False
    fixed = _entry(**on, **{
        CONF_FORECAST_EXCESS_SOC_SOURCE: FORECAST_EXCESS_SOURCE_FIXED,
        CONF_FORECAST_EXCESS_SOC: 50,
    })
    runtime = {}
    assert _engine(fixed, runtime, soc=50).loads[0].catch_up_active is True
    assert _engine(fixed, runtime, soc=50 - FORECAST_SOC_HYSTERESIS).loads[0].catch_up_active is True  # band
    assert _engine(fixed, {}, soc=50 - FORECAST_SOC_HYSTERESIS).loads[0].catch_up_active is False


def test_engine_latch_is_per_load_and_pruned():
    a, b = _load(BEHAVIOR_SOLAR_ONLY, False), _load(BEHAVIOR_SOLAR_ONLY, False)
    a.load_id, b.load_id = "a", "b"
    b.max_current = 32  # twice the power: a planned rate of 3.68 kW is under it
    runtime = {"_catch_up_on": {"gone": True}}
    _engine(_entry(), runtime, loads=[a, b])
    assert runtime["_catch_up_on"] == {"a": True, "b": False}


def test_engine_holds_through_a_wobble():
    runtime = {}
    assert _engine(_entry(), runtime, pacing=(3.68, 1.0)).loads[0].catch_up_active
    assert _engine(_entry(), runtime, pacing=(3.2, 1.0)).loads[0].catch_up_active
    assert not _engine(_entry(), runtime, pacing=(2.9, 1.0)).loads[0].catch_up_active


# --- the builder: a load's own option decides -----------------------------------------

def _built(device_type_tank=False, option=True):
    from custom_components.dynamic_ocpp_evse.const import (
        CONF_CONNECTED_TO_PHASE, CONF_DEVICE_TYPE, CONF_ENTITY_ID,
        CONF_PLUG_POWER_RATING, DEVICE_TYPE_PLUG, DOMAIN,
    )
    from custom_components.dynamic_ocpp_evse.engine.load_builders import _add_loads_to_site

    entry = SimpleNamespace(
        entry_id="p1",
        data={
            CONF_DEVICE_TYPE: DEVICE_TYPE_PLUG,
            CONF_ENTITY_ID: "plug",
            CONF_PLUG_POWER_RATING: 2300,
            CONF_CONNECTED_TO_PHASE: "A",
        },
        options={CONF_CATCH_UP_ENABLED: option} if option is not None else {},
    )

    class _States:
        def get(self, _):
            return None

    hass = SimpleNamespace(states=_States(), data={DOMAIN: {"loads": {"p1": {}}}})
    site = _site()
    _add_loads_to_site(hass, site, "hub", [entry])
    return site.loads[0]


def test_builder_reads_the_loads_own_option():
    assert _built(option=True).catch_up_enabled is True
    assert _built(option=False).catch_up_enabled is False
    assert _built(option=None).catch_up_enabled is False  # default off


# --- the settings pages --------------------------------------------------------------

@pytest.mark.parametrize(
    "schema",
    [
        lambda: _charger_timing_schema({}),
        lambda: _plug_schema({}),
        lambda: _power_station_schema({}),
    ],
    ids=["evse-charger_timing", "smart-load", "power-station"],
)
def test_each_page_offers_the_option_off_by_default(schema):
    keys = {str(k): k for k in schema().schema}
    assert CONF_CATCH_UP_ENABLED in keys
    assert keys[CONF_CATCH_UP_ENABLED].default() is DEFAULT_CATCH_UP_ENABLED is False


def test_the_page_remembers_a_ticked_option():
    keys = {str(k): k for k in _plug_schema({CONF_CATCH_UP_ENABLED: True}).schema}
    assert keys[CONF_CATCH_UP_ENABLED].default() is True


def test_every_page_has_en_and_sl_text():
    import json
    from pathlib import Path

    root = Path(__file__).parents[2] / "custom_components/dynamic_ocpp_evse/translations"
    pages = [
        ("config", "charger_timing"), ("config", "plug_config"),
        ("config", "power_station_config"), ("options", "charger_timing"),
        ("options", "plug"), ("options", "power_station"),
    ]
    for lang in ("en", "sl"):
        tr = json.loads((root / f"{lang}.json").read_text())
        for kind, step in pages:
            page = tr[kind]["step"][step]
            assert page["data"][CONF_CATCH_UP_ENABLED]
            assert page["data_description"][CONF_CATCH_UP_ENABLED]
