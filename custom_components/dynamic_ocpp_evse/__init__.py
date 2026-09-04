from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import (
    ConfigEntry,
    SOURCE_IMPORT,
    SOURCE_INTEGRATION_DISCOVERY,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.script import Script
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from datetime import datetime, timedelta
import logging
import voluptuous as vol
from .const import (
    ALL_OPERATING_MODE_KEYS,
    CHARGE_RATE_UNIT_AMPS,
    CHARGE_RATE_UNIT_AUTO,
    CHARGE_RATE_UNIT_WATTS,
    CONF_ALLOW_GRID_CHARGING_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_BATTERY_SOC_TARGET_ENTITY_ID,
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
    CONF_POWER_BUFFER_ENTITY_ID,
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
    DEFAULT_OPERATING_MODE_EVSE,
    DEFAULT_OPERATING_MODE_HOT_WATER_TANK,
    DEFAULT_OPERATING_MODE_PLUG,
    DEFAULT_OPERATING_MODE_POWER_STATION,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_PROFILE_VALIDITY_MODE,
    DEFAULT_STACK_LEVEL,
    DEFAULT_UPDATE_FREQUENCY,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_POWER_STATION,
    DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
    DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    DISTRIBUTION_MODE_SHARED,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
    MIGRATE_PLUG_SOLAR_ONLY_FLAG,
    CONF_INVERTER_FEATURES,
)
from .helpers import (
    get_entry_value,
    infer_inverter_features,
    strip_unfeatured_inverter_options,
)
from . import units
from .ocpp_discovery import repair_ocpp_device_id, scan_ocpp_chargers
from .registry import (  # noqa: F401 — re-exported; canonical home is registry.py
    get_loads_for_hub,
    get_groups_for_hub,
    get_hub_for_load,
    get_inverters_for_hub,
)

_LOGGER = logging.getLogger(__name__)

# Define the config schema
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Integration version for entity migration
INTEGRATION_VERSION = "2.0.0"

# The stored strings the generic charger → load rename replaced (2.4 → 2.5).
# Named here rather than in const/ because nothing outside the migration may
# read or write them: they exist only so entries written before the rename can
# still be recognised, both by the 2.5 step and by the older steps below that
# have to inspect an entry_type predating it.
_LEGACY_ENTRY_TYPE_CHARGER = "charger"
_LEGACY_CONF_CHARGER_PRIORITY = "charger_priority"
# The write deadband when it was a percentage of the normal value (2.5 → 2.6).
_LEGACY_CONF_CHARGE_CONTROL_DEADBAND = "inverter_charge_control_deadband"


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
        
        # Generate entity IDs for hub-created entities if not present
        entity_id = new_data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")
        if CONF_BATTERY_SOC_TARGET_ENTITY_ID not in new_data:
            new_data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = f"number.{entity_id}_home_battery_soc_target"
        if CONF_ALLOW_GRID_CHARGING_ENTITY_ID not in new_data:
            new_data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = f"switch.{entity_id}_allow_grid_charging"
        if CONF_POWER_BUFFER_ENTITY_ID not in new_data:
            new_data[CONF_POWER_BUFFER_ENTITY_ID] = f"number.{entity_id}_power_buffer"
        
        # Update the config entry with new version
        options = dict(entry.options)
        options.setdefault(CONF_EVSE_MINIMUM_CHARGE_CURRENT, new_data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT))
        options.setdefault(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, new_data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT))
        options.setdefault(CONF_UPDATE_FREQUENCY, new_data.get(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY))
        options.setdefault(CONF_OCPP_PROFILE_TIMEOUT, new_data.get(CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT))
        options.setdefault(CONF_CHARGE_PAUSE_DURATION, new_data.get(CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION))
        options.setdefault(CONF_STACK_LEVEL, new_data.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL))
        options.setdefault(CONF_CHARGE_RATE_UNIT, new_data.get(CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT))
        options.setdefault(CONF_PROFILE_VALIDITY_MODE, new_data.get(CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE))
        options.setdefault(CONF_BATTERY_SOC_ENTITY_ID, new_data.get(CONF_BATTERY_SOC_ENTITY_ID))
        options.setdefault(CONF_BATTERY_POWER_ENTITY_ID, new_data.get(CONF_BATTERY_POWER_ENTITY_ID))
        options.setdefault(CONF_BATTERY_MAX_CHARGE_POWER, new_data.get(CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER))
        options.setdefault(CONF_BATTERY_MAX_DISCHARGE_POWER, new_data.get(CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER))
        options.setdefault(CONF_BATTERY_SOC_HYSTERESIS, new_data.get(CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS))

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
        data = entry.data
        options.setdefault(CONF_EVSE_MINIMUM_CHARGE_CURRENT, data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT))
        options.setdefault(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT))
        options.setdefault(CONF_UPDATE_FREQUENCY, data.get(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY))
        options.setdefault(CONF_OCPP_PROFILE_TIMEOUT, data.get(CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT))
        options.setdefault(CONF_CHARGE_PAUSE_DURATION, data.get(CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION))
        options.setdefault(CONF_STACK_LEVEL, data.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL))
        options.setdefault(CONF_CHARGE_RATE_UNIT, data.get(CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT))
        options.setdefault(CONF_PROFILE_VALIDITY_MODE, data.get(CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE))
        options.setdefault(CONF_BATTERY_SOC_ENTITY_ID, data.get(CONF_BATTERY_SOC_ENTITY_ID))
        options.setdefault(CONF_BATTERY_POWER_ENTITY_ID, data.get(CONF_BATTERY_POWER_ENTITY_ID))
        options.setdefault(CONF_BATTERY_MAX_CHARGE_POWER, data.get(CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER))
        options.setdefault(CONF_BATTERY_MAX_DISCHARGE_POWER, data.get(CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER))
        options.setdefault(CONF_BATTERY_SOC_HYSTERESIS, data.get(CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS))

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
            # Value is in seconds (old format) — convert to minutes
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
    # are seeded — off-grid Excess is battery-side only, and a seeded limit
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
    # Idempotent by construction — each rewrite is conditional on the legacy
    # spelling still being present — and load-scoped: a hub, inverter or group
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
            # key's value — it is the one every reader already uses.
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
    # (CONF_CHARGE_CONTROL_DEADBAND_W). The two cannot be converted here — the
    # normal value is an entity read, not a stored number — and reading the old
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
                "%s: the percentage write deadband was dropped from %s — the"
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
    # every undeclared section are cleared — the form had been saving *Battery
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

    return True


def _charger_phase_count(entry: ConfigEntry) -> int:
    """How many site phases a charger draws on, from its own config.

    Used to encode a Watts-mode limit (A × V × phases), so guessing high
    overshoots the charger by that factor — a 1-phase charger asked to reset to
    a 3-phase minimum gets three times the current it should.

    No flow ever writes CONF_PHASES, so it is honored only when actually
    present (a service/YAML override) and the count otherwise comes from what
    the charger entry does store: its L1/L2/L3 → site phase mapping. The setup
    and reconfigure steps collapse the hidden mappings onto L1's phase on a
    1-/2-phase site, so the number of DISTINCT mapped phases is the charger's
    phase count as the site sees it. Nothing mapped at all falls back to 1 —
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

        # Get the OCPP device ID (options first — the reconfigure/options flow
        # writes an edited device ID to entry.options, so reading entry.data
        # would keep resetting the charger the user renamed away from)
        ocpp_device_id = get_entry_value(entry, CONF_OCPP_DEVICE_ID, None)
        if not ocpp_device_id:
            _LOGGER.error(f"No OCPP device ID configured for entry {entry.title} - cannot reset")
            return

        evse_minimum_charge_current = get_entry_value(entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
        
        # Get charge rate unit from charger config
        charge_rate_unit = get_entry_value(entry, CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT)
        
        # If set to auto, detect from sensor
        if charge_rate_unit == CHARGE_RATE_UNIT_AUTO:
            current_offered_entity = get_entry_value(
                entry, CONF_EVSE_CURRENT_OFFERED_ENTITY_ID, None
            )
            if current_offered_entity:
                sensor_state = hass.states.get(current_offered_entity)
                if sensor_state:
                    unit = sensor_state.attributes.get("unit_of_measurement")
                    charge_rate_unit = CHARGE_RATE_UNIT_WATTS if unit == "W" else CHARGE_RATE_UNIT_AMPS
                else:
                    charge_rate_unit = CHARGE_RATE_UNIT_AMPS
            else:
                charge_rate_unit = CHARGE_RATE_UNIT_AMPS
        
        # Convert limit if using Watts
        if charge_rate_unit == CHARGE_RATE_UNIT_WATTS:
            # Need to get hub config for voltage and charger config for phases
            hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
            if hub_entry_id:
                hub_entry = hass.config_entries.async_get_entry(hub_entry_id)
                if hub_entry:
                    voltage = (
                        get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
                        or DEFAULT_PHASE_VOLTAGE
                    )
                    charger_phases = _charger_phase_count(entry)
                    limit_for_charger = round(evse_minimum_charge_current * voltage * charger_phases, 1)
                    rate_unit = "W"
                else:
                    limit_for_charger = evse_minimum_charge_current
                    rate_unit = "A"
            else:
                limit_for_charger = evse_minimum_charge_current
                rate_unit = "A"
        else:
            limit_for_charger = evse_minimum_charge_current
            rate_unit = "A"
        
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
        script = Script(hass, sequence, "Reset OCPP EVSE", DOMAIN)
        await script.async_run(context=call.context)

    hass.services.async_register(DOMAIN, "reset_ocpp_evse", handle_reset_service)

    # --- Helper to find an entity by unique_id suffix within a config entry ---
    def _find_entity_state(entity_id_suffix: str, config_entry_id: str):
        """Find an entity's HA entity_id by matching unique_id pattern."""
        entity_registry = async_get_entity_registry(hass)
        for eid, entity in entity_registry.entities.items():
            if (entity.config_entry_id == config_entry_id
                    and entity.platform == DOMAIN
                    and entity.unique_id.endswith(entity_id_suffix)):
                return eid
        return None

    def _read_other_current(suffix: str, config_entry_id: str):
        """Read the float value of a charger's _min/_max current entity, or None."""
        eid = _find_entity_state(suffix, config_entry_id)
        if not eid:
            return None
        state = hass.states.get(eid)
        if units.is_unavailable(state):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        return None if units.is_unusable_number(value) else value

    # --- set_operating_mode service ---
    async def handle_set_operating_mode(call: ServiceCall):
        """Set the operating mode for a load."""
        entry_id = call.data["entry_id"]
        mode = call.data["mode"]

        entity_id = _find_entity_state("_operating_mode", entry_id)
        if not entity_id:
            _LOGGER.error("Could not find operating mode entity for load %s", entry_id)
            return

        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": entity_id, "option": mode},
            blocking=True,
        )

    hass.services.async_register(
        DOMAIN, "set_operating_mode", handle_set_operating_mode,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.string,
            vol.Required("mode"): vol.In(ALL_OPERATING_MODE_KEYS),
        }),
    )

    # --- set_distribution_mode service ---
    async def handle_set_distribution_mode(call: ServiceCall):
        """Set the distribution mode for a hub."""
        entry_id = call.data["entry_id"]
        mode = call.data["mode"]

        entity_id = _find_entity_state("_distribution_mode", entry_id)
        if not entity_id:
            _LOGGER.error("Could not find distribution mode entity for hub %s", entry_id)
            return

        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": entity_id, "option": mode},
            blocking=True,
        )

    hass.services.async_register(
        DOMAIN, "set_distribution_mode", handle_set_distribution_mode,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.string,
            vol.Required("mode"): vol.In([
                DISTRIBUTION_MODE_SHARED, DISTRIBUTION_MODE_PRIORITY,
                DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED, DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
            ]),
        }),
    )

    # --- set_max_current service ---
    async def handle_set_max_current(call: ServiceCall):
        """Set the max current for a charger."""
        entry_id = call.data["entry_id"]
        current = call.data["current"]

        entity_id = _find_entity_state("_max_current", entry_id)
        if not entity_id:
            _LOGGER.error("Could not find max current entity for charger %s", entry_id)
            return

        # Enforce min ≤ max — the min/max sliders are independent entities, so a
        # service call could otherwise leave the engine with min > max.
        min_value = _read_other_current("_min_current", entry_id)
        if min_value is not None and current < min_value:
            _LOGGER.error(
                "set_max_current for %s rejected: %.1fA is below min current %.1fA",
                entry_id, current, min_value,
            )
            return

        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": entity_id, "value": current},
            blocking=True,
        )

    hass.services.async_register(
        DOMAIN, "set_max_current", handle_set_max_current,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.string,
            vol.Required("current"): vol.Coerce(float),
        }),
    )

    # --- set_min_current service ---
    async def handle_set_min_current(call: ServiceCall):
        """Set the min current for a charger."""
        entry_id = call.data["entry_id"]
        current = call.data["current"]

        entity_id = _find_entity_state("_min_current", entry_id)
        if not entity_id:
            _LOGGER.error("Could not find min current entity for charger %s", entry_id)
            return

        # Enforce min ≤ max — see handle_set_max_current.
        max_value = _read_other_current("_max_current", entry_id)
        if max_value is not None and current > max_value:
            _LOGGER.error(
                "set_min_current for %s rejected: %.1fA is above max current %.1fA",
                entry_id, current, max_value,
            )
            return

        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": entity_id, "value": current},
            blocking=True,
        )

    hass.services.async_register(
        DOMAIN, "set_min_current", handle_set_min_current,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.string,
            vol.Required("current"): vol.Coerce(float),
        }),
    )

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
        "groups": {},  # Circuit group entries
        "inverters": {},  # Inverter entries (power sources, optional battery)
        "load_allocations": {},  # Stores current allocation for each load
    })
    # setdefault only fires once — older buckets may predate "inverters"
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
    # update listener is registered below — async_update_entry fires update
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
        "groups": [],    # List of circuit group entry_ids linked to this hub
        "inverters": [],  # List of inverter entry_ids linked to this hub
        "distribution_mode": DEFAULT_DISTRIBUTION_MODE,
        "allow_grid_charging": True,
        "power_buffer": 0,
        "max_import_power": None,
        "battery_soc_target": DEFAULT_BATTERY_SOC_TARGET,
        "battery_soc_min": DEFAULT_BATTERY_SOC_MIN,
    }

    # A hub RELOAD rebuilds the dict above, but children that are already
    # loaded never re-register — re-adopt them from their own runtime data so
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
    
    # Forward setup to hub platforms (number, switch, sensor, select for hub-level entities)
    await hass.config_entries.async_forward_entry_setups(entry, ["number", "switch", "sensor", "select"])

    # Trigger discovery for unconfigured OCPP chargers
    await _discover_and_notify_chargers(hass, entry.entry_id)

    # Auto-import: a hub still carrying legacy hub-level HARDWARE config
    # (inverter, battery or PV entities and capacities — bare charge/discharge
    # defaults don't count) gets it moved onto a standalone inverter entry.
    # The trigger is the presence of a field, not the imported flag, so a
    # release that moves one more field onto the inverter converges on the
    # next restart; blanking removes the trigger, making it self-terminating.
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

    # Verify hub exists. HA sets up config entries concurrently in arbitrary
    # order, so the hub may not be ready yet — raise ConfigEntryNotReady so HA
    # retries this load once the hub has finished setting up.
    if hub_entry_id not in hass.data[DOMAIN]["hubs"]:
        raise ConfigEntryNotReady(
            f"Hub {hub_entry_id} not ready for load {entry.title}"
        )

    # Before any entity is built: an entry still carrying a pre-2026-02-19
    # device-registry UUID as its charge point id has every OCPP command
    # rejected by ocpp 0.11.2+, and composes the wrong charge-control switch
    # name. Repaired here rather than in async_migrate_entry because it reads
    # the device registry — a version-gated migration gets one attempt, while
    # this re-tries every setup and is a no-op the moment the id is valid.
    repair_ocpp_device_id(hass, entry)

    # Store load data (runtime state written by entities, read by calculation)
    device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
    if device_type == DEVICE_TYPE_PLUG:
        default_mode = DEFAULT_OPERATING_MODE_PLUG
    elif device_type == DEVICE_TYPE_HOT_WATER_TANK:
        default_mode = DEFAULT_OPERATING_MODE_HOT_WATER_TANK
    elif device_type == DEVICE_TYPE_POWER_STATION:
        default_mode = DEFAULT_OPERATING_MODE_POWER_STATION
    else:
        default_mode = DEFAULT_OPERATING_MODE_EVSE
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
    
    # Forward setup to load platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "number", "button", "select", "switch"])
    
    return True


async def _setup_group_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up a circuit group config entry."""
    _LOGGER.info("Setting up circuit group entry: %s", entry.title)

    hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)

    # Verify hub exists — raise ConfigEntryNotReady so HA retries this group
    # once the hub has finished setting up (entry setup order is concurrent).
    if hub_entry_id not in hass.data[DOMAIN]["hubs"]:
        raise ConfigEntryNotReady(
            f"Hub {hub_entry_id} not ready for group {entry.title}"
        )

    # Store group data
    hass.data[DOMAIN]["groups"][entry.entry_id] = {
        "entry": entry,
        "hub_entry_id": hub_entry_id,
    }

    # Link group to hub
    hass.data[DOMAIN]["hubs"][hub_entry_id]["groups"].append(entry.entry_id)

    # Forward setup to sensor platform only (group sensors)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    return True


async def _setup_inverter_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up an inverter config entry (a power source linked to a hub)."""
    _LOGGER.info("Setting up inverter entry: %s", entry.title)

    hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)

    # Verify hub exists — raise ConfigEntryNotReady so HA retries this inverter
    # once the hub has finished setting up (entry setup order is concurrent).
    if hub_entry_id not in hass.data[DOMAIN]["hubs"]:
        raise ConfigEntryNotReady(
            f"Hub {hub_entry_id} not ready for inverter {entry.title}"
        )

    # Store inverter data
    hass.data[DOMAIN]["inverters"][entry.entry_id] = {
        "entry": entry,
        "hub_entry_id": hub_entry_id,
    }

    # Link inverter to hub
    hass.data[DOMAIN]["hubs"][hub_entry_id].setdefault("inverters", []).append(
        entry.entry_id
    )

    # Sensors plus the Battery Charge Control switch (write-control opt-in)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "switch"])

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
    entity_registry = async_get_entity_registry(hass)
    entity_id = entry.data.get(CONF_ENTITY_ID)
    
    if not entity_id:
        _LOGGER.warning("No entity_id found in hub config entry, skipping entity migration")
        return
    
    # Define expected hub entities with their unique_ids
    expected_entities = {
        f"number.{entity_id}_home_battery_soc_target": f"{entity_id}_home_battery_soc_target",
        f"number.{entity_id}_home_battery_soc_min": f"{entity_id}_home_battery_soc_min",
        f"number.{entity_id}_power_buffer": f"{entity_id}_power_buffer",
        f"switch.{entity_id}_allow_grid_charging": f"{entity_id}_allow_grid_charging"
    }
    
    # Check and update existing entities to be associated with this config entry
    entities_migrated = []
    for entity_entity_id, unique_id in expected_entities.items():
        # Try to find entity by unique_id (this is the key for matching)
        existing_entity = None
        for reg_entity_id, reg_entity in entity_registry.entities.items():
            if reg_entity.unique_id == unique_id and reg_entity.platform == DOMAIN:
                existing_entity = reg_entity
                break
        
        if existing_entity:
            # Entity exists with this unique_id
            if existing_entity.config_entry_id != entry.entry_id:
                _LOGGER.info(f"Migrating existing entity {existing_entity.entity_id} (unique_id: {unique_id}) to hub config entry {entry.entry_id}")
                entity_registry.async_update_entity(
                    existing_entity.entity_id,
                    config_entry_id=entry.entry_id
                )
                entities_migrated.append(unique_id)
            else:
                _LOGGER.debug(f"Entity {existing_entity.entity_id} already associated with hub config entry")
                entities_migrated.append(unique_id)
        else:
            _LOGGER.info(f"Entity with unique_id {unique_id} will be created when the platform is set up")
    
    # Update the config entry to ensure it has the required entity IDs
    updated_data = dict(entry.data)
    updated_data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = f"number.{entity_id}_home_battery_soc_target"
    updated_data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = f"switch.{entity_id}_allow_grid_charging"
    updated_data[CONF_POWER_BUFFER_ENTITY_ID] = f"number.{entity_id}_power_buffer"
    updated_data["integration_version"] = INTEGRATION_VERSION

    # Only write the entry when something actually changed — an unconditional
    # async_update_entry on every startup triggers an extra hub reload.
    if updated_data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=updated_data)
        _LOGGER.info(f"Updated hub config entry with entity IDs. Migrated {len(entities_migrated)} entities")
    else:
        _LOGGER.debug("Hub config entry already current — no entity-ID migration needed")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Load Juggler config entry."""
    entry_type = entry.data.get(ENTRY_TYPE, ENTRY_TYPE_HUB)
    
    if entry_type == ENTRY_TYPE_HUB:
        # Stop the site cycle FIRST — a tick landing mid-unload would drive
        # loads that are being torn down. async_shutdown cancels the timer and
        # drops the keepalive listener, so nothing survives the entry.
        coordinator = hass.data[DOMAIN].get("hub_coordinators", {}).pop(
            entry.entry_id, None
        )
        if coordinator is not None:
            await coordinator.async_shutdown()

        # Unload hub platforms (includes select for distribution mode)
        for domain in ["number", "switch", "sensor", "select"]:
            await hass.config_entries.async_forward_entry_unload(entry, domain)

        # Remove hub from data
        if entry.entry_id in hass.data[DOMAIN]["hubs"]:
            del hass.data[DOMAIN]["hubs"][entry.entry_id]
        # The hub's load_processors bucket is deliberately left in place: its
        # entries belong to the LOADS' entity lifecycles (they unregister
        # themselves), and a hub reload must not strand loads that stay loaded.
    
    elif entry_type == ENTRY_TYPE_LOAD:
        # Unload load platforms
        for domain in ["sensor", "number", "button", "select", "switch"]:
            await hass.config_entries.async_forward_entry_unload(entry, domain)

        # Remove load from hub's list
        hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
        if hub_entry_id in hass.data[DOMAIN]["hubs"]:
            loads_list = hass.data[DOMAIN]["hubs"][hub_entry_id]["loads"]
            if entry.entry_id in loads_list:
                loads_list.remove(entry.entry_id)
        
        # Remove load from data
        if entry.entry_id in hass.data[DOMAIN]["loads"]:
            del hass.data[DOMAIN]["loads"][entry.entry_id]
        if entry.entry_id in hass.data[DOMAIN]["load_allocations"]:
            del hass.data[DOMAIN]["load_allocations"][entry.entry_id]

    elif entry_type == ENTRY_TYPE_GROUP:
        # Unload group platforms
        await hass.config_entries.async_forward_entry_unload(entry, "sensor")

        # Remove group from hub's list
        hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
        if hub_entry_id in hass.data[DOMAIN]["hubs"]:
            groups_list = hass.data[DOMAIN]["hubs"][hub_entry_id].get("groups", [])
            if entry.entry_id in groups_list:
                groups_list.remove(entry.entry_id)

        # Remove group from data
        if entry.entry_id in hass.data[DOMAIN]["groups"]:
            del hass.data[DOMAIN]["groups"][entry.entry_id]

    elif entry_type == ENTRY_TYPE_INVERTER:
        # Unload inverter platforms
        await hass.config_entries.async_forward_entry_unload(entry, "sensor")
        await hass.config_entries.async_forward_entry_unload(entry, "switch")

        # Remove inverter from hub's list
        hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
        if hub_entry_id in hass.data[DOMAIN]["hubs"]:
            inverters_list = hass.data[DOMAIN]["hubs"][hub_entry_id].get("inverters", [])
            if entry.entry_id in inverters_list:
                inverters_list.remove(entry.entry_id)

        # Remove inverter from data
        if entry.entry_id in hass.data[DOMAIN].get("inverters", {}):
            del hass.data[DOMAIN]["inverters"][entry.entry_id]

    return True
