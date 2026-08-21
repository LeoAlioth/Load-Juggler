"""Load Juggler - the OCPP registry scan, shared by the flows and the engine.

One derivation of "which OCPP sensors belong to which charge point", reachable
from both sides of the integration:

* the config flow uses it to discover chargers (``scan_ocpp_chargers``) and to
  resolve one picked device-registry device to a charger (``ocpp_charger_for_device``);
* the runtime uses it to find a charger's connector-status sensor.

It lives at the package root rather than under ``config_flow/`` because
``engine/`` may not import the flows — a package-root module next to
``units.py`` / ``helpers.py`` / ``registry.py`` is below both layers, and this
one imports only ``const``, ``helpers`` and the Home Assistant registries, so
neither direction can grow a cycle.

Sensors are grouped by charge point and classified by the ocpp integration's
own metric keys (see ``_ocpp_metric_of``), never by guessing
``sensor.{base name}{suffix}`` off one entity_id. That is what makes a charger
with renamed entity_ids, or one whose per-phase sensors sit on connector
sub-devices, discoverable at all.

Moved here verbatim from ``config_flow/helpers.py``, where it could only be
reached by the flows.
"""

# PEP 604 unions (``str | None``) appear in this module's signatures, and
# engine/load_builders.py imports it, so it has to load on the Python 3.9
# interpreters the standalone test runners use. Nothing here evaluates
# annotations at runtime, so deferring them is enough (same arrangement as
# engine/load_builders.py and engine/auto_detect.py).
from __future__ import annotations

import logging

from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.entity_registry import (
    async_get as async_get_entity_registry,
)
from homeassistant.util import slugify

from .const import (
    CONF_CHARGER_ID,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_OCPP_DEVICE_ID,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT,
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L1,
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L2,
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L3,
    OCPP_ENTITY_SUFFIX_CURRENT_OFFERED,
    OCPP_ENTITY_SUFFIX_POWER_IMPORT,
    OCPP_ENTITY_SUFFIX_POWER_OFFERED,
    OCPP_ENTITY_SUFFIX_STATUS_CONNECTOR,
    OCPP_INTEGRATION_DOMAIN,
)
from .helpers import get_entry_value, prettify_name

_LOGGER = logging.getLogger(__name__)

# ── OCPP charger discovery ─────────────────────────────────────────────
#
# One payload key per OCPP metric. The metric key is the ocpp integration's own
# ``metric.lower().replace(".", "_")`` — which is exactly our entity suffix
# without its leading underscore, so the suffix constants stay the single
# declaration of what an OCPP sensor is called.
_OCPP_PAYLOAD_BY_SUFFIX = {
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT: "current_import_entity",
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L1: "current_import_l1_entity",
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L2: "current_import_l2_entity",
    OCPP_ENTITY_SUFFIX_CURRENT_IMPORT_L3: "current_import_l3_entity",
    OCPP_ENTITY_SUFFIX_CURRENT_OFFERED: "current_offered_entity",
    OCPP_ENTITY_SUFFIX_POWER_OFFERED: "power_offered_entity",
    OCPP_ENTITY_SUFFIX_POWER_IMPORT: "power_import_entity",
}
_OCPP_PAYLOAD_BY_METRIC = {
    suffix[1:]: payload_key for suffix, payload_key in _OCPP_PAYLOAD_BY_SUFFIX.items()
}
_OCPP_ENTITY_KEYS = tuple(_OCPP_PAYLOAD_BY_SUFFIX.values())

# The connector-status sensor is classified with the metrics above but is
# deliberately NOT one of _OCPP_ENTITY_KEYS: it never becomes a stored entry
# key, it is resolved from the registries at every setup instead (see
# ``ocpp_connector_status_entity``). So the discovery payload contract — and
# with it every stored charger entry — is exactly what it was.
_STATUS_GROUP_KEY = "status_connector_entity"
_OCPP_GROUP_KEY_BY_METRIC = {
    **_OCPP_PAYLOAD_BY_METRIC,
    OCPP_ENTITY_SUFFIX_STATUS_CONNECTOR[1:]: _STATUS_GROUP_KEY,
}
# Longest suffix first: "_current_import" must never claim an "_l1" sensor.
_OCPP_SUFFIXES = sorted(
    (*_OCPP_PAYLOAD_BY_SUFFIX, OCPP_ENTITY_SUFFIX_STATUS_CONNECTOR),
    key=len,
    reverse=True,
)

# The ocpp integration names a connector sub-device "<charge point id>-conn<n>".
_OCPP_CONNECTOR_MARK = "-conn"
# The charge point ranks 0 and its connectors 1..n, so a device that carries
# OCPP-shaped sensors without being an ocpp device sorts behind every connector.
_FOREIGN_DEVICE_RANK = 1_000_000


def _ocpp_device_identity(device) -> tuple[str, int | None] | None:
    """``(charge point id, connector number)`` for an ocpp device, else None.

    The ocpp integration stamps ``("ocpp", cpid)`` on the charge point itself
    and ``("ocpp", f"{cpid}-conn{n}")`` on each connector sub-device (whose
    ``via_device`` points back at the charge point). Reading the identifier is
    what makes the charge point id recoverable no matter how the entities or
    the device were renamed — and the charge point id is the only handle the
    ocpp services accept, so nothing else will do.
    """
    for domain, identifier in device.identifiers:
        if domain != OCPP_INTEGRATION_DOMAIN or not identifier:
            continue
        head, _mark, tail = identifier.partition(_OCPP_CONNECTOR_MARK)
        return head, int(tail) if tail.isdigit() else None
    return None


def _split_ocpp_unique_id(unique_id) -> tuple[str, str] | None:
    """``(charge point id, metric key)`` out of an ocpp sensor's unique_id.

    The format is ``ocpp.<cpid>[.conn<n>].<metric key>.sensor``, which the ocpp
    integration itself calls a persisted contract it will not change (a new
    format would orphan every existing sensor). That makes it the one registry
    field a scan can trust: unlike the entity_id it survives a rename, and
    unlike the friendly name it survives a user override.
    """
    if not unique_id:
        return None
    parts = str(unique_id).split(".")
    if len(parts) < 4 or parts[0] != OCPP_INTEGRATION_DOMAIN or parts[-1] != "sensor":
        return None
    return parts[1], parts[-2]


def _ocpp_metric_of(entity) -> str | None:
    """Which OCPP metric a registry entry carries, or None if it is not one.

    Three signals, most robust first:

    1. ``unique_id`` — the ocpp integration's own persisted format (above).
    2. the slugified ``original_name``: the metric is published with its dots
       turned into spaces ("Current.Import" → "Current Import"), and a user
       rename lands in ``name``, leaving ``original_name`` alone.
    3. the ``entity_id`` suffix — the only signal the pre-device scan had.
       Kept last so OCPP-shaped sensors the ocpp integration did NOT create
       (template sensors mirroring a charger) keep being discovered.
    """
    parsed = _split_ocpp_unique_id(entity.unique_id)
    if parsed and parsed[1] in _OCPP_GROUP_KEY_BY_METRIC:
        return parsed[1]
    from_name = slugify(entity.original_name or "")
    if from_name in _OCPP_GROUP_KEY_BY_METRIC:
        return from_name
    for suffix in _OCPP_SUFFIXES:
        if entity.entity_id.endswith(suffix):
            return suffix[1:]
    return None


def _ocpp_charger_candidates(hass) -> dict[str, dict]:
    """Group every OCPP metric sensor in the registries by charge point.

    Grouped by charge point id rather than by HA device on purpose: a
    multi-connector charger is several device-registry devices (the charge
    point plus one per connector), and the sensors Load Juggler needs are
    spread across them. One charge point is one load, so the charger-level
    sensor wins over a connector's, and the lowest connector number wins
    among connectors.

    Returns ``{charge point id: {"entities": {group key: entity_id},
    "name": str | None, "ha_device_id": str | None}}``. The group keys are the
    payload keys plus _STATUS_GROUP_KEY, which only the runtime resolver reads.
    """
    entity_registry = async_get_entity_registry(hass)
    device_registry = async_get_device_registry(hass)
    candidates: dict[str, dict] = {}

    for entity_id, entity in sorted(entity_registry.entities.items()):
        if not entity_id.startswith("sensor."):
            continue
        metric = _ocpp_metric_of(entity)
        if metric is None:
            continue

        device = (
            device_registry.async_get(entity.device_id) if entity.device_id else None
        )
        identity = _ocpp_device_identity(device) if device is not None else None
        parsed = _split_ocpp_unique_id(entity.unique_id)
        if identity is not None:
            charge_point_id, connector = identity
        elif parsed is not None:
            charge_point_id, connector = parsed[0], None
        else:
            # Neither the device nor the unique_id names the charge point: an
            # OCPP-shaped sensor from somewhere else. Fall back to the entity_id
            # base name, which is what the ocpp integration builds its own
            # entity_ids from, and what this scan used to key on throughout.
            charge_point_id = entity_id[len("sensor.") :].removesuffix(f"_{metric}")
            connector = None
        if not charge_point_id:
            continue

        group = candidates.setdefault(
            charge_point_id,
            {
                "entities": {},
                "ranks": {},
                "name": None,
                "name_rank": None,
                "ha_device_id": None,
                "device_rank": None,
            },
        )
        # Rank 0 is the charge point itself, 1..n its connectors.
        rank = connector or 0
        payload_key = _OCPP_GROUP_KEY_BY_METRIC[metric]
        if rank < group["ranks"].get(payload_key, rank + 1):
            group["entities"][payload_key] = entity_id
            group["ranks"][payload_key] = rank

        if device is None:
            continue
        # An ocpp device outranks a foreign one for both the display name and
        # the device the picker should default to; the charge point outranks
        # its own connectors.
        device_rank = rank if identity is not None else _FOREIGN_DEVICE_RANK
        if identity is not None and (
            group["device_rank"] is None or device_rank < group["device_rank"]
        ):
            group["ha_device_id"] = device.id
            group["device_rank"] = device_rank
        device_name = device.name_by_user or device.name
        if device_name and (
            group["name_rank"] is None or device_rank < group["name_rank"]
        ):
            group["name"] = prettify_name(device_name)
            group["name_rank"] = device_rank

    return candidates


def _ocpp_charger_payload(charge_point_id: str, group: dict) -> dict:
    """The one discovery payload shape, from one grouped charge point.

    ``device_id`` is the OCPP charge point id ("evbox_elvi"), never the HA
    device-registry UUID — the ocpp services cannot address a UUID. The UUID
    rides along separately as ``ha_device_id``, for the device picker only.
    """
    entities = group["entities"]
    return {
        "id": charge_point_id,
        "name": group["name"] or prettify_name(charge_point_id),
        "device_id": charge_point_id,
        "ha_device_id": group["ha_device_id"],
        **{key: entities.get(key) for key in _OCPP_ENTITY_KEYS},
    }


def _ocpp_charger_is_usable(payload: dict) -> bool:
    """A charger needs a draw reading and a setpoint we can steer.

    EITHER current_offered OR power_offered is enough: watts-only chargers
    offer power and no current, and they must stay discoverable.
    """
    return bool(payload["current_import_entity"]) and bool(
        payload["current_offered_entity"] or payload["power_offered_entity"]
    )


def scan_ocpp_chargers(hass) -> list[dict]:
    """Every OCPP charger in the registries that is not configured yet.

    The ONE scanner behind both entry points — the manual "Add OCPP Charger"
    flow and the automatic discovery spawned from ``_setup_hub_entry`` — so a
    discovered charger and a manually-added one always carry the same complete
    key set.

    Sibling sensors are found by device-registry membership and classified by
    the ocpp integration's own metric keys (see ``_ocpp_metric_of``), not by
    guessing ``sensor.{base name}{suffix}`` off one entity_id. That is what
    makes a charger with renamed entity_ids, or one whose per-phase sensors sit
    on connector sub-devices, discoverable at all.

    Returns one dict per charger with keys: id, name, device_id, ha_device_id,
    current_import_entity, current_import_l1/l2/l3_entity,
    current_offered_entity, power_offered_entity, power_import_entity
    (entity values are None when that entity does not exist).
    """
    # Already-configured chargers are identified by their current_import entity
    configured_charger_imports = {
        entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_LOAD
    }

    chargers: list[dict] = []
    for charge_point_id, group in sorted(_ocpp_charger_candidates(hass).items()):
        payload = _ocpp_charger_payload(charge_point_id, group)
        if not _ocpp_charger_is_usable(payload):
            continue
        if payload["current_import_entity"] in configured_charger_imports:
            continue
        chargers.append(payload)

    return chargers


def ocpp_charger_for_device(hass, ha_device_id: str | None) -> dict | None:
    """The scanner payload behind one device-registry device, or None.

    What the manual flow's device picker resolves through: the picker hands
    back an HA device-registry UUID, and this turns it into the very dict
    ``scan_ocpp_chargers`` produces, so the charge point id and every sensor
    entity come from one derivation on both paths. Picking a connector
    sub-device resolves to its charge point.

    Unlike the scan it does NOT drop already-configured chargers — the user
    named this device explicitly, and the duplicate check is the flow's.
    """
    if not ha_device_id:
        return None

    device = async_get_device_registry(hass).async_get(ha_device_id)
    identity = _ocpp_device_identity(device) if device is not None else None
    candidates = _ocpp_charger_candidates(hass)

    charge_point_id = identity[0] if identity is not None else None
    if charge_point_id is None:
        # A device with no ocpp identifier can still carry OCPP-shaped sensors.
        charge_point_id = next(
            (
                cpid
                for cpid, group in sorted(candidates.items())
                if group["ha_device_id"] == ha_device_id
            ),
            None,
        )
    if charge_point_id is None or charge_point_id not in candidates:
        return None

    payload = _ocpp_charger_payload(charge_point_id, candidates[charge_point_id])
    return payload if _ocpp_charger_is_usable(payload) else None


# ── the connector-status sensor, for the runtime ───────────────────────
#
# Runtime cache key inside a load's ``hass.data[DOMAIN]["loads"][entry_id]``
# bucket. That bucket is created when the load entry is set up and dropped when
# it is unloaded, which is exactly the invalidation this needs: the resolution
# is a registry read, and the registry is loaded from storage before any config
# entry sets up, so one resolution per setup is always made against a complete
# registry. Editing the charger (or reloading the entry, or restarting HA)
# re-resolves; nothing scans the registry per calculation cycle.
_RT_STATUS_ENTITY = "_ocpp_status_entity"


def _ocpp_status_entity_for(hass, charge_point_id: str | None) -> str | None:
    """This charge point's connector-status sensor, or None if it has none.

    Same classification and the same precedence as every other metric: the
    charger-level sensor outranks a connector's, and the lowest connector
    number wins among connectors — so on a multi-connector charger the status
    comes from the very connector whose current sensors the charger is
    configured with.
    """
    if not charge_point_id:
        return None
    group = _ocpp_charger_candidates(hass).get(charge_point_id)
    if group is None:
        return None
    return group["entities"].get(_STATUS_GROUP_KEY)


def ocpp_connector_status_entity(hass, entry) -> str:
    """The connector-status sensor of the charger behind one load entry.

    Resolved from the registries by metric classification rather than composed
    as ``sensor.{charge point id}_status_connector``: that string is wrong for
    a renamed status entity, and wrong on a multi-connector charger, where the
    real sensor is ``sensor.{cpid}_connector_{n}_status_connector``. Nothing
    new is stored — an existing entry is fixed the moment it is loaded again.

    Falls back to the legacy composed name when classification finds nothing,
    so a site whose "OCPP" sensors are template sensors with no registry entry
    keeps working exactly as before. Resolved once per entry setup and cached
    (see _RT_STATUS_ENTITY).
    """
    load_rt = (hass.data.get(DOMAIN, {}).get("loads") or {}).get(entry.entry_id)
    if load_rt is not None and _RT_STATUS_ENTITY in load_rt:
        return load_rt[_RT_STATUS_ENTITY]

    # The canonical charge point id for classification is the one every OCPP
    # service call uses (options-first, so an options edit is honoured); the
    # legacy fallback keeps composing off CONF_CHARGER_ID, byte-for-byte what
    # this used to be, so no working site can shift underneath itself.
    legacy_id = entry.data.get(CONF_CHARGER_ID) or entry.data.get(CONF_ENTITY_ID)
    charge_point_id = get_entry_value(entry, CONF_OCPP_DEVICE_ID, None) or legacy_id
    resolved = _ocpp_status_entity_for(hass, charge_point_id)
    if resolved is None and charge_point_id != legacy_id:
        resolved = _ocpp_status_entity_for(hass, legacy_id)
    if resolved is None:
        resolved = f"sensor.{legacy_id}{OCPP_ENTITY_SUFFIX_STATUS_CONNECTOR}"

    if load_rt is not None:
        load_rt[_RT_STATUS_ENTITY] = resolved
    return resolved
