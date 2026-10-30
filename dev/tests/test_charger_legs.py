"""A charger's phase count is the number of legs that are SET (JUG-15).

L1 is required; L2/L3 are optional (None = unused). A 1-phase charger is L1
only, 2-phase L1+L2, 3-phase all three on three different phases. Two set legs
on one site phase, or L3 without L2, are refused by the form on both edit
paths. Stored configs that used repeated legs to mean "unused" are migrated
(minor version 10) with the same phase mask.
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse import async_migrate_entry
from custom_components.dynamic_ocpp_evse.config_flow.helpers import _fill_hidden_legs
from custom_components.dynamic_ocpp_evse.const import (
    CONF_CHARGE_PAUSE_DURATION,
    CONF_CHARGE_RATE_UNIT,
    CONF_CHARGER_ID,
    CONF_CHARGER_L1_PHASE as L1,
    CONF_CHARGER_L2_PHASE as L2,
    CONF_CHARGER_L3_PHASE as L3,
    CONF_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_HUB_ENTRY_ID,
    CONF_LOAD_PRIORITY,
    CONF_NAME,
    CONF_OCPP_PROFILE_TIMEOUT,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_STACK_LEVEL,
    CONF_UPDATE_FREQUENCY,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
)
from custom_components.dynamic_ocpp_evse.helpers import (
    stored_legs,
    unuse_repeated_legs,
    validate_charger_settings,
)

MIN_MAX = {CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6, CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16}

# ── the rule ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "legs",
    [
        ("A", None, None),  # 1-phase, any phase
        ("C", None, None),
        ("A", "B", None),  # 2-phase
        ("C", "A", None),
        ("A", "B", "C"),  # 3-phase, every permutation
        ("A", "C", "B"),
        ("B", "A", "C"),
        ("B", "C", "A"),
        ("C", "A", "B"),
        ("C", "B", "A"),
    ],
)
def test_legitimate_mappings_are_accepted(legs):
    errors = {}
    validate_charger_settings(dict(zip((L1, L2, L3), legs)), errors)
    assert errors == {}


@pytest.mark.parametrize(
    "legs, field",
    [
        (("A", "A", None), L2),
        (("A", "A", "A"), L2),
        (("A", "B", "A"), L3),
        (("A", "B", "B"), L3),
        (("C", "C", "B"), L2),
        (("A", None, "C"), L3),  # L3 without L2
    ],
)
def test_duplicate_or_gapped_legs_are_refused(legs, field):
    errors = {}
    validate_charger_settings(dict(zip((L1, L2, L3), legs)), errors)
    assert list(errors) == [field]
    assert errors[field] in ("charger_phase_duplicate", "charger_phase_gap")


def test_blank_strings_count_as_unset():
    errors = {}
    validate_charger_settings({L1: "A", L2: "", L3: ""}, errors)
    assert errors == {}


def test_hidden_and_blank_legs_are_stored_as_none():
    one_phase_site = {L1: "B", L2: "B", L3: "B"}
    _fill_hidden_legs(one_phase_site, 1)
    assert one_phase_site == {L1: "B", L2: None, L3: None}

    two_phase_site = {L1: "A", L2: "B", L3: "A"}
    _fill_hidden_legs(two_phase_site, 2)
    assert two_phase_site == {L1: "A", L2: "B", L3: None}

    # a 3-phase site keeps the user's own choices as entered; blank = None
    three_phase_site = {L1: "A", L2: "C"}
    _fill_hidden_legs(three_phase_site, 3)
    assert three_phase_site == {L1: "A", L2: "C", L3: None}
    full = {L1: "C", L2: "B", L3: "A"}
    _fill_hidden_legs(full, 3)
    assert full == {L1: "C", L2: "B", L3: "A"}


def _entry(data=None, options=None, minor=9):
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=minor,
        title="Charger",
        data={ENTRY_TYPE: ENTRY_TYPE_LOAD, CONF_CHARGER_ID: "c", **(data or {})},
        options=options or {},
    )


def test_phase_count_is_the_set_legs():
    assert stored_legs(_entry(options={L1: "B", L2: None, L3: None})) == ["B"]
    assert stored_legs(_entry(options={L1: "A", L2: "C", L3: None})) == ["A", "C"]
    assert stored_legs(_entry(options={L1: "A", L2: "B", L3: "C"})) == ["A", "B", "C"]
    # nothing ever stored: the legacy default maps the legs to A/B/C
    assert stored_legs(_entry()) == ["A", "B", "C"]


# ── the engine builds the load from the legs ───────────────────────────


@pytest.mark.parametrize(
    "legs, phases, mask",
    [
        ((("A", None, None)), 1, "A"),
        ((("B", None, None)), 1, "B"),
        ((("A", "B", None)), 2, "AB"),
        ((("C", "A", None)), 2, "AC"),
        ((("A", "B", "C")), 3, "ABC"),
        ((("C", "A", "B")), 3, "ABC"),
    ],
)
async def test_engine_builds_phases_and_mask_from_the_legs(
    hass: HomeAssistant, mock_hub_entry, legs, phases, mask
):
    from custom_components.dynamic_ocpp_evse.engine.load_builders import (
        _build_evse_load,
    )

    mock_hub_entry.add_to_hass(hass)
    entry = _entry(
        data={CONF_HUB_ENTRY_ID: mock_hub_entry.entry_id},
        options={**MIN_MAX, **dict(zip((L1, L2, L3), legs))},
    )
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {}).setdefault("loads", {})

    load = _build_evse_load(hass, entry, 230, "wb", 1)

    assert load.phases == phases
    assert load.active_phases_mask == mask


def test_one_phase_charger_gets_its_16_amps_on_one_phase_only():
    """A L1-only charger is one 16 A leg on its phase - never three legs'
    worth - and the calculation permits it exactly what a phases=1 load gets."""
    from custom_components.dynamic_ocpp_evse.calculations.models import LoadContext

    load = LoadContext(
        load_id="x", entity_id="x", min_current=6, max_current=16, phases=1,
        priority=1, l1_phase="B", l2_phase="B", l3_phase="B",
    )
    assert load.active_phases_mask == "B"
    # the legs a 1-phase charger does not have carry no current
    assert load.l2_current == load.l3_current == 0


# ── both edit paths ────────────────────────────────────────────────────


async def _options_charger_current(hass, entry_id):
    result = await hass.config_entries.options.async_init(entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "charger"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LOAD_PRIORITY: 1}
    )
    assert result["step_id"] == "charger_current"
    return result


async def _setup(hass, hub, charger):
    hub.add_to_hass(hass)
    await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    charger.add_to_hass(hass)
    await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()


async def test_options_page_refuses_two_legs_on_one_phase(
    hass: HomeAssistant, mock_hub_entry, mock_charger_entry, mock_setup
):
    await _setup(hass, mock_hub_entry, mock_charger_entry)
    result = await _options_charger_current(hass, mock_charger_entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**MIN_MAX, L1: "A", L2: "A", L3: "A"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_current"
    assert result["errors"] == {"phases": "charger_phase_duplicate"}  # keyed by its section

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**MIN_MAX, L1: "A", L2: "B", L3: "A"}
    )
    assert result["errors"] == {"phases": "charger_phase_duplicate"}  # keyed by its section


@pytest.mark.parametrize(
    "submitted, stored",
    [
        ({L1: "B"}, ("B", None, None)),
        ({L1: "A", L2: "C"}, ("A", "C", None)),
        ({L1: "C", L2: "A", L3: "B"}, ("C", "A", "B")),
    ],
)
async def test_options_page_accepts_1_2_and_3_legs(
    hass: HomeAssistant, mock_hub_entry, mock_charger_entry, mock_setup,
    submitted, stored,
):
    await _setup(hass, mock_hub_entry, mock_charger_entry)
    result = await _options_charger_current(hass, mock_charger_entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**MIN_MAX, **submitted}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_timing"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 3,
            CONF_STACK_LEVEL: 3,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert (
        mock_charger_entry.options[L1],
        mock_charger_entry.options[L2],
        mock_charger_entry.options[L3],
    ) == stored


async def _wizard_charger_current(hass, hub):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "integration_discovery"},
        data={
            "hub_entry_id": hub.entry_id,
            "charger_id": "wallbox",
            "charger_name": "Wallbox Pro",
            "device_id": "device_wb_pro",
            "current_import_entity": "sensor.wallbox_current_import",
            "current_offered_entity": "sensor.wallbox_current_offered",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Wallbox Pro", CONF_ENTITY_ID: "wallbox", CONF_LOAD_PRIORITY: 1},
    )
    assert result["step_id"] == "charger_current"
    return result


async def test_create_wizard_refuses_two_legs_on_one_phase(
    hass: HomeAssistant, mock_hub_entry, mock_setup
):
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    result = await _wizard_charger_current(hass, mock_hub_entry)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**MIN_MAX, L1: "A", L2: "A", L3: "A"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_current"
    assert result["errors"] == {"phases": "charger_phase_duplicate"}  # keyed by its section

    # fixing it moves on
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**MIN_MAX, L1: "A"}
    )
    assert result["step_id"] == "charger_timing"


@pytest.mark.parametrize(
    "submitted",
    [{L1: "B"}, {L1: "A", L2: "C"}, {L1: "C", L2: "A", L3: "B"}],
)
async def test_create_wizard_accepts_1_2_and_3_legs(
    hass: HomeAssistant, mock_hub_entry, mock_setup, submitted
):
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    result = await _wizard_charger_current(hass, mock_hub_entry)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**MIN_MAX, **submitted}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_timing"


# ── the 2.10 migration ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stored, expected",
    [
        # all legs on one phase -> L1 only
        (("A", "A", "A"), ("A", None, None)),
        (("C", "C", "C"), ("C", None, None)),
        # a 1-/2-phase site's copied hidden legs
        (("A", "B", "A"), ("A", "B", None)),
        (("B", "A", "B"), ("B", "A", None)),
        (("A", "A", "B"), None),  # would leave a gap: left for the user
        # already distinct / already None: untouched
        (("A", "B", "C"), ("A", "B", "C")),
        (("C", "A", "B"), ("C", "A", "B")),
        (("A", None, None), ("A", None, None)),
        (("A", "B", None), ("A", "B", None)),
    ],
)
async def test_migration_turns_repeated_legs_into_unused(
    hass: HomeAssistant, stored, expected
):
    entry = _entry(options=dict(zip((L1, L2, L3), stored)))
    entry.add_to_hass(hass)
    before = stored_legs(entry)

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 10
    got = (entry.options[L1], entry.options[L2], entry.options[L3])
    assert got == (expected or stored)
    # behaviour preserved: the same set of phases (hence mask and permit)
    assert set(stored_legs(entry)) == set(before)


async def test_migration_is_idempotent(hass: HomeAssistant):
    entry = _entry(options={L1: "A", L2: "A", L3: "A"})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    first = (dict(entry.data), dict(entry.options))

    assert await async_migrate_entry(hass, entry)
    assert (dict(entry.data), dict(entry.options)) == first

    hass.config_entries.async_update_entry(entry, minor_version=9)
    assert await async_migrate_entry(hass, entry)
    assert (dict(entry.data), dict(entry.options)) == first
    assert not unuse_repeated_legs(dict(entry.data), dict(entry.options))


async def test_migration_writes_into_the_store_that_holds_the_leg(
    hass: HomeAssistant,
):
    """Legs in data (setup) and options (a later edit) - the effective value is
    what counts, and it is replaced where it lives."""
    entry = _entry(data={L1: "A", L2: "A", L3: "A"}, options={L3: "A"})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.data[L2] is None
    assert entry.options[L3] is None
    assert stored_legs(entry) == ["A"]


async def test_migration_leaves_other_entries_alone(hass: HomeAssistant):
    plug = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=9, title="Plug",
        data={ENTRY_TYPE: ENTRY_TYPE_LOAD, CONF_NAME: "Plug"}, options={},
    )
    plug.add_to_hass(hass)
    assert await async_migrate_entry(hass, plug)
    assert plug.minor_version == 10
    assert dict(plug.data) == {ENTRY_TYPE: ENTRY_TYPE_LOAD, CONF_NAME: "Plug"}
    assert dict(plug.options) == {}
