"""The Filters page's dials reach the control pipeline, and an entry without
them IS the pre-page pipeline - not approximately, byte for byte.

Pure tier: real apply_smoothing and real readers, a hub entry faked as the
(options, data) pair get_entry_value reads, no Home Assistant.
"""

import math
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules

load_pure_modules(
    engine_modules=("hub_calculation",), control_modules=("smoothing",)
)

from custom_components.dynamic_ocpp_evse.const import (  # noqa: E402
    CONF_FILTER_CTRL_FAST_TAU_S,
    CONF_FILTER_DEAD_BAND,
    CONF_FILTER_INPUT_TAU_S,
    CONF_FILTER_PERMIT_TAU_S,
    CONF_FILTER_RAMP_DOWN_RATE,
    CONF_FILTER_RAMP_TAU_S,
    CONF_FILTER_RAMP_UP_RATE,
    CONF_FILTER_SETTLE_SECONDS,
    CONF_SITE_UPDATE_FREQUENCY,
    CTRL_FAST_TAU_S,
    DEAD_BAND,
    EMA_TAU_S,
    PERMIT_TAU_S,
    RAMP_DOWN_RATE,
    RAMP_TAU_S,
    RAMP_UP_RATE,
    SETTLE_DRAW_SECONDS,
    ema_alpha_for,
)
from custom_components.dynamic_ocpp_evse.control.smoothing import (  # noqa: E402
    apply_smoothing,
)
from custom_components.dynamic_ocpp_evse.engine.readers import (  # noqa: E402
    _ALPHA_KEY,
    _FAST_TAU_KEY,
    _smooth,
    _smooth_directional,
    set_ema_interval,
)

DT = 2.0
EVERY_DIAL_AT_ITS_CONSTANT = {
    CONF_FILTER_INPUT_TAU_S: EMA_TAU_S,
    CONF_FILTER_PERMIT_TAU_S: PERMIT_TAU_S,
    CONF_FILTER_RAMP_TAU_S: RAMP_TAU_S,
    CONF_FILTER_CTRL_FAST_TAU_S: CTRL_FAST_TAU_S,
    CONF_FILTER_SETTLE_SECONDS: SETTLE_DRAW_SECONDS,
    CONF_FILTER_DEAD_BAND: DEAD_BAND,
    CONF_FILTER_RAMP_UP_RATE: RAMP_UP_RATE,
    CONF_FILTER_RAMP_DOWN_RATE: RAMP_DOWN_RATE,
}


def _hub(**dials):
    return SimpleNamespace(options={CONF_SITE_UPDATE_FREQUENCY: DT, **dials}, data={})


def _sensor(at=6.0):
    """apply_smoothing's whole contract with its sensor: these five fields,
    seeded mid-run so neither the cold-start nor the resume-from-zero branch
    is taken and the three stages actually run."""
    return SimpleNamespace(
        _attr_name="load", _ema_current=at, _schmitt_current=at,
        _schmitt_state="rising", _rate_limited_current=at,
    )


def _drive(hub, raws, at=6.0):
    s = _sensor(at)
    return [apply_smoothing(s, r, False, hub) for r in raws]


def test_an_entry_without_the_dials_is_the_constants_exactly():
    """The page's whole safety argument. Not isclose: identical."""
    raws = [16.0] * 12 + [8.0] * 12 + [12.5] * 6
    assert _drive(_hub(), raws) == _drive(_hub(**EVERY_DIAL_AT_ITS_CONSTANT), raws)


def test_the_permit_filter_dial_sets_the_permit_ema():
    """apply_smoothing rounds its EMA to 2 dp, so compare against the rounded
    formula exactly rather than the raw one approximately."""
    for tau in (PERMIT_TAU_S, 3.0, 20.0):
        s = _sensor(6.0)
        apply_smoothing(s, 16.0, False, _hub(**{CONF_FILTER_PERMIT_TAU_S: tau}))
        a = ema_alpha_for(DT, tau)
        assert s._ema_current == round(a * 16.0 + (1 - a) * 6.0, 2), (tau, s._ema_current)


def test_the_dead_band_dial_gates_re_commands():
    """The dead band is a Schmitt trigger on REVERSALS: a rising permit follows
    its EMA upward freely, and it is a move back the other way that must clear
    the band before the load is re-commanded. So the case is a 0.5 A drop from
    a rising state - held for good under a 2 A band (the EMA settles at 5.5
    and 6 - 5.5 never reaches 2), followed on the first cycle under 0."""
    raws = [5.5] * 15
    held = _drive(_hub(**{CONF_FILTER_DEAD_BAND: 2.0}), raws)
    free = _drive(_hub(**{CONF_FILTER_DEAD_BAND: 0.0}), raws)
    assert all(v == 6.0 for v in held), held
    assert free[0] < 6.0 and free[-1] < 6.0, free


def test_the_slew_dials_set_the_floor_the_permit_always_moves_at():
    """With the ramp response made very slow the proportional term is tiny,
    so the per-cycle step is the slew floor - and the dial moves it."""
    slow_ramp = {CONF_FILTER_RAMP_TAU_S: 60.0, CONF_FILTER_DEAD_BAND: 0.0}
    up_const = _drive(_hub(**slow_ramp), [16.0])[0]
    up_dial = _drive(_hub(**slow_ramp, **{CONF_FILTER_RAMP_UP_RATE: 1.0}), [16.0])[0]
    assert up_dial > up_const > 6.0, (up_const, up_dial)
    down_const = _drive(_hub(**slow_ramp), [6.0], at=16.0)[0]
    down_dial = _drive(_hub(**slow_ramp, **{CONF_FILTER_RAMP_DOWN_RATE: 1.0}), [6.0], at=16.0)[0]
    assert down_dial < down_const < 16.0, (down_const, down_dial)


def test_the_reading_filter_dials_reach_the_readers():
    ema = {}
    set_ema_interval(ema, DT, tau=10.0, fast_tau=0.7)
    _smooth(ema, "k", 0.0)
    assert math.isclose(_smooth(ema, "k", 100.0), 100.0 * ema_alpha_for(DT, 10.0), abs_tol=0.01)
    _smooth_directional(ema, "g", -10.0, fast_away=True)
    fast = ema_alpha_for(DT, 0.7)
    assert math.isclose(
        _smooth_directional(ema, "g", -20.0, fast_away=True),
        fast * -20.0 + (1 - fast) * -10.0, abs_tol=0.01,
    )


def test_a_hub_without_dials_gives_the_readers_the_constants():
    ema = {}
    set_ema_interval(ema, DT)
    assert ema[_ALPHA_KEY] == ema_alpha_for(DT, EMA_TAU_S)
    assert ema[_FAST_TAU_KEY] == CTRL_FAST_TAU_S


if __name__ == "__main__":
    failed = []
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except Exception as exc:  # noqa: BLE001
                failed.append(_name)
                print(f"FAIL {_name}: {type(exc).__name__}: {exc}")
    print()
    print(f"FAILED - {len(failed)} failure(s)" if failed else "OK - 0 failure(s)")
    sys.exit(1 if failed else 0)
