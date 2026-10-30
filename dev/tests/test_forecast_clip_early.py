"""Excess loads start early on the forecast PV clipping (JUG-18).

Machine-authored tests - not yet human-reviewed.

The pure pieces (the clipping span, the unavoidable clip, a load's room), the
engine wiring (``_apply_forecast_excess`` with the hub option on and the clip the
advice published) and the advice's own publication through ``run_hub_calculation``.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.dynamic_ocpp_evse.calculations.forecast import (
    clip_window_span,
    unavoidable_clip_kwh,
)
from custom_components.dynamic_ocpp_evse.calculations.forecast_excess import (
    clip_load_room_kwh,
)
from custom_components.dynamic_ocpp_evse.const import (
    CONF_FORECAST_CLIP_EARLY_ENABLED,
    CONF_FORECAST_EXCESS_ENABLED,
    DOMAIN,
)
from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
    _apply_forecast_excess,
    _publish_early_start,
)

from .test_forecast_excess import _entry, _site, _tank

T0 = datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)


def _series(*watts):
    return {T0 + timedelta(hours=i): w for i, w in enumerate(watts)}


# --- pure ------------------------------------------------------------------------------

def test_the_span_runs_from_the_first_block_over_the_threshold_to_the_end_of_the_last():
    series = _series(2000, 7000, 9000, 3000, 7000, 0)
    begins, ends = clip_window_span(series, 5000, T0, T0 + timedelta(hours=6))
    assert begins == T0 + timedelta(hours=1)
    assert ends == T0 + timedelta(hours=5)  # the 04:00 block ends at 05:00


def test_a_clip_already_under_way_begins_now():
    series = _series(2000, 7000, 9000, 3000)
    now = T0 + timedelta(hours=1, minutes=30)
    assert clip_window_span(series, 5000, now, T0 + timedelta(hours=4))[0] == now


def test_the_span_is_cut_to_the_window_and_none_without_a_clip():
    series = _series(2000, 7000, 9000, 3000, 7000, 0)
    until = T0 + timedelta(hours=3, minutes=30)
    assert clip_window_span(series, 5000, T0, until)[1] == T0 + timedelta(hours=3)
    assert clip_window_span(series, 20000, T0, T0 + timedelta(hours=6)) is None
    assert clip_window_span({}, 5000, T0, T0 + timedelta(hours=6)) is None
    assert clip_window_span(series, 5000, T0 + timedelta(hours=6), T0 + timedelta(hours=7)) is None


def test_what_the_battery_takes_comes_off_the_clip():
    # 8 kWh clip, 5 kWh at the charge rate, a 20 kWh pack with room: 3 kWh is lost
    assert unavoidable_clip_kwh(8.0, 5.0) == 3.0
    # the same, but the pack already holds too much to make room for 2 kWh of it
    assert unavoidable_clip_kwh(8.0, 5.0, 2.0) == 5.0
    # a small pack is the limit, not the rate (room_needed is clamped to capacity)
    assert unavoidable_clip_kwh(18.0, 9.5) == 8.5
    assert unavoidable_clip_kwh(2.0, 5.0) == 0.0


def test_a_load_absorbs_its_room_or_its_power_over_the_span():
    assert clip_load_room_kwh(1.5, 3000, 4) == 1.5
    assert clip_load_room_kwh(None, 3000, 4) == 12.0
    assert clip_load_room_kwh(None, 3000, 0) == 0.0


# --- the engine wiring -------------------------------------------------------------------

CLIP = {"kwh": 3.0, "start_h": 1.2, "span_h": 4.0}


def _run(options=None, clip=CLIP, loads=None, soc=85.0, runtime=None, planned=None, hours=8.0):
    runtime = {} if runtime is None else runtime
    if clip is not None:
        runtime["_clip_early"] = dict(clip)
    site = _site()
    site.battery_soc = soc
    site.loads = loads if loads is not None else [_tank()]
    on = _apply_forecast_excess(
        runtime,
        _entry(**{CONF_FORECAST_CLIP_EARLY_ENABLED: True, **(options or {})}),
        site,
        (planned, hours) if planned is not None else None,
        hours if planned is not None else None,
    )
    return on, site, runtime


def test_a_load_is_due_when_the_clip_fits_the_hours_before_the_window():
    # 3 kWh >= 1.1 x 1.9 kW x 1.2 h = 2.5 kWh: due
    boiler = _tank()
    on, site, _ = _run(loads=[boiler])
    assert boiler.early_start_ok is True and on is True and site.forecast_excess is True
    # 3 kWh < 1.9 kW x 2 h: waiting, and no site verdict, so no pool top-up either
    boiler = _tank()
    on, site, _ = _run(clip={**CLIP, "start_h": 2.0}, loads=[boiler])
    assert boiler.early_start_ok is False and on is False
    assert site.forecast_excess_pace_w == 0.0


def test_the_pool_is_topped_up_at_the_clip_over_the_hours_to_the_windows_end():
    _, site, _ = _run()
    assert site.forecast_excess_pace_w == pytest.approx(3000.0 / 5.2)


def test_it_is_off_without_the_option():
    boiler = _tank()
    site = _site()
    site.battery_soc = 85.0
    site.loads = [boiler]
    runtime = {"_clip_early": dict(CLIP)}
    on = _apply_forecast_excess(runtime, _entry(), site, None, None)
    assert on is False and boiler.early_start_ok is False
    assert "_early_group_clip_on" not in runtime and "_excess_by_clip_on" not in runtime


def test_it_is_off_without_a_clip_to_save():
    boiler = _tank()
    on, site, runtime = _run(clip=None, loads=[boiler])
    assert on is False and boiler.early_start_ok is False
    assert "_excess_by_clip_on" not in runtime  # no latch left behind


def test_the_early_start_soc_gate_applies():
    assert _run(soc=79.0)[0] is False
    assert _run(soc=80.0)[0] is True


def test_a_blocked_load_does_not_join():
    near = _tank(thermal_headroom_k=0.5)
    on, _, _ = _run(loads=[near])
    assert near.early_start_ok is False and on is False


def test_a_room_sensor_limits_the_energy_a_load_can_use():
    # 1 kWh of room: 1 kWh < 1.1 x 1.9 kW x 1.2 h, not due; with the window 0.4 h away it is
    small = _tank(early_room_kwh=1.0)
    assert _run(loads=[small])[0] is False
    small = _tank(early_room_kwh=1.0)
    assert _run(clip={**CLIP, "start_h": 0.4}, loads=[small])[0] is True


def test_without_a_room_sensor_a_load_can_use_its_power_over_the_span():
    # a 1.9 kW tank over a 1 h clip can take 1.9 kWh of the 3 kWh: 1.9 < 1.1 x 1.9 x 1.2
    boiler = _tank()
    assert _run(clip={**CLIP, "span_h": 1.0}, loads=[boiler])[0] is False
    boiler = _tank()
    assert _run(clip={**CLIP, "span_h": 1.0, "start_h": 0.9}, loads=[boiler])[0] is True


def test_the_group_starts_together_and_holds_inside_the_band():
    a, b = _tank(), _tank()
    b.load_id = "b"
    # 1.1 x 2 x 1.9 kW x 0.8 h = 3.4 kWh > 3: not due; at 0.7 h (2.9 kWh) due
    assert _run(clip={**CLIP, "start_h": 0.8}, loads=[a, b])[0] is False
    runtime = {}
    on, _, runtime = _run(clip={**CLIP, "start_h": 0.7}, loads=[a, b], runtime=runtime)
    assert on and (a.early_start_ok, b.early_start_ok) == (True, True)
    # once on it holds down to 0.9 of the group's power: 0.9 x 3.8 kW x 0.8 h = 2.7 kWh <= 3
    on, _, _ = _run(clip={**CLIP, "start_h": 0.8}, loads=[a, b], runtime=runtime)
    assert on and (a.early_start_ok, b.early_start_ok) == (True, True)


def test_it_does_not_disturb_the_planned_energy_source():
    # the planned energy alone (no clip): as before - 3 kWh, 1.3 h to full, 1.9 kW: due
    boiler = _tank()
    on, site, _ = _run(
        options={CONF_FORECAST_EXCESS_ENABLED: True}, clip=None, loads=[boiler], planned=3.0, hours=1.3
    )
    assert on and boiler.early_start_ok
    assert site.forecast_excess_pace_w == pytest.approx(3000.0 / 1.3)
    # both sources: the verdict is either's, the pool the larger pace
    boiler = _tank()
    _, site, _ = _run(
        options={CONF_FORECAST_EXCESS_ENABLED: True}, loads=[boiler], planned=30.0, hours=8.0
    )
    assert site.forecast_excess_pace_w == pytest.approx(30000.0 / 8.0)


def test_a_waiting_group_says_it_is_for_the_clipping_forecast():
    waiting, other = _tank(), _tank()
    other.load_id = "other"
    runtime = {}
    _, site, runtime = _run(clip={**CLIP, "start_h": 3.0, "kwh": 6.0}, loads=[waiting, other], runtime=runtime)
    # 6 kWh is 1.57 h of the pair's 3.8 kW: not due 3 h ahead, due from 1.4 h ahead (1.3 with the 10 % margin)
    assert not waiting.early_start_ok
    runtime["_clip_active"] = (6.0, 3.0, 4.0)  # what the engine keeps while the verdict is on
    hass = SimpleNamespace(data={DOMAIN: {"loads": {"tank": {}, "other": {}}}})
    _publish_early_start(hass, runtime, site, None, None, False)
    note = hass.data[DOMAIN]["loads"]["tank"]["early_start_note"]
    assert note.startswith("Early start ") and note.endswith(" at 3.8 kW (all loads) for forecast clipping")


def test_a_blocked_load_says_why_while_the_clip_verdict_is_on():
    near = _tank(thermal_headroom_k=0.5)
    _, site, runtime = _run(loads=[near])
    runtime["_clip_active"] = (3.0, 1.5, 4.0)
    hass = SimpleNamespace(data={DOMAIN: {"loads": {"tank": {}}}})
    _publish_early_start(hass, runtime, site, None, None, False)
    assert "boost temperature" in hass.data[DOMAIN]["loads"]["tank"]["early_start_note"]


# --- the advice publishes the clip, through the whole hub calculation --------------------

async def test_the_advice_publishes_the_clip_and_the_hub_starts_on_it(hass):
    """Destination rig: 5300 W clip threshold, a 5 kW charge rate, a 20 kWh pack at
    85 % (3 kWh of room). A 13.3 kW hour at 10:00 UTC clips 8 kWh; the battery can
    take 5 of it at its rate but only 3 of those fit, so 5 kWh is lost. The option
    on, the hub's one Excess verdict is on from the next cycle; off, it never is."""
    from freezegun import freeze_time

    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    from .test_forecast_excess import _rig

    for option, expected in ((True, True), (False, False)):
        states, run = _rig(hass, **{CONF_FORECAST_CLIP_EARLY_ENABLED: option})
        states()
        hass.states.async_set(
            "sensor.dst_forecast", "1.0",
            {"watts": {"2026-08-14T10:00:00+00:00": 13300, "2026-08-14T11:00:00+00:00": 0}},
        )
        first = run()
        assert first["forecast_clipped_kwh"] == 8.0
        runtime = next(iter(hass.data[DOMAIN]["hubs"].values()))
        if option:
            assert runtime["_clip_early"]["kwh"] == pytest.approx(5.0)
            assert runtime["_clip_early"]["span_h"] == pytest.approx(1.0)
            assert runtime["_clip_early"]["start_h"] > 0
        else:
            assert "_clip_early" in runtime  # published whenever there is a clip
        second = run()
        assert second["excess_available"] is expected
        assert second["forecast_excess"] is expected


async def test_no_clip_the_battery_cannot_take_publishes_nothing(hass):
    """A clip the battery's rate and room can take whole (1 kWh over the threshold,
    SOC 85 % of a 20 kWh pack, 3 kWh of room) leaves nothing unavoidable: the hub starts nothing."""
    from .test_forecast_excess import _rig

    states, run = _rig(hass, **{CONF_FORECAST_CLIP_EARLY_ENABLED: True})
    states(soc=85)
    hass.states.async_set(
        "sensor.dst_forecast", "1.0",
        {"watts": {"2026-08-14T10:00:00+00:00": 6300, "2026-08-14T11:00:00+00:00": 0}},
    )
    run()
    runtime = next(iter(hass.data[DOMAIN]["hubs"].values()))
    assert "_clip_early" not in runtime
    assert run()["forecast_excess"] is False
