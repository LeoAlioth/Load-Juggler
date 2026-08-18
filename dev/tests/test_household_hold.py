"""Unit tests for the asymmetric household floor hold — calculations/utils.py.

Machine-authored tests — not yet human-reviewed.

The bug behind these (ISSUES.md #12): per-phase household is derived as
inverter_output − managed draws. The draw side (OCPP, sub-second) rises the
moment a car ramps while the inverter output side lags 10-30 s (Modbus polling
plus input EMA), so the subtraction transiently clamps household to 0 and the
engine hands the real household's power out as phantom headroom.
``hold_per_phase_floor`` bridges that window: instant rise, wall-clock decay.

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_household_hold.py     (standalone, no pytest needed)
  pytest dev/tests/test_household_hold.py      (Docker / CI tier)
"""

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Module loading — shared stub loader (avoids the HA-importing package root)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules

load_pure_modules(calc_modules=("models", "utils"))

from custom_components.dynamic_ocpp_evse.calculations.models import PhaseValues
from custom_components.dynamic_ocpp_evse.calculations.utils import (
    hold_per_phase_floor,
)
from custom_components.dynamic_ocpp_evse.const.common import (
    HOUSEHOLD_HOLD_BRIDGE_SECONDS,
    HOUSEHOLD_HOLD_RESIDUAL,
)

# The engine's decay derivation (engine/hub_calculation.py:_household_hold_decay)
# restated here so the tests pin the wall-clock arithmetic, not a magic number.
def _decay(cycle_seconds):
    return HOUSEHOLD_HOLD_RESIDUAL ** (
        cycle_seconds / HOUSEHOLD_HOLD_BRIDGE_SECONDS
    )


def _close(a, b, tol=1e-9):
    return abs(a - b) < tol


# ---------------------------------------------------------------------------
# Rise / fall asymmetry
# ---------------------------------------------------------------------------

def test_rise_is_instant_on_every_phase():
    """A genuine household increase must not be smoothed at all."""
    held = PhaseValues(4.0, 4.0, 4.0)
    out = hold_per_phase_floor(PhaseValues(9.0, 4.5, 100.0), held, _decay(2.0))
    assert (out.a, out.b, out.c) == (9.0, 4.5, 100.0)


def test_equal_reading_passes_through_unchanged():
    out = hold_per_phase_floor(
        PhaseValues(4.0, 4.0, 4.0), PhaseValues(4.0, 4.0, 4.0), _decay(2.0)
    )
    assert (out.a, out.b, out.c) == (4.0, 4.0, 4.0)


def test_fall_decays_at_the_two_second_cycle_rate():
    """2 s cycle: one cycle keeps 0.1 ** (2/15) = 0.735642... of the held value."""
    d = _decay(2.0)
    assert _close(d, 0.7356422544596414)
    out = hold_per_phase_floor(PhaseValues(0.0, 0.0, 0.0), PhaseValues(4.0, 4.0, 4.0), d)
    assert _close(out.a, 4.0 * d) and _close(out.a, 2.9425690178385656)
    assert _close(out.b, 4.0 * d) and _close(out.c, 4.0 * d)


def test_fall_decays_at_the_ten_second_cycle_rate():
    """10 s cycle: one cycle keeps 0.1 ** (10/15) = 0.215443... of the held value."""
    d = _decay(10.0)
    assert _close(d, 0.2154434690031884)
    out = hold_per_phase_floor(PhaseValues(0.0, None, None), PhaseValues(4.0, None, None), d)
    assert _close(out.a, 4.0 * d) and _close(out.a, 0.8617738760127536)


def test_bridge_window_lands_on_the_residual_for_any_cycle_length():
    """The whole point of deriving decay from wall clock: after
    HOUSEHOLD_HOLD_BRIDGE_SECONDS of zero readings the held value is down to
    HOUSEHOLD_HOLD_RESIDUAL, whatever the cycle length."""
    for cycle in (1.0, 3.0, 5.0, 7.5, 15.0):  # cycle lengths dividing the window
        cycles = round(HOUSEHOLD_HOLD_BRIDGE_SECONDS / cycle)
        held = PhaseValues(4.0, None, None)
        for _ in range(cycles):
            held = hold_per_phase_floor(PhaseValues(0.0, None, None), held, _decay(cycle))
        assert _close(held.a, 4.0 * HOUSEHOLD_HOLD_RESIDUAL, 1e-9), cycle


def test_partial_fall_above_the_floor_is_taken_as_measured():
    """The hold is a floor, not an EMA: a fall that stays above the decayed
    value is trusted immediately."""
    d = _decay(2.0)
    out = hold_per_phase_floor(PhaseValues(3.5, None, None), PhaseValues(4.0, None, None), d)
    assert out.a == 3.5  # 3.5 > 4.0 * 0.7356


# ---------------------------------------------------------------------------
# None handling — both directions
# ---------------------------------------------------------------------------

def test_none_phase_in_new_stays_none():
    """None means the phase does not exist on this site — a held value from a
    previous cycle must not invent it."""
    out = hold_per_phase_floor(
        PhaseValues(4.0, None, None), PhaseValues(4.0, 7.0, 7.0), _decay(2.0)
    )
    assert out.a == 4.0
    assert out.b is None and out.c is None


def test_none_phase_in_held_takes_new():
    out = hold_per_phase_floor(
        PhaseValues(4.0, 5.0, 6.0), PhaseValues(None, None, 60.0), _decay(2.0)
    )
    assert (out.a, out.b) == (4.0, 5.0)
    assert _close(out.c, 60.0 * _decay(2.0))


def test_no_held_state_returns_the_new_reading_object():
    new = PhaseValues(4.0, None, None)
    assert hold_per_phase_floor(new, None, _decay(2.0)) is new


def test_new_none_yields_none():
    """No inverter output data — nothing to hold (the engine also clears the
    held state in this case so it cannot resurrect later)."""
    assert hold_per_phase_floor(None, PhaseValues(4.0, 4.0, 4.0), _decay(2.0)) is None


# ---------------------------------------------------------------------------
# Degenerate decay values
# ---------------------------------------------------------------------------

def test_decay_is_clamped_into_the_unit_interval():
    # > 1 would amplify the held value forever; < 0 would push it negative.
    out = hold_per_phase_floor(PhaseValues(1.0, None, None), PhaseValues(4.0, None, None), 5.0)
    assert out.a == 4.0
    out = hold_per_phase_floor(PhaseValues(1.0, None, None), PhaseValues(4.0, None, None), -5.0)
    assert out.a == 1.0


def test_zero_decay_disables_the_hold():
    out = hold_per_phase_floor(PhaseValues(0.0, None, None), PhaseValues(4.0, None, None), 0.0)
    assert out.a == 0.0


# ---------------------------------------------------------------------------
# The lag episode this fix exists for
# ---------------------------------------------------------------------------

def test_inverter_output_lag_episode_keeps_most_of_the_household():
    """Household is a steady 4 A/phase. A car ramps: the OCPP draw is subtracted
    immediately while the polled inverter output still reads the pre-ramp value,
    so the raw formula reports 0 A for 3 cycles (6 s at a 2 s cycle). The held
    value must stay well above 0 through the dip and snap back the instant the
    inverter output catches up."""
    d = _decay(2.0)
    steady = PhaseValues(4.0, 4.0, 4.0)

    held = hold_per_phase_floor(steady, None, d)
    assert (held.a, held.b, held.c) == (4.0, 4.0, 4.0)

    # 3 cycles of phantom-zero household while the inverter reading lags.
    dips = []
    for _ in range(3):
        held = hold_per_phase_floor(PhaseValues(0.0, 0.0, 0.0), held, d)
        dips.append(held.a)

    assert _close(dips[0], 4.0 * d)          # 2.9426 A
    assert _close(dips[1], 4.0 * d ** 2)     # 2.1657 A
    assert _close(dips[2], 4.0 * d ** 3)     # 1.5924 A
    # 6 s into a 15 s bridge: 0.1 ** 0.4 = 39.8 % of the real household is
    # still protected, instead of the engine seeing 0 and giving all 4 A away.
    assert _close(dips[2], 1.5924286822139893)
    assert dips[2] > 4.0 * 0.39
    for phase in (held.b, held.c):
        assert _close(phase, dips[2])

    # Inverter output catches up: full value returns on the very next cycle.
    held = hold_per_phase_floor(steady, held, d)
    assert (held.a, held.b, held.c) == (4.0, 4.0, 4.0)


def test_sustained_real_drop_is_not_held_forever():
    """A genuine household drop (household really went to 0) must converge, so
    the hold cannot pin a stale figure indefinitely."""
    d = _decay(2.0)
    held = PhaseValues(4.0, None, None)
    for _ in range(30):  # 60 s = 4 bridge windows
        held = hold_per_phase_floor(PhaseValues(0.0, None, None), held, d)
    assert held.a < 4.0 * 1e-3


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Deliberately pytest-free: the pure tier has to run on the developer's
    # machine, which has no pytest (dev/tests/conftest.py imports HA anyway).
    failed = []
    for _name, _fn in sorted(list(globals().items())):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed.append((_name, exc))
            print(f"FAIL {_name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {_name}")
    print(f"\n{'FAILED' if failed else 'OK'} — {len(failed)} failure(s)")
    sys.exit(1 if failed else 0)
