"""Full's hysteresis band sits above its setting: a battery counts as full from
full + hysteresis until it falls below full, so the target (band below) can
reach full without the bands overlapping (Anze, 2026-10-10)."""

from types import SimpleNamespace

from custom_components.dynamic_ocpp_evse.engine.hub_calculation import _latch_full


def _full_seen(rt, soc, full=90.0, hyst=2.0):
    m = SimpleNamespace(entry_id="inv", battery_soc=soc, soc_full=full)
    _latch_full(rt, [m], hyst)
    return soc >= m.soc_full          # what every consumer asks


def test_full_starts_a_hysteresis_above_and_holds_down_to_the_setting():
    rt = {}
    assert not _full_seen(rt, 90.0)   # at the setting: not yet
    assert not _full_seen(rt, 91.0)
    assert _full_seen(rt, 92.0)       # full + hysteresis: full
    assert _full_seen(rt, 90.0)       # held down to the setting
    assert not _full_seen(rt, 89.0)   # below it: not full
    assert not _full_seen(rt, 91.0)   # and not again until 92


def test_the_band_never_goes_past_100_and_no_hysteresis_is_a_plain_threshold():
    assert _full_seen({}, 100.0, full=99.0)
    assert _full_seen({}, 90.0, hyst=0)
