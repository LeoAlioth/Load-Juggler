"""The gate that lets a running charger's cut out before its command slot
(control/smoothing.cut_now, plan lj-1): straight to the permit, floored at the
minimum, smoothing re-seeded there; raises, a dip inside the dead band and a
charger that has never been commanded wait for the slot as usual."""

from types import SimpleNamespace

from custom_components.dynamic_ocpp_evse.control.smoothing import cut_now

DEAD_BAND = 0.3


def _charger(last):
    return SimpleNamespace(
        _last_commanded_limit=last, _ema_current=16.0, _schmitt_current=16.0,
        _rate_limited_current=16.0, _schmitt_state="rising",
    )


def test_a_cut_goes_straight_to_the_permit_and_reseeds_the_smoothing():
    c = _charger(16.0)
    assert cut_now(c, 12.3, 6.0, DEAD_BAND) == 12.3
    assert (c._ema_current, c._schmitt_current, c._rate_limited_current) == (12.3, 12.3, 12.3)
    assert c._schmitt_state == "falling"


def test_a_cut_never_goes_below_the_minimum():
    c = _charger(16.0)
    assert cut_now(c, 2.0, 6.0, DEAD_BAND) == 6.0
    assert c._ema_current == 6.0


def test_everything_else_waits_for_the_slot():
    for last, permit in [(16.0, 15.8), (12.0, 16.0), (None, 6.0), (6.2, 0.0)]:
        c = _charger(last)
        assert cut_now(c, permit, 6.0, DEAD_BAND) is None, (last, permit)
        assert (c._ema_current, c._schmitt_current, c._rate_limited_current) == (16.0, 16.0, 16.0)
        assert c._schmitt_state == "rising"
