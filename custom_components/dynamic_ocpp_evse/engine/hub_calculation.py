"""
Load Juggler - Main calculation module.

This file provides a unified interface for EVSE calculations.
All core calculation logic has been refactored into the calculations/ directory.
"""

# PEP 604 unions (``float | None``) appear in this module's signatures. Nothing
# here evaluates annotations at runtime (no dataclasses, NamedTuple/TypedDict or
# get_type_hints calls), so deferring them keeps the module importable on the
# Python 3.9 interpreters the standalone test runners use (same arrangement as
# engine/auto_detect.py).
from __future__ import annotations

import logging
import math
import time

from ..calculations import (
    SiteContext,
    LoadContext,
    PhaseValues,
    CircuitGroup,
    calculate_all_charger_targets,
    excess_margin,
    clipping_forecast,
    battery_max_soc,
    headroom_deficit_kwh,
    recommended_charge_limit,
)
from ..const import (
    CHARGE_RATE_UNIT_WATTS,
    CONF_AUTO_DETECT_PHASE_MAPPING,
    CONF_BASE_CONSUMPTION,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_SOC_FULL,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_CHARGER_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_CHARGER_PRIORITY,
    CONF_CHARGE_RATE_UNIT,
    CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
    CONF_CIRCUIT_GROUP_MEMBERS,
    CONF_CLIMATE_ENTITY_ID,
    CONF_CONNECTED_TO_PHASE,
    CONF_DEVICE_TYPE,
    CONF_ENABLE_MAX_IMPORT_POWER,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_POWER_IMPORT_ENTITY_ID,
    CONF_EXCESS_HYSTERESIS,
    CONF_EXCESS_TRIGGER_MARGIN,
    CONF_FORECAST_SOC_FLOOR,
    CONF_GRID_EXPORT_LIMIT,
    CONF_HEATING_ELEMENT_POWER,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_INVERTER_SUPPORTS_ASYMMETRIC,
    CONF_INVERT_PHASES,
    CONF_MAIN_BREAKER_RATING,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_NAME,
    CONF_PHASES,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_PLUG_MAX_CURRENT,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
    CONF_PLUG_POWER_RATING,
    CONF_PLUG_SWITCH_ENTITY_ID,
    CONF_SITE_UPDATE_FREQUENCY,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_STATION_AC_INPUT_ENTITY_ID,
    CONF_STATION_AC_OUTPUT_ENTITY_ID,
    CONF_STATION_BATTERY_LEVEL_ENTITY_ID,
    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
    CONF_STATION_CHARGE_SPEED_ENTITY_ID,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_TANK_NORMAL_TEMPERATURE,
    CONF_TANK_POWER_ENTITY_ID,
    CONF_TANK_PRIORITIZE_BELOW_NORMAL,
    CONF_TOTAL_ALLOCATED_CURRENT,
    CONF_WIRING_TOPOLOGY,
    DEFAULT_BASE_CONSUMPTION,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_BATTERY_SOC_FULL,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_BATTERY_SOC_MIN,
    DEFAULT_BATTERY_SOC_TARGET,
    DEFAULT_CHARGER_PRIORITY,
    DEFAULT_CHARGE_RATE_UNIT,
    DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
    DEFAULT_DISTRIBUTION_MODE,
    DEFAULT_EXCESS_HYSTERESIS,
    DEFAULT_EXCESS_TRIGGER_MARGIN,
    DEFAULT_FORECAST_SOC_FLOOR,
    DEFAULT_GRID_EXPORT_LIMIT,
    DEFAULT_HEATING_ELEMENT_POWER,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_OPERATING_MODE_EVSE,
    DEFAULT_OPERATING_MODE_HOT_WATER_TANK,
    DEFAULT_OPERATING_MODE_PLUG,
    DEFAULT_OPERATING_MODE_POWER_STATION,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_PLUG_MAX_CURRENT,
    DEFAULT_PLUG_POWER_RATING,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DEFAULT_STATION_CHARGE_LIMIT,
    DEFAULT_STATION_MAX_CHARGE_POWER,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DEFAULT_TANK_NORMAL_TEMPERATURE,
    DEFAULT_TANK_PRIORITIZE_BELOW_NORMAL,
    DEFAULT_WIRING_TOPOLOGY,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_POWER_STATION,
    DOMAIN,
    EMA_ALPHA,
    ENTRY_TYPE,
    ENTRY_TYPE_CHARGER,
    FORECAST_SOC_HYSTERESIS,
    GRID_STALE_TIMEOUT,
    HOUSEHOLD_HOLD_BRIDGE_SECONDS,
    HOUSEHOLD_HOLD_RESIDUAL,
    INPUT_STALE_TIMEOUT,
    SETTLE_DRAW_CYCLES,
    SETTLE_DRAW_TOLERANCE,
    SETTLE_PERMIT_MARGIN,
    STATION_MODE_STANDARD,
    SUSPENDED_EV_IDLE_TIMEOUT,
    WIRING_TOPOLOGY_PARALLEL,
    WIRING_TOPOLOGY_SERIES,
    behavior_for,
    resolve_operating_mode,
    resolve_tank_mode_priority,
)
from ..calculations.utils import (
    is_number,
    compute_household_per_phase,
    grid_without_managed_draws,
    hold_per_phase_floor,
)
from ..helpers import get_entry_value
from ..registry import (
    get_chargers_for_hub,
    get_groups_for_hub,
    get_inverters_for_hub,
)
from .. import units
from .auto_detect import check_inversion, check_phase_mapping
from . import fleet
from .forecast_reader import (
    read_forecast_series,
    forecast_window,
    resolve_forecast_sensor,
    configured_forecast_sensors,
)

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


def _stale_guard(hub_runtime: dict, ema_dict: dict, key: str, raw, fallback):
    """Bound how long an unavailable sensor may coast on its held EMA value.

    While `raw` is _UNAVAILABLE the EMA downstream holds the last known value —
    fine for a brief dropout, but a sensor that died at 8 kW must not feed
    phantom power forever. After INPUT_STALE_TIMEOUT seconds of continuous
    unavailability this clears the EMA state and returns `fallback` instead,
    so the safe value takes effect immediately rather than decaying through
    the EMA. A fresh reading clears the timer.
    """
    stale_since = hub_runtime.setdefault("_input_stale_since", {})
    if raw is _UNAVAILABLE:
        since = stale_since.setdefault(key, time.monotonic())
        elapsed = time.monotonic() - since
        if elapsed > INPUT_STALE_TIMEOUT:
            if ema_dict.pop(key, None) is not None:
                _LOGGER.warning(
                    "Input '%s' unavailable for %.0fs (>%ds) — falling back to %s",
                    key,
                    elapsed,
                    INPUT_STALE_TIMEOUT,
                    fallback,
                )
            return fallback
        return raw
    stale_since.pop(key, None)
    return raw


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


def _clamp_reported_phase_draw(charger, entry, charger_entity_id, max_current):
    """Clamp a charger's reported per-phase draws at its max_current.

    Some OCPP integrations put the *total* current where a per-phase figure is
    expected — 24 A total on a 3-phase charger booked as 24 A on each line adds
    up to 72 A. The feedback loop then subtracts three times the real draw from
    grid consumption, fabricates export, and the engine hands out phantom
    surplus. Applies to every path that derives all phases from one number.

    Allows 10 % tolerance when using W-based charging profiles (voltage and
    rounding variance make a legitimate draw sit just above max_current).
    """
    cru = get_entry_value(entry, CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT)
    clamp_threshold = (
        max_current * 1.1 if cru == CHARGE_RATE_UNIT_WATTS else max_current
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


def _read_entity(hass, entity_id: str, default=0, unit: str = None, voltage: float = 0.0):
    """Read a numeric value from an HA entity, converted to ``unit``.

    Args:
        hass: Home Assistant instance
        entity_id: The entity ID to read
        default: Default value if entity not configured
        unit: Canonical unit wanted — "A", "W" or "V". None reads the raw
              number (percentages, and entities we ourselves wrote).
        voltage: Site phase voltage, required for A↔W conversions.

    Returns:
        float: The entity's numeric value, converted.
        _UNAVAILABLE: The entity is configured but currently unavailable/unknown.
        default: The entity_id is not provided (not configured).

    Conversion lives in units.py — every accepted unit is handled there, in
    one place, so no caller has to remember a half-done conversion. So does the
    availability predicate: ``units.is_unavailable`` is the one definition of
    an unusable state, and ``units.is_unusable_number`` catches the readings
    that parse but cannot be used (a "nan" state, or an Inf manufactured by the
    conversion itself). Both resolve to the same sentinel, so a NaN sensor now
    engages the caller's holdover and stale timeout exactly like a dead one
    instead of feeding NaN into the arithmetic.
    """
    if not entity_id:
        return default
    state = hass.states.get(entity_id)
    if units.is_unavailable(state):
        return _UNAVAILABLE
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return _UNAVAILABLE

    entity_unit = state.attributes.get("unit_of_measurement")
    if unit == units.DOMAIN_AMPS:
        value = units.to_amps(value, entity_unit, voltage)
    elif unit == units.DOMAIN_WATTS:
        value = units.to_watts(value, entity_unit, voltage)
    elif unit == units.DOMAIN_VOLTS:
        value = units.to_volts(value, entity_unit)
    if units.is_unusable_number(value):
        return _UNAVAILABLE
    return value


def _read_inverter_output(hass, entity_id, voltage):
    """Read one inverter output phase in amps, SIGNED (A/mA/W/kW all accepted).

    The sign is real information, so it is passed straight through. A hybrid
    with another (AC-coupled) inverter on its load port legitimately reads
    NEGATIVE output up to the child's production: power flows IN through the
    parent's AC-out port. Taking a magnitude there fabricates output that does
    not exist, and clamping it to 0 throws away the very term the fleet sum
    needs to net the child's back-feed against its parent.

    Consumers must therefore treat a negative reading as "power flowing into
    this inverter", not as production; the non-negativity clamps live at the
    aggregates where physics demands them (a member's derived production, the
    fleet solar total, per-phase household), never on the raw reading.

    Returns None for an unconfigured phase, _UNAVAILABLE for a configured
    sensor that is temporarily unreadable (the EMA smoother holds the last
    known value for those).
    """
    if not entity_id:
        return None
    value = _read_entity(hass, entity_id, None, unit=units.DOMAIN_AMPS, voltage=voltage)
    if value is _UNAVAILABLE or value is None:
        return _UNAVAILABLE
    return value


def _coerce(v, default=0):
    """Convert _UNAVAILABLE sentinel to a safe default for non-smoothed use."""
    return default if v is _UNAVAILABLE else v


def _check_entity_availability(hass, hub_entry) -> list:
    """Return unavailable hub-configured entities as ``(label, entity_id)``.

    Grid CTs are tracked separately (stale-timeout logic); this covers the
    solar, battery, inverter-output and max-import-power sensors so a missing
    feed shows up on the hub Status sensor instead of silently defaulting to 0.
    Returns the short label and the entity_id so the caller can both name the
    sensor in the status line and spell out the full detail in a warning.
    """
    unavailable = []
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
        if units.is_unavailable(hass.states.get(entity_id)):
            unavailable.append((label, entity_id))
    # Per-inverter entries: their output and battery sensors feed the fleet
    # aggregation, which fails open member-by-member — the status sensor is
    # where a dropout becomes visible, named per inverter.
    inverter_entries = get_inverters_for_hub(hass, hub_entry.entry_id)
    for inv_entry in inverter_entries:
        inv_name = get_entry_value(inv_entry, CONF_NAME, inv_entry.title)
        inv_checks = (
            (f"Solar production ({inv_name})", CONF_SOLAR_PRODUCTION_ENTITY_ID),
            (f"Inverter {inv_name} output (L1)", CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID),
            (f"Inverter {inv_name} output (L2)", CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID),
            (f"Inverter {inv_name} output (L3)", CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID),
            (f"Battery SOC ({inv_name})", CONF_BATTERY_SOC_ENTITY_ID),
            (f"Battery power ({inv_name})", CONF_BATTERY_POWER_ENTITY_ID),
        )
        for label, conf_key in inv_checks:
            entity_id = get_entry_value(inv_entry, conf_key, None)
            if not entity_id:
                continue
            if units.is_unavailable(hass.states.get(entity_id)):
                unavailable.append((label, entity_id))

    # Forecast sources fail open in the clipping maths — the status sensor is
    # the only place a dropout is visible. A configured forecast DEVICE with
    # no watts-bearing sensor right now (integration down, states missing)
    # counts as unavailable; legacy directly-configured sensors are checked
    # like any other entity.
    forecast_devices = list(
        get_entry_value(hub_entry, CONF_SOLAR_FORECAST_DEVICE_IDS, None) or []
    )
    for inv_entry in inverter_entries:
        for device_id in (
            get_entry_value(inv_entry, CONF_SOLAR_FORECAST_DEVICE_IDS, None) or []
        ):
            if device_id not in forecast_devices:
                forecast_devices.append(device_id)
    for device_id in forecast_devices:
        if resolve_forecast_sensor(hass, device_id) is None:
            unavailable.append(("Solar forecast device", device_id))
    for entity_id in get_entry_value(hub_entry, CONF_SOLAR_FORECAST_ENTITY_IDS, None) or []:
        if units.is_unavailable(hass.states.get(entity_id)):
            unavailable.append(("Solar forecast sensor", entity_id))
    return unavailable


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


def _read_grid_phase(hass, entity_id, voltage):
    """One grid phase in amps, SIGNED (A/mA/W/kW all accepted).

    Sign is the whole point of this reading — negative means export — so
    unlike the inverter-output reader this one must not take an absolute
    value. A meter's power entity is usually the only signed option it
    publishes (its current entity is often magnitude-only), which is why
    watts are accepted here at all.

    Returns _UNAVAILABLE for a configured-but-unreadable sensor; the caller's
    stale guard decides what to hold.
    """
    if not entity_id:
        return None
    return _read_entity(hass, entity_id, None, unit=units.DOMAIN_AMPS, voltage=voltage)


def _read_grid_phases(hass, hub_entry, voltage=DEFAULT_PHASE_VOLTAGE):
    """Read per-phase grid current and apply inversion.

    Returns a 3-list, one entry per phase:
      * ``None``          — no CT configured on this phase
      * ``_UNAVAILABLE``  — CT configured but its reading is not usable
      * ``float``         — signed amps (negative means export)

    The sentinel is NOT coerced to 0 A here, and that is the whole point. 0 A on
    a grid phase means "the house is importing nothing", which grants the entire
    main breaker as headroom — the single most dangerous value this function
    could invent. It used to return exactly that and rely on a separate stale
    block downstream to overwrite it, i.e. on two independently hand-rolled
    "is this usable?" tests agreeing forever. Propagating the sentinel instead
    forces the holdover to be what decides the substitute value; the caller has
    the EMA history and the stale timer, this reader has neither.

    The consumption/export split is deliberately not done here either: the
    caller only has a meaningful split after smoothing and the holdover, so it
    splits the smoothed phases itself.
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
        if not entity:
            raw_phases.append(None)
            continue
        raw = _read_grid_phase(hass, entity, voltage)
        if units.is_unusable_number(raw):
            # Sentinel (or anything else non-numeric) passes straight through —
            # inverting or defaulting it here would destroy the information the
            # caller's holdover needs.
            raw_phases.append(_UNAVAILABLE)
            continue
        raw_phases.append(-raw if invert_phases else raw)

    return raw_phases


def _resolve_grid_phases(raw_phases, ema_inputs, main_breaker_rating):
    """Substitute a safe value for every unreadable grid phase.

    The counterpart to _read_grid_phases' refusal to invent a number, and the
    ONLY place allowed to decide what an unreadable grid CT stands in for.
    Returns ``(resolved_phases, any_stale)``; ``resolved_phases`` holds only
    floats and Nones, so nothing unusable can reach the EMA smoothing, the
    engine, or the published grid figures.

    Documented failure-mode behaviour, unchanged:
      * a reading we have history for → hold the last known EMA value, which a
        brief dropout then coasts on with no visible effect;
      * no history at all (cold start) → assume the phase is loaded right up to
        the main breaker. Worst case on purpose: it hands out no headroom, where
        the 0 A this used to fall back to handed out all of it.
    The >GRID_STALE_TIMEOUT escalation is the caller's, driven by the
    ``any_stale`` flag through _track_grid_stale.

    Driven by the READINGS, not by a second walk over the config keys. The
    sentinel is the single source of truth for "this CT is unreadable", so there
    is no membership list here that can drift away from the reader's — which is
    what made the old coerce-to-0 arrangement a landmine rather than merely a
    duplication.

    Pure: a list, the EMA dict, a number. No hass, no config entry.
    """
    resolved = list(raw_phases)
    any_stale = False
    for i, raw in enumerate(resolved):
        if raw is None:
            continue  # No CT and no inverter sensor on this phase
        if not units.is_unusable_number(raw):
            continue  # Usable signed reading
        held = ema_inputs.get(f"grid_{i}")
        resolved[i] = main_breaker_rating if held is None else held
        any_stale = True
    return resolved, any_stale


def _track_grid_stale(hub_runtime, any_stale, now):
    """Seconds of CONTINUOUS grid-CT unavailability, 0 while the CTs are healthy.

    State lives in ``hub_runtime['grid_stale_since']``; a single healthy cycle
    clears it, so the duration only ever measures an unbroken outage. The caller
    compares it against GRID_STALE_TIMEOUT to force charging EVSEs down to
    minimum current and shed binary loads.

    Pure apart from the log lines: the clock is passed in, which is what lets
    the timeout path be tested without waiting a minute.
    """
    if any_stale:
        if "grid_stale_since" not in hub_runtime:
            hub_runtime["grid_stale_since"] = now
            _LOGGER.warning("Grid CT sensor(s) unavailable — holding last known values")
        return now - hub_runtime["grid_stale_since"]
    if "grid_stale_since" in hub_runtime:
        _LOGGER.info(
            "Grid CT sensors recovered after %.0fs",
            now - hub_runtime["grid_stale_since"],
        )
    hub_runtime.pop("grid_stale_since", None)
    return 0


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
    # Each configured phase is read independently — a B/C-only configuration
    # is valid, and one phase being momentarily unavailable must not discard
    # the others. Unavailable phases carry the _UNAVAILABLE sentinel, which
    # the EMA smoothing downstream resolves to the last known value.
    inverter_output_per_phase = None
    if any(inv_entities):
        inv_values = [_read_inverter_output(hass, e, voltage) for e in inv_entities]
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
    # The sliders refuse to cross each other (number.py), but a state restored
    # from an install that predates that guard still can. An inverted interval
    # makes every permit nonsensical, so collapse it — downwards, so a bad pair
    # can never authorise MORE current than the configured maximum. (The power
    # station builder below resolves its own inverted pair the other way; its
    # min is a trickle floor, not a hardware limit.)
    if min_current > max_current:
        _LOGGER.warning(
            "%s: min_current %.1fA is above max_current %.1fA — using %.1fA for both",
            charger_entity_id, min_current, max_current, max_current,
        )
        min_current = max_current

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
        rated_current=max_current,
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
        if not units.is_unavailable(evse_state):
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
                    _clamp_reported_phase_draw(
                        charger, entry, charger_entity_id, max_current
                    )
                    current_draw = "current_import_attr"
                else:
                    # A single total-ish reading copied onto every active phase
                    # needs the same clamp: if the entity really carries the
                    # site total, replicating it would triple-book the draw.
                    current_import = float(evse_state.state)
                    charger.l1_current = current_import
                    if phases >= 2:
                        charger.l2_current = current_import
                    if phases >= 3:
                        charger.l3_current = current_import
                    _clamp_reported_phase_draw(
                        charger, entry, charger_entity_id, max_current
                    )
                    current_draw = "current_import_total"
            except (ValueError, TypeError):
                pass

    # Fallback to Power Active Import if no current import data available
    if current_draw is None and evse_power_import:
        power_state = hass.states.get(evse_power_import)
        if not units.is_unavailable(power_state):
            try:
                # kW-aware: an OCPP integration reporting kW would otherwise
                # make a charging car look like it draws ~nothing, and the
                # engine would hand its allocation to something else too.
                power_w = units.to_watts(
                    float(power_state.state),
                    power_state.attributes.get("unit_of_measurement"),
                    voltage,
                )
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

    # No current-import source found — the engine cannot see this EVSE's real
    # draw, so its footprint falls back to its permit (it may reserve more than
    # it uses). Plugs and tanks always carry a correct draw and are never
    # flagged unmetered.
    charger.unmetered = current_draw is None

    # Draw-settle detection: the measured draw is trusted as the EVSE's real
    # footprint — freeing the unused gap to lower-priority loads — only when
    # two conditions hold: it has held steady for SETTLE_DRAW_CYCLES cycles
    # *and* it is measurably below the permit we offered last cycle. A car
    # drawing essentially what we offered (util ≈ 1.0) is using all of it, so
    # we keep treating the permit as its footprint. A still-ramping car keeps
    # changing and stays unsettled. Unmetered chargers have no draw to settle.
    measured_draw = max(charger.l1_current, charger.l2_current, charger.l3_current)
    if charger.unmetered:
        charger.draw_settled = False
        charger_rt.pop("_settle_last_draw", None)
        charger_rt.pop("_settle_count", None)
    else:
        last_draw = charger_rt.get("_settle_last_draw")
        if last_draw is not None and abs(measured_draw - last_draw) <= SETTLE_DRAW_TOLERANCE:
            charger_rt["_settle_count"] = charger_rt.get("_settle_count", 0) + 1
        else:
            charger_rt["_settle_count"] = 0
        charger_rt["_settle_last_draw"] = measured_draw
        steady = charger_rt["_settle_count"] >= SETTLE_DRAW_CYCLES
        under_permit = (
            measured_draw + SETTLE_PERMIT_MARGIN
            < charger_rt.get("_last_permit", 0)
        )
        charger.draw_settled = steady and under_permit

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
        mode.key,
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
    plug_max_current = get_entry_value(
        entry, CONF_PLUG_MAX_CURRENT, DEFAULT_PLUG_MAX_CURRENT
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
    # is on AND the reading is steady. A transient reading (a switch-off dip, a
    # compressor inrush spike) must not overwrite the configured rating, so we
    # require N consecutive readings within ±20 % of the *first* one before
    # committing the value. The candidate and its run length live in charger_rt
    # so they survive across calculation cycles.
    _POWER_STABLE_CYCLES = 3
    _POWER_STABLE_TOLERANCE = 0.20
    if power_monitor_entity and on and power_draw and power_draw > 10:
        candidate = charger_rt.get("power_candidate")
        if candidate is None or candidate <= 0:
            # First reading of a run — remember it as the yardstick to compare
            # the next cycles against.
            charger_rt["power_candidate"] = power_draw
            charger_rt["power_stable_count"] = 1
        elif abs(power_draw - candidate) <= candidate * _POWER_STABLE_TOLERANCE:
            stable_count = charger_rt.get("power_stable_count", 0) + 1
            charger_rt["power_stable_count"] = stable_count
            if stable_count >= _POWER_STABLE_CYCLES:
                power_rating = power_draw
                charger_rt["device_power"] = math.ceil(power_draw / 10) * 10
        else:
            # The reading moved off the candidate — the run is broken, restart
            # counting against the new value.
            charger_rt["power_candidate"] = power_draw
            charger_rt["power_stable_count"] = 1
    else:
        charger_rt["power_candidate"] = None
        charger_rt["power_stable_count"] = 0

    # Clamp to 0.1 A so the value survives the calculator's round(x, 1) and the
    # plug is not permanently locked off due to a very low power rating.
    equivalent_current = max(0.1, power_rating / (voltage * phases)) if voltage > 0 else 0

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
        rated_current=plug_max_current,
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


def _build_power_station_charger(hass, entry, voltage, charger_entity_id, priority):
    """Build a LoadContext for a portable power station (modulating load).

    The station charges at a commandable rate, so to the engine it is an EVSE
    without the OCPP: min/max current from the *configured* watt bounds (not the
    device's own, so it can be held below what the hardware allows), and the
    allocation is written back as an AC charging speed.

    Its managed draw is the charging component only — ``ac_input - ac_output``.
    Whatever is plugged into the station passes through to its outputs and is
    ordinary household consumption, not ours: counting it here would let the
    feedback loop add it back as available surplus.
    """
    charger_rt = hass.data[DOMAIN]["chargers"].get(entry.entry_id, {})

    # Charge bounds: runtime sliders win over the configured values, mirroring
    # the EVSE's min/max current.
    config_min = get_entry_value(
        entry, CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER
    )
    config_max = get_entry_value(
        entry, CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
    )
    min_power = charger_rt.get("station_min_charge_power") or config_min
    max_power = charger_rt.get("station_max_charge_power") or config_max
    if max_power < min_power:
        max_power = min_power

    connected_to_phase = get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A") or "A"
    phases = len(connected_to_phase)
    denom = voltage * phases
    min_current = min_power / denom if denom > 0 else 0
    max_current = max_power / denom if denom > 0 else 0

    speed_entity = entry.data.get(CONF_STATION_CHARGE_SPEED_ENTITY_ID)
    soc = _coerce(
        _read_entity(
            hass, get_entry_value(entry, CONF_STATION_BATTERY_LEVEL_ENTITY_ID, None), None
        ),
        None,
    )
    charge_limit = _coerce(
        _read_entity(
            hass, get_entry_value(entry, CONF_STATION_CHARGE_LIMIT_ENTITY_ID, None), None
        ),
        None,
    )
    if charge_limit is None:
        charge_limit = DEFAULT_STATION_CHARGE_LIMIT

    # Status. The station is inactive — and its power goes back to other loads —
    # once it has reached its own charge limit. It is *unavailable* when the
    # control entity is gone: these integrations talk BLE, which allows one
    # connection at a time, so opening the vendor app silently takes control
    # away from Home Assistant. Continuing to allocate power to a station we
    # cannot command would strand that power.
    speed_state = hass.states.get(speed_entity) if speed_entity else None
    if units.is_unavailable(speed_state):
        connector_status = "Unavailable"
    elif soc is not None and soc >= charge_limit:
        connector_status = "Available"
    else:
        connector_status = "Charging"

    # Managed draw: the charging component of the wall draw. Falls back to the
    # commanded speed when the AC sensors aren't configured, and only while the
    # station was last told to charge — an idle station draws nothing.
    ac_in = _coerce(
        _read_entity(
            hass,
            get_entry_value(entry, CONF_STATION_AC_INPUT_ENTITY_ID, None),
            None,
            unit="W",
        ),
        None,
    )
    ac_out = _coerce(
        _read_entity(
            hass,
            get_entry_value(entry, CONF_STATION_AC_OUTPUT_ENTITY_ID, None),
            None,
            unit="W",
        ),
        None,
    )
    if ac_in is not None and ac_out is not None:
        actual_draw_w = max(0.0, ac_in - abs(ac_out))
    elif charger_rt.get("station_charging"):
        # _read_entity parses and unit-converts; _coerce only maps the
        # unavailable sentinel, so the raw state string must not go through it.
        actual_draw_w = _coerce(_read_entity(hass, speed_entity, 0, unit="W"), 0) or 0
    else:
        actual_draw_w = 0

    mode = resolve_operating_mode(
        DEVICE_TYPE_POWER_STATION,
        charger_rt.get("operating_mode", DEFAULT_OPERATING_MODE_POWER_STATION.key),
    )
    # Storm reserve overrides the mode: filling a backup reserve only from
    # surplus is not a reserve, so it competes as a must-run load.
    if charger_rt.get("station_storm_reserve"):
        mode = STATION_MODE_STANDARD

    charger = LoadContext(
        charger_id=entry.entry_id,
        entity_id=charger_entity_id,
        min_current=min_current,
        max_current=max_current,
        phases=phases,
        priority=priority,
        active_phases_mask=connected_to_phase,
        connector_status=connector_status,
        device_type=DEVICE_TYPE_POWER_STATION,
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode.priority,
        rated_current=max_current,
        **_phase_draw(actual_draw_w, connected_to_phase, voltage),
    )
    _LOGGER.debug(
        "  Station %s [%s]: %.0f-%.0fW on %s prio=%d soc=%s%% limit=%s%% "
        "draw=%.0fW [%s]",
        charger_entity_id,
        mode.key,
        min_power,
        max_power,
        connected_to_phase,
        priority,
        _fv(soc),
        _fv(charge_limit),
        actual_draw_w,
        connector_status,
    )
    return charger


def _build_hot_water_tank_charger(hass, entry, voltage, charger_entity_id, priority):
    """Build a LoadContext for a hot water tank (climate-driven binary load).

    To the engine the tank is a smart load (plug): a fixed-power binary draw.
    The climate entity owns temperature regulation; the HA layer reads its
    hvac_action and writes the setpoint. Tank operating modes (Freeze
    Protection / Normal / Solar Priority / Solar Excess) map to engine modes
    here.
    """
    charger_rt = hass.data[DOMAIN]["chargers"].get(entry.entry_id, {})

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

    # Set power: the runtime slider if set, else the configured element
    # power. A configured tank power sensor overrides it with the live draw
    # while the element is heating, and is written back so the slider learns.
    #
    # The heating gate is essential: standby electronics or a circulation pump
    # keep the sensor at a few watts with the element off, and learning from
    # that would shrink the tank's equivalent_current to the 0.1 A floor —
    # a 2 kW load booked as free. With no hvac_action to confirm heating we
    # keep the configured rating rather than learn from an unknown state.
    element_power = get_entry_value(
        entry, CONF_HEATING_ELEMENT_POWER, DEFAULT_HEATING_ELEMENT_POWER
    )
    slider_power = charger_rt.get("device_power")
    power_rating = slider_power if slider_power else element_power
    power_entity = get_entry_value(entry, CONF_TANK_POWER_ENTITY_ID, None)
    live = None
    if power_entity:
        live = _coerce(_read_entity(hass, power_entity, 0, unit="W"))
        if live and live > 10 and hvac_action == "heating":
            power_rating = live
            charger_rt["device_power"] = round(live, 0)

    connected_to_phase = get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A") or "A"
    phases = len(connected_to_phase)

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

    # Cold-tank promotion: a Solar Priority tank below its normal temperature is
    # bumped to the Normal urgency tier so it beats other solar-priority loads
    # in contention. Only the tier is raised — the behavior stays Solar Priority,
    # so the tank still draws from solar + above-min battery and never deep-cycles
    # the bank below its minimum SOC. Toggleable per tank (default on).
    raw_temp = (
        climate_state.attributes.get("current_temperature") if climate_state else None
    )
    try:
        current_temp = float(raw_temp) if raw_temp is not None else None
    except (TypeError, ValueError):
        current_temp = None
    normal_temp = charger_rt.get("tank_normal_temperature") or get_entry_value(
        entry, CONF_TANK_NORMAL_TEMPERATURE, DEFAULT_TANK_NORMAL_TEMPERATURE
    )

    # Surplus demotion: a tank aiming at boost is heating on energy the site
    # would otherwise dump, so it competes at the Excess tier instead of its
    # mode's own. The label is whatever the command layer last wrote — one cycle
    # stale, which is the honest reading. Resolving it here instead would have to
    # use PRE-feedback export (chargers are built before the feedback loop and
    # the excess latch), and that figure is already depressed by the tank's own
    # draw, so a boosting tank would look like it wasn't. The lag only shifts
    # allocation order, and only while the site is contended.
    setpoint_label = charger_rt.get("tank_setpoint_label")

    mode_priority, elevated = resolve_tank_mode_priority(
        mode.key,
        mode.priority,
        current_temp,
        normal_temp,
        get_entry_value(
            entry,
            CONF_TANK_PRIORITIZE_BELOW_NORMAL,
            DEFAULT_TANK_PRIORITIZE_BELOW_NORMAL,
        ),
        setpoint_label,
    )
    charger_rt["tank_priority_elevated"] = elevated

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
        mode_priority=mode_priority,
        rated_current=equivalent_current,
        **_phase_draw(actual_draw_w, connected_to_phase, voltage),
    )
    _LOGGER.debug(
        "  Tank %s [%s]: %.0fW on %s prio=%d tier=%d (%s) [%s]",
        charger_entity_id,
        mode.key,
        power_rating,
        connected_to_phase,
        priority,
        mode_priority,
        setpoint_label,
        connector_status,
    )
    return charger


def _add_chargers_to_site(hass, site, hub_entry_id, charger_entries=None):
    """Build LoadContext objects for all chargers and add them to the site.

    ``charger_entries`` overrides the hub's registered loads (used by tests and
    by any caller that already knows the entries); None reads the registry.
    """
    if charger_entries is None:
        chargers = get_chargers_for_hub(hass, hub_entry_id)
    else:
        chargers = charger_entries

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
        elif device_type == DEVICE_TYPE_POWER_STATION:
            charger = _build_power_station_charger(
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


def _apply_feedback_loop(site, solar_is_derived, members):
    """Subtract charger draws from grid readings to prevent double-counting.

    Grid CTs measure total site current INCLUDING charger draws. Without this
    adjustment, the engine double-counts charger power as both 'consumption'
    and 'charger demand'. Modifies site.consumption and site.export_current
    in-place.
    """
    # Off-grid: the grid phase readings are synthetic zeros (no CTs exist) and
    # never contained the charger draws — subtracting them here would fabricate
    # export equal to each charger's own draw. Solar was already derived from
    # the inverter output upstream, so nothing needs re-deriving either.
    if site.is_off_grid:
        return

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
    new_consumption, new_export = grid_without_managed_draws(
        site.consumption, site.export_current, total_draws
    )
    adj_consumption = (new_consumption.a, new_consumption.b, new_consumption.c)
    adj_export = (new_export.a, new_export.b, new_export.c)

    for i, label in enumerate(_PHASE_LABELS):
        cons = orig_consumption[i]
        draw = total_draws[i]
        if cons is None:
            continue
        raw_grid = cons - (orig_export[i] or 0)
        # Warn when household consumption gets clamped to 0 by feedback
        if draw > 0 and adj_consumption[i] == 0 and cons > 0:
            _LOGGER.warning(
                "Phase %s: household -> 0 after feedback "
                "(raw_grid=%.1fA - charger=%.1fA = %.1fA)",
                label,
                raw_grid,
                draw,
                raw_grid - draw,
            )

    site.consumption = new_consumption
    site.export_current = new_export

    # Update derived solar after feedback. Same per-member derivation as the
    # first pass — only the export term changes, so a fleet where every member
    # measures its own production has nothing to redo (solar_is_derived False).
    solar_note = ""
    if solar_is_derived:
        export_after = site.export_current.total * site.voltage
        total = fleet.solar_total(members, site.voltage)
        if total is None:
            total = max(
                0.0, export_after + fleet.charging_power_total(members)
            )
        site.solar_production_total = total
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


# Legacy hub-level fleet fields: while any of these are configured on the hub
# entry itself (pre-import installs), the hub acts as one implicit inverter
# merged into the fleet. The one-time auto-import moves them onto a standalone
# inverter entry and blanks them here.
_LEGACY_FLEET_KEYS = (
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
)


def _smooth_member_output(output_pv, hub_runtime, ema_inputs, key_prefix):
    """Stale-guard + EMA a member's per-phase inverter output.

    The legacy hub member uses the historic ``inv_0..2`` keys, so pre-import
    behavior (and smoothing state) is bit-identical; inverter entries get
    keys namespaced by their entry_id.
    """
    if output_pv is None:
        return None
    smoothed = [
        _smooth(
            ema_inputs,
            f"{key_prefix}{i}",
            _stale_guard(
                hub_runtime,
                ema_inputs,
                f"{key_prefix}{i}",
                getattr(output_pv, p),
                None,
            ),
        )
        for i, p in enumerate(("a", "b", "c"))
    ]
    return PhaseValues(*smoothed)


def _read_fleet_member(hass, entry, hub_runtime, ema_inputs, voltage, *, legacy):
    """Read one inverter (an inverter entry, or the hub's legacy fields) into
    a FleetMember. ``legacy`` selects the historic EMA key namespace."""
    (
        max_power,
        max_power_per_phase,
        supports_asymmetric,
        topology,
        output_pv,
    ) = _read_inverter_config(hass, entry, voltage)
    output_prefix = "inv_" if legacy else f"inv_{entry.entry_id}_"
    output_pv = _smooth_member_output(output_pv, hub_runtime, ema_inputs, output_prefix)

    # Solar production sensor: this inverter's own PV output. The legacy hub
    # member keeps the historic "solar" EMA key so pre-import smoothing (and
    # its stale guard) is bit-identical.
    solar_entity = get_entry_value(entry, CONF_SOLAR_PRODUCTION_ENTITY_ID, None)
    solar_key = "solar" if legacy else f"solar_{entry.entry_id}"
    solar_measured = None
    if solar_entity:
        raw_solar = _read_entity(hass, solar_entity, 0, unit="W")  # kW→W if needed
        # A dead solar sensor must not keep feeding its last reading forever
        # (e.g. 8 kW held into the night) — fall back to 0 W after timeout.
        raw_solar = _stale_guard(hub_runtime, ema_inputs, solar_key, raw_solar, 0.0)
        solar_measured = _smooth(ema_inputs, solar_key, raw_solar)
        # _smooth returns None when the entity is unavailable and there is no
        # EMA history yet (a fresh start at night) — 0 W, not None, since the
        # household maths downstream cannot take None.
        if solar_measured is None:
            solar_measured = 0.0

    soc_entity = get_entry_value(entry, CONF_BATTERY_SOC_ENTITY_ID, None)
    power_entity = get_entry_value(entry, CONF_BATTERY_POWER_ENTITY_ID, None)
    battery_soc = (
        _coerce(_read_entity(hass, soc_entity, None), None) if soc_entity else None
    )
    power_key = "battery_power" if legacy else f"battery_power_{entry.entry_id}"
    raw_power = (
        _read_entity(hass, power_entity, None, unit="W") if power_entity else None
    )
    if power_entity:
        # A dead battery power sensor falls back to None after timeout,
        # dropping the battery-power-derived terms instead of coasting.
        raw_power = _stale_guard(hub_runtime, ema_inputs, power_key, raw_power, None)
    battery_power = (
        _smooth(ema_inputs, power_key, raw_power) if power_entity else None
    )

    return fleet.FleetMember(
        entry_id=entry.entry_id,
        name=get_entry_value(entry, CONF_NAME, entry.title if not legacy else "Hub"),
        max_power=max_power,
        max_power_per_phase=max_power_per_phase,
        supports_asymmetric=supports_asymmetric,
        topology=topology,
        output=output_pv,
        has_solar_entity=bool(solar_entity),
        solar_measured=solar_measured,
        forecast_device_ids=tuple(
            get_entry_value(entry, CONF_SOLAR_FORECAST_DEVICE_IDS, None) or ()
        ),
        has_battery=bool(soc_entity or power_entity),
        has_battery_power_entity=bool(power_entity),
        battery_soc=float(battery_soc) if battery_soc is not None else None,
        battery_power=float(battery_power) if battery_power is not None else None,
        charge_cap=get_entry_value(entry, CONF_BATTERY_MAX_CHARGE_POWER, None),
        discharge_cap=get_entry_value(entry, CONF_BATTERY_MAX_DISCHARGE_POWER, None),
        soc_full=get_entry_value(entry, CONF_BATTERY_SOC_FULL, DEFAULT_BATTERY_SOC_FULL),
        capacity_kwh=get_entry_value(
            entry, CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH
        ),
    )


def _read_fleet_members(hass, hub_entry, hub_runtime, ema_inputs, voltage):
    """All of the hub's inverters: standalone inverter entries plus, while its
    legacy hub-level fields remain configured, the hub itself as one implicit
    member (using the historic EMA keys, so pre-import behavior is
    bit-identical)."""
    members = []
    if any(get_entry_value(hub_entry, key, None) for key in _LEGACY_FLEET_KEYS):
        members.append(
            _read_fleet_member(
                hass, hub_entry, hub_runtime, ema_inputs, voltage, legacy=True
            )
        )
    hub_entry_id = getattr(hub_entry, "entry_id", None)
    if hub_entry_id:
        for entry in get_inverters_for_hub(hass, hub_entry_id):
            members.append(
                _read_fleet_member(
                    hass, entry, hub_runtime, ema_inputs, voltage, legacy=False
                )
            )
    return members


def _mixed_household_per_phase(site, members):
    """Per-phase household for a mixed-topology fleet: the parallel formula on
    the parallel members' summed outputs plus the series formula on the series
    members' — grid-bus loads show on the CT + parallel outputs, behind-series
    loads show in the series outputs. Best-effort superposition; uniform
    fleets never come here and keep the exact single-formula path."""
    original = site.inverter_output_per_phase
    try:
        site.inverter_output_per_phase = fleet.sum_outputs(
            members, WIRING_TOPOLOGY_PARALLEL
        )
        parallel_hh = (
            compute_household_per_phase(site, WIRING_TOPOLOGY_PARALLEL)
            if site.inverter_output_per_phase is not None
            else None
        )
        site.inverter_output_per_phase = fleet.sum_outputs(
            members, WIRING_TOPOLOGY_SERIES
        )
        series_hh = (
            compute_household_per_phase(site, WIRING_TOPOLOGY_SERIES)
            if site.inverter_output_per_phase is not None
            else None
        )
    finally:
        site.inverter_output_per_phase = original

    if parallel_hh is None and series_hh is None:
        return None
    values = []
    for phase in ("a", "b", "c"):
        parts = [
            getattr(hh, phase)
            for hh in (parallel_hh, series_hh)
            if hh is not None and getattr(hh, phase) is not None
        ]
        values.append(max(0.0, sum(parts)) if parts else None)
    return PhaseValues(*values)


def _household_hold_decay(hub_entry):
    """Per-cycle retention factor for the household floor hold.

    Derived from wall clock, not a magic per-cycle number: after
    HOUSEHOLD_HOLD_BRIDGE_SECONDS of a zero reading the held value has decayed
    to HOUSEHOLD_HOLD_RESIDUAL, whatever the configured cycle length.
    """
    try:
        cycle_seconds = float(
            get_entry_value(
                hub_entry,
                CONF_SITE_UPDATE_FREQUENCY,
                DEFAULT_SITE_UPDATE_FREQUENCY,
            )
        )
    except (TypeError, ValueError):
        cycle_seconds = float(DEFAULT_SITE_UPDATE_FREQUENCY)
    if not math.isfinite(cycle_seconds) or cycle_seconds <= 0:
        cycle_seconds = float(DEFAULT_SITE_UPDATE_FREQUENCY)
    return HOUSEHOLD_HOLD_RESIDUAL ** (
        cycle_seconds / HOUSEHOLD_HOLD_BRIDGE_SECONDS
    )


def _compute_forecast_advice(
    hass,
    hub_entry,
    hub_runtime,
    site,
    battery_soc,
    members,
):
    """Advisory battery headroom from the PV clipping forecast.

    Returns ``(hub_advice_or_None, per_inverter_advice)``. Enabled only when
    an export limit, fleet battery capacity and at least one forecast source
    are configured — the hub publishes advice sensors, it never commands the
    house battery (a future write-control on the inverter entries will
    optionally push these values to a device).

    Fleet semantics: capacity and charge rate are FLEET sums, and the charge
    sum is UNGATED — the forecast reserves room for the rest of the day, not
    for the current instant, so a battery that is momentarily full still
    counts (its ceiling advice is exactly what empties it). Reserving
    ``absorbable_kwh`` across the fleet at one uniform ceiling ``s`` gives
    ``Σ cap_i × (100 − s)/100`` — precisely what battery_max_soc computes
    from the summed capacity — so every battery is advised the same percent,
    splitting the headroom proportionally to capacity by construction. The
    recommended charge limit splits proportionally to each member's charge
    cap, clamped to its own cap.

    ONE fleet-level ratchet (`hub_runtime["_forecast_max_soc"]`), mirroring
    the Excess latch: the ceiling rises freely, falls only past
    FORECAST_SOC_HYSTERESIS. Never per-member — the ceiling is uniform by
    construction and per-member ratchets would diverge. Whole percent —
    inverter SOC registers are integers.
    """
    export_limit = (
        get_entry_value(hub_entry, CONF_GRID_EXPORT_LIMIT, DEFAULT_GRID_EXPORT_LIMIT)
        or 0
    )
    # Fleet capacity: the hub's legacy capacity field arrives via the implicit
    # legacy member, entry capacities via theirs.
    capacity_kwh = fleet.capacity_total(members)
    # Forecast sources are per inverter (each PV array belongs to one), but
    # clipping is a site question — every array competes for the same export
    # headroom — so the fleet's devices merge into one site forecast. The
    # hub's legacy fields arrive via the implicit legacy member.
    device_ids = fleet.forecast_device_ids(members)
    legacy_entity_ids = (
        get_entry_value(hub_entry, CONF_SOLAR_FORECAST_ENTITY_IDS, None) or []
    )
    if export_limit <= 0 or capacity_kwh <= 0 or not (device_ids or legacy_entity_ids):
        hub_runtime.pop("_forecast_max_soc", None)
        hub_runtime.pop("_forecast_parse_memo", None)
        return None, {}

    base_consumption = (
        get_entry_value(hub_entry, CONF_BASE_CONSUMPTION, DEFAULT_BASE_CONSUMPTION) or 0
    )
    soc_floor = get_entry_value(
        hub_entry, CONF_FORECAST_SOC_FLOOR, DEFAULT_FORECAST_SOC_FLOOR
    )
    threshold = export_limit + base_consumption

    # UNGATED fleet charge rate (see docstring) and total inverter capacity.
    fleet_charge_cap = sum(m.charge_cap or 0 for m in members) or None
    fleet_max_power, _, _ = fleet.inverter_limits(members)

    # Cap the summed series at what the site can physically produce — an
    # AC-coupled string inverter cannot deliver what Open-Meteo models from
    # kWp, and without the cap an oversized array over-reserves badly.
    power_cap = None
    if fleet_max_power:
        power_cap = fleet_max_power + (fleet_charge_cap or 0)

    entity_ids = configured_forecast_sensors(hass, device_ids, legacy_entity_ids)
    series = read_forecast_series(hass, entity_ids, hub_runtime)
    now, until = forecast_window()
    fc = clipping_forecast(
        series,
        threshold,
        now,
        until,
        charge_cap_w=fleet_charge_cap,
        power_cap_w=power_cap,
    )

    max_soc = battery_max_soc(fc.absorbable_kwh, capacity_kwh, soc_floor)
    proposed = round(max_soc)
    prev = hub_runtime.get("_forecast_max_soc")
    if prev is not None and prev - FORECAST_SOC_HYSTERESIS <= proposed < prev:
        proposed = prev
    hub_runtime["_forecast_max_soc"] = proposed

    deficit = headroom_deficit_kwh(fc.absorbable_kwh, capacity_kwh, battery_soc)
    charge_limit = None
    if fleet_charge_cap:
        charge_limit = recommended_charge_limit(
            fc.absorbable_kwh,
            battery_soc,
            proposed,
            fleet_charge_cap,
            site.solar_production_total or 0,
            threshold,
            FORECAST_SOC_HYSTERESIS,
        )

    # Per-inverter advice: the uniform ceiling for every battery member, and
    # the fleet charge limit split proportionally to each member's charge cap,
    # clamped to its own cap.
    per_inverter = {}
    hub_id = getattr(hub_entry, "entry_id", None)
    for m in members:
        if m.entry_id == hub_id or not m.has_battery or not (m.capacity_kwh or 0) > 0:
            continue
        member_limit = None
        if charge_limit is not None and fleet_charge_cap and m.charge_cap:
            member_limit = round(
                min(m.charge_cap, charge_limit * m.charge_cap / fleet_charge_cap), 0
            )
        per_inverter[m.entry_id] = {
            "forecast_battery_max_soc": proposed,
            "forecast_charge_limit_w": member_limit,
        }

    _LOGGER.debug(
        "Forecast advice: clip %.2f kWh / storable %.2f kWh above %dW to %s"
        " | max SOC %d%% (raw %.1f) deficit %.2f kWh charge cap %s",
        fc.clipped_kwh,
        fc.absorbable_kwh,
        threshold,
        until,
        proposed,
        max_soc,
        deficit,
        f"{charge_limit:.0f}W" if charge_limit is not None else "n/a",
    )

    return {
        "forecast_clipped_kwh": round(fc.clipped_kwh, 2),
        "forecast_absorbable_kwh": round(fc.absorbable_kwh, 2),
        "forecast_battery_max_soc": proposed,
        "forecast_headroom_deficit_kwh": round(deficit, 2),
        "forecast_charge_limit_w": (
            round(charge_limit, 0) if charge_limit is not None else None
        ),
    }, per_inverter


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
    excess_available=False,
    excess_margin_power=0,
    forecast_advice=None,
    inverters_data=None,
):
    """Build the result dict returned by run_hub_calculation."""
    # Grid available power (based on consumption after feedback loop).
    # Off-grid there is no grid feed at all — headroom is 0 by definition.
    if site.is_off_grid:
        grid_headroom = 0.0
    else:
        grid_headroom = sum(
            max(0, main_breaker_rating - c) * voltage
            for c in (site.consumption.a, site.consumption.b, site.consumption.c)
            if c is not None
        )

    # Battery rated discharge power (gated by SOC >= minimum). This is the
    # battery's capability, not what is spare right now — see battery_remaining.
    #
    # Mirror the distribution engine's gate (_calculate_inverter_limit): in
    # derived-solar mode the engine can only add battery discharge to the pool
    # when a battery-power sensor is present (without it the battery's effect on
    # the grid CT can't be untangled, so the engine treats it as 0). The display
    # must use the same gate or these sensors would advertise battery headroom
    # the engine never actually grants — masking exactly the case where a large
    # load stays off despite a healthy SOC.
    battery_discharge_unusable = site.solar_is_derived and battery_power is None
    if (
        battery_soc is not None
        and battery_soc_min is not None
        and battery_soc >= battery_soc_min
        and battery_max_discharge_power
        and not battery_discharge_unusable
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

    # Unmanaged (household) draw, W. NOT household_consumption_total — that is
    # only the inverter-served share (solar + battery − export), which omits
    # everything the grid is serving and understated household by the full
    # grid import. The site-bus identity counts both supply paths:
    #   net grid + solar + battery discharge − managed draw
    # (battery power is positive when discharging, so the signed value also
    # handles charging; export shows up as negative net grid).
    #  1. Measured solar: the identity is exact.
    #  2. Derived solar with inverter output entities: use the engine's
    #     per-phase household (grid + inverter output − export per phase),
    #     since derived solar is itself built from these terms.
    #  3. Last resort: the identity with derived solar — best effort.
    hh_phases = getattr(site, "household_consumption", None)
    _identity_household = max(
        0,
        net_consumption
        + (site.solar_production_total or 0)
        + (battery_power or 0)
        - total_evse_power,
    )
    if not site.solar_is_derived and site.solar_production_total:
        household_power = round(_identity_household, 0)
    elif hh_phases is not None:
        household_power = round(
            sum(v for v in (hh_phases.a, hh_phases.b, hh_phases.c) if v is not None)
            * voltage,
            0,
        )
    else:
        household_power = round(_identity_household, 0)

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
    #  - Inverter: rated capacity minus what the inverters are *already*
    #    outputting. That output is MEASURED when output entities exist and
    #    otherwise estimated topology-aware per fleet member — the old
    #    solar + battery_power form was the series (DC-coupled) model only, and
    #    on a parallel (AC-coupled) site it understated the output by the whole
    #    battery charge power, advertising headroom the site does not have.
    #
    #    The figure is site.inverter_output_total — captured at READ time,
    #    before the feedback loop, the same one the calculator's coverage gate
    #    consumes (#17). Recomputing it here from the post-feedback scalars
    #    inflated the estimate on a derived-solar site by the managed draws the
    #    feedback loop folds back into solar, understating Site Remaining Power
    #    by exactly the running loads' draw (issue #48).
    inverter_sourced = solar_available + battery_remaining
    if site.inverter_max_power:
        current_inverter_output = (
            site.inverter_output_total
            if site.inverter_output_total is not None
            else 0.0
        )
        # Headroom is clamped to the inverter's own rating: a negative measured
        # output (a cascaded inverter feeding power IN through the load port)
        # means the site is absorbing, but it does NOT raise this inverter's AC
        # output capability above its nameplate — so it cannot buy extra
        # headroom. Above the rating the headroom is 0, as before.
        inverter_headroom = max(
            0.0,
            min(
                float(site.inverter_max_power),
                site.inverter_max_power - current_inverter_output,
            ),
        )
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
    #
    # A phase is gated on whether IT exists (consumption is not None), never on
    # its index versus the phase count: the site's phases need not be a prefix
    # of A/B/C — a B+C-only installation is explicitly supported. Indexing by
    # count would zero phase C and hand phase A (which does not exist) the
    # inverter share.
    phase_cons = (site.consumption.a, site.consumption.b, site.consumption.c)
    num_phases = site.num_phases or 1
    raw_phase_headroom = [
        max(0, main_breaker_rating - c) if c is not None else 0.0
        for c in phase_cons
    ]
    total_raw_headroom = sum(raw_phase_headroom)
    grid_current = grid_headroom / voltage if voltage else 0
    inverter_current_share = (
        inverter_sourced / voltage / num_phases if voltage else 0
    )
    available_per_phase = []
    for i, raw_hr in enumerate(raw_phase_headroom):
        if phase_cons[i] is None:
            available_per_phase.append(0)
            continue
        if total_raw_headroom > 0:
            grid_part = grid_current * (raw_hr / total_raw_headroom)
        else:
            grid_part = 0
        available_per_phase.append(round(grid_part + inverter_current_share, 1))

    # Per-pool remaining current (A) — the headroom each source still offers to
    # managed loads, broken out for diagnostics. grid + inverter is the total
    # remaining current available to loads. solar and battery are the two parts
    # that feed the inverter pool: the inverter figure is their sum capped by
    # the inverter's own rated headroom, so it can be smaller than solar +
    # battery when the inverter is the binding constraint. A managed load only
    # turns on if its minimum current fits within the inverter (off-grid) or
    # grid + inverter (grid-tied) figure — so a battery reading of ~0 here is
    # the usual reason a large load stays off despite a healthy SOC.
    grid_remaining_current = grid_headroom / voltage if voltage else 0
    solar_remaining_current = solar_available / voltage if voltage else 0
    battery_remaining_current = battery_remaining / voltage if voltage else 0
    inverter_remaining_current = inverter_sourced / voltage if voltage else 0

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

    # Per-charger actual draw — the measured current the load is really
    # pulling (sum of phase currents). For a binary load this is what the
    # device draws right now, which can be far below its reserved allocation
    # (e.g. a metered plug switched on but its appliance idle).
    charger_draw = {
        c.charger_id: round(c.l1_current + c.l2_current + c.l3_current, 1)
        for c in site.chargers
    }

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
        "available_grid_current": round(grid_remaining_current, 1),
        "available_solar_current": round(solar_remaining_current, 1),
        "available_battery_current": round(battery_remaining_current, 1),
        "available_inverter_current": round(inverter_remaining_current, 1),
        "total_site_available_power": round(total_site_available, 0),
        "grid_power": round(net_consumption, 0),
        "available_grid_power": round(grid_headroom, 0),
        "available_battery_power": battery_remaining,
        "total_evse_power": total_evse_power,
        "household_power": household_power,
        "solar_power": round(site.solar_production_total or 0, 0),
        "available_solar_power": round(solar_available, 0),
        "total_export_power": round(site.total_export_power, 0),
        # The one Excess decision, computed by excess_margin() with the hysteresis
        # latch applied. Every Excess-mode load reads this rather than re-deriving
        # the rule — including the hot water tank, whose boost setpoint is
        # resolved in the HA layer. The margin is how many watts past (or short
        # of) the trigger the site is; the per-sink split is in the debug log.
        "excess_available": excess_available,
        "excess_margin_power": round(excess_margin_power, 0),
        # Per-charger targets
        "charger_targets": charger_targets,
        "charger_available": charger_available,
        "charger_names": charger_names,
        "charger_modes": charger_modes,
        "charger_rank": charger_rank,
        "charger_draw": charger_draw,
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
        # Per-inverter-entry data (for the inverter devices' own sensors)
        "inverters": inverters_data or {},
        # Advisory battery headroom from the PV clipping forecast. Keys are
        # present only while the feature is configured — the matching sensors
        # are gated the same way.
        **(forecast_advice or {}),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_hub_calculation(hass, hub_entry, charger_entries=None):
    """
    Run the hub calculation: read HA states, build SiteContext, calculate targets.

    This is the ONE site calculation for a hub, run once per site cycle by the
    hub's DataUpdateCoordinator (see sensor.py). It takes no entity — every
    cycle-counted mechanism inside (settle counters, input EMAs, power-stable
    counts) advances exactly once per call, so a site with N loads no longer
    advances them N times per interval.

    Args:
        hass: Home Assistant instance
        hub_entry: the hub's ConfigEntry
        charger_entries: optional explicit list of load config entries; None
            reads the hub's registered loads

    Returns:
        dict with calculated values including:
            - CONF_TOTAL_ALLOCATED_CURRENT: Total allocated current (A)
            - CONF_PHASES: Number of phases
            - CONF_CHARGING_MODE: Current charging mode
            - charger_targets: per-charger target currents
            - Other site/charger data
    """
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
    # Excess trigger, derived from the physical export limit: engage once
    # export is within the trigger margin of the limit (an inverter curtails
    # slightly under the limit, so a trigger exactly AT it would never fire).
    # No limit configured (0) means the grid can absorb everything — the
    # allowance is infinite, grid-side Excess never triggers, and only the
    # battery side of excess_margin() remains.
    grid_export_limit = (
        get_entry_value(hub_entry, CONF_GRID_EXPORT_LIMIT, DEFAULT_GRID_EXPORT_LIMIT)
        or 0
    )
    excess_trigger_margin = get_entry_value(
        hub_entry, CONF_EXCESS_TRIGGER_MARGIN, DEFAULT_EXCESS_TRIGGER_MARGIN
    )
    # Release band once Excess is engaged (the latch below applies it).
    excess_hysteresis = (
        get_entry_value(hub_entry, CONF_EXCESS_HYSTERESIS, DEFAULT_EXCESS_HYSTERESIS)
        or 0
    )
    if grid_export_limit > 0:
        excess_threshold = max(0.0, grid_export_limit - excess_trigger_margin)
    else:
        excess_threshold = float("inf")

    # --- Read per-phase grid current (raw, signed; W/kW converted to A) ---
    # Entries are floats, None (no CT on that phase) or the _UNAVAILABLE
    # sentinel (CT configured but unreadable) — _resolve_grid_phases below is
    # the only thing allowed to substitute a number for the sentinel. Until
    # then, every test here has to be on None, never on truthiness or 0.
    raw_phases = _read_grid_phases(hass, hub_entry, voltage)
    has_grid_cts = any(r is not None for r in raw_phases)

    # A site phase exists if it has EITHER a grid CT or an inverter output
    # sensor configured — the phase count is the combination of both. For a
    # phase with an inverter sensor but no grid CT (an off-grid site, or a
    # partially grid-metered one), grid current is taken as 0 A so the phase
    # still counts. Without this a 1-phase off-grid site would look 3-phase
    # and per-phase figures would be split across phantom phases. A phase whose
    # CT is merely unreadable is NOT 0 A — the sentinel is not None, so it falls
    # through to the holdover instead.
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

    # --- Resolve unreadable grid CTs (the only place allowed to substitute) ---
    raw_phases, any_grid_stale = _resolve_grid_phases(
        raw_phases, ema_inputs, main_breaker_rating
    )
    grid_stale_duration = _track_grid_stale(
        hub_runtime, any_grid_stale, time.monotonic()
    )

    smoothed_phases = [
        _smooth(ema_inputs, f"grid_{i}", r) for i, r in enumerate(raw_phases)
    ]
    consumption = [max(0, r) if r is not None else None for r in smoothed_phases]
    export = [max(0, -r) if r is not None else None for r in smoothed_phases]
    consumption_pv = PhaseValues(*consumption)
    export_pv = PhaseValues(*export)

    total_export_current = export_pv.total
    total_export_power = total_export_current * voltage if voltage > 0 else 0

    # --- Inverter fleet (inverter entries + legacy hub-level fields) ---
    # Every battery/inverter scalar below is a fleet aggregate. With a single
    # member (the classic setup) each aggregate reduces to exactly the old
    # singleton value — see engine/fleet.py for the per-member gating rules.
    members = _read_fleet_members(hass, hub_entry, hub_runtime, ema_inputs, voltage)

    # --- Solar production (unified for grid and off-grid) ---
    # Per member: its own production sensor when configured, else derived from
    # its inverter output (parallel output IS production, series output minus
    # its own battery power). Falls back to grid export + the fleet's charging
    # draw when no member knows either. Off-grid, export is naturally 0.
    # Solar counts as derived unless EVERY member measures its own production;
    # a partly-measured fleet still needs the post-feedback re-derivation for
    # the members that only have inverter outputs (or none at all).
    solar_is_derived = not fleet.solar_is_measured(members)
    solar_production_total = fleet.solar_total(members, voltage)
    if solar_production_total is None:
        solar_production_total = max(
            0.0,
            (total_export_power or 0) + fleet.charging_power_total(members),
        )

    # --- Battery data (fleet) ---
    battery_soc = fleet.weighted_soc(members)
    battery_power = fleet.battery_power_total(members)
    battery_soc_hysteresis = get_entry_value(
        hub_entry, CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS
    )
    # Charge capacity sums only members whose own battery is below its own
    # full-SOC; discharge is summed after the SOC-min hysteresis latch below.
    battery_max_charge_power = fleet.charge_power_total(members)

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

    # --- Inverter configuration (fleet) ---
    # Member outputs are already stale-guarded + smoothed at read time (per-
    # member EMA keys). The topology scalar is 'series' if ANY member is
    # series: the series solar formula on the summed outputs with the summed
    # battery power is exact for any mix, because parallel members contribute
    # no battery term — so the post-feedback re-derivation stays correct.
    (
        inverter_max_power,
        inverter_max_power_per_phase,
        inverter_supports_asymmetric,
    ) = fleet.inverter_limits(members)
    wiring_topology = fleet.fleet_topology(members)
    inverter_output_per_phase = fleet.sum_outputs(members)

    # Read-time power figures for the calculator's inverter coverage gate
    # (target_calculator._inverter_covers_load): what the fleet is putting out
    # right now, and which way the grid is flowing. Same measured-then-estimated
    # aggregation the display headroom uses, but taken HERE, before the feedback
    # loop: post-feedback the derived solar has the managed draws folded back
    # into it, which inflates the estimate by exactly the draw the gate then has
    # to discount — the two errors would cancel the load-off add-back and the
    # gate would suppress itself (issue #41).
    inverter_output_total = fleet.output_power_total(
        members,
        voltage,
        solar_w=solar_production_total,
        battery_power_w=battery_power,
    )
    # Signed net grid flow (+ import / − export) from the smoothed phases. Off
    # grid every phase reads 0, so this is 0 — correct: nothing to import.
    net_grid_power = sum(r for r in smoothed_phases if r is not None) * voltage

    # --- Runtime state from shared hub data (hub_runtime already fetched above) ---
    distribution_mode = hub_runtime.get("distribution_mode", DEFAULT_DISTRIBUTION_MODE)
    allow_grid_charging = hub_runtime.get("allow_grid_charging", True)
    power_buffer = hub_runtime.get("power_buffer", 0)
    battery_soc_target = hub_runtime.get(
        "battery_soc_target", DEFAULT_BATTERY_SOC_TARGET
    )
    battery_soc_min = hub_runtime.get("battery_soc_min", DEFAULT_BATTERY_SOC_MIN)
    # With exactly one battery this is its real full-SOC (classic behavior,
    # including the calculations-level gates that read it); a multi-battery
    # fleet passes None — its full gating already happened per member in
    # fleet.charge_power_total(), and a fleet-SOC gate would be wrong.
    battery_soc_full = fleet.soc_full_scalar(members)

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

        # The min floor's band sits ABOVE the setting (mirror of the target's):
        # the floor is protective, so discharge stops AT the configured floor
        # and only resumes once the battery has recovered a full hysteresis
        # above it (floor 20, hysteresis 3 → stop at 20, resume at 23). The
        # target's band sits below its setting for the same reason in reverse —
        # charging never overshoots the configured ceiling.
        was_above_min = hub_runtime.get("_soc_above_min", False)
        if was_above_min:
            now_above_min = battery_soc >= battery_soc_min
        else:
            now_above_min = battery_soc >= battery_soc_min + battery_soc_hysteresis
        hub_runtime["_soc_above_min"] = now_above_min
        if not now_above_min:
            battery_soc_min = battery_soc_min + battery_soc_hysteresis

    # Discharge capacity sums only members whose OWN battery is at/above the
    # (hysteresis-adjusted) hub minimum — a battery below the floor cannot be
    # counted dischargeable because a full sibling lifts the fleet SOC.
    battery_max_discharge_power = fleet.discharge_power_total(members, battery_soc_min)

    # Apply power buffer to reduce effective max grid import power
    if max_grid_import_power is not None and power_buffer > 0:
        max_grid_import_power = max(0, max_grid_import_power - power_buffer)

    # (Excess-export hysteresis is applied after the feedback loop below — it
    # must see the same post-feedback export figure the engine's excess pool
    # compares against the threshold.)

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
        _fv(solar_production_total),
        "measured" if not solar_is_derived else "derived",
        _fv(total_export_current),
        _fv(total_export_power),
    )
    _extra = []
    if any(m.has_battery for m in members):
        _bat_dir = (
            "chg"
            if (battery_power or 0) < 0
            else ("dischg" if (battery_power or 0) > 0 else "idle")
        )
        _hyst_min = "*" if now_above_min else ""
        _hyst_tgt = "*" if now_above_target else ""
        _n_batteries = sum(1 for m in members if m.has_battery)
        _extra.append(
            f"Bat(x{_n_batteries}): {_fv(battery_soc)}%/{_fv(battery_power)}W({_bat_dir}) "
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
        battery_soc_full=float(battery_soc_full)
        if battery_soc_full is not None
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
        inverter_output_total=float(inverter_output_total)
        if inverter_output_total is not None
        else None,
        net_grid_power=float(net_grid_power),
        excess_export_threshold=excess_threshold,
        allow_grid_charging=allow_grid_charging,
        power_buffer=power_buffer,
        distribution_mode=distribution_mode,
        is_off_grid=not has_grid_cts,
    )

    # --- Add chargers ---
    hub_entry_id = (
        hub_entry.entry_id
        if hasattr(hub_entry, "entry_id")
        else hub_entry.data.get("hub_entry_id")
    )
    _add_chargers_to_site(hass, site, hub_entry_id, charger_entries)

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
    _apply_feedback_loop(site, solar_is_derived, members)

    # Excess trigger + hysteresis latch. excess_margin() returns the watts by
    # which everything the site is absorbing (grid export + battery charging)
    # exceeds everything it is allowed to absorb (export allowance + battery
    # charge allowance); >= 0 means on. Once engaged, the band widens by the
    # hub's Excess hysteresis so a load doesn't chatter at the trigger point.
    # The latch lives here so the calculator stays stateless — it just reads
    # site.excess_hysteresis. The per-sink breakdown goes to excess_margin()'s
    # own debug line.
    #
    # Evaluated on POST-feedback figures: the pools compare export with charger
    # draws added back, and pre-feedback export is already eaten by the Excess
    # load's own draw — the band would never engage exactly when a load is running.
    was_excess_on = hub_runtime.get("_excess_on", False)
    margin = excess_margin(site, excess_hysteresis if was_excess_on else 0)
    excess_on = margin >= 0
    hub_runtime["_excess_on"] = excess_on
    site.excess_hysteresis = excess_hysteresis if excess_on else 0
    _LOGGER.debug(
        "Excess %s (margin %+.0fW)", "ON" if excess_on else "off", margin
    )

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
    if fleet.mixed_topologies(members):
        household = _mixed_household_per_phase(site, members)
    else:
        household = compute_household_per_phase(site, site.wiring_topology)
    if household is not None:
        # Asymmetric hold on the household floor. The managed draw side of the
        # subtraction (OCPP, sub-second) reacts before the polled inverter
        # output does, so a ramping car transiently zeroes household and the
        # engine would hand the real household's power out as headroom. Rises
        # pass straight through; falls are bridged over
        # HOUSEHOLD_HOLD_BRIDGE_SECONDS of wall clock.
        decay = _household_hold_decay(hub_entry)
        raw_household = household
        household = hold_per_phase_floor(
            household, hub_runtime.get("_household_held"), decay
        )
        hub_runtime["_household_held"] = household
        site.household_consumption = household
        _LOGGER.debug(
            "Per-phase household from inverter output (%s): A=%.1fA B=%.1fA "
            "C=%.1fA (raw A=%.1fA B=%.1fA C=%.1fA, hold decay %.3f)",
            site.wiring_topology,
            household.a if household.a is not None else 0,
            household.b if household.b is not None else 0,
            household.c if household.c is not None else 0,
            raw_household.a if raw_household.a is not None else 0,
            raw_household.b if raw_household.b is not None else 0,
            raw_household.c if raw_household.c is not None else 0,
            decay,
        )
    else:
        # No inverter output data at all — nothing to hold, and the held value
        # must be dropped so it cannot resurrect on a later cycle.
        hub_runtime.pop("_household_held", None)

    # --- Calculate targets ---
    calculate_all_charger_targets(site)

    # --- Grid stale fallback: force min_current after timeout ---
    grid_stale = grid_stale_duration > GRID_STALE_TIMEOUT
    if grid_stale:
        _LOGGER.warning(
            "Grid CT unavailable for %.0fs (>%ds) — charging EVSEs falling to "
            "minimum current, binary loads switched off",
            grid_stale_duration,
            GRID_STALE_TIMEOUT,
        )
        for charger in site.chargers:
            # Only an EVSE already charging keeps a minimum-current permit (a
            # hard stop mid-charge is worse than 6 A on a blind site). Binary
            # loads (plugs/tanks) and idle EVSEs get no permit — a permit > 0
            # switches a binary load ON, and energizing a load the engine had
            # deliberately shed while it cannot see the site is unsafe.
            if (
                charger.device_type == DEVICE_TYPE_EVSE
                and charger.connector_status == "Charging"
            ):
                charger.allocated_current = charger.min_current
                charger.available_current = charger.min_current
            else:
                charger.allocated_current = 0
                charger.available_current = 0

    charger_targets = {c.charger_id: c.allocated_current for c in site.chargers}
    charger_available = {c.charger_id: c.available_current for c in site.chargers}
    charger_names = {c.charger_id: c.entity_id for c in site.chargers}

    # Persist this cycle's permit for next-cycle settle detection — an EVSE
    # only counts as "settled and under-drawing" when its measured draw stays
    # below the permit we last offered it.
    chargers_rt = hass.data[DOMAIN].get("chargers", {})
    for c in site.chargers:
        rt = chargers_rt.get(c.charger_id)
        if rt is not None:
            rt["_last_permit"] = c.available_current

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
    # Any fleet member with its own production sensor counts as a
    # power-measurement input for the setup-completeness check.
    has_solar_entity = any(m.has_solar_entity for m in members)

    if not has_grid_cts and not has_inverter_output and not has_solar_entity:
        hub_status = "Setup incomplete"
        hub_warnings.append(
            "No power-measurement input configured. Add at least one in the "
            "hub options: grid CT current sensors (grid-tied sites), inverter "
            "output power sensors, or a solar production sensor."
        )
    elif not has_grid_cts:
        # Off-grid: no grid CTs, so the battery is the primary state source.
        # Any fleet member's battery satisfies the requirement — the battery
        # may live on the hub's legacy fields or on an inverter entry.
        hub_warnings.append("Off-grid mode (no grid CTs)")
        if not any(m.battery_soc is not None or m.has_battery for m in members):
            hub_status = "Setup incomplete"
            hub_warnings.append(
                "Off-grid hub needs a battery SOC sensor — it drives the "
                "operating-mode logic. Set it on the hub or an inverter entry."
            )
        if not any(m.has_battery_power_entity for m in members):
            hub_status = "Setup incomplete"
            hub_warnings.append(
                "Off-grid hub needs a battery power sensor — it is used to "
                "detect available solar surplus. Set it on the hub or an "
                "inverter entry."
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

    # Configured non-grid sensors that are currently unavailable. Name them in
    # the status line itself (not just the warnings attribute) so the user sees
    # *which* sensor dropped out at a glance, without expanding attributes.
    unavailable = _check_entity_availability(hass, hub_entry)
    if unavailable:
        hub_warnings.extend(
            f"{label} ({entity_id}) is unavailable" for label, entity_id in unavailable
        )
        if hub_status == "OK":
            labels = [label for label, _ in unavailable]
            named = ", ".join(labels[:2])
            if len(labels) > 2:
                named += f" +{len(labels) - 2} more"
            hub_status = f"Sensor unavailable: {named}"

    # --- Per-inverter data for the inverter-entry sensors ---
    # The legacy implicit member (the hub's own fields) has no device of its
    # own — its values already show on the hub's fleet sensors.
    inverters_data = {
        m.entry_id: {
            "name": m.name,
            "solar_w": fleet.member_solar_production(m, voltage),
            "battery_soc": m.battery_soc,
            "battery_power": m.battery_power,
        }
        for m in members
        if m.entry_id != hub_entry_id
    }

    # --- PV clipping forecast (advisory battery headroom) ---
    # Computed post-feedback so solar_production_total excludes managed draws.
    forecast_advice, forecast_per_inverter = _compute_forecast_advice(
        hass,
        hub_entry,
        hub_runtime,
        site,
        battery_soc,
        members,
    )
    for inv_id, advice in forecast_per_inverter.items():
        if inv_id in inverters_data:
            inverters_data[inv_id].update(advice)

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
        excess_available=excess_on,
        excess_margin_power=margin,
        forecast_advice=forecast_advice,
        inverters_data=inverters_data,
    )


__all__ = [
    "SiteContext",
    "LoadContext",
    "PhaseValues",
    "calculate_all_charger_targets",
    "run_hub_calculation",
]
