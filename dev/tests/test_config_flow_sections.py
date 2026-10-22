"""The setup pages' headings (config_flow/groups.py, config_flow/sections.py).

Machine-authored tests - not yet human-reviewed.

A long page shows its fields under headed sections: the form is built flat and
nested on the way out, and what comes back is flattened, so handlers and stored
options are unchanged. The groups, the translations and the form must agree.
"""
import json
from pathlib import Path

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.config_flow.groups import CONFIG, OPTIONS
from custom_components.dynamic_ocpp_evse.config_flow.sections import (
    SectionedSchema,
    flat_fields,
    flatten,
    sectioned,
)
from custom_components.dynamic_ocpp_evse.const import (
    CONF_FILTER_CTRL_FAST_TAU_S,
    CONF_FILTER_INPUT_TAU_S,
    CONF_FILTER_PERMIT_TAU_S,
)

from .test_config_flow_e2e import _open_options

BASE = Path("custom_components/dynamic_ocpp_evse/translations")
GROUPS = [("a", ("x", "y"), False), ("b", ("z",), True)]


def test_every_section_has_a_heading_and_its_labels_in_both_languages():
    for lang in ("en", "sl"):
        data = json.loads((BASE / f"{lang}.json").read_text(encoding="utf-8"))
        for part, steps in (("config", CONFIG), ("options", OPTIONS)):
            for step, groups in steps.items():
                block = data[part]["step"][step]
                keys = {k for _, ks, _ in groups for k in ks}
                for name, ks, _ in groups:
                    sec = (block.get("sections") or {}).get(name)
                    if sec is None:
                        continue                  # no field of this page is in it
                    assert sec["name"].strip(), (lang, part, step, name)
                    assert set(sec["data"]) <= set(ks), (lang, part, step, name)
                # nothing grouped is left where the form would not show it
                assert not keys & set(block.get("data") or {}), (lang, part, step)
                assert not keys & set(block.get("data_description") or {}), (lang, part, step)
                # and what is left at the top is only the load's own name
                assert set(block.get("data") or {}) <= {"name", "entity_id"}, (lang, part, step)


def test_no_field_is_in_two_sections_and_no_section_is_named_like_a_field():
    for steps in (CONFIG, OPTIONS):
        for step, groups in steps.items():
            keys = [k for _, ks, _ in groups for k in ks]
            names = [n for n, _, _ in groups]
            assert len(keys) == len(set(keys)), step
            assert len(names) == len(set(names)) and not set(names) & set(keys), step


def test_a_form_is_nested_by_its_groups_and_flat_input_still_validates():
    flat = vol.Schema({vol.Optional("x"): int, vol.Optional("y", default=5): int,
                       vol.Optional("z"): int, vol.Optional("name"): str})
    nested = sectioned(flat, GROUPS)
    assert isinstance(nested, SectionedSchema)
    assert [str(k) for k in nested.schema] == ["name", "a", "b"]
    assert isinstance(nested.schema[next(k for k in nested.schema if str(k) == "a")], section)
    assert set(map(str, flat_fields(nested))) == {"x", "y", "z", "name"}
    # the frontend sends it nested; a caller that never saw the sections, flat
    assert flatten(nested({"name": "n", "a": {"x": 1}, "b": {}}), GROUPS) == {"name": "n", "x": 1, "y": 5}
    assert flatten(nested({"name": "n", "x": 1, "z": 3}), GROUPS) == {"name": "n", "x": 1, "y": 5, "z": 3}


def test_a_section_with_none_of_its_fields_is_left_out():
    nested = sectioned(vol.Schema({vol.Optional("x"): int}), GROUPS)
    assert [str(k) for k in nested.schema] == ["a"]


async def test_the_filters_page_takes_its_fields_nested_or_flat(
    hass: HomeAssistant, mock_hub_entry: MockConfigEntry, mock_setup
):
    mock_hub_entry.add_to_hass(hass)
    result = await _open_options(hass, mock_hub_entry.entry_id, step="hub_filters")
    assert [str(k) for k in result["data_schema"].schema] == ["filters", "settling"]
    nested = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"filters": {CONF_FILTER_CTRL_FAST_TAU_S: 1.0, CONF_FILTER_INPUT_TAU_S: 5.0,
                                CONF_FILTER_PERMIT_TAU_S: 9.0}, "settling": {}},
    )
    assert nested["type"] == FlowResultType.CREATE_ENTRY
    assert mock_hub_entry.options[CONF_FILTER_PERMIT_TAU_S] == 9.0
    assert mock_hub_entry.options[CONF_FILTER_INPUT_TAU_S] == 5.0


async def test_an_error_on_a_sectioned_field_is_keyed_by_its_section(
    hass: HomeAssistant, mock_hub_entry: MockConfigEntry, mock_setup
):
    mock_hub_entry.add_to_hass(hass)
    result = await _open_options(hass, mock_hub_entry.entry_id, step="hub_filters")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"filters": {CONF_FILTER_CTRL_FAST_TAU_S: 10.0, CONF_FILTER_INPUT_TAU_S: 5.0}, "settling": {}},
    )
    # the frontend shows an error above a section and none under a field inside one
    assert result["errors"] == {"filters": "fast_filter_not_below_input"}
