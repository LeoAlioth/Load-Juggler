"""Load Juggler - config-flow helpers: validation, ordering and discovery.

The module-level utilities the flow steps lean on, none of them bound to a flow
instance: the unit sets a form may offer (one declaration, shared with the
readers), the entity-unit and forecast-device validators, entry-title
composition, the controlled-device and priority-order helpers behind the
priority page, the OCPP charger scan the discovery step and ``__init__.py``
both call, and the hub phase count derived from the configured grid CTs.

Moved verbatim out of the single-file config_flow.py.
"""
import logging
import voluptuous as vol
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.entity_registry import (
    async_entries_for_device as er_async_entries_for_device,
    async_get as async_get_entity_registry,
)
from homeassistant.helpers.selector import selector
from .. import units
from ..const import (
    CONF_CHARGER_PRIORITY,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_NAME,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PRIORITY_ORDER,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    DEFAULT_CHARGER_PRIORITY,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_CHARGER,
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT,
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L1,
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L2,
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L3,
    OCPP_ENTITY_SUFFIX_CURRENT_OFFERED,
    OCPP_ENTITY_SUFFIX_POWER_IMPORT,
    OCPP_ENTITY_SUFFIX_POWER_OFFERED,
)
from ..helpers import get_entry_value, prettify_name
from ..registry import get_inverters_for_hub

_LOGGER = logging.getLogger(__name__)
_POWER_FACTOR = 0.9  # 90% of detected limit for safe headroom

# One declaration, shared with the readers: a unit offered here must be one
# units.py can convert (see ENTITY_UNIT_CONTRACTS and test_unit_contracts.py).
_CURRENT_UNITS = units.CURRENT_UNITS
_POWER_UNITS = units.POWER_UNITS
_SOC_UNITS = units.SOC_UNITS
_VOLTAGE_UNITS = units.VOLTAGE_UNITS


def _validate_entity_units(
    hass, user_input: dict, field_unit_map: dict, errors: dict
) -> None:
    """Validate that provided entities report expected measurement units.

    Silently skips when an entity's state is unavailable/unknown or has no
    unit_of_measurement attribute — the user is never blocked by missing state.
    Only flags an error when a unit is present and clearly wrong.
    """
    for field_key, valid_units in field_unit_map.items():
        entity_id = user_input.get(field_key)
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if units.is_unavailable(state):
            continue
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit not in valid_units:
            errors[field_key] = "invalid_unit"


def _validate_forecast_devices(hass, user_input: dict, errors: dict) -> str | None:
    """Every selected solar forecast device must offer a ``watts`` sensor.

    A forecast device (one Open-Meteo Solar Forecast config entry per PV
    array) exposes several sensors; the clipping forecast reads the ``watts``
    attribute (a mapping of block-start timestamps to average watts) from one
    of them. A device with sensor entities but none of their states loaded
    yet never blocks the user — mirroring _validate_entity_units — but a
    device whose loaded sensors carry no watts mapping is the wrong device.

    Returns the offending device's display name so the step can name it in
    the form error via description_placeholders, or None when valid.
    """
    entity_registry = async_get_entity_registry(hass)
    for device_id in user_input.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or []:
        sensors = [
            e.entity_id
            for e in er_async_entries_for_device(entity_registry, device_id)
            if e.domain == "sensor"
        ]
        states = [s for s in (hass.states.get(eid) for eid in sensors) if s is not None]
        if sensors and not states:
            continue  # states not loaded yet — never block on missing state
        if any(isinstance(s.attributes.get("watts"), dict) for s in states):
            continue
        errors[CONF_SOLAR_FORECAST_DEVICE_IDS] = "forecast_device_no_watts"
        device = async_get_device_registry(hass).async_get(device_id)
        if device:
            return device.name_by_user or device.name or device_id
        return device_id
    return None


def _compose_entry_title(name: str, type_label: str) -> str:
    """Compose a config-entry title without doubling the device-type label.

    The type label is appended only when the user's name doesn't already
    contain it — so a device left at its default name (e.g. "Hot Water Tank")
    becomes just "Hot Water Tank", not "Hot Water Tank Hot Water Tank", while
    a custom name like "Kitchen" still becomes "Kitchen Hot Water Tank".
    """
    name = (name or "").strip()
    if not name:
        return type_label
    if type_label.lower() in name.lower():
        return name
    return f"{name} {type_label}"


# --- Device-priority reordering (used by the hub options flow) ---

def _controlled_devices(hass, hub_entry_id: str) -> list:
    """All controllable load entries (EVSE, plug, tank) linked to a hub."""
    return [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
        and e.data.get(CONF_HUB_ENTRY_ID) == hub_entry_id
    ]


def _devices_by_priority(devices: list) -> list:
    """Devices sorted by effective priority, then title for a stable tie-break."""
    return sorted(
        devices,
        key=lambda e: (
            get_entry_value(e, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            e.title,
        ),
    )


def _priority_order_schema(devices: list) -> vol.Schema:
    """Ordered multi-select listing every controlled device, current order first."""
    ordered = _devices_by_priority(devices)
    options = [
        {"value": e.entry_id, "label": e.title or e.data.get(CONF_NAME, e.entry_id)}
        for e in ordered
    ]
    return vol.Schema(
        {
            vol.Required(
                CONF_PRIORITY_ORDER,
                default=[e.entry_id for e in ordered],
            ): selector(
                {
                    "select": {
                        "options": options,
                        "multiple": True,
                        "mode": "dropdown",
                        "sort": False,
                    }
                }
            ),
        }
    )


def _apply_priority_order(hass, devices: list, chosen: list) -> None:
    """Write rank 1..N to each device from the chosen order.

    Devices the user left out keep their current ranking at the end. Only the
    per-device priority number is touched, so the distribution engine is
    unchanged — it still sorts by (mode urgency, priority).
    """
    placed = list(chosen)
    for entry in _devices_by_priority(devices):
        if entry.entry_id not in placed:
            placed.append(entry.entry_id)
    for rank, entry_id in enumerate(placed, start=1):
        child = hass.config_entries.async_get_entry(entry_id)
        if not child or get_entry_value(child, CONF_CHARGER_PRIORITY, None) == rank:
            continue
        hass.config_entries.async_update_entry(
            child, options={**child.options, CONF_CHARGER_PRIORITY: rank}
        )


def scan_ocpp_chargers(hass) -> list[dict]:
    """Every OCPP charger in the entity registry that is not configured yet.

    The ONE scanner behind both entry points — the manual "Add OCPP Charger"
    flow and the automatic discovery spawned from ``_setup_hub_entry`` — so a
    discovered charger and a manually-added one always carry the same complete
    key set. Two things this settles for both paths:

    - ``device_id`` is the OCPP charge point id, i.e. the entity base name
      ("evbox_elvi"), never the internal HA device-registry UUID. The UUID is
      meaningless to the ocpp integration's services.
    - a charger is usable when it reports EITHER current_offered OR
      power_offered, so watts-only chargers are discovered too.

    Returns one dict per charger with keys: id, name, device_id,
    current_import_entity, current_import_l1/l2/l3_entity,
    current_offered_entity, power_offered_entity, power_import_entity
    (entity values are None when that entity does not exist).
    """
    chargers: list[dict] = []

    entity_registry = async_get_entity_registry(hass)
    device_registry = async_get_device_registry(hass)

    # Already-configured chargers are identified by their current_import entity
    configured_charger_imports = {
        entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
    }

    def _existing(entity_id: str) -> str | None:
        return entity_id if entity_id in entity_registry.entities else None

    for entity_id, entity in entity_registry.entities.items():
        if not (
            entity_id.startswith("sensor.")
            and entity_id.endswith(OCPP_ENTITY_SUFFIX_CURRENT_IMPORT)
        ):
            continue
        if entity_id in configured_charger_imports:
            continue

        # Extract charger base name
        base_name = entity_id.replace("sensor.", "").replace(
            OCPP_ENTITY_SUFFIX_CURRENT_IMPORT, ""
        )

        current_offered_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_OFFERED}"
        )
        # Fallback for watts-only chargers, which offer power instead of current
        power_offered_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_POWER_OFFERED}"
        )
        if not current_offered_entity and not power_offered_entity:
            continue

        # Per-phase current import (fallback 1) and power import (fallback 2)
        current_import_l1_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L1}"
        )
        current_import_l2_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L2}"
        )
        current_import_l3_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L3}"
        )
        power_import_entity = _existing(
            f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_POWER_IMPORT}"
        )

        # Prefer the device's friendly name for display purposes only
        device_name = prettify_name(base_name)
        if entity.device_id:
            device = device_registry.async_get(entity.device_id)
            if device and device.name:
                device_name = prettify_name(device.name)

        chargers.append(
            {
                "id": base_name,
                "name": device_name,
                "device_id": base_name,
                "current_import_entity": entity_id,
                "current_import_l1_entity": current_import_l1_entity,
                "current_import_l2_entity": current_import_l2_entity,
                "current_import_l3_entity": current_import_l3_entity,
                "current_offered_entity": current_offered_entity,
                "power_offered_entity": power_offered_entity,
                "power_import_entity": power_import_entity,
            }
        )

    return chargers


def _hub_phase_count(hass, hub_entry_id: str | None) -> int:
    """Number of phases this site has, as the engine sees it.

    A site phase exists when it has a grid CT or an inverter output sensor
    (mirrors ``run_hub_calculation``'s phase derivation). The inverter output
    entities may live on the hub's own legacy fields OR — after the one-time
    auto-import — on any of its inverter child entries, so the whole fleet is
    consulted. Without that, an off-grid 3-phase site collapses to 1 phase
    post-import, hiding the L2/L3 mapping fields and force-mapping every
    charger leg onto L1's phase.
    """
    if not hub_entry_id:
        return 3  # Default to 3 if unknown
    hub_entry = hass.config_entries.async_get_entry(hub_entry_id)
    if not hub_entry:
        return 3
    opts = {**hub_entry.data, **hub_entry.options}
    # Count from grid CT entities first
    count = sum(
        1
        for key in (
            CONF_PHASE_A_CURRENT_ENTITY_ID,
            CONF_PHASE_B_CURRENT_ENTITY_ID,
            CONF_PHASE_C_CURRENT_ENTITY_ID,
        )
        if opts.get(key)
    )
    if count > 0:
        return count
    # Off-grid fallback: infer from the inverter output entities of the whole
    # fleet — the hub's own (pre-import) fields plus every inverter child
    # entry. A phase counts once, no matter how many members feed it.
    sources = [opts] + [
        {**inverter.data, **inverter.options}
        for inverter in get_inverters_for_hub(hass, hub_entry_id)
    ]
    count = sum(
        1
        for key in (
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
        )
        if any(source.get(key) for source in sources)
    )
    return max(count, 1)
