"""OCPP charger discovery — device-registry driven, not entity-name guessing.

The scan used to take one ``sensor.*_current_import`` entity_id, strip the
suffix off it and guess every sibling as ``sensor.{base}{suffix}``. These tests
pin the replacement: siblings are found through the device the ocpp integration
created, and the charge point id — the only handle its services accept — is
read off the device-registry identifier it stamps.

Every case here is one the old prefix guess got wrong or could not see at all,
plus the contract cases (payload key set, watts-only chargers, the
already-configured filter) that must survive the change untouched.
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.config_flow.helpers import (
    ocpp_charger_for_device,
    scan_ocpp_chargers,
)
from custom_components.dynamic_ocpp_evse.const import (
    CONF_CHARGE_PAUSE_DURATION,
    CONF_CHARGE_RATE_UNIT,
    CONF_CHARGER_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_POWER_IMPORT_ENTITY_ID,
    CONF_EVSE_POWER_OFFERED_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_LOAD_PRIORITY,
    CONF_NAME,
    CONF_OCPP_DEVICE_ID,
    CONF_OCPP_PROFILE_TIMEOUT,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_STACK_LEVEL,
    CONF_UPDATE_FREQUENCY,
    DEFAULT_CHARGE_PAUSE_DURATION,
    DEFAULT_OCPP_PROFILE_TIMEOUT,
    DEFAULT_PROFILE_VALIDITY_MODE,
    DEFAULT_STACK_LEVEL,
    DEFAULT_UPDATE_FREQUENCY,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
    FIELD_OCPP_DEVICE,
)

# The payload contract both entry points depend on: the manual wizard reads
# these keys off _selected_charger and __init__ splats them into the discovery
# flow, which stores each one on the created entry.
PAYLOAD_KEYS = {
    "id",
    "name",
    "device_id",
    "ha_device_id",
    "current_import_entity",
    "current_import_l1_entity",
    "current_import_l2_entity",
    "current_import_l3_entity",
    "current_offered_entity",
    "power_offered_entity",
    "power_import_entity",
}


@pytest.fixture
def ocpp_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry standing in for the ocpp integration."""
    entry = MockConfigEntry(domain="ocpp", title="OCPP")
    entry.add_to_hass(hass)
    return entry


def _ocpp_device(hass, ocpp_entry, charge_point_id, connector=None, name=None):
    """The device the ocpp integration creates for a charge point/connector."""
    identifier = (
        charge_point_id if connector is None else f"{charge_point_id}-conn{connector}"
    )
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=ocpp_entry.entry_id,
        identifiers={("ocpp", identifier)},
        name=name or identifier,
    )


def _metric_key(metric: str) -> str:
    return metric.lower().replace(".", "_")


def _ocpp_sensor(
    hass,
    ocpp_entry,
    device,
    charge_point_id,
    metric,
    *,
    connector=None,
    object_id=None,
    unique_id=None,
    original_name=None,
    platform="ocpp",
):
    """Register one OCPP metric sensor exactly as the ocpp integration does.

    unique_id ``ocpp.<cpid>[.conn<n>].<metric key>.sensor``, name the metric
    with its dots turned into spaces, entity_id ``sensor.<cpid>_<metric key>``
    (``sensor.<cpid>_connector_<n>_<metric key>`` on a connector). Any of the
    three can be overridden, which is how the tests below model renames and
    non-ocpp lookalikes.
    """
    key = _metric_key(metric)
    if unique_id is None:
        parts = ["ocpp", charge_point_id, key, "sensor"]
        if connector is not None:
            parts.insert(2, f"conn{connector}")
        unique_id = ".".join(parts)
    if object_id is None:
        object_id = (
            f"{charge_point_id}_{key}"
            if connector is None
            else f"{charge_point_id}_connector_{connector}_{key}"
        )
    return (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            platform,
            unique_id,
            suggested_object_id=object_id,
            original_name=metric.replace(".", " ")
            if original_name is None
            else original_name,
            config_entry=ocpp_entry if platform == "ocpp" else None,
            device_id=device.id if device is not None else None,
        )
        .entity_id
    )


def _standard_charger(hass, ocpp_entry, charge_point_id, metrics=None):
    """A plain single-connector charger with the usual entity naming."""
    device = _ocpp_device(hass, ocpp_entry, charge_point_id)
    for metric in metrics or ("Current.Import", "Current.Offered"):
        _ocpp_sensor(hass, ocpp_entry, device, charge_point_id, metric)
    return device


def _configured_load(hass, hub_entry_id, current_import_entity):
    """A stored Load Juggler load entry pointing at a current_import sensor."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Configured",
            CONF_ENTITY_ID: "lj_configured",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_HUB_ENTRY_ID: hub_entry_id,
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: current_import_entity,
        },
    )
    entry.add_to_hass(hass)
    return entry


# ── the charge point id comes off the device ───────────────────────────


async def test_charge_point_id_comes_from_the_device_identifier(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    """Renamed entity_ids AND foreign unique_ids — only the device knows.

    The old scan needed ``sensor.<cpid>_current_import`` to exist and then
    guessed its siblings off that string, so this charger was invisible to it.
    Here the charge point id survives only on the device-registry identifier,
    and the metric only on the integration-supplied name.
    """
    device = _ocpp_device(hass, ocpp_entry, "evbox_elvi", name="Garage Wallbox")
    imported = _ocpp_sensor(
        hass, ocpp_entry, device, "evbox_elvi", "Current.Import",
        object_id="garage_wallbox_amps", unique_id="7f3c-current-import",
    )
    offered = _ocpp_sensor(
        hass, ocpp_entry, device, "evbox_elvi", "Current.Offered",
        object_id="garage_wallbox_limit", unique_id="7f3c-current-offered",
    )

    chargers = scan_ocpp_chargers(hass)

    assert len(chargers) == 1
    charger = chargers[0]
    # The charge point id, not the entity base name and not the HA UUID.
    assert charger["id"] == "evbox_elvi"
    assert charger["device_id"] == "evbox_elvi"
    assert charger["ha_device_id"] == device.id
    assert charger["name"] == "Garage Wallbox"
    assert charger["current_import_entity"] == imported == "sensor.garage_wallbox_amps"
    assert charger["current_offered_entity"] == offered == "sensor.garage_wallbox_limit"


async def test_siblings_match_by_unique_id_when_entity_ids_are_renamed(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    """No device at all, renamed entity_ids — the ocpp unique_id still names both."""
    for metric, object_id in (
        ("Current.Import", "shed_draw"),
        ("Current.Offered", "shed_cap"),
        ("Power.Active.Import", "shed_watts"),
    ):
        _ocpp_sensor(
            hass, ocpp_entry, None, "shed_cp", metric, object_id=object_id
        )

    chargers = scan_ocpp_chargers(hass)

    assert [c["id"] for c in chargers] == ["shed_cp"]
    assert chargers[0]["current_import_entity"] == "sensor.shed_draw"
    assert chargers[0]["current_offered_entity"] == "sensor.shed_cap"
    assert chargers[0]["power_import_entity"] == "sensor.shed_watts"
    # No ocpp device in the registry, so the picker has nothing to default to.
    assert chargers[0]["ha_device_id"] is None


async def test_per_phase_sensors_on_connector_devices_join_one_charger(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    """A multi-connector charger is one load spread over several devices.

    The ocpp integration puts connector sensors on ``<cpid>-conn<n>``
    sub-devices and names them ``sensor.<cpid>_connector_<n>_<metric>``. The old
    scan read that entity_id as base name "<cpid>_connector_1", then looked for
    ``sensor.<cpid>_connector_1_current_offered`` — which lives on the charge
    point, not the connector — found nothing offered, and skipped the charger.
    """
    charge_point = _ocpp_device(hass, ocpp_entry, "abb_terra")
    conn1 = _ocpp_device(hass, ocpp_entry, "abb_terra", connector=1)
    conn2 = _ocpp_device(hass, ocpp_entry, "abb_terra", connector=2)

    offered = _ocpp_sensor(
        hass, ocpp_entry, charge_point, "abb_terra", "Current.Offered"
    )
    per_phase = {
        metric: _ocpp_sensor(
            hass, ocpp_entry, conn1, "abb_terra", metric, connector=1
        )
        for metric in (
            "Current.Import",
            "Current.Import.L1",
            "Current.Import.L2",
            "Current.Import.L3",
        )
    }
    second = _ocpp_sensor(
        hass, ocpp_entry, conn2, "abb_terra", "Current.Import", connector=2
    )

    chargers = scan_ocpp_chargers(hass)

    assert len(chargers) == 1, "one charge point is one load, not one per connector"
    charger = chargers[0]
    assert charger["id"] == "abb_terra"
    assert charger["ha_device_id"] == charge_point.id
    assert charger["current_offered_entity"] == offered
    # Connector 1 wins over connector 2 for the shared metric.
    assert charger["current_import_entity"] == per_phase["Current.Import"]
    assert charger["current_import_entity"] != second
    assert charger["current_import_l1_entity"] == per_phase["Current.Import.L1"]
    assert charger["current_import_l2_entity"] == per_phase["Current.Import.L2"]
    assert charger["current_import_l3_entity"] == per_phase["Current.Import.L3"]


async def test_l1_sensor_is_not_mistaken_for_the_plain_current_import(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    """A charger that reports per-phase current but no total keeps them apart."""
    device = _ocpp_device(hass, ocpp_entry, "phasey")
    l1 = _ocpp_sensor(hass, ocpp_entry, device, "phasey", "Current.Import.L1")
    _ocpp_sensor(hass, ocpp_entry, device, "phasey", "Current.Offered")

    chargers = scan_ocpp_chargers(hass)

    # No total draw sensor → nothing to steer against, so not discovered.
    assert chargers == []
    payload = ocpp_charger_for_device(hass, device.id)
    assert payload is None, "the L1 sensor must not stand in for the total"
    # …but it was classified as L1, not swallowed by the plain suffix.
    assert l1 == "sensor.phasey_current_import_l1"


# ── contracts that must survive the rewrite ───────────────────────────


async def test_payload_carries_exactly_the_contract_keys(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    _standard_charger(hass, ocpp_entry, "keys_cp")

    (charger,) = scan_ocpp_chargers(hass)

    assert set(charger) == PAYLOAD_KEYS
    # Absent sensors are present as None, never missing.
    assert charger["power_offered_entity"] is None
    assert charger["current_import_l3_entity"] is None


async def test_watts_only_charger_is_discovered(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    """power_offered with no current_offered is a usable charger."""
    device = _ocpp_device(hass, ocpp_entry, "watts_cp")
    imported = _ocpp_sensor(hass, ocpp_entry, device, "watts_cp", "Current.Import")
    offered = _ocpp_sensor(hass, ocpp_entry, device, "watts_cp", "Power.Offered")

    (charger,) = scan_ocpp_chargers(hass)

    assert charger["current_import_entity"] == imported
    assert charger["current_offered_entity"] is None
    assert charger["power_offered_entity"] == offered


async def test_charger_with_no_offered_sensor_is_skipped(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    _standard_charger(hass, ocpp_entry, "mute_cp", metrics=("Current.Import",))

    assert scan_ocpp_chargers(hass) == []


async def test_already_configured_charger_drops_out_of_the_scan(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry, mock_hub_entry: MockConfigEntry
):
    """Filtering is still by the stored current_import entity id."""
    mock_hub_entry.add_to_hass(hass)
    _standard_charger(hass, ocpp_entry, "taken_cp")
    _standard_charger(hass, ocpp_entry, "free_cp")

    assert {c["id"] for c in scan_ocpp_chargers(hass)} == {"taken_cp", "free_cp"}

    _configured_load(hass, mock_hub_entry.entry_id, "sensor.taken_cp_current_import")

    assert [c["id"] for c in scan_ocpp_chargers(hass)] == ["free_cp"]


async def test_ocpp_shaped_sensors_without_the_ocpp_integration_still_scan(
    hass: HomeAssistant
):
    """Template sensors mirroring a charger keep working (entity_id fallback).

    Neither an ocpp device nor an ocpp unique_id nor an OCPP metric name — the
    entity_id suffix is all there is, which is exactly what the pre-device scan
    keyed on. The charge point id falls back to the entity base name.
    """
    registry = er.async_get(hass)
    for object_id in ("fake_cp_current_import", "fake_cp_current_offered"):
        registry.async_get_or_create(
            "sensor",
            "template",
            f"tpl-{object_id}",
            suggested_object_id=object_id,
            original_name="Some template sensor",
        )

    (charger,) = scan_ocpp_chargers(hass)

    assert charger["id"] == "fake_cp"
    assert charger["device_id"] == "fake_cp"
    assert charger["name"] == "Fake Cp"
    assert charger["ha_device_id"] is None
    assert charger["current_import_entity"] == "sensor.fake_cp_current_import"


async def test_two_chargers_are_reported_separately(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    _standard_charger(hass, ocpp_entry, "beta_cp")
    _standard_charger(hass, ocpp_entry, "alpha_cp")

    assert [c["id"] for c in scan_ocpp_chargers(hass)] == ["alpha_cp", "beta_cp"]


# ── the single-device resolver behind the picker ───────────────────────


async def test_resolver_returns_the_same_payload_as_the_scan(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    device = _standard_charger(hass, ocpp_entry, "same_cp")

    (scanned,) = scan_ocpp_chargers(hass)

    assert ocpp_charger_for_device(hass, device.id) == scanned


async def test_resolver_maps_a_connector_device_to_its_charge_point(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    """Picking "Terra Connector 1" must still configure the charge point."""
    charge_point = _ocpp_device(hass, ocpp_entry, "terra")
    conn = _ocpp_device(hass, ocpp_entry, "terra", connector=1)
    _ocpp_sensor(hass, ocpp_entry, charge_point, "terra", "Current.Offered")
    _ocpp_sensor(hass, ocpp_entry, conn, "terra", "Current.Import", connector=1)

    payload = ocpp_charger_for_device(hass, conn.id)

    assert payload["device_id"] == "terra"
    assert payload["ha_device_id"] == charge_point.id


async def test_resolver_still_resolves_an_already_configured_charger(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry, mock_hub_entry: MockConfigEntry
):
    """The scan hides configured chargers; an explicit pick does not."""
    mock_hub_entry.add_to_hass(hass)
    device = _standard_charger(hass, ocpp_entry, "again_cp")
    _configured_load(hass, mock_hub_entry.entry_id, "sensor.again_cp_current_import")

    assert scan_ocpp_chargers(hass) == []
    assert ocpp_charger_for_device(hass, device.id)["device_id"] == "again_cp"


async def test_resolver_rejects_a_device_that_is_not_a_usable_charger(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry
):
    bare = _ocpp_device(hass, ocpp_entry, "bare_cp")
    stranger = dr.async_get(hass).async_get_or_create(
        config_entry_id=ocpp_entry.entry_id,
        identifiers={("some_other_integration", "thing")},
        name="Not a charger",
    )

    assert ocpp_charger_for_device(hass, bare.id) is None
    assert ocpp_charger_for_device(hass, stranger.id) is None
    assert ocpp_charger_for_device(hass, "no-such-device") is None
    assert ocpp_charger_for_device(hass, None) is None


# ── the charger_info device picker ─────────────────────────────────────


def _field(schema, key):
    """``(marker, validator)`` for one key of a voluptuous schema."""
    for marker, validator in schema.schema.items():
        if str(marker) == key:
            return marker, validator
    raise AssertionError(f"{key} not in schema: {[str(m) for m in schema.schema]}")


async def _start_discovery(hass, hub_entry, charger):
    """Open the charger wizard on charger_info for one scanned charger."""
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "integration_discovery"},
        data={
            "hub_entry_id": hub_entry.entry_id,
            "charger_id": charger["id"],
            "charger_name": charger["name"],
            **{k: v for k, v in charger.items() if k not in ("id", "name")},
        },
    )


async def _finish_charger_wizard(hass, flow_id):
    """Walk charger_current and charger_timing with plain valid values."""
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_CHARGER_L2_PHASE: "B",
            CONF_CHARGER_L3_PHASE: "C",
        },
    )
    assert result["step_id"] == "charger_timing"
    return await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: DEFAULT_PROFILE_VALIDITY_MODE,
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_OCPP_PROFILE_TIMEOUT: DEFAULT_OCPP_PROFILE_TIMEOUT,
            CONF_CHARGE_PAUSE_DURATION: DEFAULT_CHARGE_PAUSE_DURATION,
            CONF_STACK_LEVEL: DEFAULT_STACK_LEVEL,
        },
    )


async def test_charger_info_offers_a_device_picker_not_a_text_field(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry, mock_hub_entry: MockConfigEntry
):
    """The free-text charge point id is gone; an ocpp device picker replaces it."""
    mock_hub_entry.add_to_hass(hass)
    device = _standard_charger(hass, ocpp_entry, "picker_cp")
    (charger,) = scan_ocpp_chargers(hass)

    result = await _start_discovery(hass, mock_hub_entry, charger)

    assert result["step_id"] == "charger_info"
    schema = result["data_schema"]
    assert CONF_OCPP_DEVICE_ID not in [str(m) for m in schema.schema]
    marker, validator = _field(schema, FIELD_OCPP_DEVICE)
    assert validator.config.get("integration") == "ocpp"
    # Pre-filled with the device discovery matched, so leaving it alone is a
    # no-op — and suggested_value rather than default, so it can be cleared.
    assert marker.description == {"suggested_value": device.id}


async def test_the_manual_flow_preselects_the_scanned_device(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry, mock_hub_entry: MockConfigEntry
):
    """Same picker, same pre-fill, on the "Add OCPP Charger" path."""
    mock_hub_entry.add_to_hass(hass)
    device = _standard_charger(hass, ocpp_entry, "manual_cp")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "evse"}
    )
    assert result["step_id"] == "discover_chargers"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"charger": "manual_cp"}
    )

    assert result["step_id"] == "charger_info"
    marker, _validator = _field(result["data_schema"], FIELD_OCPP_DEVICE)
    assert marker.description == {"suggested_value": device.id}


async def test_picking_another_device_rewrites_the_charge_point_and_its_sensors(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry, mock_hub_entry: MockConfigEntry
):
    """Correcting a mis-matched charger moves every OCPP field at once.

    The picked device decides the stored charge point id and the whole sensor
    set — one derivation, the scanner's. Only CONF_CHARGER_ID stays behind: the
    discovery unique_id was already claimed on it.
    """
    mock_hub_entry.add_to_hass(hass)
    _standard_charger(hass, ocpp_entry, "alpha_cp")
    bravo = _ocpp_device(hass, ocpp_entry, "bravo_cp")
    for metric in (
        "Current.Import",
        "Current.Import.L1",
        "Power.Offered",
        "Power.Active.Import",
    ):
        _ocpp_sensor(hass, ocpp_entry, bravo, "bravo_cp", metric)

    alpha = next(c for c in scan_ocpp_chargers(hass) if c["id"] == "alpha_cp")
    result = await _start_discovery(hass, mock_hub_entry, alpha)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Alpha",
            CONF_ENTITY_ID: "lj_alpha",
            CONF_LOAD_PRIORITY: 1,
            FIELD_OCPP_DEVICE: bravo.id,
        },
    )
    assert result["step_id"] == "charger_current"

    result = await _finish_charger_wizard(hass, result["flow_id"])

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["result"].data
    # The charge point id, resolved off the picked device — not its UUID.
    assert data[CONF_OCPP_DEVICE_ID] == "bravo_cp"
    assert bravo.id not in data.values()
    assert data[CONF_CHARGER_ID] == "alpha_cp"
    assert data[CONF_EVSE_CURRENT_IMPORT_ENTITY_ID] == "sensor.bravo_cp_current_import"
    assert (
        data[CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID]
        == "sensor.bravo_cp_current_import_l1"
    )
    assert data[CONF_EVSE_POWER_OFFERED_ENTITY_ID] == "sensor.bravo_cp_power_offered"
    assert (
        data[CONF_EVSE_POWER_IMPORT_ENTITY_ID]
        == "sensor.bravo_cp_power_active_import"
    )
    # bravo is watts-only, so alpha's current_offered must not linger.
    assert data[CONF_EVSE_CURRENT_OFFERED_ENTITY_ID] is None
    assert FIELD_OCPP_DEVICE not in data
    assert FIELD_OCPP_DEVICE not in result["result"].options


async def test_leaving_the_picker_alone_keeps_what_discovery_found(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry, mock_hub_entry: MockConfigEntry
):
    mock_hub_entry.add_to_hass(hass)
    device = _standard_charger(hass, ocpp_entry, "keep_cp")
    (charger,) = scan_ocpp_chargers(hass)

    result = await _start_discovery(hass, mock_hub_entry, charger)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Keep",
            CONF_ENTITY_ID: "lj_keep",
            CONF_LOAD_PRIORITY: 1,
            FIELD_OCPP_DEVICE: device.id,
        },
    )
    result = await _finish_charger_wizard(hass, result["flow_id"])

    data = result["result"].data
    assert data[CONF_OCPP_DEVICE_ID] == "keep_cp"
    assert data[CONF_CHARGER_ID] == "keep_cp"
    assert data[CONF_EVSE_CURRENT_IMPORT_ENTITY_ID] == "sensor.keep_cp_current_import"
    assert (
        data[CONF_EVSE_CURRENT_OFFERED_ENTITY_ID] == "sensor.keep_cp_current_offered"
    )


async def test_picking_a_device_that_is_no_charger_re_shows_the_form(
    hass: HomeAssistant, ocpp_entry: MockConfigEntry, mock_hub_entry: MockConfigEntry
):
    """A device with no usable OCPP sensors is refused on the field, not stored."""
    mock_hub_entry.add_to_hass(hass)
    _standard_charger(hass, ocpp_entry, "good_cp")
    empty = _ocpp_device(hass, ocpp_entry, "empty_cp")
    (charger,) = scan_ocpp_chargers(hass)

    result = await _start_discovery(hass, mock_hub_entry, charger)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Good",
            CONF_ENTITY_ID: "lj_good",
            CONF_LOAD_PRIORITY: 1,
            FIELD_OCPP_DEVICE: empty.id,
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_info"
    assert result["errors"] == {FIELD_OCPP_DEVICE: "ocpp_device_not_usable"}
