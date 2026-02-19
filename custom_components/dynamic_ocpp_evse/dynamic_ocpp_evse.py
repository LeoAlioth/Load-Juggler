"""
Dynamic OCPP EVSE - Main calculation module.

This file provides a unified interface for EVSE calculations.
All core calculation logic has been refactored into the calculations/ directory.
"""

import logging
import time

from .calculations import (
    SiteContext,
    LoadContext,
    PhaseValues,
    CircuitGroup,
    calculate_all_charger_targets,
)
from .const import *
from .calculations.utils import is_number, compute_household_per_phase
from .helpers import get_entry_value
from .auto_detect import check_inversion, check_phase_mapping

_LOGGER = logging.getLogger(__name__)

# Phase labels used for loop-based per-phase processing
_PHASE_LABELS = ("A", "B", "C")


def _smooth(ema_dict: dict, key: str, raw, alpha: float = EMA_ALPHA):
    """Apply EMA smoothing to a sensor reading. Returns smoothed value.

    State is stored in ema_dict[key] between calls. None values pass through.
    """
    if raw is None:
        return None
    prev = ema_dict.get(key)
    if prev is None:
        ema_dict[key] = float(raw)
        return float(raw)
    smoothed = alpha * float(raw) + (1 - alpha) * prev
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


def _read_entity(hass, entity_id: str, default=0):
    """Read a numeric value from an HA entity, falling back to a default."""
    if not entity_id:
        return default
    state = hass.states.get(entity_id)
    if state and state.state not in ('unknown', 'unavailable', None, ''):
        try:
            return float(state.state)
        except (ValueError, TypeError):
            pass
    return default


def _read_inverter_output(hass, entity_id, voltage):
    """Read inverter output, auto-detecting A vs W and converting to A."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if not st or st.state in ('unknown', 'unavailable', None, ''):
        return None
    try:
        value = abs(float(st.state))
    except (ValueError, TypeError):
        return None
    # Auto-detect unit from entity attributes
    unit = st.attributes.get("unit_of_measurement", "").upper()
    if unit == "W" and voltage > 0:
        value = value / voltage  # Convert W → A
    return value


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
        for conf in (CONF_PHASE_A_CURRENT_ENTITY_ID, CONF_PHASE_B_CURRENT_ENTITY_ID, CONF_PHASE_C_CURRENT_ENTITY_ID)
    ]
    invert_phases = get_entry_value(hub_entry, CONF_INVERT_PHASES, False)

    raw_phases = []
    for entity in phase_entities:
        raw = _read_entity(hass, entity, 0) if entity else None
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
    inverter_max_power_per_phase = get_entry_value(hub_entry, CONF_INVERTER_MAX_POWER_PER_PHASE, None)
    inverter_supports_asymmetric = get_entry_value(hub_entry, CONF_INVERTER_SUPPORTS_ASYMMETRIC, False)
    wiring_topology = get_entry_value(hub_entry, CONF_WIRING_TOPOLOGY, DEFAULT_WIRING_TOPOLOGY)

    # Read per-phase inverter output entities (optional)
    inv_entities = [
        get_entry_value(hub_entry, conf, None)
        for conf in (CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID, CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID, CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID)
    ]
    inverter_output_per_phase = None
    if inv_entities[0]:
        inv_values = [_read_inverter_output(hass, e, voltage) for e in inv_entities]
        if inv_values[0] is not None:
            inverter_output_per_phase = PhaseValues(*inv_values)

    return inverter_max_power, inverter_max_power_per_phase, inverter_supports_asymmetric, wiring_topology, inverter_output_per_phase


def _build_evse_charger(hass, entry, voltage, charger_entity_id, priority):
    """Build a LoadContext for an OCPP EVSE charger."""
    charger_rt = hass.data[DOMAIN]["chargers"].get(entry.entry_id, {})
    config_min = get_entry_value(entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
    config_max = get_entry_value(entry, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)
    min_current = charger_rt.get("min_current") or config_min
    max_current = charger_rt.get("max_current") or config_max

    phases = int(get_entry_value(entry, CONF_PHASES, 3) or 3)

    # Read connector status from OCPP entity
    connector_status_entity = f"sensor.{charger_entity_id}_status_connector"
    connector_status_state = hass.states.get(connector_status_entity)
    connector_status = connector_status_state.state if connector_status_state else "Unknown"

    # Read L1/L2/L3 → site phase mapping
    l1_phase = get_entry_value(entry, CONF_CHARGER_L1_PHASE, "A")
    l2_phase = get_entry_value(entry, CONF_CHARGER_L2_PHASE, "B")
    l3_phase = get_entry_value(entry, CONF_CHARGER_L3_PHASE, "C")

    # Read per-charger operating mode from runtime data
    operating_mode = charger_rt.get("operating_mode", OPERATING_MODE_STANDARD)

    charger = LoadContext(
        charger_id=entry.entry_id,
        entity_id=charger_entity_id,
        min_current=min_current,
        max_current=max_current,
        phases=phases,
        priority=priority,
        connector_status=connector_status,
        operating_mode=operating_mode,
        l1_phase=l1_phase,
        l2_phase=l2_phase,
        l3_phase=l3_phase,
    )

    # Get OCPP current draw for this charger
    evse_import = entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
    if evse_import:
        evse_state = hass.states.get(evse_import)
        if evse_state and evse_state.state not in ['unknown', 'unavailable', None]:
            try:
                attrs = evse_state.attributes
                l1 = _read_phase_attr(attrs, ('l1_current', 'l1', 'phase_1', 'current_phase_1'))
                l2 = _read_phase_attr(attrs, ('l2_current', 'l2', 'phase_2', 'current_phase_2'))
                l3 = _read_phase_attr(attrs, ('l3_current', 'l3', 'phase_3', 'current_phase_3'))

                if l1 is not None or l2 is not None or l3 is not None:
                    charger.l1_current = l1 or 0
                    charger.l2_current = l2 or 0
                    charger.l3_current = l3 or 0

                    # Clamp per-phase draws at max_current (some chargers report total in per-phase)
                    # Allow 10% tolerance when using W-based profiles (voltage/rounding variance)
                    cru = get_entry_value(entry, CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT)
                    clamp_threshold = max_current * 1.1 if cru == CHARGE_RATE_UNIT_WATTS else max_current
                    for attr in ('l1_current', 'l2_current', 'l3_current'):
                        val = getattr(charger, attr)
                        if val > clamp_threshold:
                            _LOGGER.warning(
                                "EVSE %s: %s=%.1fA exceeds max_current=%.1fA — "
                                "clamping (charger may be reporting total instead of per-phase)",
                                charger_entity_id, attr, val, max_current,
                            )
                            setattr(charger, attr, max_current)
                else:
                    current_import = float(evse_state.state)
                    charger.l1_current = current_import
                    if phases >= 2:
                        charger.l2_current = current_import
                    if phases >= 3:
                        charger.l3_current = current_import
            except (ValueError, TypeError):
                pass

    _LOGGER.debug(
        "  EVSE %s [%s]: %s-%sA %dph(hw) L1->%s/L2->%s/L3->%s mask=%s(%dph) "
        "prio=%d [%s] draw=L1:%s/L2:%s/L3:%s",
        charger_entity_id, operating_mode,
        _fv(min_current), _fv(max_current), phases,
        l1_phase, l2_phase, l3_phase,
        charger.active_phases_mask,
        len(charger.active_phases_mask) if charger.active_phases_mask else 0,
        priority, connector_status,
        _fv(charger.l1_current), _fv(charger.l2_current), _fv(charger.l3_current),
    )
    return charger


def _build_plug_charger(hass, entry, voltage, charger_entity_id, priority, plug_auto_power):
    """Build a LoadContext for a smart load (plug) device."""
    charger_rt = hass.data[DOMAIN]["chargers"].get(entry.entry_id, {})
    slider_power = charger_rt.get("device_power", None)
    config_power = get_entry_value(entry, CONF_PLUG_POWER_RATING, DEFAULT_PLUG_POWER_RATING)
    power_rating = slider_power if slider_power is not None and slider_power > 0 else config_power

    connected_to_phase = get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A")
    phases = len(connected_to_phase)

    # Determine connector status from plug switch state
    plug_switch_entity = entry.data.get(CONF_PLUG_SWITCH_ENTITY_ID)
    plug_switch_state = hass.states.get(plug_switch_entity) if plug_switch_entity else None

    # Check power monitor if available (more reliable than switch state)
    power_monitor_entity = get_entry_value(entry, CONF_PLUG_POWER_MONITOR_ENTITY_ID, None)
    if power_monitor_entity:
        power_draw = _read_entity(hass, power_monitor_entity, 0)
        connector_status = "Charging" if power_draw > 10 else "Available"

        # Auto-adjust power rating from monitored draw (rolling average)
        if power_draw > 10:
            if DOMAIN not in hass.data:
                hass.data[DOMAIN] = {}
            avg_key = f"plug_power_avg_{entry.entry_id}"
            readings = hass.data[DOMAIN].get(avg_key, [])
            readings.append(power_draw)
            if len(readings) > 5:
                readings = readings[-5:]
            hass.data[DOMAIN][avg_key] = readings
            power_rating = sum(readings) / len(readings)
            plug_auto_power[entry.entry_id] = round(power_rating, 0)
            _LOGGER.debug(
                "Plug %s: auto-adjusted power rating to %.0fW (avg of %d readings)",
                charger_entity_id, power_rating, len(readings),
            )
    elif plug_switch_state:
        connector_status = "Charging" if plug_switch_state.state == "on" else "Available"
    else:
        connector_status = "Charging"

    equivalent_current = power_rating / (voltage * phases) if voltage > 0 else 0

    # Read per-charger operating mode from runtime data
    operating_mode = charger_rt.get("operating_mode", OPERATING_MODE_CONTINUOUS)

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
        operating_mode=operating_mode,
    )
    _LOGGER.debug(
        "  Plug %s [%s]: %.0fW on %s prio=%d [%s]%s",
        charger_entity_id, operating_mode, power_rating, connected_to_phase,
        priority, connector_status,
        " (auto-adj)" if entry.entry_id in plug_auto_power else "",
    )
    return charger


def _add_chargers_to_site(hass, site, hub_entry_id, sensor):
    """Build LoadContext objects for all chargers and add them to the site.

    Returns plug_auto_power dict for auto-adjusted plug power ratings.
    """
    from . import get_chargers_for_hub

    if hasattr(sensor, '_charger_entries'):
        chargers = sensor._charger_entries
    else:
        chargers = get_chargers_for_hub(hass, hub_entry_id)

    plug_auto_power = {}
    for entry in chargers:
        device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        charger_entity_id = entry.data.get(CONF_ENTITY_ID, f"charger_{entry.entry_id}")
        priority = get_entry_value(entry, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY)

        if device_type == DEVICE_TYPE_PLUG:
            charger = _build_plug_charger(hass, entry, site.voltage, charger_entity_id, priority, plug_auto_power)
        else:
            charger = _build_evse_charger(hass, entry, site.voltage, charger_entity_id, priority)

        # Clamp active_phases_mask to only include phases that exist on the site
        site_phases = {p for p, v in zip(_PHASE_LABELS, (site.consumption.a, site.consumption.b, site.consumption.c)) if v is not None}
        mask_phases = set(charger.active_phases_mask) if charger.active_phases_mask else set()
        if mask_phases and not mask_phases.issubset(site_phases):
            clamped = "".join(sorted(mask_phases & site_phases)) or charger.l1_phase
            _LOGGER.warning(
                "%s %s: phase mask %s includes phases not on site (%s) — clamping to %s",
                "Plug" if charger.device_type == DEVICE_TYPE_PLUG else "EVSE",
                charger_entity_id, charger.active_phases_mask,
                "".join(sorted(site_phases)), clamped,
            )
            charger.active_phases_mask = clamped

        site.chargers.append(charger)

    return plug_auto_power


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
                label, raw_grid, draw, raw_grid - draw,
            )
        adj_consumption.append(adj_cons)
        adj_export.append(adj_exp)

    site.consumption = PhaseValues(*adj_consumption)
    site.export_current = PhaseValues(*adj_export)

    # Update derived solar to match adjusted export + battery charge absorption
    solar_note = ""
    if solar_is_derived:
        site.solar_production_total = site.export_current.total * site.voltage
        # Battery charging absorbs solar power invisible to grid CT — add it back
        if site.battery_power is not None and site.battery_power < 0:
            site.solar_production_total += abs(site.battery_power)
        solar_note = f" | Solar(derived)={site.solar_production_total:.0f}W"

    _LOGGER.debug(
        "--- Feedback --- Subtracted A=%.1f B=%.1f C=%.1fA -> "
        "cons=(%s/%s/%s) exp=(%s/%s/%s)%s",
        total_draws[0], total_draws[1], total_draws[2],
        *[_fv(v) for v in adj_consumption],
        *[_fv(v) for v in adj_export],
        solar_note,
    )


def _build_circuit_groups(hass, hub_entry_id):
    """Build CircuitGroup objects from config entries for this hub.

    Returns list of CircuitGroup model objects for the calculation engine.
    """
    from . import get_groups_for_hub

    group_entries = get_groups_for_hub(hass, hub_entry_id)
    groups = []
    for entry in group_entries:
        if entry is None:
            continue
        options = {**entry.data, **entry.options}
        current_limit = options.get(CONF_CIRCUIT_GROUP_CURRENT_LIMIT, DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT)
        member_ids = options.get(CONF_CIRCUIT_GROUP_MEMBERS, [])
        group = CircuitGroup(
            group_id=entry.entry_id,
            name=options.get(CONF_NAME, "Circuit Group"),
            current_limit=float(current_limit),
            member_ids=member_ids,
        )
        groups.append(group)
        _LOGGER.debug(
            "  Circuit group '%s': limit=%.0fA, members=%s",
            group.name, group.current_limit, member_ids,
        )
    return groups


def _build_hub_result(site, raw_phases, voltage, main_breaker_rating,
                      battery_soc, battery_soc_min, battery_max_discharge_power,
                      battery_power, charger_targets, charger_available, charger_names,
                      plug_auto_power, auto_detect_notifications=None, group_data=None,
                      grid_stale=False):
    """Build the result dict returned by run_hub_calculation."""
    # Grid headroom per phase
    available_per_phase = []
    for raw in raw_phases:
        if raw is not None:
            available_per_phase.append(round(main_breaker_rating - raw, 1))
        else:
            available_per_phase.append(0)

    total_site_available = sum(
        max(0, main_breaker_rating - r) * voltage
        for r in raw_phases if r is not None
    )

    # Grid available power (based on consumption after feedback loop)
    grid_headroom = sum(
        max(0, main_breaker_rating - c) * voltage
        for c in (site.consumption.a, site.consumption.b, site.consumption.c) if c is not None
    )

    # Battery discharge power available for EV charging
    if (battery_soc is not None and battery_soc_min is not None
            and battery_soc >= battery_soc_min and battery_max_discharge_power):
        available_battery_power = round(float(battery_max_discharge_power), 0)
    else:
        available_battery_power = 0

    # Total EVSE power = sum of actual charger draws
    total_evse_power = round(
        sum((c.l1_current + c.l2_current + c.l3_current) * voltage for c in site.chargers), 0
    )

    # Net site consumption
    net_consumption = sum(r for r in raw_phases if r is not None) * voltage

    # Cap available power by max grid import power limit (if configured)
    if site.max_grid_import_power is not None:
        import_headroom = max(0, site.max_grid_import_power - max(0, net_consumption))
        total_site_available = min(total_site_available, import_headroom)
        post_feedback_import = sum(
            c * voltage for c in (site.consumption.a, site.consumption.b, site.consumption.c) if c is not None
        )
        grid_headroom = min(grid_headroom, max(0, site.max_grid_import_power - max(0, post_feedback_import)))

    # Solar power available to chargers = solar production - household loads
    # (household_consumption_total is set after feedback loop, so it excludes charger draws)
    solar_available = 0
    if site.solar_production_total and site.solar_production_total > 0:
        household = getattr(site, 'household_consumption_total', None)
        if household is not None:
            solar_available = max(0, site.solar_production_total - household)
        else:
            # Derived solar mode: export IS the solar available (best approximation)
            solar_available = max(0, site.solar_production_total)

    # Build per-charger operating modes dict
    charger_modes = {c.charger_id: c.operating_mode for c in site.chargers}

    # Per-charger active phase count (for W-based OCPP profiles)
    # Uses actual draw to detect 1-phase car on 3-phase EVSE; falls back to configured phases.
    charger_active_phases = {}
    for c in site.chargers:
        active = sum(1 for cur in (c.l1_current, c.l2_current, c.l3_current) if cur > 1.0)
        charger_active_phases[c.charger_id] = active if active > 0 else c.phases

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
        "available_battery_power": available_battery_power,
        "total_evse_power": total_evse_power,
        "solar_power": round(site.solar_production_total or 0, 0),
        "available_solar_power": round(solar_available, 0),

        # Per-charger targets
        "charger_targets": charger_targets,
        "charger_available": charger_available,
        "charger_names": charger_names,
        "charger_modes": charger_modes,
        "charger_active_phases": charger_active_phases,
        "distribution_mode": site.distribution_mode,

        # Auto-detected plug power ratings
        "plug_auto_power": plug_auto_power,

        # Auto-detection notifications (inversion, phase mapping)
        "auto_detect_notifications": auto_detect_notifications or [],

        # Circuit group data (for group sensors)
        "group_data": group_data or {},

        # Grid sensor health
        "grid_stale": grid_stale,
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
    voltage = get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
    main_breaker_rating = get_entry_value(hub_entry, CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING)
    excess_threshold = get_entry_value(hub_entry, CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD)

    # --- Read per-phase grid current (raw) ---
    raw_phases, _, _ = _read_grid_phases(hass, hub_entry)

    # --- Input EMA smoothing (grid CT, solar, battery power) ---
    hub_runtime = hass.data[DOMAIN]["hubs"].get(hub_entry.entry_id, {})
    ema_inputs = hub_runtime.setdefault("_ema_inputs", {})

    # --- Detect stale grid CT readings (configured but unavailable) ---
    phase_confs = (CONF_PHASE_A_CURRENT_ENTITY_ID, CONF_PHASE_B_CURRENT_ENTITY_ID, CONF_PHASE_C_CURRENT_ENTITY_ID)
    any_grid_stale = False
    for i, conf in enumerate(phase_confs):
        entity_id = get_entry_value(hub_entry, conf, None)
        if not entity_id:
            continue  # Phase not configured
        state = hass.states.get(entity_id)
        if state is None or state.state in ('unknown', 'unavailable', None, ''):
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
            _LOGGER.info("Grid CT sensors recovered after %.0fs",
                         time.monotonic() - hub_runtime["grid_stale_since"])
        hub_runtime.pop("grid_stale_since", None)
        grid_stale_duration = 0

    smoothed_phases = [_smooth(ema_inputs, f"grid_{i}", r) for i, r in enumerate(raw_phases)]
    consumption = [max(0, r) if r is not None else None for r in smoothed_phases]
    export = [max(0, -r) if r is not None else None for r in smoothed_phases]
    consumption_pv = PhaseValues(*consumption)
    export_pv = PhaseValues(*export)

    total_export_current = export_pv.total
    total_export_power = total_export_current * voltage if voltage > 0 else 0

    # --- Solar production ---
    solar_production_entity = get_entry_value(hub_entry, CONF_SOLAR_PRODUCTION_ENTITY_ID, None)
    solar_is_derived = not solar_production_entity
    if solar_production_entity:
        raw_solar = _read_entity(hass, solar_production_entity, 0)
        solar_production_total = _smooth(ema_inputs, "solar", raw_solar)
    else:
        raw_solar = None  # derived — no raw reading
        solar_production_total = total_export_power

    # --- Battery data ---
    battery_soc_entity = get_entry_value(hub_entry, CONF_BATTERY_SOC_ENTITY_ID, None)
    battery_power_entity = get_entry_value(hub_entry, CONF_BATTERY_POWER_ENTITY_ID, None)
    battery_soc = _read_entity(hass, battery_soc_entity, None) if battery_soc_entity else None
    raw_battery_power = _read_entity(hass, battery_power_entity, None) if battery_power_entity else None
    battery_power = _smooth(ema_inputs, "battery_power", raw_battery_power) if battery_power_entity else None
    battery_soc_hysteresis = get_entry_value(hub_entry, CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS)
    battery_max_charge_power = get_entry_value(hub_entry, CONF_BATTERY_MAX_CHARGE_POWER, None)
    battery_max_discharge_power = get_entry_value(hub_entry, CONF_BATTERY_MAX_DISCHARGE_POWER, None)

    # In derived mode, battery charging absorbs solar power invisible to grid CT.
    # Add it back to recover true solar production estimate.
    if solar_is_derived and battery_power is not None and battery_power < 0:
        solar_production_total += abs(battery_power)

    # --- Max grid import power (entity override → shared hub data → None) ---
    enable_max_import = get_entry_value(hub_entry, CONF_ENABLE_MAX_IMPORT_POWER, True)
    max_import_power_entity = get_entry_value(hub_entry, CONF_MAX_IMPORT_POWER_ENTITY_ID, None)
    if max_import_power_entity:
        max_grid_import_power = _read_entity(hass, max_import_power_entity, None)
    elif enable_max_import:
        hub_rt = hass.data[DOMAIN]["hubs"].get(hub_entry.entry_id, {})
        max_grid_import_power = hub_rt.get("max_import_power", None)
    else:
        max_grid_import_power = None

    # --- Inverter configuration ---
    inverter_max_power, inverter_max_power_per_phase, inverter_supports_asymmetric, \
        wiring_topology, inverter_output_per_phase = _read_inverter_config(hass, hub_entry, voltage)

    # Smooth inverter output per-phase (if configured)
    if inverter_output_per_phase is not None:
        inv_smoothed = [
            _smooth(ema_inputs, f"inv_{i}", getattr(inverter_output_per_phase, p))
            for i, p in enumerate(("a", "b", "c"))
        ]
        inverter_output_per_phase = PhaseValues(*inv_smoothed)

    # --- Runtime state from shared hub data (hub_runtime already fetched above) ---
    distribution_mode = hub_runtime.get("distribution_mode", DEFAULT_DISTRIBUTION_MODE)
    allow_grid_charging = hub_runtime.get("allow_grid_charging", True)
    power_buffer = hub_runtime.get("power_buffer", 0)
    battery_soc_target = hub_runtime.get("battery_soc_target", DEFAULT_BATTERY_SOC_TARGET)
    battery_soc_min = hub_runtime.get("battery_soc_min", DEFAULT_BATTERY_SOC_MIN)

    # Apply power buffer to reduce effective max grid import power
    if max_grid_import_power is not None and power_buffer > 0:
        max_grid_import_power = max(0, max_grid_import_power - power_buffer)

    # --- Debug logging ---
    invert_phases = get_entry_value(hub_entry, CONF_INVERT_PHASES, False)
    _LOGGER.debug(
        "--- Hub Update --- CT: A=%sA B=%sA C=%sA (%dph, invert=%s) | "
        "Solar: %sW (%s) | Export: %sA/%sW",
        _fv2(raw_phases[0], smoothed_phases[0]),
        _fv2(raw_phases[1], smoothed_phases[1]),
        _fv2(raw_phases[2], smoothed_phases[2]),
        consumption_pv.active_count, "on" if invert_phases else "off",
        _fv2(raw_solar, solar_production_total), solar_production_entity or "derived",
        _fv(total_export_current), _fv(total_export_power),
    )
    _extra = []
    if battery_soc_entity:
        _bat_dir = "chg" if (battery_power or 0) < 0 else ("dischg" if (battery_power or 0) > 0 else "idle")
        _extra.append(
            f"Bat: {_fv(battery_soc)}%/{_fv2(raw_battery_power, battery_power)}W({_bat_dir}) "
            f"min={_fv(battery_soc_min)}% tgt={_fv(battery_soc_target)}%"
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
        f"{max_grid_import_power:.0f}W" if max_grid_import_power is not None else "unlimited",
        (" | " + " | ".join(_extra)) if _extra else "",
    )
    if inverter_output_per_phase:
        _LOGGER.debug(
            "  Inverter output: A=%sA B=%sA C=%sA",
            _fv(inverter_output_per_phase.a), _fv(inverter_output_per_phase.b),
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
        battery_soc_target=float(battery_soc_target) if battery_soc_target is not None else None,
        battery_soc_hysteresis=float(battery_soc_hysteresis) if battery_soc_hysteresis is not None else 5,
        battery_max_charge_power=float(battery_max_charge_power) if battery_max_charge_power is not None else None,
        battery_max_discharge_power=float(battery_max_discharge_power) if battery_max_discharge_power is not None else None,
        max_grid_import_power=float(max_grid_import_power) if max_grid_import_power is not None else None,
        inverter_max_power=float(inverter_max_power) if inverter_max_power is not None else None,
        inverter_max_power_per_phase=float(inverter_max_power_per_phase) if inverter_max_power_per_phase is not None else None,
        inverter_supports_asymmetric=inverter_supports_asymmetric,
        wiring_topology=wiring_topology,
        inverter_output_per_phase=inverter_output_per_phase,
        excess_export_threshold=excess_threshold,
        allow_grid_charging=allow_grid_charging,
        power_buffer=power_buffer,
        distribution_mode=distribution_mode,
    )

    # --- Add chargers ---
    hub_entry_id = hub_entry.entry_id if hasattr(hub_entry, 'entry_id') else hub_entry.data.get('hub_entry_id')
    plug_auto_power = _add_chargers_to_site(hass, site, hub_entry_id, sensor)

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
                charger.active_phases_mask = "".join(sorted({charger.l1_phase, charger.l2_phase, charger.l3_phase}))
            elif charger.phases == 2:
                charger.active_phases_mask = "".join(sorted({charger.l1_phase, charger.l2_phase}))
            elif charger.phases == 1:
                charger.active_phases_mask = charger.l1_phase
            _LOGGER.debug(
                "Auto-remap applied for %s: L1:%s→%s L2:%s→%s L3:%s→%s mask=%s",
                charger.entity_id, old[0], charger.l1_phase,
                old[1], charger.l2_phase, old[2], charger.l3_phase,
                charger.active_phases_mask,
            )

    # --- Feedback loop ---
    _apply_feedback_loop(site, solar_is_derived, voltage)

    # Compute household_consumption_total when solar entity provides ground truth
    if not solar_is_derived and solar_production_total > 0:
        export_power_after_feedback = site.export_current.total * site.voltage
        bp = float(battery_power) if battery_power is not None else 0
        site.household_consumption_total = max(0, solar_production_total + bp - export_power_after_feedback)
        _LOGGER.debug(
            "Computed household_consumption_total=%.1fW (solar=%.1fW + bat=%.1fW - export=%.1fW)",
            site.household_consumption_total, solar_production_total, bp, export_power_after_feedback,
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
            grid_stale_duration, GRID_STALE_TIMEOUT,
        )
        for charger in site.chargers:
            charger.allocated_current = charger.min_current if charger.connector_status == "Charging" else 0
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
        active_draws = [per_phase_draw[p] for p in ("A", "B", "C")
                        if site.consumption and getattr(site.consumption, p.lower()) is not None]
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
    auto_detect_state = hub_runtime.setdefault("_auto_detect", {})
    auto_notifications = []
    inv_notif = check_inversion(
        auto_detect_state, smoothed_phases, site.chargers,
        hub_entry.entry_id, get_entry_value(hub_entry, CONF_NAME, "Hub"),
    )
    if inv_notif:
        auto_notifications.append(inv_notif)
    if get_entry_value(hub_entry, CONF_AUTO_DETECT_PHASE_MAPPING, True):
        pm_results = check_phase_mapping(
            auto_detect_state, smoothed_phases, site.chargers,
            hub_entry.entry_id,
        )
        for notif in pm_results:
            # Store auto-remap for next cycle
            remap = notif.pop("auto_remap", None)
            if remap:
                auto_detect_state.setdefault("phase_remap", {})[remap["charger_id"]] = remap
                # Reset correlation state so re-detection runs with new mapping
                # (allows 2-phase detection to verify/correct after 1-phase remap)
                pm_state = auto_detect_state.get("phase_map", {})
                pm_state.pop(remap["charger_id"], None)
            auto_notifications.append(notif)

    # --- Build result ---
    return _build_hub_result(
        site, raw_phases, voltage, main_breaker_rating,
        battery_soc, battery_soc_min, battery_max_discharge_power,
        battery_power, charger_targets, charger_available, charger_names,
        plug_auto_power, auto_notifications, group_data,
        grid_stale=grid_stale,
    )


__all__ = [
    "SiteContext",
    "LoadContext",
    "PhaseValues",
    "calculate_all_charger_targets",
    "run_hub_calculation",
]
