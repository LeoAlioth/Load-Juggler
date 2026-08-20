"""Load Juggler - HA-state edge of the engine: sensor reading and smoothing.

Everything in here is the boundary between Home Assistant's entity states and
the numbers the calculation engine works with: the ``_UNAVAILABLE`` sentinel a
configured-but-unreadable sensor resolves to, ``_read_entity()`` and the
per-domain readers built on it (grid phases, inverter output, inverter and
fleet-member configuration), the EMA smoothing and stale-guard holdover that
decide what an unavailable feed stands in for, and the small debug formatters
that print a reading and its smoothed value side by side.

Split out of hub_calculation.py, which now consumes these readers rather than
defining them; config_flow.py reuses two of them for its live previews.
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

from ..calculations import PhaseValues
from ..const import (
    CHARGE_RATE_UNIT_WATTS,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_SOC_FULL,
    CONF_CHARGE_RATE_UNIT,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_INVERTER_SUPPORTS_ASYMMETRIC,
    CONF_INVERT_PHASES,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_NAME,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_WIRING_TOPOLOGY,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_BATTERY_SOC_FULL,
    DEFAULT_CHARGE_RATE_UNIT,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_WIRING_TOPOLOGY,
    EMA_ALPHA,
    INPUT_STALE_TIMEOUT,
)
from ..calculations.utils import is_number
from ..helpers import get_entry_value
from ..registry import get_inverters_for_hub
from .. import units
from . import fleet
from .forecast_reader import resolve_forecast_sensor

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
