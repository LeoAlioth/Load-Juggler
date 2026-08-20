import logging
from functools import partial
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_ID,
    CONF_GRID_EXPORT_LIMIT,
    CONF_HUB_ENTRY_ID,
    CONF_NAME,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_SITE_UPDATE_FREQUENCY,
    CONF_UPDATE_FREQUENCY,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DEFAULT_UPDATE_FREQUENCY,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_POWER_STATION,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_CHARGER,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
)
from .helpers import get_entry_value, hub_has_battery, fleet_has_forecast_sources
from .engine.hub_calculation import run_hub_calculation
from .entities.load import LoadJugglerDeviceSensor
from .entities.load_sensors import (
    LoadJugglerAllocatedCurrentSensor,
    LoadJugglerDeviceStatusSensor,
    LoadJugglerEffectivePrioritySensor,
    LoadJugglerPhaseMaskSensor,
    LoadJugglerPlugStatusSensor,
    LoadJugglerStationStatusSensor,
    LoadJugglerTankStatusSensor,
)
from .entities.hub import (
    LoadJugglerHubSensor,
    LoadJugglerHubStatusSensor,
    LoadJugglerHubDataSensor,
    HUB_SENSOR_DEFINITIONS,
    publish_hub_data,
)
from .entities.mixins import SITE_CYCLE_WORKERS, attach_site_cycle_listeners
from .entities.circuit_group import LoadJugglerCircuitGroupSensor
from .entities.inverter import (
    LoadJugglerInverterDataSensor,
    LoadJugglerInverterChargeControlSensor,
    LoadJugglerInverterSocControlSensor,
    INVERTER_SENSOR_DEFINITIONS,
)
from .control.inverter import soc_targets
from .registry import get_hub_for_charger

DynamicOcppEvseChargerSensor = LoadJugglerDeviceSensor
DynamicOcppEvseHubSensor = LoadJugglerHubSensor
DynamicOcppEvseHubDataSensor = LoadJugglerHubDataSensor

_LOGGER = logging.getLogger(__name__)

# No SCAN_INTERVAL: nothing on this platform polls. Every sensor here is either
# pushed by its hub's site cycle (entities/mixins.SiteCycleConsumerMixin), driven
# by it directly (a load's LoadJugglerDeviceSensor), or awaited by it as a
# site-cycle worker (entities/mixins.SiteCycleWorkerMixin).


async def async_run_hub_cycle(hass: HomeAssistant, hub_entry: ConfigEntry) -> dict:
    """Run ONE site cycle for a hub: calculate once, then serve every consumer.

    This is the hub coordinator's update method and the only thing that drives
    the engine. Running it per load (as the per-charger coordinators used to)
    advanced every cycle-counted mechanism in the engine — settle counters,
    input EMAs, power-stable counts — N times per interval on an N-load site.

    The order of the second half of the cycle is the contract:

    1. publish the result, which is what every reader on this hub then shows;
    2. load processors, awaited sequentially in entry_id order — two loads must
       never dispatch OCPP commands concurrently;
    3. site-cycle workers, awaited sequentially — entities whose per-cycle work
       is an await rather than a read.
    """
    hub_entry_id = hub_entry.entry_id

    # Adopt (or re-adopt) this hub's read-only sensors before the cycle runs, so
    # the state they publish at the end of it is this cycle's. Covers both a
    # child entry set up before its hub and a hub reload that replaced the
    # coordinator underneath already-loaded children — see
    # entities/mixins.attach_site_cycle_listeners.
    attach_site_cycle_listeners(
        hass,
        hub_entry_id,
        hass.data.get(DOMAIN, {}).get("hub_coordinators", {}).get(hub_entry_id),
    )

    hub_data = run_hub_calculation(hass, hub_entry)

    for notif in hub_data.get("auto_detect_notifications", []):
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": notif["title"],
                "message": notif["message"],
                "notification_id": notif["notification_id"],
            },
        )
        _LOGGER.warning("AutoDetect notification: %s", notif["notification_id"])

    published = publish_hub_data(hass, hub_entry_id, hub_data)

    processors = (
        hass.data.get(DOMAIN, {}).get("load_processors", {}).get(hub_entry_id, {})
    )
    for _entry_id, processor in sorted(processors.items()):
        await processor.async_process(hub_data)

    # Workers last, and on the PUBLISHED result rather than the raw one. The
    # inverter charge-limit write is the first of these, and it consumes
    # published["inverters"][…]["forecast_charge_limit_w"] — advice that only
    # exists once the cycle has produced it, which is why workers cannot run
    # before publish_hub_data. Handing over the same dict the readers see also
    # means the register write and the sensor reporting it are always from one
    # cycle. Awaiting them one at a time is what keeps a single writer per
    # register now that no platform poll serializes them.
    workers = (
        hass.data.get(DOMAIN, {}).get(SITE_CYCLE_WORKERS, {}).get(hub_entry_id, {})
    )
    for worker in list(workers.values()):
        await worker.async_run_site_cycle(published)

    return published


def _create_hub_coordinator(
    hass: HomeAssistant, config_entry: ConfigEntry, name: str
) -> DataUpdateCoordinator:
    """Build and register the hub's site-cycle coordinator."""
    site_update_frequency = get_entry_value(
        config_entry, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY
    )
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=config_entry,
        name=f"Load Juggler Site Cycle - {name}",
        update_method=partial(async_run_hub_cycle, hass, config_entry),
        update_interval=timedelta(seconds=site_update_frequency),
    )
    hass.data.setdefault(DOMAIN, {}).setdefault("hub_coordinators", {})[
        config_entry.entry_id
    ] = coordinator

    # A DataUpdateCoordinator only arms its timer while it has at least one
    # listener. The read-only sensors do subscribe now, but the site cycle must
    # run regardless of how many of them exist — the loads are driven from it
    # and they publish their own state at the end of async_process, never as
    # coordinator listeners. This keepalive is what makes the cycle independent
    # of its audience; it is released when the hub entry unloads (as is the
    # timer itself, via config_entry above and the explicit shutdown in
    # __init__.py's unload).
    @callback
    def _keepalive() -> None:
        """No state of its own — the cycle's effects are published elsewhere."""

    config_entry.async_on_unload(coordinator.async_add_listener(_keepalive))
    _LOGGER.info(
        "Site cycle for %s runs every %ss (one calculation per site, "
        "regardless of load count)",
        name,
        site_update_frequency,
    )
    return coordinator


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities
):
    """Set up the Load Juggler Sensor from a config entry."""
    entry_type = config_entry.data.get(ENTRY_TYPE)

    if entry_type == ENTRY_TYPE_HUB:
        name = config_entry.data.get(CONF_NAME, "Site Load Management")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "site_load_management")

        # Any battery on the fleet — the hub's legacy fields or an inverter
        # entry — enables the hub's (fleet-aggregate) battery sensors.
        has_battery = hub_has_battery(hass, config_entry)
        has_phase_b = bool(
            get_entry_value(config_entry, CONF_PHASE_B_CURRENT_ENTITY_ID, None)
        )
        has_phase_c = bool(
            get_entry_value(config_entry, CONF_PHASE_C_CURRENT_ENTITY_ID, None)
        )
        # PV clipping forecast needs all three of its inputs configured —
        # matches the gate in _compute_forecast_advice, so a disabled feature
        # creates no sensors rather than five permanently-unknown ones.
        # Fleet capacity: the hub's own (legacy) capacity plus every linked
        # inverter entry's — matches the engine's forecast gate.
        fleet_capacity = get_entry_value(config_entry, CONF_BATTERY_CAPACITY_KWH, 0) or 0
        for child in hass.config_entries.async_entries(DOMAIN):
            if (
                child.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
                and child.data.get(CONF_HUB_ENTRY_ID) == config_entry.entry_id
            ):
                fleet_capacity += get_entry_value(child, CONF_BATTERY_CAPACITY_KWH, 0) or 0
        has_forecast = (
            fleet_has_forecast_sources(hass, config_entry)
            and (get_entry_value(config_entry, CONF_GRID_EXPORT_LIMIT, 0) or 0) > 0
            and fleet_capacity > 0
        )

        entities = [
            LoadJugglerHubSensor(hass, config_entry, name, entity_id),
            LoadJugglerHubStatusSensor(hass, config_entry, name, entity_id),
        ]
        for defn in HUB_SENSOR_DEFINITIONS:
            if defn.get("requires_battery") and not has_battery:
                continue
            if defn.get("requires_phase") == "B" and not has_phase_b:
                continue
            if defn.get("requires_phase") == "C" and not has_phase_c:
                continue
            if defn.get("requires_forecast") and not has_forecast:
                continue
            entities.append(
                LoadJugglerHubDataSensor(hass, config_entry, name, entity_id, defn)
            )

        coordinator = _create_hub_coordinator(hass, config_entry, name)

        async_add_entities(entities)
        phases = "A" + ("B" if has_phase_b else "") + ("C" if has_phase_c else "")
        _LOGGER.info(
            f"Setting up hub sensors for {name} (battery={'yes' if has_battery else 'no'}, phases={phases})"
        )

        await coordinator.async_config_entry_first_refresh()
        return

    if entry_type == ENTRY_TYPE_GROUP:
        name = config_entry.data.get(CONF_NAME, "Circuit Group")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "circuit_group")
        sensor = LoadJugglerCircuitGroupSensor(hass, config_entry, name, entity_id)
        async_add_entities([sensor])
        _LOGGER.info("Setting up circuit group sensor for %s", name)
        return

    if entry_type == ENTRY_TYPE_INVERTER:
        name = config_entry.data.get(CONF_NAME, "Inverter")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "inverter")
        inv_has_battery = bool(
            get_entry_value(config_entry, CONF_BATTERY_SOC_ENTITY_ID, None)
        )
        # Forecast advice sensors need this battery to have a capacity AND
        # the hub's forecast to be enabled (export limit + forecast sources) —
        # matching the engine's per-inverter advice gate.
        hub_entry = hass.config_entries.async_get_entry(
            config_entry.data.get(CONF_HUB_ENTRY_ID)
        )
        inv_has_forecast = (
            inv_has_battery
            and (get_entry_value(config_entry, CONF_BATTERY_CAPACITY_KWH, 0) or 0) > 0
            and hub_entry is not None
            and (get_entry_value(hub_entry, CONF_GRID_EXPORT_LIMIT, 0) or 0) > 0
            # Any array on the fleet feeds the site forecast — the advice for
            # THIS battery does not require THIS inverter to own a forecast
            # device (an AC-coupled array's clipping is absorbed here too).
            and fleet_has_forecast_sources(hass, hub_entry)
        )
        entities = []
        for defn in INVERTER_SENSOR_DEFINITIONS:
            if defn.get("requires_battery") and not inv_has_battery:
                continue
            if defn.get("requires_forecast") and not inv_has_forecast:
                continue
            entities.append(
                LoadJugglerInverterDataSensor(hass, config_entry, entity_id, defn)
            )
        # Write-control status — created only with a target register, since
        # that sensor is also what drives the writes.
        writes_charge_limit = bool(
            get_entry_value(config_entry, CONF_CHARGE_LIMIT_ENTITY_ID, None)
        )
        if writes_charge_limit:
            entities.append(
                LoadJugglerInverterChargeControlSensor(hass, config_entry, entity_id)
            )
        # The SOC ceiling's own reporter, gated on its own targets. A pure reader
        # of what the charge-control worker records — the writes stay in that one
        # worker, so this adds a sensor and not a second writer.
        writes_soc_limit = bool(soc_targets(config_entry))
        if writes_soc_limit:
            entities.append(
                LoadJugglerInverterSocControlSensor(hass, config_entry, entity_id)
            )
        async_add_entities(entities)
        _LOGGER.info(
            "Setting up inverter sensors for %s (battery=%s, forecast=%s, "
            "charge control=%s, SOC control=%s)",
            name,
            "yes" if inv_has_battery else "no",
            "yes" if inv_has_forecast else "no",
            "yes" if writes_charge_limit else "no",
            "yes" if writes_soc_limit else "no",
        )
        return

    if entry_type != ENTRY_TYPE_CHARGER:
        _LOGGER.debug(
            "Skipping sensor setup for unknown entry type: %s", config_entry.title
        )
        return

    name = config_entry.data[CONF_NAME]
    entity_id = config_entry.data[CONF_ENTITY_ID]
    charger_entry_id = config_entry.entry_id

    hub_entry = get_hub_for_charger(hass, charger_entry_id)
    if not hub_entry:
        _LOGGER.error("No hub found for charger: %s", name)
        return

    # No coordinator here: the load is driven by its hub's site cycle, which it
    # joins by registering itself as a load processor when HA adds it (see
    # LoadJugglerDeviceSensor.async_added_to_hass). Its own update_frequency
    # still gates command dispatch inside async_process.
    _LOGGER.info(
        f"Setting up load {name} (site cycle: "
        f"{get_entry_value(hub_entry, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY)}s, "
        f"command rate: {get_entry_value(config_entry, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)}s)"
    )

    sensor = LoadJugglerDeviceSensor(hass, config_entry, hub_entry, name, entity_id)

    allocated_sensor = LoadJugglerAllocatedCurrentSensor(
        hass, config_entry, hub_entry, name, entity_id
    )
    effective_priority_sensor = LoadJugglerEffectivePrioritySensor(
        hass, config_entry, hub_entry, name, entity_id
    )

    device_type = config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
    if device_type == DEVICE_TYPE_HOT_WATER_TANK:
        status_sensor = LoadJugglerTankStatusSensor(
            hass, config_entry, hub_entry, name, entity_id
        )
    elif device_type == DEVICE_TYPE_PLUG:
        status_sensor = LoadJugglerPlugStatusSensor(
            hass, config_entry, hub_entry, name, entity_id
        )
    elif device_type == DEVICE_TYPE_POWER_STATION:
        status_sensor = LoadJugglerStationStatusSensor(
            hass, config_entry, hub_entry, name, entity_id
        )
    else:
        status_sensor = LoadJugglerDeviceStatusSensor(
            hass, config_entry, hub_entry, name, entity_id
        )
    entities = [sensor, allocated_sensor, effective_priority_sensor, status_sensor]

    # Phase mask sensor — only for 3-phase EVSEs (L1/L2/L3 mapped to 3 distinct
    # site phases). For 1-/2-phase loads the mask is trivial, so it is omitted.
    l1 = get_entry_value(config_entry, CONF_CHARGER_L1_PHASE, "A")
    l2 = get_entry_value(config_entry, CONF_CHARGER_L2_PHASE, "B")
    l3 = get_entry_value(config_entry, CONF_CHARGER_L3_PHASE, "C")
    if device_type == DEVICE_TYPE_EVSE and len({l1, l2, l3}) == 3:
        entities.append(
            LoadJugglerPhaseMaskSensor(hass, config_entry, hub_entry, name, entity_id)
        )

    async_add_entities(entities)

    # No per-charger options-update listener is registered here. Option changes
    # are handled centrally by _async_options_updated (in __init__.py), which
    # does a clean full reload of the entry — and, for a hub, of its chargers —
    # so a changed site_update_frequency is picked up by rebuilding the hub's
    # coordinator from scratch. A second listener that swapped the coordinator
    # in place raced with that reload and leaked the old coordinator's timer.
