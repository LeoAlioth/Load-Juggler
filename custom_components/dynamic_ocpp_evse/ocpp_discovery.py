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
    CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    CONF_EVSE_POWER_IMPORT_ENTITY_ID,
    CONF_EVSE_POWER_OFFERED_ENTITY_ID,
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


def _ocpp_device_identities(device) -> set[tuple[str, int | None]]:
    """Every ``(charge point id, connector number)`` an ocpp device carries.

    The ocpp integration stamps ``("ocpp", cpid)`` on the charge point itself
    and ``("ocpp", f"{cpid}-conn{n}")`` on each connector sub-device (whose
    ``via_device`` points back at the charge point). Reading the identifier is
    what makes the charge point id recoverable no matter how the entities or
    the device were renamed.

    Every identifier, not the first one: the charge point device carries TWO —
    its ``cpid`` and the ``cp_id`` the charger reports over the wire — and
    ``device.identifiers`` is a SET, so "the first" is whatever the string
    hashes decide, which Python re-seeds every process. Picking one that way
    made the resolved charge point id differ between restarts; the cp_id then
    keys nothing (the sensors' unique_ids are built from the cpid) and composes
    a charge-control switch name that does not exist. Callers match or
    intersect against this set instead, which needs no order at all.
    """
    identities: set[tuple[str, int | None]] = set()
    for domain, identifier in device.identifiers:
        if domain != OCPP_INTEGRATION_DOMAIN or not identifier:
            continue
        head, _mark, tail = identifier.partition(_OCPP_CONNECTOR_MARK)
        identities.add((head, int(tail) if tail.isdigit() else None))
    return identities


def _split_ocpp_unique_id(unique_id) -> tuple[str, int | None, str] | None:
    """``(charge point id, connector number, metric key)`` from an ocpp unique_id.

    The format is ``ocpp.<cpid>[.conn<n>].<metric key>.sensor``, which the ocpp
    integration itself calls a persisted contract it will not change (a new
    format would orphan every existing sensor). That makes it the one registry
    field a scan can trust: unlike the entity_id it survives a rename, unlike
    the friendly name it survives a user override, and unlike the device
    identifiers it names the ``cpid`` and nothing else — which is why it, not
    the device, decides the charge point id (see ``_ocpp_device_identities``).

    The id is rejoined from every part between the domain and the metric rather
    than read as ``parts[1]``, so a cpid containing dots survives the split.
    """
    if not unique_id:
        return None
    parts = str(unique_id).split(".")
    if len(parts) < 4 or parts[0] != OCPP_INTEGRATION_DOMAIN or parts[-1] != "sensor":
        return None
    head = parts[1:-2]
    connector = None
    if len(head) > 1 and head[-1].startswith("conn") and head[-1][4:].isdigit():
        connector = int(head[-1][4:])
        head = head[:-1]
    charge_point_id = ".".join(head)
    if not charge_point_id:
        return None
    return charge_point_id, connector, parts[-2]


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
    if parsed and parsed[2] in _OCPP_GROUP_KEY_BY_METRIC:
        return parsed[2]
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
        identities = _ocpp_device_identities(device) if device is not None else set()
        parsed = _split_ocpp_unique_id(entity.unique_id)
        if parsed is not None:
            # The unique_id decides, not the device: it names the cpid and only
            # the cpid, while the charge point device also carries the reported
            # cp_id (see ``_ocpp_device_identities``). Keying the groups — and
            # so the stored charge point id — on the cpid is what makes the
            # ocpp service calls and the composed entity names agree.
            charge_point_id, connector = parsed[0], parsed[1]
        elif identities:
            # A sensor on an ocpp device whose unique_id is not ocpp-shaped.
            # Sorted so two identifiers cannot answer differently per restart.
            charge_point_id, connector = sorted(
                identities, key=lambda i: (i[0], i[1] is not None, i[1] or 0)
            )[0]
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
        is_ocpp_device = any(head == charge_point_id for head, _conn in identities)
        device_rank = rank if is_ocpp_device else _FOREIGN_DEVICE_RANK
        if is_ocpp_device and (
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
    # Already-configured chargers are identified by their current_import entity.
    # Options-first: re-pointing a charger at another OCPP device on the options
    # page rewrites that entity into options, and a charger re-pointed onto this
    # device must stop being offered as an undiscovered one.
    configured_charger_imports = {
        get_entry_value(entry, CONF_EVSE_CURRENT_IMPORT_ENTITY_ID, None)
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
    identities = _ocpp_device_identities(device) if device is not None else set()
    candidates = _ocpp_charger_candidates(hass)

    # INTERSECT rather than pick: the charge point device carries both its cpid
    # and the charger-reported cp_id, and only the cpid keys the candidate map
    # (the groups are keyed off the sensors' unique_ids, which are built from
    # the cpid). So the intersection IS the cpid — no guessing which of the two
    # identifiers is which, and no dependence on the identifier set's order,
    # which Python re-seeds every process. Storing the cp_id instead was silent:
    # the ocpp services still resolve it, but the composed charge-control switch
    # name never matches, and the fan-out gives up without a word.
    matched = sorted({head for head, _conn in identities} & set(candidates))
    charge_point_id = matched[0] if matched else None
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
    if charge_point_id is None:
        return None

    payload = _ocpp_charger_payload(charge_point_id, candidates[charge_point_id])
    return payload if _ocpp_charger_is_usable(payload) else None


def ocpp_device_for_charge_point(hass, charge_point_id: str | None) -> str | None:
    """The device-registry UUID a stored charge point id belongs to, or None.

    The reverse of ``ocpp_charger_for_device``, and what pre-selects the device
    picker on the options charger page. Read straight off the identifier the
    ocpp integration stamps (``_ocpp_device_identities``), not off the candidate
    groups, so the picker still opens on the right device for a charger whose
    sensors are all unclassifiable. The charge point outranks its connectors —
    the same rule as the scan — so the picker never opens on a single leg.

    None when nothing in the device registry claims that charge point id, which
    is the template-sensor case: there is no device to point at.
    """
    if not charge_point_id:
        return None
    best_id = None
    best_rank = None
    for device in async_get_device_registry(hass).devices.values():
        # ANY identifier may be the match — the charge point device carries its
        # cp_id alongside its cpid, and this is a membership question, so it
        # never needed to single one out.
        ranks = [
            conn or 0
            for head, conn in _ocpp_device_identities(device)
            if head == charge_point_id
        ]
        if not ranks:
            continue
        rank = min(ranks)
        if best_rank is None or rank < best_rank:
            best_id, best_rank = device.id, rank
    return best_id


def repair_ocpp_device_id(hass, entry) -> str | None:
    """Rewrite a stored charge point id that names no charge point.

    Versions before 2026-02-19 stored the Home Assistant device-registry UUID
    in ``CONF_OCPP_DEVICE_ID``; the scan was fixed to store the charge point id
    the ocpp services actually accept, but nothing repaired the entries already
    written. Those sites kept working only because the ocpp integration quietly
    fell back to its first charger when a ``devid`` matched nothing —
    ``except KeyError: cp = list(self.charge_points.values())[0]`` — which was
    always right on a one-charger site. ocpp 0.11.2 replaced that fallback with
    an error, so every ``set_charge_rate`` on such an entry now raises and the
    charger runs unmanaged.

    The trigger is VALIDITY, not the shape of the stored string: anything the
    registry cannot resolve to a charge point is repaired, which covers the
    UUIDs, the serial numbers older ocpp releases stamped as a third device
    identifier, and a hand-typed value that no longer matches. Healthy entries
    are left alone, so this is a no-op on every boot after the first.

    The charger is re-identified through the stored Current.Import entity —
    the one anchor that stayed correct, since the scan maps sensors to charge
    points. ONLY the id is rewritten: the sensor entity keys are working, and
    a silent repair should not move them (re-pointing a charger deliberately
    does, but that is a user action on the options page).

    Returns the repaired charge point id, or None when nothing was changed.
    """
    stored = get_entry_value(entry, CONF_OCPP_DEVICE_ID, None)
    candidates = _ocpp_charger_candidates(hass)
    if not stored or stored in candidates:
        return None

    current_import = get_entry_value(entry, CONF_EVSE_CURRENT_IMPORT_ENTITY_ID, None)
    if not current_import:
        return None
    repaired = next(
        (
            cpid
            for cpid, group in sorted(candidates.items())
            if group["entities"].get("current_import_entity") == current_import
        ),
        None,
    )
    if repaired is None:
        # The registry cannot say which charger this is — a charger removed, or
        # renamed sensors on an entry that also lost its id. Leave it: a wrong
        # rewrite is worse than the error the user can see and act on.
        _LOGGER.warning(
            "Stored OCPP charge point id %r for %s matches no charger and could"
            " not be repaired from %s — OCPP commands will fail until the"
            " charger is re-selected on the options page",
            stored,
            entry.title,
            current_import,
        )
        return None

    # Options-first, exactly as ``get_entry_value`` reads it: a stale id in
    # options would shadow a repaired one in data.
    data = dict(entry.data)
    options = dict(entry.options)
    for store in (data, options):
        if CONF_OCPP_DEVICE_ID in store:
            store[CONF_OCPP_DEVICE_ID] = repaired
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    _LOGGER.info(
        "Repaired the OCPP charge point id for %s: %r is not a charge point,"
        " %r is (resolved from %s)",
        entry.title,
        stored,
        repaired,
        current_import,
    )
    return repaired


# Which stored entry key each payload sensor key lands on. ONE declaration, so
# the create wizard's final step and the options charger page can never move a
# different subset of a re-pointed charger's sensors.
_OCPP_ENTRY_KEY_BY_PAYLOAD = {
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "current_import_entity",
    CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID: "current_import_l1_entity",
    CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID: "current_import_l2_entity",
    CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID: "current_import_l3_entity",
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "current_offered_entity",
    CONF_EVSE_POWER_OFFERED_ENTITY_ID: "power_offered_entity",
    CONF_EVSE_POWER_IMPORT_ENTITY_ID: "power_import_entity",
}


def ocpp_entry_fields(payload: dict) -> dict:
    """The whole OCPP side of a charger entry, from one resolved payload.

    The charge point id every ocpp service call addresses plus every sensor
    entity key — as one dict, so re-pointing a charger is all-or-nothing on
    both edit paths. Absent sensors come back as None rather than missing, so
    a watts-only charger cannot inherit the previous charger's current sensor.
    """
    return {
        CONF_OCPP_DEVICE_ID: payload["device_id"],
        **{
            entry_key: payload.get(payload_key)
            for entry_key, payload_key in _OCPP_ENTRY_KEY_BY_PAYLOAD.items()
        },
    }


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


# ── the charge-control switch, for the runtime ─────────────────────────
#
# Cached beside the status sensor, in the same per-load bucket and with the
# same invalidation (see _RT_STATUS_ENTITY).
_RT_CHARGE_CONTROL_ENTITY = "_ocpp_charge_control_entity"

# The ocpp integration's switch unique_id: ``switch.ocpp.<cpid>[.conn<n>].<key>``
# — the same persisted-contract shape as the sensors', one platform deeper.
_OCPP_CHARGE_CONTROL_KEY = "charge_control"


def _ocpp_charge_control_entity_for(hass, charge_point_id: str | None) -> str | None:
    """This charge point's charge-control switch, or None if it has none.

    Classified off the switch's unique_id rather than composed, for the same
    reason the status sensor is: the entity_id is ``slugify(f"{cpid}_{key}")``,
    so any cpid the slug would alter — a capital, a space, a dot — composes a
    name that does not exist, and a multi-connector charger names the connector
    in it. The charger-level switch outranks a connector's, lowest connector
    number first, exactly as the metric grouping ranks its sensors.
    """
    if not charge_point_id:
        return None
    best_entity = None
    best_rank = None
    for entity_id, entity in sorted(async_get_entity_registry(hass).entities.items()):
        if not entity_id.startswith("switch."):
            continue
        parsed = _split_ocpp_switch_unique_id(entity.unique_id)
        if parsed is None:
            continue
        cpid, connector, key = parsed
        if cpid != charge_point_id or key != _OCPP_CHARGE_CONTROL_KEY:
            continue
        rank = connector or 0
        if best_rank is None or rank < best_rank:
            best_entity, best_rank = entity_id, rank
    return best_entity


def _split_ocpp_switch_unique_id(unique_id) -> tuple[str, int | None, str] | None:
    """``(charge point id, connector number, key)`` from an ocpp switch id.

    ``switch.ocpp.<cpid>[.conn<n>].<key>`` — the platform leads here, where the
    sensors' format trails it with ``.sensor``, so the two are split apart
    rather than sharing one parser.
    """
    if not unique_id:
        return None
    parts = str(unique_id).split(".")
    if len(parts) < 4 or parts[0] != "switch" or parts[1] != OCPP_INTEGRATION_DOMAIN:
        return None
    head = parts[2:-1]
    connector = None
    if len(head) > 1 and head[-1].startswith("conn") and head[-1][4:].isdigit():
        connector = int(head[-1][4:])
        head = head[:-1]
    charge_point_id = ".".join(head)
    if not charge_point_id:
        return None
    return charge_point_id, connector, parts[-1]


def ocpp_charge_control_entity(hass, entry) -> str:
    """The charge-control switch of the charger behind one load entry.

    Same shape, and the same reasoning, as ``ocpp_connector_status_entity``:
    resolved from the registry, with the legacy composed name kept as the
    fallback so a template-sensor site is unchanged. Nothing new is stored.
    """
    load_rt = (hass.data.get(DOMAIN, {}).get("loads") or {}).get(entry.entry_id)
    if load_rt is not None and _RT_CHARGE_CONTROL_ENTITY in load_rt:
        return load_rt[_RT_CHARGE_CONTROL_ENTITY]

    legacy_id = entry.data.get(CONF_CHARGER_ID) or entry.data.get(CONF_ENTITY_ID)
    charge_point_id = get_entry_value(entry, CONF_OCPP_DEVICE_ID, None) or legacy_id
    resolved = _ocpp_charge_control_entity_for(hass, charge_point_id)
    if resolved is None and charge_point_id != legacy_id:
        resolved = _ocpp_charge_control_entity_for(hass, legacy_id)
    if resolved is None:
        resolved = f"switch.{legacy_id}_{_OCPP_CHARGE_CONTROL_KEY}"

    if load_rt is not None:
        load_rt[_RT_CHARGE_CONTROL_ENTITY] = resolved
    return resolved
