"""
Load Juggler - Main calculation module.

This file provides a unified interface for EVSE calculations.
All core calculation logic has been refactored into the calculations/ directory.
"""

import logging
import math
import time

from ..calculations import (
    SiteContext,
    LoadContext,
    PhaseValues,
    CircuitGroup,
    calculate_all_charger_targets,
)
from ..const import *
from ..calculations.utils import is_number, compute_household_per_phase
from ..helpers import get_entry_value
from .auto_detect import check_inversion, check_phase_mapping

_LOGGER = logging.getLogger(__name__)

# Phase labels used for loop-based per-phase processing
_PHASE_LABELS = ("A", "B", "C")

# Sentinel: sensor is configured but currently unavailable/unknown
_UNAVAILABLE = object()


def _smooth(ema_dict: dict, key: str, raw, alpha: float = EMA_ALPHA):
    """Apply EMA smoothing to a sensor reading. Returns smoothed value.

    State is stored in ema_dict[key] between calls.
    - None values pass through (sensor not configured).
    - _UNAVAILABLE holds the last known EMA value (sensor temporarily down).
    """
    if raw is None:
        return None
    if raw is _UNAVAILABLE:
        # Sensor unavailable — hold last known EMA value
        return ema_dict.get(key)
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return ema_dict.get(key)
    if not math.isfinite(val):
        return ema_dict.get(key)
    prev = ema_dict.get(key)
    if prev is None:
        ema_dict[key] = val
        return val
    smoothed = alpha * val + (1 - alpha) * prev
    ema_dict[key] = smoothed
    return round(smoothed, 2)


def _read_phase_attr(attrs: dict, keys: tuple) -> float | None:
    """Try to read a numeric phase current from entity attributes using multiple naming conventions.

    Case-insensitive: handles L1/l1/L1_current/l1_current etc.
    """
    lower_attrs = {k.lower(): v for k, v in attrs.items()}
    for key in keys:
        val = lower_attrs.get(key.lower())
        if val is not None and is_number(val):
            return float(val)
    return None


def _read_entity(hass, entity_id: str, default=0, unit: str = None):
    """Read a numeric value from an HA entity with optional unit conversion.

    Args:
        hass: Home Assistant instance
        entity_id: The entity ID to read
        default: Default value if entity not configured
        unit: Target unit for conversion. Supported: "A", "W"
              - "A": Converts W→A (divides by voltage), kW→W→A
              - "W": Converts kW→W (multiplies by 1000)

    Returns:
        float: The entity's numeric value (converted if unit specified).
        _UNAVAILABLE: The entity is configured but currently unavailable/unknown.
        default: The entity_id is not provided (not configured).
    """
    if not entity_id:
        return default
    state = hass.states.get(entity_id)
    if not state or state.state in ("unknown", "unavailable", None, ""):
        return _UNAVAILABLE
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return _UNAVAILABLE

    # Apply unit conversion if requested
    if unit and value != 0:
        entity_unit = state.attributes.get("unit_of_measurement", "")
        if entity_unit:
            entity_unit = entity_unit.upper()

            # Convert kW → W
            if unit == "W" and entity_unit == "KW":
                value = value * 1000
            elif unit == "A" and entity_unit == "KW":
                # kW → W → A (need voltage context, handled by caller)
                value = value * 1000  # Just do kW → W, caller handles W → A
            elif unit == "A" and entity_unit == "W":
                # W → A conversion requires voltage - caller must handle this
                pass  # Return W value, caller handles conversion

    return value


def _read_inverter_output(hass, entity_id, voltage):
    """Read inverter output, auto-detecting A vs W vs kW and converting to A."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if not st or st.state in ("unknown", "unavailable", None, ""):
        return None
    try:
        value = abs(float(st.state))
    except (ValueError, TypeError):
        return None
    # Auto-detect unit from entity attributes
    unit = st.attributes.get("unit_of_measurement", "").upper()
    if unit == "KW" and voltage > 0:
        value = (value * 1000) / voltage  # Convert kW → W → A
    elif unit == "W" and voltage > 0:
        value = value / voltage  # Convert W → A
    # If unit is A or unknown, assume already in Amperes
    return value


def _coerce(v, default=0):
    """Convert _UNAVAILABLE sentinel to a safe default for non-smoothed use."""
    return default if v is _UNAVAILABLE else v


def _check_entity_availability(hass, hub_entry) -> list:
    """Return warnings for hub-configured entities that are currently unavailable.

    Grid CTs are tracked separately (stale-timeout logic); this covers the
    solar, battery, inverter-output and max-import-power sensors so a missing
    feed shows up on the hub Status sensor instead of silently defaulting to 0.
    """
    warnings = []
    checks = (
        ("Solar production sensor", CONF_SOLAR_PRODUCTION_ENTITY_ID),
        ("Battery SOC sensor", CONF_BATTERY_SOC_ENTITY_ID),
        ("Battery power sensor", CONF_BATTERY_POWER_ENTITY_ID),
        ("Inverter output sensor (L1)", CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID),
        ("Inverter output sensor (L2)", CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID),
        ("Inverter output sensor (L3)", CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID),
        ("Max import power sensor", CONF_MAX_IMPORT_POWER_ENTITY_ID),
    )
    for label, conf_key in checks:
        entity_id = get_entry_value(hub_entry, conf_key, None)
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            warnings.append(f"{label} ({entity_id}) is unavailable")
    return warnings


def _fv(v):
    """Format value for debug: None->'n/a', number->'12.3'."""
    if v is None:
        return "n/a"
    if isinstance(v, (int, float)):
        return f"{v:.1f}"
    return str(v)


def _fv2(raw, smoothed):
    """Format smoothed(raw) pair. Always shows both values."""
    if raw is None:
        return _fv(smoothed)
    return f"{_fv(smoothed)}({_fv(raw)})"


def _derive_solar_production(
    inverter_output_per_phase, wiring_topology, export_power, battery_power, voltage
):
    """Derive solar production from available sensor data.

    Unified formula for grid and off-grid sites:
    - With inverter output: series → solar = inverter_output - battery_power
                            parallel → solar = inverter_output
    - Without inverter output: fallback → solar = export + battery_charging
    For off-grid, export is naturally 0 (no grid CTs), so the inverter-based
    formula is used.
    """
    if inverter_output_per_phase is not None:
        inv_watts = inverter_output_per_phase.total * voltage
        if wiring_topology == WIRING_TOPOLOGY_SERIES:
            # Battery is behind inverter: inverter_output = solar + battery_power
            # (battery_power > 0 = discharging, < 0 = charging)
            bp = battery_power if battery_power is not None else 0
            return max(0, inv_watts - bp)
        else:
            # Parallel: inverter output IS solar
            return max(0, inv_watts)
    else:
        # Fallback: derive from grid export + battery charge absorption
        solar = export_power or 0
        if battery_power is not None and battery_power < 0:
            solar += abs(battery_power)
        return max(0, solar)


# ---------------------------------------------------------------------------
# Subfunctions for run_hub_calculation
# ---------------------------------------------------------------------------


def _read_grid_phases(hass, hub_entry):
    """Read per-phase grid current, apply inversion, split into consumption/export.

    Returns (raw_phases, consumption_pv, export_pv) where raw_phases is a 3-list
    of raw current values (None for unconfigured phases).
    """
    phase_entities = [
        get_entry_value(hub_entry, conf, None)
        for conf in (
            CONF_PHASE_A_CURRENT_ENTITY_ID,
            CONF_PHASE_B_CURRENT_ENTITY_ID,
            CONF_PHASE_C_CURRENT_ENTITY_ID,
        )
    ]
    invert_phases = get_entry_value(hub_entry, CONF_INVERT_PHASES, False)

    raw_phases = []
    for entity in phase_entities:
        raw = _coerce(_read_entity(hass, entity, 0)) if entity else None
        if raw is not None and invert_phases:
            raw = -raw
        raw_phases.append(raw)

    consumption = [max(0, r) if r is not None else None for r in raw_phases]
    export = [max(0, -r) if r is not None else None for r in raw_phases]

    return raw_phases, PhaseValues(*consumption), PhaseValues(*export)


def _read_inverter_config(hass, hub_entry, voltage):
    """Read inverter configuration and per-phase output entities.

    Returns (inverter_max_power, inverter_max_power_per_phase,
             inverter_supports_asymmetric, wiring_topology, inverter_output_per_phase).
    """
    inverter_max_power = get_entry_value(hub_entry, CONF_INVERTER_MAX_POWER, None)
    inverter_max_power_per_phase = get_entry_value(
        hub_entry, CONF_INVERTER_MAX_POWER_PER_PHASE, None
    )
    inverter_supports_asymmetric = get_entry_value(
        hub_entry, CONF_INVERTER_SUPPORTS_ASYMMETRIC, False
    )
    wiring_topology = get_entry_value(
        hub_entry, CONF_WIRING_TOPOLOGY, DEFAULT_WIRING_TOPOLOGY
    )

    # Read per-phase inverter output entities (optional)
    inv_entities = [
        get_entry_value(hub_entry, conf, None)
        for conf in (
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
            CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
        )
    ]
    inverter_output_per_phase = None
    if inv_entities[0]:
        inv_values = [_read_inverter_output(hass, e, voltage) for e in inv_entities]
        if inv_values[0] is not None:
            inverter_output_per_phase = PhaseValues(*inv_values)

    return (
        inverter_max_power,
        inverter_max_power_per_phase,
        inverter_supports_asymmetric,
        wiring_topology,
        inverter_output_per_phase,
    )


def _build_evse_charger(hass, entry, voltage, charger_entity_id, priority):
    """Build a LoadContext for an OCPP EVSE charger."""
    charger_rt = hass.data[DOMAIN]["chargers"].get(entry.entry_id, {})
    config_min = get_entry_value(
        entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT
    )
    config_max = get_entry_value(
        entry, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT
    )
    min_current = charger_rt.get("min_current") or config_min
    max_current = charger_rt.get("max_current") or config_max

    phases = int(get_entry_value(entry, CONF_PHASES, 3) or 3)

    # Get OCPP device ID for sensor lookups (different from Load Juggler entity_id)
    ocpp_device_id = entry.data.get(CONF_CHARGER_ID, charger_entity_id)

    # Read connector status from OCPP entity
    connector_status_entity = f"sensor.{ocpp_device_id}_status_connector"
    connector_status_state = hass.states.get(connector_status_entity)
    connector_status = (
        connector_status_state.state if connector_status_state else "Unknown"
    )

    # Read L1/L2/L3 → site phase mapping
    l1_phase = get_entry_value(entry, CONF_CHARGER_L1_PHASE, "A")
    l2_phase = get_entry_value(entry, CONF_CHARGER_L2_PHASE, "B")
    l3_phase = get_entry_value(entry, CONF_CHARGER_L3_PHASE, "C")

    # Resolve the per-charger operating mode from runtime data.
    mode = resolve_operating_mode(
        DEVICE_TYPE_EVSE,
        charger_rt.get("operating_mode", DEFAULT_OPERATING_MODE_EVSE.key),
    )

    charger = LoadContext(
        charger_id=entry.entry_id,
        entity_id=charger_entity_id,
        min_current=min_current,
        max_current=max_current,
        phases=phases,
        priority=priority,
        connector_status=connector_status,
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode.priority,
        l1_phase=l1_phase,
        l2_phase=l2_phase,
        l3_phase=l3_phase,
    )

    # Get OCPP current draw for this charger with fallback chain:
    # 1. Current Import per-phase entities (sensor.{id}_current_import_l1/l2/l3)
    # 2. Current Import entity (per-phase attributes or total)
    # 3. Power Active Import (convert W → A)
    evse_import = entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
    evse_import_l1 = entry.data.get(CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID)
    evse_import_l2 = entry.data.get(CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID)
    evse_import_l3 = entry.data.get(CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID)
    evse_power_import = entry.data.get(CONF_EVSE_POWER_IMPORT_ENTITY_ID)
    current_draw = None

    # Try per-phase current import entities first (separate sensors for each phase)
    if evse_import_l1 or evse_import_l2 or evse_import_l3:
        l1_val = (
            _coerce(_read_entity(hass, evse_import_l1, None), None)
            if evse_import_l1
            else None
        )
        l2_val = (
            _coerce(_read_entity(hass, evse_import_l2, None), None)
            if evse_import_l2
            else None
        )
        l3_val = (
            _coerce(_read_entity(hass, evse_import_l3, None), None)
            if evse_import_l3
            else None
        )

        if l1_val is not None or l2_val is not None or l3_val is not None:
            charger.l1_current = l1_val if l1_val is not None else 0
            charger.l2_current = l2_val if l2_val is not None else 0
            charger.l3_current = l3_val if l3_val is not None else 0
            current_draw = "current_import_l1l2l3"
            _LOGGER.debug(
                "EVSE %s: Using per-phase current import entities: L1=%.1f L2=%.1f L3=%.1f",
                charger_entity_id,
                charger.l1_current,
                charger.l2_current,
                charger.l3_current,
            )

    # Try Current Import entity with per-phase attributes or total
    if current_draw is None and evse_import:
        evse_state = hass.states.get(evse_import)
        if evse_state and evse_state.state not in ["unknown", "unavailable", None]:
            try:
                attrs = evse_state.attributes
                l1 = _read_phase_attr(
                    attrs, ("l1_current", "l1", "phase_1", "current_phase_1")
                )
                l2 = _read_phase_attr(
                    attrs, ("l2_current", "l2", "phase_2", "current_phase_2")
                )
                l3 = _read_phase_attr(
                    attrs, ("l3_current", "l3", "phase_3", "current_phase_3")
                )

                if l1 is not None or l2 is not None or l3 is not None:
                    charger.l1_current = l1 or 0
                    charger.l2_current = l2 or 0
                    charger.l3_current = l3 or 0

                    # Clamp per-phase draws at max_current (some chargers report total in per-phase)
                    # Allow 10% tolerance when using W-based profiles (voltage/rounding variance)
                    cru = get_entry_value(
                        entry, CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT
                    )
                    clamp_threshold = (
                        max_current * 1.1
                        if cru == CHARGE_RATE_UNIT_WATTS
                        else max_current
                    )
                    for attr in ("l1_current", "l2_current", "l3_current"):
                        val = getattr(charger, attr)
                        if val > clamp_threshold:
                            _LOGGER.warning(
                                "EVSE %s: %s=%.1fA exceeds max_current=%.1fA — "
                                "clamping (charger may be reporting total instead of per-phase)",
                                charger_entity_id,
                                attr,
                                val,
                                max_current,
                            )
                            setattr(charger, attr, max_current)
                    current_draw = "current_import_attr"
                else:
                    current_import = float(evse_state.state)
                    charger.l1_current = current_import
                    if phases >= 2:
                        charger.l2_current = current_import
                    if phases >= 3:
                        charger.l3_current = current_import
                    current_draw = "current_import_total"
            except (ValueError, TypeError):
                pass

    # Fallback to Power Active Import if no current import data available
    if current_draw is None and evse_power_import:
        power_state = hass.states.get(evse_power_import)
        if power_state and power_state.state not in ["unknown", "unavailable", None]:
            try:
                power_w = float(power_state.state)
                if power_w > 0 and voltage > 0:
                    # Convert W → A (total power across all phases)
                    power_per_phase = power_w / phases
                    current_per_phase = power_per_phase / voltage
                    charger.l1_current = current_per_phase
                    if phases >= 2:
                        charger.l2_current = current_per_phase
                    if phases >= 3:
                        charger.l3_current = current_per_phase
                    current_draw = "power_import"
                    _LOGGER.debug(
                        "EVSE %s: Using Power Active Import fallback: %.1fW → %.1fA per phase",
                        charger_entity_id,
                        power_w,
                        current_per_phase,
                    )
            except (ValueError, TypeError):
                pass

    if current_draw:
        _LOGGER.debug(
            "EVSE %s: Current draw source: %s", charger_entity_id, current_draw
        )

    # SuspendedEV grace period: car may briefly pause during normal charging (BMS
    # balancing). Only treat as inactive after SUSPENDED_EV_IDLE_TIMEOUT seconds
    # of continuous SuspendedEV + near-zero draw.
    total_draw = charger.l1_current + charger.l2_current + charger.l3_current
    if connector_status == "SuspendedEV" and total_draw < 1.0:
        if "_suspended_ev_since" not in charger_rt:
            charger_rt["_suspended_ev_since"] = time.monotonic()
        idle_duration = time.monotonic() - charger_rt["_suspended_ev_since"]
        if idle_duration >= SUSPENDED_EV_IDLE_TIMEOUT:
            _LOGGER.debug(
                "EVSE %s: SuspendedEV idle for %.0fs (>%ds) — treating as inactive",
                charger_entity_id,
                idle_duration,
                SUSPENDED_EV_IDLE_TIMEOUT,
            )
            charger.connector_status = "Finishing"
    else:
        charger_rt.pop("_suspended_ev_since", None)

    _LOGGER.debug(
        "  EVSE %s [%s]: %s-%sA %dph(hw) L1->%s/L2->%s/L3->%s mask=%s(%dph) "
        "prio=%d [%s] draw=L1:%s/L2:%s/L3:%s",
        charger_entity_id,
        operating_mode,
        _fv(min_current),
        _fv(max_current),
        phases,
        l1_phase,
        l2_phase,
        l3_phase,
        charger.active_phases_mask,
        len(charger.active_phases_mask) if charger.active_phases_mask else 0,
        priority,
        charger.connector_status,
        _fv(charger.l1_current),
        _fv(charger.l2_current),
        _fv(charger.l3_current),
    )
    return charger


def _phase_draw(draw_w, connected_to_phase, voltage):
    """Distribute a binary load's total draw (W) across its connected phases.

    Returns a dict of LoadContext kwargs (l1/l2/l3 phase + current) so the
    load's actual draw is counted in Total Managed Power and subtracted by
    the consumption feedback loop, exactly like an EVSE's metered draw.
    """
    chars = list(connected_to_phase) or ["A"]
    phases = len(chars)
    per_phase = draw_w / (voltage * phases) if voltage > 0 and phases > 0 else 0
    return {
        "l1_phase": chars[0],
        "l2_phase": chars[1] if phases > 1 else "B",
        "l3_phase": chars[2] if phases > 2 else "C",
        "l1_current": per_phase if phases >= 1 else 0,
        "l2_current": per_phase if phases >= 2 else 0,
        "l3_current": per_phase if phases >= 3 else 0,
    }


def _build_plug_charger(hass, entry, voltage, charger_entity_id, priority):
    """Build a LoadContext for a smart load (plug) device."""
    charger_rt = hass.data[DOMAIN]["chargers"].get(entry.entry_id, {})
    slider_power = charger_rt.get("device_power", None)
    config_power = get_entry_value(
        entry, CONF_PLUG_POWER_RATING, DEFAULT_PLUG_POWER_RATING
    )
    # Set power: the runtime slider if set, else the configured rating.
    power_rating = (
        slider_power if slider_power is not None and slider_power > 0 else config_power
    )

    connected_to_phase = get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A") or "A"
    phases = len(connected_to_phase)

    plug_switch_entity = entry.data.get(CONF_PLUG_SWITCH_ENTITY_ID)
    plug_switch_state = (
        hass.states.get(plug_switch_entity) if plug_switch_entity else None
    )
    power_monitor_entity = get_entry_value(
        entry, CONF_PLUG_POWER_MONITOR_ENTITY_ID, None
    )

    power_draw = None
    if power_monitor_entity:
        power_draw = _coerce(
            _read_entity(hass, power_monitor_entity, 0, unit="W")
        )  # Convert kW→W if needed

    # On/off: the switch is authoritative when present; without a switch the
    # power monitor decides; with neither, assume the load is on.
    if plug_switch_state is not None:
        on = plug_switch_state.state == "on"
    elif power_monitor_entity:
        on = power_draw is not None and power_draw > 10
    else:
        on = True
    connector_status = "Charging" if on else "Available"

    # Learn the device's real power from the monitor — but only while the plug
    # is actually on. A reading taken while it is off is standby/phantom draw,
    # not the device's rating, so it must not overwrite the set power.
    if power_monitor_entity and on and power_draw and power_draw > 10:
        power_rating = power_draw
        charger_rt["device_power"] = round(power_draw, 0)

    equivalent_current = power_rating / (voltage * phases) if voltage > 0 else 0

    # Actual draw — the measured draw while the plug is on (else the set power
    # if there is no monitor), 0 when off. Populates the load's per-phase
    # currents so the plug counts toward Total Managed Power and the feedback.
    if power_monitor_entity:
        actual_draw_w = power_draw if (on and power_draw and power_draw > 0) else 0
    else:
        actual_draw_w = power_rating if on else 0

    # Resolve the per-charger operating mode from runtime data.
    mode = resolve_operating_mode(
        DEVICE_TYPE_PLUG,
        charger_rt.get("operating_mode", DEFAULT_OPERATING_MODE_PLUG.key),
    )

    charger = LoadContext(
        charger_id=entry.entry_id,
        entity_id=charger_entity_id,
        min_current=equivalent_current,
        max_current=equivalent_current,
        phases=phases,
        priority=priority,
        active_phases_mask=connected_to_phase,
        connector_status=connector_status,
        device_type=DEVICE_TYPE_PLUG,
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode.priority,
        **_phase_draw(actual_draw_w, connected_to_phase, voltage),
    )
    _LOGGER.debug(
        "  Plug %s [%s]: %.0fW on %s prio=%d [%s]%s",
        charger_entity_id,
        mode.key,
        power_rating,
        connected_to_phase,
        priority,
        connector_status,
        " (metered)" if power_monitor_entity else "",
    )
    return charger


def _build_hot_water_tank_charger(hass, entry, voltage, charger_entity_id, priority):
    """Build a LoadContext for a hot water tank (climate-driven binary load).

    To the engine the tank is a smart load (plug): a fixed-power binary draw.
    The climate entity owns temperature regulation; the HA layer reads its
    hvac_action and writes the setpoint. Tank operating modes (Freeze
    Protection / Normal / Solar Only) map to engine modes here.
    """
    charger_rt = hass.data[DOMAIN]["chargers"].get(entry.entry_id, {})

    # Set power: the runtime slider if set, else the configured element
    # power. A configured tank power sensor overrides it with the live draw
    # while the element is heating, and is written back so the slider learns.
    element_power = get_entry_value(
        entry, CONF_HEATING_ELEMENT_POWER, DEFAULT_HEATING_ELEMENT_POWER
    )
    slider_power = charger_rt.get("device_power")
    power_rating = slider_power if slider_power else element_power
    power_entity = get_entry_value(entry, CONF_TANK_POWER_ENTITY_ID, None)
    live = None
    if power_entity:
        live = _coerce(_read_entity(hass, power_entity, 0, unit="W"))
        if live and live > 10:
            power_rating = live
            charger_rt["device_power"] = round(live, 0)

    connected_to_phase = get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A") or "A"
    phases = len(connected_to_phase)

    # Connector status from the climate entity's hvac_action: a thermostat
    # reporting "idle" means the tank is satisfied — mark it inactive so the
    # engine reallocates that power. Anything else is treated as an active load.
    climate_entity = entry.data.get(CONF_CLIMATE_ENTITY_ID)
    connector_status = "Charging"
    climate_state = hass.states.get(climate_entity) if climate_entity else None
    hvac_action = (
        climate_state.attributes.get("hvac_action") if climate_state else None
    )
    if hvac_action == "idle":
        connector_status = "Available"

    equivalent_current = power_rating / (voltage * phases) if voltage > 0 else 0

    # Actual draw — the element only consumes while the thermostat is calling
    # for heat. Use the live power sensor if configured, else the element
    # rating while hvac_action is "heating". Populates per-phase currents so
    # the tank counts toward Total Managed Power and the feedback loop.
    if power_entity:
        actual_draw_w = live if (live and live > 0) else 0
    else:
        actual_draw_w = power_rating if hvac_action == "heating" else 0

    # Resolve the tank's operating mode. Its behavior (Freeze Protection /
    # Normal are must-run Full Power; Solar Priority follows the sun) is mapped
    # centrally in const/modes.py. resolve_tank_setpoint() independently picks
    # *which* setpoint (away/normal/boost) to aim at — the mode behavior only
    # decides how the tank competes for power, not whether it runs.
    mode = resolve_operating_mode(
        DEVICE_TYPE_HOT_WATER_TANK,
        charger_rt.get("operating_mode", DEFAULT_OPERATING_MODE_HOT_WATER_TANK.key),
    )

    charger = LoadContext(
        charger_id=entry.entry_id,
        entity_id=charger_entity_id,
        min_current=equivalent_current,
        max_current=equivalent_current,
        phases=phases,
        priority=priority,
        active_phases_mask=connected_to_phase,
        connector_status=connector_status,
        device_type=DEVICE_TYPE_HOT_WATER_TANK,
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode.priority,
        **_phase_draw(actual_draw_w, connected_to_phase, voltage),
    )
    _LOGGER.debug(
        "  Tank %s [%s]: %.0fW on %s prio=%d [%s]",
        charger_entity_id,
        mode.key,
        power_rating,
        connected_to_phase,
        priority,
        connector_status,
    )
    return charger


def _add_chargers_to_site(hass, site, hub_entry_id, sensor):
    """Build LoadContext objects for all chargers and add them to the site."""
    from .. import get_chargers_for_hub

    if hasattr(sensor, "_charger_entries"):
        chargers = sensor._charger_entries
    else:
        chargers = get_chargers_for_hub(hass, hub_entry_id)

    for entry in chargers:
        device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        charger_entity_id = entry.data.get(CONF_ENTITY_ID, f"charger_{entry.entry_id}")
        priority = get_entry_value(
            entry, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY
        )

        if device_type == DEVICE_TYPE_PLUG:
            charger = _build_plug_charger(
                hass, entry, site.voltage, charger_entity_id, priority
            )
        elif device_type == DEVICE_TYPE_HOT_WATER_TANK:
            charger = _build_hot_water_tank_charger(
                hass, entry, site.voltage, charger_entity_id, priority
            )
        else:
            charger = _build_evse_charger(
                hass, entry, site.voltage, charger_entity_id, priority
            )

        # Clamp active_phases_mask to only include phases that exist on the site
        site_phases = {
            p
            for p, v in zip(
                _PHASE_LABELS,
                (site.consumption.a, site.consumption.b, site.consumption.c),
            )
            if v is not None
        }
        mask_phases = (
            set(charger.active_phases_mask) if charger.active_phases_mask else set()
        )
        if mask_phases and not mask_phases.issubset(site_phases):
            clamped = "".join(sorted(mask_phases & site_phases)) or charger.l1_phase
            _LOGGER.warning(
                "%s %s: phase mask %s includes phases not on site (%s) — clamping to %s",
                "Plug" if charger.device_type == DEVICE_TYPE_PLUG else "EVSE",
                charger_entity_id,
                charger.active_phases_mask,
                "".join(sorted(site_phases)),
                clamped,
            )
            charger.active_phases_mask = clamped

        site.chargers.append(charger)


def _apply_feedback_loop(site, solar_is_derived, voltage):
    """Subtract charger draws from grid readings to prevent double-counting.

    Grid CTs measure total site current INCLUDING charger draws. Without this
    adjustment, the engine double-counts charger power as both 'consumption'
    and 'charger demand'. Modifies site.consumption and site.export_current
    in-place.
    """
    # Sum charger draws per site phase
    total_draws = [0.0, 0.0, 0.0]
    for c in site.chargers:
        a_draw, b_draw, c_draw = c.get_site_phase_draw()
        total_draws[0] += a_draw
        total_draws[1] += b_draw
        total_draws[2] += c_draw

    if not any(d > 0 for d in total_draws):
        return

    # Reconstruct raw grid current, remove charger draw, re-split
    orig_consumption = (site.consumption.a, site.consumption.b, site.consumption.c)
    orig_export = (site.export_current.a, site.export_current.b, site.export_current.c)
    adj_consumption = []
    adj_export = []

    for i, label in enumerate(_PHASE_LABELS):
        cons = orig_consumption[i]
        exp = orig_export[i]
        draw = total_draws[i]
        if cons is None:
            adj_consumption.append(None)
            adj_export.append(None)
            continue
        raw_grid = cons - (exp or 0)
        true_grid = raw_grid - draw
        adj_cons = max(0.0, true_grid)
        adj_exp = max(0.0, -true_grid)

        # Warn when household consumption gets clamped to 0 by feedback
        if draw > 0 and adj_cons == 0 and cons > 0:
            _LOGGER.warning(
                "Phase %s: household -> 0 after feedback "
                "(raw_grid=%.1fA - charger=%.1fA = %.1fA)",
                label,
                raw_grid,
                draw,
                raw_grid - draw,
            )
        adj_consumption.append(adj_cons)
        adj_export.append(adj_exp)

    site.consumption = PhaseValues(*adj_consumption)
    site.export_current = PhaseValues(*adj_export)

    # Update derived solar after feedback (unified formula for grid and off-grid)
    solar_note = ""
    if solar_is_derived:
        export_after = site.export_current.total * site.voltage
        site.solar_production_total = _derive_solar_production(
            site.inverter_output_per_phase,
            site.wiring_topology,
            export_after,
            site.battery_power,
            site.voltage,
        )
        solar_note = f" | Solar(derived)={site.solar_production_total:.0f}W"

    _LOGGER.debug(
        "--- Feedback --- Subtracted A=%.1f B=%.1f C=%.1fA -> "
        "cons=(%s/%s/%s) exp=(%s/%s/%s)%s",
        total_draws[0],
        total_draws[1],
        total_draws[2],
        *[_fv(v) for v in adj_consumption],
        *[_fv(v) for v in adj_export],
        solar_note,
    )


def _build_circuit_groups(hass, hub_entry_id):
    """Build CircuitGroup objects from config entries for this hub.

    Returns list of CircuitGroup model objects for the calculation engine.
    """
    from .. import get_groups_for_hub

    group_entries = get_groups_for_hub(hass, hub_entry_id)
    # Build set of valid charger entry_ids for member validation
    valid_charger_ids = {
        e.entry_id
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER
    }
    groups = []
    for entry in group_entries:
        if entry is None:
            continue
        options = {**entry.data, **entry.options}
        current_limit = options.get(
            CONF_CIRCUIT_GROUP_CURRENT_LIMIT, DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT
        )
        raw_member_ids = options.get(CONF_CIRCUIT_GROUP_MEMBERS, [])
        # Filter out stale member references (deleted chargers)
        member_ids = [mid for mid in raw_member_ids if mid in valid_charger_ids]
        stale = set(raw_member_ids) - set(member_ids)
        if stale:
            _LOGGER.warning(
                "Circuit group '%s': removed %d stale member(s) — entries no longer exist",
                options.get(CONF_NAME, "Circuit Group"),
                len(stale),
            )
        group = CircuitGroup(
            group_id=entry.entry_id,
            name=options.get(CONF_NAME, "Circuit Group"),
            current_limit=float(current_limit),
            member_ids=member_ids,
        )
        groups.append(group)
        _LOGGER.debug(
            "  Circuit group '%s': limit=%.0fA, members=%s",
            group.name,
            group.current_limit,
            member_ids,
        )
    return groups


def _build_hub_result(
    site,
    raw_phases,
    voltage,
    main_breaker_rating,
    battery_soc,
    battery_soc_min,
    battery_max_discharge_power,
    battery_power,
    charger_targets,
    charger_available,
    charger_names,
    auto_detect_notifications=None,
    group_data=None,
    grid_stale=False,
    hub_status="OK",
    hub_warnings=None,
):
    """Build the result dict returned by run_hub_calculation."""
    # Grid available power (based on consumption after feedback loop)
    grid_headroom = sum(
        max(0, main_breaker_rating - c) * voltage
        for c in (site.consumption.a, site.consumption.b, site.consumption.c)
        if c is not None
    )

    # Battery rated discharge power (gated by SOC >= minimum). This is the
    # battery's capability, not what is spare right now — see battery_remaining.
    if (
        battery_soc is not None
        and battery_soc_min is not None
        and battery_soc >= battery_soc_min
        and battery_max_discharge_power
    ):
        battery_rated_discharge = round(float(battery_max_discharge_power), 0)
    else:
        battery_rated_discharge = 0

    # Total EVSE power = sum of actual charger draws
    total_evse_power = round(
        sum(
            (c.l1_current + c.l2_current + c.l3_current) * voltage
            for c in site.chargers
        ),
        0,
    )

    # Net site consumption
    net_consumption = sum(r for r in raw_phases if r is not None) * voltage

    # Cap grid headroom by max grid import power limit (if configured)
    if site.max_grid_import_power is not None:
        post_feedback_import = sum(
            c * voltage
            for c in (site.consumption.a, site.consumption.b, site.consumption.c)
            if c is not None
        )
        grid_headroom = min(
            grid_headroom,
            max(0, site.max_grid_import_power - max(0, post_feedback_import)),
        )

    # Solar power available to chargers = solar production - household loads
    # (household_consumption_total is set after feedback loop, so it excludes charger draws)
    solar_available = 0
    if site.solar_production_total and site.solar_production_total > 0:
        household = getattr(site, "household_consumption_total", None)
        if household is not None:
            solar_available = max(0, site.solar_production_total - household)
        else:
            # Derived solar mode: export IS the solar available (best approximation)
            solar_available = max(0, site.solar_production_total)

    # Battery power still spare for managed loads = rated discharge minus the
    # discharge already serving the household.
    current_battery_discharge = max(0, battery_power or 0)
    battery_remaining = max(0, battery_rated_discharge - current_battery_discharge)

    # Site remaining power = grid import headroom + power the inverter can
    # still source from solar and battery for managed loads. On an off-grid
    # system grid_headroom is 0, so this is purely inverter-sourced; on a
    # grid-tied system it is the sum of both paths.
    #
    # Two ceilings apply, and we take the lower:
    #  - Source: solar surplus + spare battery discharge.
    #  - Inverter: rated capacity minus what the inverter is *already*
    #    outputting (its AC output ≈ solar production + battery discharge).
    inverter_sourced = solar_available + battery_remaining
    if site.inverter_max_power:
        current_inverter_output = max(
            0, (site.solar_production_total or 0) + (battery_power or 0)
        )
        inverter_headroom = max(0, site.inverter_max_power - current_inverter_output)
        inverter_sourced = min(inverter_sourced, inverter_headroom)
        # Battery Remaining Power is likewise bounded by the inverter: the
        # battery cannot deliver more to loads than the inverter can pass.
        battery_remaining = min(battery_remaining, inverter_headroom)
    total_site_available = grid_headroom + inverter_sourced

    # Per-phase remaining current (A) = total remaining current on that phase,
    # i.e. grid + inverter. Each phase gets its share of grid headroom
    # (proportional to its raw breaker headroom, preserving asymmetric
    # loading) plus an equal share of inverter-sourced power. Summed across
    # the active phases this matches Site Remaining Power / voltage.
    num_phases = site.num_phases or 1
    phase_cons = (site.consumption.a, site.consumption.b, site.consumption.c)
    raw_phase_headroom = [
        max(0, main_breaker_rating - c) if c is not None else 0.0
        for c in phase_cons
    ]
    total_raw_headroom = sum(raw_phase_headroom[:num_phases])
    grid_current = grid_headroom / voltage if voltage else 0
    inverter_current_share = (
        inverter_sourced / voltage / num_phases if voltage else 0
    )
    available_per_phase = []
    for i, raw_hr in enumerate(raw_phase_headroom):
        if i >= num_phases:
            available_per_phase.append(0)
            continue
        if total_raw_headroom > 0:
            grid_part = grid_current * (raw_hr / total_raw_headroom)
        else:
            grid_part = 0
        available_per_phase.append(round(grid_part + inverter_current_share, 1))

    # Build per-charger operating modes dict
    charger_modes = {c.charger_id: c.operating_mode for c in site.chargers}

    # Per-charger effective priority rank — the order the engine serves loads
    # when power is contended: mode urgency first, then the configured priority
    # number (the same sort key _sort_chargers uses to distribute power). Rank
    # 1 is served first. Exposed so each device can show where it really
    # stands, since mode urgency can override the configured priority number.
    _ranked = sorted(
        site.chargers,
        key=lambda c: (c.mode_priority, c.priority),
    )
    charger_rank = {c.charger_id: idx + 1 for idx, c in enumerate(_ranked)}

    # Per-charger active phase count (for W-based OCPP profiles)
    # Uses actual draw to detect 1-phase car on 3-phase EVSE; falls back to configured phases.
    charger_active_phases = {}
    charger_phase_masks = {}
    for c in site.chargers:
        active = sum(
            1 for cur in (c.l1_current, c.l2_current, c.l3_current) if cur > 1.0
        )
        charger_active_phases[c.charger_id] = active if active > 0 else c.phases
        # Live site-phase mask: which site phases A/B/C are actively drawing
        site_draw = c.get_site_phase_draw()
        charger_phase_masks[c.charger_id] = "".join(
            phase for phase, draw in zip(("A", "B", "C"), site_draw) if draw > 1.0
        )

    return {
        CONF_TOTAL_ALLOCATED_CURRENT: round(sum(charger_targets.values()), 1),
        CONF_PHASES: site.num_phases,
        "calc_used": "calculate_all_charger_targets",
        # Site-level data for hub sensor
        "battery_soc": site.battery_soc,
        "battery_soc_min": site.battery_soc_min,
        "battery_soc_target": site.battery_soc_target,
        "battery_power": battery_power,
        "available_current_a": available_per_phase[0],
        "available_current_b": available_per_phase[1],
        "available_current_c": available_per_phase[2],
        "total_site_available_power": round(total_site_available, 0),
        "grid_power": round(net_consumption, 0),
        "available_grid_power": round(grid_headroom, 0),
        "available_battery_power": battery_remaining,
        "total_evse_power": total_evse_power,
        "solar_power": round(site.solar_production_total or 0, 0),
        "available_solar_power": round(solar_available, 0),
        "total_export_power": round(site.total_export_power, 0),
        # Per-charger targets
        "charger_targets": charger_targets,
        "charger_available": charger_available,
        "charger_names": charger_names,
        "charger_modes": charger_modes,
        "charger_rank": charger_rank,
        "charger_active_phases": charger_active_phases,
        "charger_phase_masks": charger_phase_masks,
        "distribution_mode": site.distribution_mode,
        # Auto-detection notifications (inversion, phase mapping)
        "auto_detect_notifications": auto_detect_notifications or [],
        # Circuit group data (for group sensors)
        "group_data": group_data or {},
        # Grid sensor health
        "grid_stale": grid_stale,
        # Hub status
        "hub_status": hub_status,
        "hub_warnings": hub_warnings or [],
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_hub_calculation(sensor):
    """
    Run the hub calculation: read HA states, build SiteContext, calculate targets.

    This is the main entry point for Home Assistant sensor updates.
    Reads HA entity states from hub config, builds a SiteContext, runs the
    calculation engine, and returns results for all chargers.

    Args:
        sensor: The HA sensor object containing config, hub_entry, and hass

    Returns:
        dict with calculated values including:
            - CONF_TOTAL_ALLOCATED_CURRENT: Total allocated current (A)
            - CONF_PHASES: Number of phases
            - CONF_CHARGING_MODE: Current charging mode
            - charger_targets: per-charger target currents
            - Other site/charger data
    """
    hass = sensor.hass
    hub_entry = sensor.hub_entry

    # --- Read hub config values ---
    voltage = (
        get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
        or DEFAULT_PHASE_VOLTAGE
    )
    if voltage <= 0:
        voltage = DEFAULT_PHASE_VOLTAGE
    main_breaker_rating = get_entry_value(
        hub_entry, CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING
    )
    excess_threshold = get_entry_value(
        hub_entry, CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD
    )

    # --- Read per-phase grid current (raw) ---
    raw_phases, _, _ = _read_grid_phases(hass, hub_entry)
    has_grid_cts = any(r is not None for r in raw_phases)

    # A site phase exists if it has EITHER a grid CT or an inverter output
    # sensor configured — the phase count is the combination of both. For a
    # phase with an inverter sensor but no grid CT (an off-grid site, or a
    # partially grid-metered one), grid current is taken as 0 A so the phase
    # still counts. Without this a 1-phase off-grid site would look 3-phase
    # and per-phase figures would be split across phantom phases.
    inv_phase_confs = (
        CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    )
    for i, conf in enumerate(inv_phase_confs):
        if raw_phases[i] is None and get_entry_value(hub_entry, conf, None):
            raw_phases[i] = 0.0
    # Nothing configured at all — fall back to a single phase.
    if all(r is None for r in raw_phases):
        raw_phases = [0.0, None, None]

    # --- Input EMA smoothing (grid CT, solar, battery power) ---
    hub_runtime = hass.data[DOMAIN]["hubs"].get(hub_entry.entry_id, {})
    ema_inputs = hub_runtime.setdefault("_ema_inputs", {})

    # --- Detect stale grid CT readings (configured but unavailable) ---
    phase_confs = (
        CONF_PHASE_A_CURRENT_ENTITY_ID,
        CONF_PHASE_B_CURRENT_ENTITY_ID,
        CONF_PHASE_C_CURRENT_ENTITY_ID,
    )
    any_grid_stale = False
    for i, conf in enumerate(phase_confs):
        entity_id = get_entry_value(hub_entry, conf, None)
        if not entity_id:
            continue  # Phase not configured
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            # Sensor is unavailable — hold last EMA value instead of using 0
            held = ema_inputs.get(f"grid_{i}")
            if held is not None:
                raw_phases[i] = held
            else:
                # No previous reading — assume breaker load for safety
                raw_phases[i] = main_breaker_rating
            any_grid_stale = True

    if any_grid_stale:
        if "grid_stale_since" not in hub_runtime:
            hub_runtime["grid_stale_since"] = time.monotonic()
            _LOGGER.warning("Grid CT sensor(s) unavailable — holding last known values")
        grid_stale_duration = time.monotonic() - hub_runtime["grid_stale_since"]
    else:
        if "grid_stale_since" in hub_runtime:
            _LOGGER.info(
                "Grid CT sensors recovered after %.0fs",
                time.monotonic() - hub_runtime["grid_stale_since"],
            )
        hub_runtime.pop("grid_stale_since", None)
        grid_stale_duration = 0

    smoothed_phases = [
        _smooth(ema_inputs, f"grid_{i}", r) for i, r in enumerate(raw_phases)
    ]
    consumption = [max(0, r) if r is not None else None for r in smoothed_phases]
    export = [max(0, -r) if r is not None else None for r in smoothed_phases]
    consumption_pv = PhaseValues(*consumption)
    export_pv = PhaseValues(*export)

    total_export_current = export_pv.total
    total_export_power = total_export_current * voltage if voltage > 0 else 0

    # --- Solar production (direct entity, or derived after inverter output below) ---
    solar_production_entity = get_entry_value(
        hub_entry, CONF_SOLAR_PRODUCTION_ENTITY_ID, None
    )
    solar_is_derived = not solar_production_entity
    if solar_production_entity:
        raw_solar = _read_entity(
            hass, solar_production_entity, 0, unit="W"
        )  # Convert kW→W if needed
        solar_production_total = _smooth(ema_inputs, "solar", raw_solar)
        # _smooth returns None when the solar entity is unavailable and there
        # is no EMA history yet (e.g. a fresh start at night). Treat that as
        # 0 W — None would crash the household-consumption math further down.
        if solar_production_total is None:
            solar_production_total = 0
    else:
        raw_solar = None  # derived — calculated after inverter output is read
        solar_production_total = 0  # placeholder

    # --- Battery data ---
    battery_soc_entity = get_entry_value(hub_entry, CONF_BATTERY_SOC_ENTITY_ID, None)
    battery_power_entity = get_entry_value(
        hub_entry, CONF_BATTERY_POWER_ENTITY_ID, None
    )
    battery_soc = (
        _coerce(_read_entity(hass, battery_soc_entity, None), None)
        if battery_soc_entity
        else None
    )
    raw_battery_power = (
        _read_entity(hass, battery_power_entity, None, unit="W")
        if battery_power_entity
        else None
    )  # Convert kW→W if needed
    battery_power = (
        _smooth(ema_inputs, "battery_power", raw_battery_power)
        if battery_power_entity
        else None
    )
    battery_soc_hysteresis = get_entry_value(
        hub_entry, CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS
    )
    battery_max_charge_power = get_entry_value(
        hub_entry, CONF_BATTERY_MAX_CHARGE_POWER, None
    )
    battery_max_discharge_power = get_entry_value(
        hub_entry, CONF_BATTERY_MAX_DISCHARGE_POWER, None
    )

    # --- Max grid import power (entity override → shared hub data → None) ---
    enable_max_import = get_entry_value(hub_entry, CONF_ENABLE_MAX_IMPORT_POWER, True)
    max_import_power_entity = get_entry_value(
        hub_entry, CONF_MAX_IMPORT_POWER_ENTITY_ID, None
    )
    if max_import_power_entity:
        max_grid_import_power = _coerce(
            _read_entity(hass, max_import_power_entity, None, unit="W"), None
        )  # Convert kW→W if needed
    elif enable_max_import:
        hub_rt = hass.data[DOMAIN]["hubs"].get(hub_entry.entry_id, {})
        max_grid_import_power = hub_rt.get("max_import_power", None)
    else:
        max_grid_import_power = None

    # --- Inverter configuration ---
    (
        inverter_max_power,
        inverter_max_power_per_phase,
        inverter_supports_asymmetric,
        wiring_topology,
        inverter_output_per_phase,
    ) = _read_inverter_config(hass, hub_entry, voltage)

    # Smooth inverter output per-phase (if configured)
    if inverter_output_per_phase is not None:
        inv_smoothed = [
            _smooth(ema_inputs, f"inv_{i}", getattr(inverter_output_per_phase, p))
            for i, p in enumerate(("a", "b", "c"))
        ]
        inverter_output_per_phase = PhaseValues(*inv_smoothed)

    # --- Derive solar production (unified for grid and off-grid) ---
    # Uses inverter output when available; falls back to grid export + battery.
    # For off-grid sites (no grid CTs), export is 0 so inverter formula applies.
    if solar_is_derived:
        solar_production_total = _derive_solar_production(
            inverter_output_per_phase,
            wiring_topology,
            total_export_power,
            battery_power,
            voltage,
        )

    # --- Runtime state from shared hub data (hub_runtime already fetched above) ---
    distribution_mode = hub_runtime.get("distribution_mode", DEFAULT_DISTRIBUTION_MODE)
    allow_grid_charging = hub_runtime.get("allow_grid_charging", True)
    power_buffer = hub_runtime.get("power_buffer", 0)
    battery_soc_target = hub_runtime.get(
        "battery_soc_target", DEFAULT_BATTERY_SOC_TARGET
    )
    battery_soc_min = hub_runtime.get("battery_soc_min", DEFAULT_BATTERY_SOC_MIN)

    # Apply SOC hysteresis — adjust thresholds so engine stays stateless
    now_above_target = False
    now_above_min = False
    if (
        battery_soc is not None
        and battery_soc_hysteresis
        and battery_soc_hysteresis > 0
    ):
        was_above_target = hub_runtime.get("_soc_above_target", False)
        if was_above_target:
            now_above_target = (
                battery_soc >= battery_soc_target - battery_soc_hysteresis
            )
        else:
            now_above_target = battery_soc >= battery_soc_target
        hub_runtime["_soc_above_target"] = now_above_target
        if now_above_target:
            battery_soc_target = battery_soc_target - battery_soc_hysteresis

        was_above_min = hub_runtime.get("_soc_above_min", False)
        if was_above_min:
            now_above_min = battery_soc >= battery_soc_min - battery_soc_hysteresis
        else:
            now_above_min = battery_soc >= battery_soc_min
        hub_runtime["_soc_above_min"] = now_above_min
        if now_above_min:
            battery_soc_min = battery_soc_min - battery_soc_hysteresis

    # Apply power buffer to reduce effective max grid import power
    if max_grid_import_power is not None and power_buffer > 0:
        max_grid_import_power = max(0, max_grid_import_power - power_buffer)

    # Apply excess-export hysteresis — keep the engine stateless. Once Excess
    # mode is on, the effective threshold drops by EXCESS_EXPORT_HYSTERESIS so a
    # charger doesn't chatter on/off when export hovers near the threshold.
    was_excess_on = hub_runtime.get("_excess_on", False)
    if was_excess_on:
        excess_on = total_export_power >= excess_threshold - EXCESS_EXPORT_HYSTERESIS
    else:
        excess_on = total_export_power > excess_threshold
    hub_runtime["_excess_on"] = excess_on
    if excess_on:
        excess_threshold = max(0, excess_threshold - EXCESS_EXPORT_HYSTERESIS)

    # --- Debug logging ---
    invert_phases = get_entry_value(hub_entry, CONF_INVERT_PHASES, False)
    _LOGGER.debug(
        "--- Hub Update --- CT: A=%sA B=%sA C=%sA (%dph, invert=%s) | "
        "Solar: %sW (%s) | Export: %sA/%sW",
        _fv2(raw_phases[0], smoothed_phases[0]),
        _fv2(raw_phases[1], smoothed_phases[1]),
        _fv2(raw_phases[2], smoothed_phases[2]),
        consumption_pv.active_count,
        "on" if invert_phases else "off",
        _fv2(raw_solar, solar_production_total),
        solar_production_entity or "derived",
        _fv(total_export_current),
        _fv(total_export_power),
    )
    _extra = []
    if battery_soc_entity:
        _bat_dir = (
            "chg"
            if (battery_power or 0) < 0
            else ("dischg" if (battery_power or 0) > 0 else "idle")
        )
        _hyst_min = "*" if now_above_min else ""
        _hyst_tgt = "*" if now_above_target else ""
        _extra.append(
            f"Bat: {_fv(battery_soc)}%/{_fv2(raw_battery_power, battery_power)}W({_bat_dir}) "
            f"min={_fv(battery_soc_min)}%{_hyst_min} tgt={_fv(battery_soc_target)}%{_hyst_tgt}"
        )
    if inverter_max_power or inverter_max_power_per_phase:
        _extra.append(
            f"Inv: {_fv(inverter_max_power)}W/{_fv(inverter_max_power_per_phase)}W/ph "
            f"{'asym' if inverter_supports_asymmetric else 'sym'} {wiring_topology}"
        )
    _LOGGER.debug(
        "  dist=%s grid_chg=%s buf=%sW max_import=%s%s",
        distribution_mode,
        "on" if allow_grid_charging else "off",
        _fv(power_buffer),
        f"{max_grid_import_power:.0f}W"
        if max_grid_import_power is not None
        else "unlimited",
        (" | " + " | ".join(_extra)) if _extra else "",
    )
    if inverter_output_per_phase:
        _LOGGER.debug(
            "  Inverter output: A=%sA B=%sA C=%sA",
            _fv(inverter_output_per_phase.a),
            _fv(inverter_output_per_phase.b),
            _fv(inverter_output_per_phase.c),
        )

    # --- Build SiteContext ---
    site = SiteContext(
        voltage=voltage,
        main_breaker_rating=main_breaker_rating,
        grid_current=PhaseValues(*raw_phases),
        consumption=consumption_pv,
        export_current=export_pv,
        solar_production_total=solar_production_total,
        solar_is_derived=solar_is_derived,
        battery_soc=float(battery_soc) if battery_soc is not None else None,
        battery_power=float(battery_power) if battery_power is not None else None,
        battery_soc_min=float(battery_soc_min) if battery_soc_min is not None else None,
        battery_soc_target=float(battery_soc_target)
        if battery_soc_target is not None
        else None,
        battery_soc_hysteresis=float(battery_soc_hysteresis)
        if battery_soc_hysteresis is not None
        else 5,
        battery_max_charge_power=float(battery_max_charge_power)
        if battery_max_charge_power is not None
        else None,
        battery_max_discharge_power=float(battery_max_discharge_power)
        if battery_max_discharge_power is not None
        else None,
        max_grid_import_power=float(max_grid_import_power)
        if max_grid_import_power is not None
        else None,
        inverter_max_power=float(inverter_max_power)
        if inverter_max_power is not None
        else None,
        inverter_max_power_per_phase=float(inverter_max_power_per_phase)
        if inverter_max_power_per_phase is not None
        else None,
        inverter_supports_asymmetric=inverter_supports_asymmetric,
        wiring_topology=wiring_topology,
        inverter_output_per_phase=inverter_output_per_phase,
        excess_export_threshold=excess_threshold,
        allow_grid_charging=allow_grid_charging,
        power_buffer=power_buffer,
        distribution_mode=distribution_mode,
    )

    # --- Add chargers ---
    hub_entry_id = (
        hub_entry.entry_id
        if hasattr(hub_entry, "entry_id")
        else hub_entry.data.get("hub_entry_id")
    )
    _add_chargers_to_site(hass, site, hub_entry_id, sensor)

    # --- Build circuit groups ---
    site.circuit_groups = _build_circuit_groups(hass, hub_entry_id)

    # Apply auto-detected phase remaps from previous cycles
    auto_detect_state = hub_runtime.setdefault("_auto_detect", {})
    phase_remaps = auto_detect_state.get("phase_remap", {})
    for charger in site.chargers:
        remap = phase_remaps.get(charger.charger_id)
        if remap:
            old = (charger.l1_phase, charger.l2_phase, charger.l3_phase)
            charger.l1_phase = remap["l1_phase"]
            charger.l2_phase = remap["l2_phase"]
            charger.l3_phase = remap["l3_phase"]
            # Recalculate active_phases_mask from new mapping
            if charger.phases == 3:
                charger.active_phases_mask = "".join(
                    sorted({charger.l1_phase, charger.l2_phase, charger.l3_phase})
                )
            elif charger.phases == 2:
                charger.active_phases_mask = "".join(
                    sorted({charger.l1_phase, charger.l2_phase})
                )
            elif charger.phases == 1:
                charger.active_phases_mask = charger.l1_phase
            _LOGGER.debug(
                "Auto-remap applied for %s: L1:%s→%s L2:%s→%s L3:%s→%s mask=%s",
                charger.entity_id,
                old[0],
                charger.l1_phase,
                old[1],
                charger.l2_phase,
                old[2],
                charger.l3_phase,
                charger.active_phases_mask,
            )

    # --- Feedback loop ---
    _apply_feedback_loop(site, solar_is_derived, voltage)

    # Compute household_consumption_total when solar entity provides ground truth
    if not solar_is_derived and solar_production_total > 0:
        export_power_after_feedback = site.export_current.total * site.voltage
        bp = float(battery_power) if battery_power is not None else 0
        site.household_consumption_total = max(
            0, solar_production_total + bp - export_power_after_feedback
        )
        _LOGGER.debug(
            "Computed household_consumption_total=%.1fW (solar=%.1fW + bat=%.1fW - export=%.1fW)",
            site.household_consumption_total,
            solar_production_total,
            bp,
            export_power_after_feedback,
        )

    # Compute per-phase household from inverter output entities (after feedback)
    household = compute_household_per_phase(site, site.wiring_topology)
    if household is not None:
        site.household_consumption = household
        _LOGGER.debug(
            "Per-phase household from inverter output (%s): A=%.1fA B=%.1fA C=%.1fA",
            site.wiring_topology,
            household.a if household.a is not None else 0,
            household.b if household.b is not None else 0,
            household.c if household.c is not None else 0,
        )

    # --- Calculate targets ---
    calculate_all_charger_targets(site)

    # --- Grid stale fallback: force min_current after timeout ---
    grid_stale = grid_stale_duration > GRID_STALE_TIMEOUT
    if grid_stale:
        _LOGGER.warning(
            "Grid CT unavailable for %.0fs (>%ds) — all chargers falling to minimum current",
            grid_stale_duration,
            GRID_STALE_TIMEOUT,
        )
        for charger in site.chargers:
            charger.allocated_current = (
                charger.min_current if charger.connector_status == "Charging" else 0
            )
            charger.available_current = charger.min_current

    charger_targets = {c.charger_id: c.allocated_current for c in site.chargers}
    charger_available = {c.charger_id: c.available_current for c in site.chargers}
    charger_names = {c.charger_id: c.entity_id for c in site.chargers}

    # --- Build per-group allocation data for group sensors ---
    group_data = {}
    charger_by_id = {c.charger_id: c for c in site.chargers}
    for group in site.circuit_groups:
        per_phase_draw = {"A": 0.0, "B": 0.0, "C": 0.0}
        for mid in group.member_ids:
            c = charger_by_id.get(mid)
            if c and c.allocated_current > 0 and c.active_phases_mask:
                for phase in c.active_phases_mask:
                    per_phase_draw[phase] += c.allocated_current
        # Headroom = limit minus max draw on any active phase
        active_draws = [
            per_phase_draw[p]
            for p in ("A", "B", "C")
            if site.consumption and getattr(site.consumption, p.lower()) is not None
        ]
        max_draw = max(active_draws) if active_draws else 0
        headroom = max(0, group.current_limit - max_draw)
        group_data[group.group_id] = {
            "name": group.name,
            "current_limit": group.current_limit,
            "member_ids": group.member_ids,
            "per_phase_draw": per_phase_draw,
            "max_phase_draw": round(max_draw, 1),
            "headroom": round(headroom, 1),
        }

    # --- Auto-detection (inversion + phase mapping) ---
    # auto_detect_state already initialized above (line 926)
    auto_notifications = []
    inv_notif = check_inversion(
        auto_detect_state,
        smoothed_phases,
        site.chargers,
        hub_entry.entry_id,
        get_entry_value(hub_entry, CONF_NAME, "Hub"),
    )
    if inv_notif:
        auto_notifications.append(inv_notif)
    if get_entry_value(hub_entry, CONF_AUTO_DETECT_PHASE_MAPPING, True):
        pm_results = check_phase_mapping(
            auto_detect_state,
            smoothed_phases,
            site.chargers,
            hub_entry.entry_id,
        )
        for notif in pm_results:
            # Store auto-remap for next cycle
            remap = notif.pop("auto_remap", None)
            if remap:
                auto_detect_state.setdefault("phase_remap", {})[remap["charger_id"]] = (
                    remap
                )
                # Reset correlation state so re-detection runs with new mapping
                # (allows 2-phase detection to verify/correct after 1-phase remap)
                pm_state = auto_detect_state.get("phase_map", {})
                pm_state.pop(remap["charger_id"], None)
            auto_notifications.append(notif)

    # --- Hub status (config validation + runtime state) ---
    # The hub Status sensor names exactly which sensor/input is missing or
    # unavailable so the user knows precisely what to fix.
    hub_status = "OK"
    hub_warnings = []

    has_inverter_output = inverter_output_per_phase is not None
    has_solar_entity = bool(solar_production_entity)

    if not has_grid_cts and not has_inverter_output and not has_solar_entity:
        hub_status = "Setup incomplete"
        hub_warnings.append(
            "No power-measurement input configured. Add at least one in the "
            "hub options: grid CT current sensors (grid-tied sites), inverter "
            "output power sensors, or a solar production sensor."
        )
    elif not has_grid_cts:
        # Off-grid: no grid CTs, so the battery is the primary state source.
        hub_warnings.append("Off-grid mode (no grid CTs)")
        if not battery_soc_entity:
            hub_status = "Setup incomplete"
            hub_warnings.append(
                "Off-grid hub needs a battery SOC sensor — it drives the "
                "operating-mode logic. Set it in the hub options."
            )
        if not battery_power_entity:
            hub_status = "Setup incomplete"
            hub_warnings.append(
                "Off-grid hub needs a battery power sensor — it is used to "
                "detect available solar surplus. Set it in the hub options."
            )
        if not has_inverter_output and not has_solar_entity:
            hub_warnings.append(
                "Off-grid hub has no inverter output or solar production "
                "sensor — available solar can only be inferred from battery "
                "charging. Add one for an accurate measurement."
            )

    if grid_stale:
        hub_status = "Grid sensors unavailable"
        hub_warnings.append(
            f"Grid CT sensors unavailable (stale for {grid_stale_duration:.0f}s)."
        )

    # Configured non-grid sensors that are currently unavailable.
    unavailable_warnings = _check_entity_availability(hass, hub_entry)
    if unavailable_warnings:
        hub_warnings.extend(unavailable_warnings)
        if hub_status == "OK":
            hub_status = "Sensor unavailable"

    # --- Build result ---
    return _build_hub_result(
        site,
        raw_phases,
        voltage,
        main_breaker_rating,
        battery_soc,
        battery_soc_min,
        battery_max_discharge_power,
        battery_power,
        charger_targets,
        charger_available,
        charger_names,
        auto_notifications,
        group_data,
        grid_stale=grid_stale,
        hub_status=hub_status,
        hub_warnings=hub_warnings,
    )


__all__ = [
    "SiteContext",
    "LoadContext",
    "PhaseValues",
    "calculate_all_charger_targets",
    "run_hub_calculation",
]
