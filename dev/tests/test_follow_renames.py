"""An entry's settings follow a renamed entity.

Machine-authored tests - not yet human-reviewed.

Home Assistant moves an entity's history when its id is renamed, but not the
settings that name it: after the renames of 2026-09-28 a smart load's switch
pointed at an id that no longer existed until it was picked again by hand.

Runnable two ways:
  python3 dev/tests/test_follow_renames.py   (standalone, no pytest needed)
  pytest dev/tests/test_follow_renames.py    (Docker / CI tier)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules  # noqa: E402

load_pure_modules(root_modules=("registry",))

from custom_components.dynamic_ocpp_evse.registry import follow_renames  # noqa: E402

RENAMES = {"switch.kotlovnica_well_pump": "switch.well_pump", "sensor.old_power": "sensor.pump_power"}


def test_every_setting_naming_a_renamed_entity_follows_it():
    data = {"plug_switch_entity_id": "switch.kotlovnica_well_pump", "name": "Well Pump",
            "phases": ["sensor.old_power", "sensor.other"], "map": {"sensor.old_power": "A"}}
    out = follow_renames(data, RENAMES)
    assert out == {"plug_switch_entity_id": "switch.well_pump", "name": "Well Pump",
                   "phases": ["sensor.pump_power", "sensor.other"], "map": {"sensor.pump_power": "A"}}


def test_only_a_whole_id_is_a_reference():
    data = {"a": "switch.kotlovnica_well_pump_2", "b": 16, "c": None, "d": True}
    assert follow_renames(data, RENAMES) == data


if __name__ == "__main__":
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
    print(f"\n{'FAILED' if failed else 'OK'} - {len(failed)} failure(s)")
    sys.exit(1 if failed else 0)
