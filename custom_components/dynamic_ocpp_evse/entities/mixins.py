"""Shared entity mixins for hub and charger entities.

Provides HubEntityMixin and ChargerEntityMixin to eliminate duplicated
device_info, _write_to_*_data, and state-restore boilerplate across
number.py, select.py, switch.py, sensor.py, and button.py.

Also holds the mixins that define how an entity joins its hub's site cycle
instead of running on a clock of its own:
:class:`SiteFreshnessMixin` (availability keyed on the producer),
:class:`SiteCycleConsumerMixin` (that, plus push updates instead of polling)
and :class:`SiteCycleWorkerMixin` (per-cycle work the coordinator must AWAIT).
"""

import logging
from datetime import datetime, timezone

from homeassistant.core import callback

from ..const import (
    DOMAIN,
    CONF_NAME,
    CONF_HUB_ENTRY_ID,
    CONF_DEVICE_TYPE,
    CONF_SITE_UPDATE_FREQUENCY,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_POWER_STATION,
)
from ..helpers import get_entry_value
from .. import units
from .freshness import is_producer_fresh

_LOGGER = logging.getLogger(__name__)

# hass.data[DOMAIN] bucket holding every site-cycle reader, keyed by hub entry
# id then by an opaque per-entity key. See attach_site_cycle_listeners.
SITE_CYCLE_LISTENERS = "site_cycle_listeners"

# hass.data[DOMAIN] bucket holding every site-cycle WORKER, same two-level
# shape. Deliberately a bucket of its own rather than a guest in
# ``load_processors``: that one is keyed by load entry, is walked in entry_id
# order because two loads must never dispatch OCPP commands concurrently, and
# every consumer of it reads its members as managed loads. A worker is none of
# those things — see SiteCycleWorkerMixin for the contract this bucket carries.
SITE_CYCLE_WORKERS = "site_cycle_workers"


@callback
def attach_site_cycle_listeners(hass, hub_entry_id, coordinator) -> None:
    """Bind every registered site-cycle reader of this hub to ``coordinator``.

    Called at the top of each site cycle (sensor.py), which makes one bucket
    answer both ways a reader can end up unsubscribed:

    * **Setup order.** A group, inverter or load entry can have its sensor
      platform set up before its hub's — so ``hub_coordinators[hub_entry_id]``
      may not exist yet when the entity is added. It registers here anyway and
      the hub's first tick adopts it, mirroring how loads join their hub via
      the ``load_processors`` registry.
    * **Hub reload.** Reloading only the hub entry (the UI's Reload button)
      shuts its coordinator down and builds a new one, while the children's
      entities stay loaded. Rebinding on every tick is what stops them from
      sitting on a dead subscription — frozen at their last value, and still
      claiming to be available.

    Re-binding an entity that is already on this coordinator is a no-op, so the
    per-tick cost is a dict walk.
    """
    if coordinator is None:
        return
    registry = (
        hass.data.get(DOMAIN, {}).get(SITE_CYCLE_LISTENERS, {}).get(hub_entry_id, {})
    )
    for entity in list(registry.values()):
        entity.bind_site_cycle_coordinator(coordinator)


class LoadJugglerEntity:
    """Constructor boilerplate that every Load Juggler entity repeats."""

    def _init_entity(self, hass, config_entry, name, unique_id) -> None:
        """Store the four things every one of our entities needs.

        The unique_id is passed in fully built, and stays spelled out at each
        call site: it is the identity HA stores in its registry, so it must be
        greppable next to the class that owns it.

        name=None is for entities named through ``_attr_translation_key``:
        assigning ``_attr_name = None`` would instead tell HA "this entity IS
        the device", so the attribute must stay unset for those.
        """
        self.hass = hass
        self.config_entry = config_entry
        if name is not None:
            self._attr_name = name
        self._attr_unique_id = unique_id

    def _hub_data(self) -> dict:
        """The site result this entity's hub last published (``{}`` if none).

        Every subclass mixin answers ``_site_hub_entry_id`` from its own entry
        shape — a hub entity is its own hub, everything else carries the hub's
        id in its entry data.
        """
        return (
            self.hass.data.get(DOMAIN, {})
            .get("hub_data", {})
            .get(self._site_hub_entry_id)
            or {}
        )


class SiteFreshnessMixin:
    """Availability keyed on the freshness of this entity's producer.

    A site-cycle reader holds no measurement of its own — it mirrors what the
    hub's engine last published. So it is available exactly while that
    publication is recent (see entities/freshness.py for the window), and
    unavailable before the first cycle and after the producer stops.

    ``_site_hub_entry_id`` and ``_hub_data()`` come from the device mixin
    (Hub/Charger/Group/Inverter via LoadJugglerEntity) — deliberately not
    redefined here, where they would win the MRO over the device's answer.
    """

    def _site_update_frequency(self):
        """The hub's configured site cycle length, in seconds."""
        hub_entry_id = self._site_hub_entry_id
        if not hub_entry_id:
            return DEFAULT_SITE_UPDATE_FREQUENCY
        if hub_entry_id == getattr(self.config_entry, "entry_id", None):
            hub_entry = self.config_entry
        else:
            hub_entry = self.hass.config_entries.async_get_entry(hub_entry_id)
        if hub_entry is None:
            return DEFAULT_SITE_UPDATE_FREQUENCY
        return get_entry_value(
            hub_entry, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY
        )

    def _is_fresh(self, last_update) -> bool:
        """True when ``last_update`` is inside this hub's freshness window."""
        return is_producer_fresh(
            last_update, self._site_update_frequency(), datetime.now(timezone.utc)
        )

    @property
    def available(self) -> bool:
        hub_data = self._hub_data()
        if not hub_data:
            return False
        return self._is_fresh(hub_data.get("last_update"))


class SiteCycleConsumerMixin(SiteFreshnessMixin):
    """A pure reader of its hub's site result: pushed, never polled.

    Subclasses implement ``_read_site_data()`` — a synchronous read of
    ``hass.data`` into ``_attr_*`` / private fields. This mixin owns everything
    around it: the coordinator subscription, the error containment, and the
    state write.

    Polling is off. The hub coordinator refreshes hub_data once per
    ``site_update_frequency``; a platform scan on its own 10 s clock could only
    re-read the same dict at the wrong moments — late for a fast site, and
    pointlessly often for a slow one.
    """

    _attr_should_poll = False

    # The coordinator this entity is currently subscribed to, and the
    # unsubscribe HA hands back for it.
    _site_cycle_coordinator = None
    _site_cycle_unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        hub_entry_id = self._site_hub_entry_id
        registry = (
            self.hass.data.setdefault(DOMAIN, {})
            .setdefault(SITE_CYCLE_LISTENERS, {})
            .setdefault(hub_entry_id, {})
        )
        key = id(self)
        registry[key] = self

        @callback
        def _deregister() -> None:
            registry.pop(key, None)
            if self._site_cycle_unsub is not None:
                self._site_cycle_unsub()
                self._site_cycle_unsub = None
                self._site_cycle_coordinator = None

        self.async_on_remove(_deregister)

        # Usually the hub is already up, so bind now rather than waiting a
        # tick; attach_site_cycle_listeners covers the case where it is not.
        coordinator = (
            self.hass.data.get(DOMAIN, {})
            .get("hub_coordinators", {})
            .get(hub_entry_id)
        )
        if coordinator is not None:
            self.bind_site_cycle_coordinator(coordinator)

        # Read once now, before HA publishes this entity's first state. Entities
        # are added asynchronously, so a cycle may already have run — without
        # this, a site on a 60 s cadence would show every reader as unavailable
        # for up to a minute after a restart, waiting for a push.
        self._refresh_from_site_data()

    @callback
    def bind_site_cycle_coordinator(self, coordinator) -> None:
        """Subscribe to ``coordinator``, replacing any earlier subscription."""
        if coordinator is self._site_cycle_coordinator:
            return
        if self._site_cycle_unsub is not None:
            self._site_cycle_unsub()
        self._site_cycle_unsub = coordinator.async_add_listener(
            self._handle_site_cycle_update
        )
        self._site_cycle_coordinator = coordinator

    @callback
    def _handle_site_cycle_update(self) -> None:
        """One site cycle finished: re-read the result and publish."""
        self._refresh_from_site_data()
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    async def async_update(self) -> None:
        """Re-read on demand.

        Polling never calls this. It stays because ``homeassistant.update_entity``
        does, and because the HA test tier drives these sensors directly.
        """
        self._refresh_from_site_data()

    def _refresh_from_site_data(self) -> None:
        """Run the subclass read, containing any error to this one entity."""
        try:
            self._read_site_data()
        except Exception as err:  # noqa: BLE001 - one bad sensor must not stop the rest
            _LOGGER.error(
                "Error updating %s: %s", self._attr_name, err, exc_info=True
            )

    def _read_site_data(self) -> None:
        """Read this entity's value out of hass.data. Implemented by subclasses."""
        raise NotImplementedError


class SiteCycleWorkerMixin:
    """An entity whose per-cycle work the site cycle has to AWAIT.

    The async counterpart of :class:`SiteCycleConsumerMixin`. A reader can ride
    a coordinator listener because re-reading a dict is synchronous. An entity
    whose work is an *await* — a Modbus register write, a service call — cannot:
    a listener is a plain callback, so it could only spawn a task per tick and
    then let those tasks race each other on a slow write.

    Such an entity therefore joins the cycle the way a load does
    (``entities/load.py``): it registers itself in a bucket that the hub
    coordinator walks from inside its own async body. Registration — not a
    coordinator reference — is the whole link, so a worker whose entry is set up
    before its hub's is simply picked up by the next tick, and the entry is
    released with the entity.

    What the coordinator guarantees (``sensor.py: async_run_hub_cycle``):

    * a worker is awaited **after** ``publish_hub_data``, and is handed the
      published result — the same dict this hub's readers see, so a worker can
      never act on a different cycle than the sensors reporting it;
    * the workers of one hub are awaited **sequentially**, so nothing here can
      overlap with itself or with another worker. This is what replaces the
      serialization a platform poll used to provide for free.

    Order *among* workers is insertion order and deliberately not promised: each
    one actuates its own device, so only the serialization matters.

    Subclasses implement ``_async_site_cycle_work(hub_data)``; this mixin owns
    the registration, the error containment and the state write.
    """

    # Polling is off by construction. A poll would be a second, unserialized
    # caller of the very work this mixin exists to keep inside the cycle.
    _attr_should_poll = False

    async def async_added_to_hass(self) -> None:
        """Join this hub's worker registry, and leave it when removed."""
        await super().async_added_to_hass()
        workers = (
            self.hass.data.setdefault(DOMAIN, {})
            .setdefault(SITE_CYCLE_WORKERS, {})
            .setdefault(self._site_hub_entry_id, {})
        )
        key = id(self)
        workers[key] = self

        @callback
        def _unregister() -> None:
            workers.pop(key, None)

        self.async_on_remove(_unregister)

    async def async_run_site_cycle(self, hub_data) -> None:
        """Do this cycle's work, then publish this entity's state.

        Errors are contained the way a load processor's are: one worker that
        raises must not stop its siblings, nor the cycle that called it.
        """
        try:
            await self._async_site_cycle_work(hub_data)
        except Exception as err:  # noqa: BLE001 - one bad worker must not stop the cycle
            _LOGGER.error(
                "Error updating %s: %s", self._attr_name, err, exc_info=True
            )
        # Polling is off and there is no coordinator listener, so this is the one
        # place this entity's state reaches HA. Skipped before HA has registered
        # it — a hub tick can precede the entity being added.
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    async def _async_site_cycle_work(self, hub_data) -> None:
        """This cycle's work for this entity. Implemented by subclasses."""
        raise NotImplementedError


def _apply_restored_number(entity, last_state):
    """Set ``entity._attr_native_value`` from ``last_state``, clamped to range.

    A restored state is just the last value HA saw — it predates any change to
    the entity's bounds. Reconfiguring a charger's min/max, or shipping a new
    default range, otherwise brings the slider back outside its own
    native_min/native_max: HA renders it out of range and every consumer
    downstream inherits an impossible number (issue #38). Anything unparseable
    or missing leaves the constructor's default in place.
    """
    if units.is_unavailable(last_state):
        return
    try:
        value = float(last_state.state)
    except (ValueError, TypeError):
        return
    # A NaN would survive the clamp below (min/max propagate it) and land on the
    # slider as a permanently broken value.
    if units.is_unusable_number(value):
        return
    low, high = entity._attr_native_min_value, entity._attr_native_max_value
    clamped = min(max(value, low), high)
    if clamped != value:
        _LOGGER.info(
            "%s: restored value %s is outside the current %s–%s range — clamped to %s",
            entity._attr_name, value, low, high, clamped,
        )
    entity._attr_native_value = clamped


class HubEntityMixin(LoadJugglerEntity):
    """Mixin for hub-level entities.

    Provides:
      - device_info property (Electrical System Hub)
      - _write_to_hub_data(value) using class attribute _hub_data_key
      - _restore_and_publish_number() for NumberEntity + RestoreEntity subclasses

    Subclasses must set _hub_data_key to the dict key in hass.data[DOMAIN]["hubs"][entry_id].
    """

    _hub_data_key = None

    @property
    def _site_hub_entry_id(self):
        """A hub entity's own entry IS the hub."""
        return self.config_entry.entry_id

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME, "Site Load Management"),
            "manufacturer": "Load Juggler",
            "model": "Electrical System Hub",
        }

    def _write_to_hub_data(self, value):
        """Write a value to hass.data[DOMAIN]['hubs'][entry_id][_hub_data_key]."""
        hub_data = self.hass.data.get(DOMAIN, {}).get("hubs", {}).get(self.config_entry.entry_id)
        if hub_data is not None:
            hub_data[self._hub_data_key] = value

    async def _restore_and_publish_number(self):
        """Restore a NumberEntity's last state and publish to shared hub data."""
        _apply_restored_number(self, await self.async_get_last_state())
        self.async_write_ha_state()
        self._write_to_hub_data(self._attr_native_value)


class ChargerEntityMixin(LoadJugglerEntity):
    """Mixin for charger-level entities.

    Provides:
      - device_info property (EV Charger / Smart Load, linked to hub)
      - _write_to_charger_data(value) using class attribute _charger_data_key
      - _restore_and_publish_number() for NumberEntity + RestoreEntity subclasses

    Subclasses must set _charger_data_key to the dict key in
    hass.data[DOMAIN]["chargers"][entry_id].

    Uses self.hub_entry if stored, otherwise looks up via get_hub_for_charger().
    """

    _charger_data_key = None

    @property
    def _site_hub_entry_id(self):
        return self.config_entry.data.get(CONF_HUB_ENTRY_ID)

    def _charger_runtime(self) -> dict:
        """This load's runtime dict in ``hass.data[DOMAIN]["chargers"]``.

        Written by the load's own processor and by the control/ modules (mode,
        dynamic_control, tank/station state); read by the load's diagnostic
        sensors. ``{}`` before the entry's setup has populated it.
        """
        return (
            self.hass.data.get(DOMAIN, {})
            .get("chargers", {})
            .get(self.config_entry.entry_id, {})
        )

    def _domain_bucket(self, key) -> dict:
        """One of the flat, domain-wide buckets in ``hass.data[DOMAIN]``."""
        return self.hass.data.get(DOMAIN, {}).get(key, {})

    @property
    def _hub_entry(self):
        """Get the hub ConfigEntry for this charger."""
        if hasattr(self, 'hub_entry') and self.hub_entry:
            return self.hub_entry
        from .. import get_hub_for_charger
        return get_hub_for_charger(self.hass, self.config_entry.entry_id)

    @property
    def device_info(self):
        device_type = self.config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        model = {
            DEVICE_TYPE_PLUG: "Smart Load",
            DEVICE_TYPE_HOT_WATER_TANK: "Hot Water Tank",
            DEVICE_TYPE_POWER_STATION: "Power Station",
        }.get(device_type, "EV Charger")
        hub = self._hub_entry
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Load Juggler",
            "model": model,
            "via_device": (DOMAIN, hub.entry_id) if hub else None,
        }

    def _write_to_charger_data(self, value):
        """Write a value to hass.data[DOMAIN]['chargers'][entry_id][_charger_data_key]."""
        charger_data = self.hass.data.get(DOMAIN, {}).get("chargers", {}).get(self.config_entry.entry_id)
        if charger_data is not None:
            charger_data[self._charger_data_key] = value

    async def _restore_and_publish_number(self):
        """Restore a NumberEntity's last state and publish to shared charger data."""
        _apply_restored_number(self, await self.async_get_last_state())
        self.async_write_ha_state()
        self._write_to_charger_data(self._attr_native_value)


class GroupEntityMixin(LoadJugglerEntity):
    """Mixin for circuit group entities.

    Provides:
      - device_info property (Circuit Group, linked to hub via via_device)
    """

    @property
    def _site_hub_entry_id(self):
        return self.config_entry.data.get(CONF_HUB_ENTRY_ID)

    @property
    def device_info(self):
        hub_entry_id = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Load Juggler",
            "model": "Circuit Group",
            "via_device": (DOMAIN, hub_entry_id) if hub_entry_id else None,
        }


class InverterEntityMixin(LoadJugglerEntity):
    """Mixin for inverter entities (a power source linked to a hub).

    Provides:
      - device_info property (Inverter, linked to hub via via_device)
      - _write_to_inverter_data(value) using class attribute _inverter_data_key
    """

    _inverter_data_key = None

    @property
    def _site_hub_entry_id(self):
        return self.config_entry.data.get(CONF_HUB_ENTRY_ID)

    def _inverter_runtime(self) -> dict:
        """This inverter's runtime dict in ``hass.data[DOMAIN]["inverters"]``."""
        return (
            self.hass.data.get(DOMAIN, {})
            .get("inverters", {})
            .get(self.config_entry.entry_id, {})
        )

    def _inverter_section(self, hub_data) -> dict:
        """This inverter's section of a given published site result.

        Split out from ``_my_inverter_data`` for the site-cycle worker, which is
        handed the published dict rather than fetching it — same extraction, so
        a worker and the sensors beside it cannot read the fleet aggregate two
        different ways.
        """
        return (hub_data or {}).get("inverters", {}).get(
            self.config_entry.entry_id
        ) or {}

    def _my_inverter_data(self) -> dict:
        """This inverter's section of the hub's published fleet aggregate."""
        return self._inverter_section(self._hub_data())

    @property
    def device_info(self):
        hub_entry_id = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Load Juggler",
            "model": "Inverter",
            "via_device": (DOMAIN, hub_entry_id) if hub_entry_id else None,
        }

    def _write_to_inverter_data(self, value):
        """Write to hass.data[DOMAIN]['inverters'][entry_id][_inverter_data_key].

        setdefault rather than a lookup: the switch can restore its state
        before the inverter entry's own setup has populated the bucket.
        """
        inverters = self.hass.data.setdefault(DOMAIN, {}).setdefault("inverters", {})
        inverters.setdefault(self.config_entry.entry_id, {})[
            self._inverter_data_key
        ] = value
