from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.config_entries import (
    ConfigEntry,
    SOURCE_IMPORT,
    SOURCE_INTEGRATION_DISCOVERY,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers import entity_registry as er
import logging
import operator
import voluptuous as vol
from .const import (
    ALL_OPERATING_MODE_KEYS,
    CHARGE_RATE_UNIT_AUTO,
    CHARGE_RATE_UNIT_WATTS,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_CHARGE_PAUSE_DURATION,
    CONF_CHARGE_RATE_UNIT,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EXCESS_EXPORT_THRESHOLD,
    CONF_EXCESS_TRIGGER_MARGIN,
    CONF_GRID_EXPORT_LIMIT,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_LOAD_PRIORITY,
    CONF_OCPP_DEVICE_ID,
    CONF_OCPP_PROFILE_TIMEOUT,
    CONF_PHASES,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_STACK_LEVEL,
    CONF_UPDATE_FREQUENCY,
    DEFAULT_BATTERY_MAX_POWER,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_BATTERY_SOC_MIN,
    DEFAULT_BATTERY_SOC_TARGET,
    DEFAULT_CHARGE_CONTROL_DEADBAND_W,
    DEFAULT_CHARGE_PAUSE_DURATION,
    DEFAULT_CHARGE_RATE_UNIT,
    DEFAULT_DISTRIBUTION_MODE,
    DEFAULT_EXCESS_EXPORT_THRESHOLD,
    DEFAULT_EXCESS_TRIGGER_MARGIN,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_OCPP_PROFILE_TIMEOUT,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_PROFILE_VALIDITY_MODE,
    DEFAULT_STACK_LEVEL,
    DEFAULT_UPDATE_FREQUENCY,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_PLUG,
    DISTRIBUTION_MODES,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
    EVSE_RT_COMMANDED_LIMIT,
    MIGRATE_PLUG_SOLAR_ONLY_FLAG,
    modes_for,
    CONF_INVERTER_FEATURES,
)
from .helpers import (
    get_entry_value,
    infer_inverter_features,
    strip_unfeatured_inverter_options,
)
from . import units
from .ocpp_discovery import repair_ocpp_device_id, scan_ocpp_chargers
from .registry import follow_renames

_LOGGER = logging.getLogger(__name__)

# how long renamed entities are gathered before they are followed
RENAME_SETTLE_S = 5.0

# The platforms each entry type forwards to - set up and unloaded alike.
PLATFORMS = {
    ENTRY_TYPE_HUB: ["number", "switch", "sensor", "select"],
    ENTRY_TYPE_LOAD: ["sensor", "number", "button", "select", "switch"],
    ENTRY_TYPE_GROUP: ["sensor"],
    # Sensors plus the write-control opt-in switches
    ENTRY_TYPE_INVERTER: ["sensor", "switch"],
}

# Define the config schema
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# The stored strings the generic charger → load rename replaced (2.4 → 2.5).
# Named here rather than in const/ because nothing outside the migration may
# read or write them: they exist only so entries written before the rename can
# still be recognised, both by the 2.5 step and by the older steps below that
# have to inspect an entry_type predating it.
_LEGACY_ENTRY_TYPE_CHARGER = "charger"
_LEGACY_CONF_CHARGER_PRIORITY = "charger_priority"
# The write deadband when it was a percentage of the normal value (2.5 → 2.6).
_LEGACY_CONF_CHARGE_CONTROL_DEADBAND = "inverter_charge_control_deadband"


# The options a 2.x entry starts from, seeded from its data (by the v1 → v2
# step and by the 2.0 → 2.1 one): (key, default when data lacks it too).
_V2_SEEDED_OPTIONS = (
    (CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT),
    (CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT),
    (CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY),
    (CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT),
    (CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION),
    (CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL),
    (CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT),
    (CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE),
    (CONF_BATTERY_SOC_ENTITY_ID, None),
    (CONF_BATTERY_POWER_ENTITY_ID, None),
    (CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER),
    (CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER),
    (CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS),
)


def _seed_v2_options(options: dict, data) -> None:
    for key, default in _V2_SEEDED_OPTIONS:
        options.setdefault(key, data.get(key, default))


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entry to new version."""
    _LOGGER.info("Migrating from version %s.%s to version 2.5",
                 entry.version,
                 getattr(entry, 'minor_version', 0))

    if entry.version < 2:
        # Migrate from V1 (single config) to V2 (hub + load architecture)
        new_data = dict(entry.data)
        
        # Mark this as a hub entry (legacy entries become hubs)
        new_data[ENTRY_TYPE] = ENTRY_TYPE_HUB
        
        # Update the config entry with new version
        options = dict(entry.options)
        _seed_v2_options(options, new_data)

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options=options,
            version=2,
            minor_version=2
        )

        _LOGGER.info(
            "Migration to version 2.2 successful. Legacy entry converted to hub. "
            "You will need to add loads separately after migration."
        )
        # No return: async_update_entry mutates the entry in place, so the
        # minor-version steps below see 2.2 and run in this same pass.

    # Handle minor version updates if version is already 2
    if entry.version == 2 and getattr(entry, 'minor_version', 0) < 1:
        options = dict(entry.options)
        _seed_v2_options(options, entry.data)

        hass.config_entries.async_update_entry(
            entry,
            options=options,
            minor_version=1
        )
        _LOGGER.info("Updated minor version to 1 and seeded options")

    # Migrate 2.1 → 2.2: convert charge_pause_duration from seconds to minutes
    if entry.version == 2 and getattr(entry, 'minor_version', 0) < 2:
        options = dict(entry.options)
        old_pause = options.get(CONF_CHARGE_PAUSE_DURATION)
        if old_pause is not None and old_pause > 10:
            # Value is in seconds (old format) - convert to minutes
            new_pause = max(1, round(old_pause / 60))
            options[CONF_CHARGE_PAUSE_DURATION] = new_pause
            _LOGGER.info("Migrated charge_pause_duration from %ds to %dmin", old_pause, new_pause)

        hass.config_entries.async_update_entry(
            entry,
            options=options,
            minor_version=2
        )
        _LOGGER.info("Updated minor version to 2")

    # Migrate 2.2 → 2.3: the smart-plug "Solar Only" mode was split. Its old
    # behavior (run while battery SOC > minimum) is now "Solar Priority", and
    # the key "Solar Only" was reused for a new target-gated mode. Flag plug
    # load entries so the operating-mode select migrates its restored
    # "Solar Only" state to "Solar Priority" exactly once (see select.py).
    if entry.version == 2 and getattr(entry, 'minor_version', 0) < 3:
        new_data = dict(entry.data)
        # An entry this old still stores the pre-rename entry_type, so match
        # the legacy value as well as the current one.
        if (
            entry.data.get(ENTRY_TYPE)
            in (ENTRY_TYPE_LOAD, _LEGACY_ENTRY_TYPE_CHARGER)
            and entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_PLUG
        ):
            new_data[MIGRATE_PLUG_SOLAR_ONLY_FLAG] = True
        hass.config_entries.async_update_entry(
            entry, data=new_data, minor_version=3
        )
        _LOGGER.info("Updated minor version to 3")

    # Migrate 2.3 → 2.4: the Excess export threshold and the grid export limit
    # collapsed into ONE field. `grid_export_limit` is now the physical export
    # ceiling; the Excess trigger derives from it as limit − trigger margin
    # (default 500 W). Seed the limit as old threshold + margin so the
    # effective trigger point does not move. Only grid-tied hubs (≥1 grid CT)
    # are seeded - off-grid Excess is battery-side only, and a seeded limit
    # would wrongly enable the clipping forecast maths there.
    if entry.version == 2 and getattr(entry, 'minor_version', 0) < 4:
        options = dict(entry.options)
        is_hub = entry.data.get(ENTRY_TYPE, ENTRY_TYPE_HUB) == ENTRY_TYPE_HUB
        has_grid_cts = any(
            get_entry_value(entry, conf, None)
            for conf in (
                CONF_PHASE_A_CURRENT_ENTITY_ID,
                CONF_PHASE_B_CURRENT_ENTITY_ID,
                CONF_PHASE_C_CURRENT_ENTITY_ID,
            )
        )
        if is_hub and has_grid_cts and not options.get(CONF_GRID_EXPORT_LIMIT):
            old_threshold = get_entry_value(
                entry, CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD
            )
            options[CONF_GRID_EXPORT_LIMIT] = (
                old_threshold + DEFAULT_EXCESS_TRIGGER_MARGIN
            )
            options.setdefault(
                CONF_EXCESS_TRIGGER_MARGIN, DEFAULT_EXCESS_TRIGGER_MARGIN
            )
            _LOGGER.info(
                "Migrated excess_export_threshold %sW to grid_export_limit %sW"
                " (trigger stays at limit - %sW margin)",
                old_threshold,
                options[CONF_GRID_EXPORT_LIMIT],
                DEFAULT_EXCESS_TRIGGER_MARGIN,
            )
        hass.config_entries.async_update_entry(
            entry, options=options, minor_version=4
        )
        _LOGGER.info("Updated minor version to 4")

    # Migrate 2.4 → 2.5: "charger" was the codebase's generic word for a
    # managed device, but a smart plug, a hot water tank and a power station
    # are not chargers. The stored strings follow the code rename: the
    # entry_type VALUE "charger" becomes "load", and the priority KEY
    # "charger_priority" becomes "load_priority" in both data and options.
    #
    # Idempotent by construction - each rewrite is conditional on the legacy
    # spelling still being present - and load-scoped: a hub, inverter or group
    # entry carries neither, so it passes through with only its minor_version
    # bumped. CONF_CHARGER_ID is deliberately NOT touched: it holds the OCPP
    # charge-point identifier, which really is a charger's.
    if entry.version == 2 and getattr(entry, 'minor_version', 0) < 5:
        data = dict(entry.data)
        options = dict(entry.options)
        changed = []

        if data.get(ENTRY_TYPE) == _LEGACY_ENTRY_TYPE_CHARGER:
            data[ENTRY_TYPE] = ENTRY_TYPE_LOAD
            changed.append(f"{ENTRY_TYPE}={ENTRY_TYPE_LOAD}")

        for store, label in ((data, "data"), (options, "options")):
            if _LEGACY_CONF_CHARGER_PRIORITY not in store:
                continue
            value = store.pop(_LEGACY_CONF_CHARGER_PRIORITY)
            # A half-migrated entry (both spellings present) keeps the new
            # key's value - it is the one every reader already uses.
            store.setdefault(CONF_LOAD_PRIORITY, value)
            changed.append(f"{label}.{CONF_LOAD_PRIORITY}")

        hass.config_entries.async_update_entry(
            entry, data=data, options=options, minor_version=5
        )
        if changed:
            _LOGGER.info(
                "Migrated %s to the load naming: %s", entry.title, ", ".join(changed)
            )
        _LOGGER.info("Updated minor version to 5")

    # Migrate 2.5 → 2.6: the charge-register write deadband stopped being a
    # percentage of the normal value and became an absolute figure in watts
    # (CONF_CHARGE_CONTROL_DEADBAND_W). The two cannot be converted here - the
    # normal value is an entity read, not a stored number - and reading the old
    # number as watts would be far worse than dropping it: a stored 5 would mean
    # 5 W, which is no deadband at all on registers that go over Modbus and in
    # some firmwares to EEPROM. So the legacy key is dropped and the new default
    # applies; an inverter whose deadband was deliberately tuned needs it set
    # again, which the release notes say.
    #
    # Inverter-scoped and idempotent: only an entry still carrying the legacy
    # spelling is touched, and every other entry type passes through with just
    # its minor_version bumped.
    if entry.version == 2 and getattr(entry, "minor_version", 0) < 6:
        data = dict(entry.data)
        options = dict(entry.options)
        dropped = [
            label
            for store, label in ((data, "data"), (options, "options"))
            if store.pop(_LEGACY_CONF_CHARGE_CONTROL_DEADBAND, None) is not None
        ]
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, minor_version=6
        )
        if dropped:
            _LOGGER.info(
                "%s: the percentage write deadband was dropped from %s - the"
                " setting is now absolute watts, defaulting to %sW",
                entry.title,
                ", ".join(dropped),
                DEFAULT_CHARGE_CONTROL_DEADBAND_W,
            )
        _LOGGER.info("Updated minor version to 6")

    # 2.7: inverter entries declare their FEATURES (PV array / battery /
    # battery write-control) on the first page of their setup, and the pages
    # for undeclared sections are not shown. Entries from before the list
    # existed get it inferred from what they had configured, and the keys of
    # every undeclared section are cleared - the form had been saving *Battery
    # max charge power* at its default on PV-only entries, and that phantom
    # pack took a share of every fleet sum (2026-09-03, live).
    if entry.version == 2 and getattr(entry, "minor_version", 0) < 7:
        options = dict(entry.options)
        if (
            entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
            and CONF_INVERTER_FEATURES not in options
        ):
            features = infer_inverter_features({**entry.data, **options})
            options[CONF_INVERTER_FEATURES] = features
            strip_unfeatured_inverter_options(options, features)
            _LOGGER.info(
                "%s: inverter features inferred as %s", entry.title, features or "none"
            )
        hass.config_entries.async_update_entry(entry, options=options, minor_version=7)
        _LOGGER.info("Updated minor version to 7")

    # 2.8: drop the pre-2.4 export threshold. The <4 step above derived
    # ``grid_export_limit`` from it (limit = threshold + trigger margin) and
    # nothing has read it since; it only survived because that step copied the
    # options forward wholesale. Pruned AFTER the derivation, so a 2.1 entry
    # still reaches 2.8 correctly in the one migration pass.
    if entry.version == 2 and getattr(entry, "minor_version", 0) < 8:
        data = dict(entry.data)
        options = dict(entry.options)
        dropped = [
            label
            for store, label in ((data, "data"), (options, "options"))
            if store.pop(CONF_EXCESS_EXPORT_THRESHOLD, None) is not None
        ]
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, minor_version=8
        )
        if dropped:
            _LOGGER.info(
                "%s: dropped the legacy export threshold from %s - the export"
                " limit has been its own setting since 2.4",
                entry.title,
                ", ".join(dropped),
            )
        _LOGGER.info("Updated minor version to 8")

    return True


def _charger_phase_count(entry: ConfigEntry) -> int:
    """How many site phases a charger draws on, from its own config.

    Used to encode a Watts-mode limit (A × V × phases), so guessing high
    overshoots the charger by that factor - a 1-phase charger asked to reset to
    a 3-phase minimum gets three times the current it should.

    No flow ever writes CONF_PHASES, so it is honored only when actually
    present (a service/YAML override) and the count otherwise comes from what
    the charger entry does store: its L1/L2/L3 → site phase mapping. The setup
    and reconfigure steps collapse the hidden mappings onto L1's phase on a
    1-/2-phase site, so the number of DISTINCT mapped phases is the charger's
    phase count as the site sees it. Nothing mapped at all falls back to 1 -
    under-encoding a limit is the safe direction.
    """
    configured = get_entry_value(entry, CONF_PHASES, None)
    if configured:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            _LOGGER.debug("Ignoring non-numeric %s: %r", CONF_PHASES, configured)

    mapped = {
        get_entry_value(entry, key, None)
        for key in (
            CONF_CHARGER_L1_PHASE,
            CONF_CHARGER_L2_PHASE,
            CONF_CHARGER_L3_PHASE,
        )
    }
    mapped.discard(None)
    return max(1, len(mapped))


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Load Juggler component."""
    
    async def handle_reset_service(call):
        """Handle the reset service call."""
        entry_id = call.data.get("entry_id")
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return

        # Get the OCPP device ID (options first - the reconfigure/options flow
        # writes an edited device ID to entry.options, so reading entry.data
        # would keep resetting the charger the user renamed away from)
        ocpp_device_id = get_entry_value(entry, CONF_OCPP_DEVICE_ID, None)
        if not ocpp_device_id:
            _LOGGER.error(f"No OCPP device ID configured for entry {entry.title} - cannot reset")
            return

        # The charger's minimum current, in amps unless the charger takes watts
        # (configured, or auto-detected from its current-offered sensor's unit)
        # and the hub's voltage is there to convert with.
        limit_for_charger = get_entry_value(entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
        rate_unit = "A"
        charge_rate_unit = get_entry_value(entry, CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT)
        if charge_rate_unit == CHARGE_RATE_UNIT_AUTO:
            offered = get_entry_value(entry, CONF_EVSE_CURRENT_OFFERED_ENTITY_ID, None)
            offered_state = hass.states.get(offered) if offered else None
            if offered_state and offered_state.attributes.get("unit_of_measurement") == "W":
                charge_rate_unit = CHARGE_RATE_UNIT_WATTS
        hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
        hub_entry = hass.config_entries.async_get_entry(hub_entry_id) if hub_entry_id else None
        if charge_rate_unit == CHARGE_RATE_UNIT_WATTS and hub_entry:
            voltage = (
                get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
                or DEFAULT_PHASE_VOLTAGE
            )
            limit_for_charger = round(limit_for_charger * voltage * _charger_phase_count(entry), 1)
            rate_unit = "W"

        # Stack level for reset should be 1 lower than regular operation
        configured_stack_level = int(get_entry_value(entry, CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL))
        reset_stack_level = max(1, configured_stack_level - 1)

        sequence = [
            {
                "action": "ocpp.clear_profile",
                "target": {},
                "data": {"devid": ocpp_device_id}
            },
            {"delay": {"seconds": 10}},
            {
                "action": "ocpp.set_charge_rate",
                "target": {},
                "data": {
                    "devid": ocpp_device_id,
                    "custom_profile": {
                        "chargingProfileId": 10,
                        "stackLevel": reset_stack_level,
                        "chargingProfileKind": "Relative",
                        "chargingProfilePurpose": "TxDefaultProfile",
                        "chargingSchedule": {
                            "chargingRateUnit": rate_unit,
                            "chargingSchedulePeriod": [
                                {"startPeriod": 0, "limit": limit_for_charger}
                            ]
                        }
                    }
                }
            }
        ]
        # From here the charger no longer holds the limit its load last
        # recorded as accepted: clear_profile hands it back to its own default
        # until a profile lands again. Unknown is the honest state for the
        # stuck-readout watch (engine/readout_watch.py), which judges nothing
        # against an unknown limit - the next accepted command re-arms it.
        load_rt = hass.data.get(DOMAIN, {}).get("loads", {}).get(entry_id)
        if load_rt is not None:
            load_rt.pop(EVSE_RT_COMMANDED_LIMIT, None)

        script = Script(hass, sequence, "Reset OCPP EVSE", DOMAIN)
        await script.async_run(context=call.context)

        # The charger's reporting intervals, set to this load's update
        # frequency: MeterValueSampleInterval paces the meter values of a
        # transaction, ClockAlignedDataInterval the ones a charger sends
        # without one (the go-eCharger of the 2026-10-03 field case runs no
        # transaction: 120 s between readings). The ocpp integration writes
        # its own Meter interval / Idle interval options to these two keys on
        # every connect, so a reset re-applies Load Juggler's. Its configure
        # reads the key first and writes only a different value. Each key on
        # its own: one the charger does not know or refuses, or a charger that
        # is offline, is logged and the reset carries on.
        interval = str(int(get_entry_value(
            entry, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY
        )))
        for key in ("MeterValueSampleInterval", "ClockAlignedDataInterval"):
            try:
                response = await hass.services.async_call(
                    "ocpp",
                    "configure",
                    {"devid": ocpp_device_id, "ocpp_key": key, "value": interval},
                    blocking=True,
                    return_response=True,
                )
            except Exception as err:
                _LOGGER.warning(
                    "%s: could not set the charger's %s to %s s: %s",
                    entry.title, key, interval, err,
                )
                continue
            _LOGGER.info(
                "%s: sent the charger %s = %s s%s",
                entry.title, key, interval,
                " (the charger needs a reboot to apply it)"
                if isinstance(response, dict) and response.get("reboot_required")
                else "",
            )

    hass.services.async_register(DOMAIN, "reset_ocpp_evse", handle_reset_service)

    def _own_entity(entry_id: str, platform: str, suffix: str):
        """The entity_id of one of an entry's own entities, or None.

        Its unique_id is the entry's entity-id prefix plus ``suffix``.
        """
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return None
        return er.async_get(hass).async_get_entity_id(
            platform, DOMAIN, f"{entry.data.get(CONF_ENTITY_ID)}{suffix}"
        )

    # How each platform's entity is set: its service, and that service's value key.
    setters = {"select": ("select_option", "option"), "number": ("set_value", "value")}

    def _forward(suffix, platform, field, sibling=None):
        """A service handing ``call.data[field]`` to one of the entry's own
        entities, through that platform's own (validating) set service.

        ``sibling`` is ``(suffix, crosses)`` for the min/max current pair: the
        sliders are independent entities, so a service call could otherwise
        leave the engine with min > max - a value that ``crosses(value,
        sibling_value)`` is rejected.
        """
        service, key = setters[platform]

        async def handle(call: ServiceCall):
            entry_id = call.data["entry_id"]
            value = call.data[field]
            entity_id = _own_entity(entry_id, platform, suffix)
            if not entity_id:
                _LOGGER.error("Could not find the %s entity of %s", suffix[1:].replace("_", " "), entry_id)
                return
            if sibling is not None:
                other_suffix, crosses = sibling
                other = units.read_number(
                    hass, _own_entity(entry_id, "number", other_suffix)
                )
                if other is not None and crosses(value, other):
                    _LOGGER.error(
                        "%s for %s rejected: %.1fA crosses %s %.1fA",
                        call.service, entry_id, value, other_suffix[1:].replace("_", " "), other,
                    )
                    return
            await hass.services.async_call(
                platform, service, {"entity_id": entity_id, key: value}, blocking=True
            )

        return handle

    current = vol.Schema({
        vol.Required("entry_id"): cv.string,
        vol.Required("current"): vol.Coerce(float),
    })
    for service, handler, schema in (
        ("set_operating_mode", _forward("_operating_mode", "select", "mode"),
         vol.Schema({
             vol.Required("entry_id"): cv.string,
             vol.Required("mode"): vol.In(ALL_OPERATING_MODE_KEYS),
         })),
        ("set_distribution_mode", _forward("_distribution_mode", "select", "mode"),
         vol.Schema({
             vol.Required("entry_id"): cv.string,
             vol.Required("mode"): vol.In(DISTRIBUTION_MODES),
         })),
        ("set_max_current", _forward("_max_current", "number", "current", ("_min_current", operator.lt)), current),
        ("set_min_current", _forward("_min_current", "number", "current", ("_max_current", operator.gt)), current),
    ):
        hass.services.async_register(DOMAIN, service, handler, schema=schema)

    # Renamed entities: gathered for a few seconds - a rename tool changes
    # dozens at once - then written into every entry that names them, each
    # of which then reloads. Registered here rather than per entry so a
    # rename landing while an entry reloads is not missed.
    pending: dict = {}

    async def _follow() -> None:
        renames = dict(pending)
        pending.clear()
        for entry in hass.config_entries.async_entries(DOMAIN):
            data, options = follow_renames(dict(entry.data), renames), follow_renames(dict(entry.options), renames)
            if data != dict(entry.data) or options != dict(entry.options):
                _LOGGER.info("Following renamed entities in %s: %s", entry.title, renames)
                hass.config_entries.async_update_entry(entry, data=data, options=options)

    flush = Debouncer(hass, _LOGGER, cooldown=RENAME_SETTLE_S, immediate=False, function=_follow)

    @callback
    def _renamed(event) -> None:
        old, new = event.data.get("old_entity_id"), event.data.get("entity_id")
        if event.data.get("action") != "update" or not old or not new or old == new:
            return
        for k, v in list(pending.items()):   # renamed twice before the flush: a to b to c
            if v == old:
                pending[k] = new
        pending[old] = new
        hass.async_create_task(flush.async_call())

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _renamed)

    return True


# Runtime-only bucket in hass.data[DOMAIN]: config entry ids whose
# operating-mode select still owes the one-time 2.2 → 2.3 plug remap.
PENDING_PLUG_MODE_MIGRATION = "pending_plug_mode_migration"


def consume_plug_mode_migration(hass: HomeAssistant, entry_id: str) -> bool:
    """Claim the one-time plug operating-mode migration for ``entry_id``.

    Returns True at most once per entry, for the operating-mode select that
    restores a stale "Solar Only" plug state (see select.py).

    Why a runtime marker instead of reading the persisted flag directly: the
    select used to clear MIGRATE_PLUG_SOLAR_ONLY_FLAG from entry.data inside
    async_added_to_hass, and async_update_entry fires the entry's update
    listener → a reload of an entry that may still be SETUP_IN_PROGRESS
    (OperationNotAllowed). async_setup_entry now does the persisted-flag
    bookkeeping at a point where no update listener is registered yet, and
    hands the one-shot to the select through this in-memory marker.
    """
    pending = hass.data.get(DOMAIN, {}).get(PENDING_PLUG_MODE_MIGRATION)
    if not pending or entry_id not in pending:
        return False
    pending.discard(entry_id)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Load Juggler from a config entry."""
    hass.data.setdefault(DOMAIN, {
        "hubs": {},
        "loads": {},
        "inverters": {},  # Inverter entries (power sources, optional battery)
        "load_allocations": {},  # Stores current allocation for each load
    })
    # setdefault only fires once - older buckets may predate "inverters"
    hass.data[DOMAIN].setdefault("inverters", {})
    
    entry_type = entry.data.get(ENTRY_TYPE)
    
    # Handle legacy entries (without entry_type) - treat as hub
    if not entry_type:
        _LOGGER.info("Migrating legacy config entry to hub type")
        new_data = dict(entry.data)
        new_data[ENTRY_TYPE] = ENTRY_TYPE_HUB
        hass.config_entries.async_update_entry(entry, data=new_data)
        entry_type = ENTRY_TYPE_HUB

    # Hand the pending one-time plug operating-mode remap (2.2 → 2.3) to the
    # select as an in-memory marker BEFORE platforms are set up. Idempotent, so
    # a ConfigEntryNotReady retry below simply re-arms it.
    pending_plug_migration = MIGRATE_PLUG_SOLAR_ONLY_FLAG in entry.data
    if pending_plug_migration:
        hass.data[DOMAIN].setdefault(PENDING_PLUG_MODE_MIGRATION, set()).add(
            entry.entry_id
        )

    # Every child entry needs its hub up first. HA sets up config entries
    # concurrently in arbitrary order, so the hub may not be ready yet -
    # ConfigEntryNotReady has HA retry the child once it has finished.
    if entry_type in (ENTRY_TYPE_LOAD, ENTRY_TYPE_GROUP, ENTRY_TYPE_INVERTER):
        hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
        if hub_entry_id not in hass.data[DOMAIN]["hubs"]:
            raise ConfigEntryNotReady(
                f"Hub {hub_entry_id} not ready for {entry_type} {entry.title}"
            )

    if entry_type == ENTRY_TYPE_HUB:
        await _setup_hub_entry(hass, entry)
    elif entry_type == ENTRY_TYPE_LOAD:
        await _setup_load_entry(hass, entry)
    elif entry_type == ENTRY_TYPE_GROUP:
        await _setup_group_entry(hass, entry)
    elif entry_type == ENTRY_TYPE_INVERTER:
        await _setup_inverter_entry(hass, entry)

    # Strip the persisted flag now that the select has had its chance: platform
    # setup above is awaited, so the select already ran async_added_to_hass.
    # This is deliberately AFTER the platform forward (a setup that raises
    # ConfigEntryNotReady leaves the flag in place for the retry) and BEFORE the
    # update listener is registered below - async_update_entry fires update
    # listeners, and doing this from inside entity setup reloaded an entry that
    # could still be SETUP_IN_PROGRESS (issue #34).
    if pending_plug_migration:
        hass.config_entries.async_update_entry(
            entry,
            data={
                k: v
                for k, v in entry.data.items()
                if k != MIGRATE_PLUG_SOLAR_ONLY_FLAG
            },
        )

    # Reload entry when options change (e.g. battery entities added/removed)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry):
    """Reload the config entry when options are changed.

    For a hub, also reload its loads and groups so hub-level settings
    (e.g. site_update_frequency) propagate to them via a clean rebuild.
    """
    await hass.config_entries.async_reload(entry.entry_id)

    if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_HUB:
        for child in hass.config_entries.async_entries(DOMAIN):
            if child.data.get(CONF_HUB_ENTRY_ID) == entry.entry_id:
                await hass.config_entries.async_reload(child.entry_id)


async def _setup_hub_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up a hub config entry."""
    _LOGGER.info("Setting up hub entry: %s", entry.title)
    
    # Store hub data (runtime state written by entities, read by calculation)
    hass.data[DOMAIN]["hubs"][entry.entry_id] = {
        "entry": entry,
        "loads": [],  # List of load entry_ids linked to this hub
        "distribution_mode": DEFAULT_DISTRIBUTION_MODE,
        "allow_grid_charging": True,
        "power_buffer": 0,
        "max_import_power": None,
        "battery_soc_target": DEFAULT_BATTERY_SOC_TARGET,
        "battery_soc_min": DEFAULT_BATTERY_SOC_MIN,
    }

    # A hub RELOAD rebuilds the dict above, but children that are already
    # loaded never re-register - re-adopt them from their own runtime data so
    # a reload doesn't strand every load until the next restart. (Inverters
    # and groups are resolved from the config entries instead; loads keep a
    # runtime list because their allocation state lives alongside it.)
    hass.data[DOMAIN]["hubs"][entry.entry_id]["loads"] = [
        load_entry_id
        for load_entry_id, load_data in hass.data[DOMAIN]["loads"].items()
        if load_data.get("hub_entry_id") == entry.entry_id
    ]

    # Check if entities need migration
    await _migrate_hub_entities_if_needed(hass, entry)
    
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS[ENTRY_TYPE_HUB])

    # Trigger discovery for unconfigured OCPP chargers
    await _discover_and_notify_chargers(hass, entry.entry_id)

    # Auto-import: a hub still carrying legacy hub-level HARDWARE config
    # (inverter, battery or PV entities and capacities - bare charge/discharge
    # defaults don't count) gets it moved onto a standalone inverter entry.
    # The trigger is the presence of a field, so a release that moves one
    # more field onto the inverter converges on the next restart; blanking
    # removes the trigger, making it self-terminating.
    # Until the import lands the engine keeps treating the hub's fields as one
    # implicit fleet member, so nothing is lost or double-counted in between.
    if any(
        get_entry_value(entry, key, None)
        for key in (
            CONF_SOLAR_PRODUCTION_ENTITY_ID,
            CONF_SOLAR_FORECAST_DEVICE_IDS,
            CONF_BATTERY_SOC_ENTITY_ID,
            CONF_BATTERY_POWER_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
            CONF_INVERTER_MAX_POWER,
            CONF_INVERTER_MAX_POWER_PER_PHASE,
        )
    ):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={"hub_entry_id": entry.entry_id},
            )
        )

    return True


async def _setup_load_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up a load config entry."""
    _LOGGER.info("Setting up load entry: %s", entry.title)
    
    hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)

    # Before any entity is built: an entry still carrying a pre-2026-02-19
    # device-registry UUID as its charge point id has every OCPP command
    # rejected by ocpp 0.11.2+, and composes the wrong charge-control switch
    # name. Repaired here rather than in async_migrate_entry because it reads
    # the device registry - a version-gated migration gets one attempt, while
    # this re-tries every setup and is a no-op the moment the id is valid.
    repair_ocpp_device_id(hass, entry)

    # Store load data (runtime state written by entities, read by calculation)
    default_mode = modes_for(entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE))[1]
    hass.data[DOMAIN]["loads"][entry.entry_id] = {
        "entry": entry,
        "hub_entry_id": hub_entry_id,
        "min_current": None,
        "max_current": None,
        "device_power": None,
        "dynamic_control": True,
        "operating_mode": default_mode.key,
    }
    
    # Link load to hub
    hass.data[DOMAIN]["hubs"][hub_entry_id]["loads"].append(entry.entry_id)
    
    # Initialize load allocation
    hass.data[DOMAIN]["load_allocations"][entry.entry_id] = 0
    
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS[ENTRY_TYPE_LOAD])
    
    return True


async def _setup_group_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up a circuit group config entry."""
    _LOGGER.info("Setting up circuit group entry: %s", entry.title)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS[ENTRY_TYPE_GROUP])

    return True


async def _setup_inverter_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up an inverter config entry (a power source linked to a hub)."""
    _LOGGER.info("Setting up inverter entry: %s", entry.title)

    hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)

    # Store inverter data
    hass.data[DOMAIN]["inverters"][entry.entry_id] = {
        "entry": entry,
        "hub_entry_id": hub_entry_id,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS[ENTRY_TYPE_INVERTER])

    return True


async def _discover_and_notify_chargers(hass: HomeAssistant, hub_entry_id: str):
    """Discover unconfigured OCPP chargers and create discovery flows.

    The scan itself is the shared ``ocpp_discovery`` scanner the config flow
    goes through too, so an auto-discovered charger is described exactly like a
    manually added one: the OCPP charge
    point id (read off the device-registry identifier the ocpp integration
    stamps, NOT the HA device-registry UUID, which the ocpp services cannot
    address), plus the full set of per-phase current and power entities found
    by device membership, and watts-only chargers (power_offered, no
    current_offered) included. The whole dict is handed to the discovery flow,
    which stores every key on the created entry.
    """
    for charger in scan_ocpp_chargers(hass):
        _LOGGER.info("Discovered OCPP charger: %s (%s)", charger["name"], charger["id"])

        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={
                "hub_entry_id": hub_entry_id,
                "charger_id": charger["id"],
                "charger_name": charger["name"],
                **{k: v for k, v in charger.items() if k not in ("id", "name")},
            },
        )


async def _migrate_hub_entities_if_needed(hass: HomeAssistant, entry: ConfigEntry):
    """Check if entities need to be migrated to the new hub architecture."""
    entity_registry = er.async_get(hass)
    entity_id = entry.data.get(CONF_ENTITY_ID)
    
    if not entity_id:
        _LOGGER.warning("No entity_id found in hub config entry, skipping entity migration")
        return
    
    # Check and update existing entities to be associated with this config entry
    for platform, unique_id in (
        ("number", f"{entity_id}_home_battery_soc_target"),
        ("number", f"{entity_id}_home_battery_soc_min"),
        ("number", f"{entity_id}_power_buffer"),
        ("switch", f"{entity_id}_allow_grid_charging"),
    ):
        existing_id = entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
        if existing_id is None:
            _LOGGER.info(f"Entity with unique_id {unique_id} will be created when the platform is set up")
        elif entity_registry.async_get(existing_id).config_entry_id != entry.entry_id:
            _LOGGER.info(f"Migrating existing entity {existing_id} (unique_id: {unique_id}) to hub config entry {entry.entry_id}")
            entity_registry.async_update_entity(existing_id, config_entry_id=entry.entry_id)
        else:
            _LOGGER.debug(f"Entity {existing_id} already associated with hub config entry")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Load Juggler config entry."""
    entry_type = entry.data.get(ENTRY_TYPE, ENTRY_TYPE_HUB)
    domain_data = hass.data[DOMAIN]

    if entry_type == ENTRY_TYPE_HUB:
        # Stop the site cycle FIRST - a tick landing mid-unload would drive
        # loads that are being torn down. async_shutdown cancels the timer and
        # drops the keepalive listener, so nothing survives the entry.
        coordinator = domain_data.get("hub_coordinators", {}).pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_shutdown()

    await hass.config_entries.async_unload_platforms(entry, PLATFORMS.get(entry_type, []))

    if entry_type == ENTRY_TYPE_HUB:
        # The hub's load_processors bucket is deliberately left in place: its
        # entries belong to the LOADS' entity lifecycles (they unregister
        # themselves), and a hub reload must not strand loads that stay loaded.
        domain_data["hubs"].pop(entry.entry_id, None)

    elif entry_type == ENTRY_TYPE_LOAD:
        # Remove load from hub's list
        hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
        if hub_entry_id in domain_data["hubs"]:
            loads_list = domain_data["hubs"][hub_entry_id]["loads"]
            if entry.entry_id in loads_list:
                loads_list.remove(entry.entry_id)
        domain_data["loads"].pop(entry.entry_id, None)
        domain_data["load_allocations"].pop(entry.entry_id, None)

    elif entry_type == ENTRY_TYPE_INVERTER:
        domain_data.get("inverters", {}).pop(entry.entry_id, None)

    return True
