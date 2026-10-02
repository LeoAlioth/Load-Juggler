"""An entry's settings follow a renamed entity.

Machine-authored tests - not yet human-reviewed.

Home Assistant moves an entity's history when its id is renamed, but not the
settings that name it: after the renames of 2026-09-28 a smart load's switch
pointed at an id that no longer existed until it was picked again by hand.
"""

from custom_components.dynamic_ocpp_evse.registry import follow_renames

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
