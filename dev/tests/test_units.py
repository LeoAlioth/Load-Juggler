"""Exhaustive unit-conversion matrix — units.py.

Machine-authored tests — not yet human-reviewed.

Every unit the config flow accepts for a field has to survive the trip to the
engine's canonical domain. The bug that motivated this: a grid CT configured
as a 1300 W power sensor was read as 1300 A and multiplied by voltage again,
publishing ~300 kW of grid power. The matrix below states one physical
quantity in every accepted unit and insists all spellings agree.

Runnable two ways:
  python3 dev/tests/test_units.py   (standalone, no pytest needed)
  pytest dev/tests/test_units.py    (Docker / CI tier)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules  # noqa: E402

# units.py imports nothing but const/, so it loads without Home Assistant.
load_pure_modules(calc_modules=(), root_modules=("units",))

from custom_components.dynamic_ocpp_evse import units  # noqa: E402

V = 230.0

# 6 A on a 230 V phase = 1380 W, expressed every way a sensor might publish it.
SIX_AMPS = [
    (6.0, "A"),
    (6000.0, "mA"),
    (1380.0, "W"),
    (1.38, "kW"),
]


def test_every_accepted_unit_reads_as_the_same_current():
    for value, unit in SIX_AMPS:
        assert abs(units.to_amps(value, unit, V) - 6.0) < 1e-9, unit


def test_every_accepted_unit_reads_as_the_same_power():
    for value, unit in SIX_AMPS:
        assert abs(units.to_watts(value, unit, V) - 1380.0) < 1e-6, unit


def test_case_and_whitespace_in_the_unit_are_tolerated():
    # HA units come from other integrations; don't trust their spelling.
    assert units.to_amps(6000.0, "ma", V) == 6.0
    assert units.to_amps(1.38, " kW ", V) == 6.0
    assert units.to_watts(1.38, "KW", V) == 1380.0


def test_unknown_unit_is_passed_through_as_already_canonical():
    """A sensor with no unit is nearly always already in the wanted unit —
    converting would be a guess, and this matches the historical behaviour."""
    assert units.to_amps(6.0, None, V) == 6.0
    assert units.to_amps(6.0, "", V) == 6.0
    assert units.to_watts(1380.0, "widgets", V) == 1380.0


def test_sign_survives_every_conversion():
    """Export is a negative grid reading — losing the sign loses the meaning."""
    for value, unit in SIX_AMPS:
        assert units.to_amps(-value, unit, V) < 0, unit
        assert units.to_watts(-value, unit, V) < 0, unit


def test_missing_voltage_leaves_power_unconverted_rather_than_dividing_by_zero():
    assert units.to_amps(1380.0, "W", 0) == 1380.0
    assert units.to_amps(1.38, "kW", 0) == 1.38
    # Pure-scale conversions need no voltage at all.
    assert units.to_amps(6000.0, "mA", 0) == 6.0
    assert units.to_watts(1.38, "kW", 0) == 1380.0


def test_volts_from_millivolts():
    assert units.to_volts(51.2, "V") == 51.2
    assert units.to_volts(51200.0, "mV") == 51.2
    assert units.to_volts(51.2, None) == 51.2


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
