"""
Dynamic OCPP EVSE - Main calculation module.

This file provides a unified interface for EVSE calculations.
All core calculation logic has been refactored into the calculations/ directory.
"""

from .calculations import (
    SiteContext,
    ChargerContext,
    PhaseValues,
    calculate_all_charger_targets,
)
from .const import *
from .calculations.utils import is_number, compute_household_per_phase


def _read_phase_attr(attrs: dict, keys: tuple) -> float | None:
    """Try to read a numeric phase current from entity attributes using multiple naming conventions."""
    for key in keys:
        val = attrs.get(key)
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
    import logging
    _LOGGER = logging.getLogger(__name__)
    from .helpers import get_entry_value

    hass = sensor.hass
    hub_entry = sensor.hub_entry

    # --- Read hub config values ---
    voltage = get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
    main_breaker_rating = get_entry_value(hub_entry, CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING)
    excess_threshold = get_entry_value(hub_entry, CONF_EXCESS_EXPORT_THRESHOLD, DEFAULT_EXCESS_EXPORT_THRESHOLD)
    invert_phases = get_entry_value(hub_entry, CONF_INVERT_PHASES, False)

    # --- Read per-phase grid current from HA entities ---
    phase_a_entity = get_entry_value(hub_entry, CONF_PHASE_A_CURRENT_ENTITY_ID, None)
    phase_b_entity = get_entry_value(hub_entry, CONF_PHASE_B_CURRENT_ENTITY_ID, None)
    phase_c_entity = get_entry_value(hub_entry, CONF_PHASE_C_CURRENT_ENTITY_ID, None)

    # Read raw phase values - None if entity not configured (phase doesn't exist)
    raw_phase_a = _read_entity(hass, phase_a_entity, 0) if phase_a_entity else None
    raw_phase_b = _read_entity(hass, phase_b_entity, 0) if phase_b_entity else None
    raw_phase_c = _read_entity(hass, phase_c_entity, 0) if phase_c_entity else None

    # Apply inversion if configured (only for existing phases)
    if invert_phases:
        if raw_phase_a is not None:
            raw_phase_a = -raw_phase_a
        if raw_phase_b is not None:
            raw_phase_b = -raw_phase_b
        if raw_phase_c is not None:
            raw_phase_c = -raw_phase_c

    # Split into consumption (import, positive) and export (surplus, positive)
    # Convention: raw > 0 means importing from grid, raw < 0 means exporting
    # None = phase doesn't physically exist
    phase_a_consumption = max(0, raw_phase_a) if raw_phase_a is not None else None
    phase_b_consumption = max(0, raw_phase_b) if raw_phase_b is not None else None
    phase_c_consumption = max(0, raw_phase_c) if raw_phase_c is not None else None

    phase_a_export_current = max(0, -raw_phase_a) if raw_phase_a is not None else None
    phase_b_export_current = max(0, -raw_phase_b) if raw_phase_b is not None else None
    phase_c_export_current = max(0, -raw_phase_c) if raw_phase_c is not None else None

    # Use PhaseValues for clean aggregation (sums non-None values only)
    consumption_pv = PhaseValues(phase_a_consumption, phase_b_consumption, phase_c_consumption)
    export_pv = PhaseValues(phase_a_export_current, phase_b_export_current, phase_c_export_current)

    total_export_current = export_pv.total
    total_export_power = total_export_current * voltage if voltage > 0 else 0

    # Solar production: use dedicated entity if configured, otherwise derive from grid meter
    solar_production_entity = get_entry_value(hub_entry, CONF_SOLAR_PRODUCTION_ENTITY_ID, None)
    solar_is_derived = not solar_production_entity
    if solar_production_entity:
        solar_production_total = _read_entity(hass, solar_production_entity, 0)
    else:
        solar_production_total = total_export_power

    # --- Read battery data from HA entities ---
    battery_soc_entity = get_entry_value(hub_entry, CONF_BATTERY_SOC_ENTITY_ID, None)
    battery_power_entity = get_entry_value(hub_entry, CONF_BATTERY_POWER_ENTITY_ID, None)
    battery_soc_target_entity = get_entry_value(hub_entry, CONF_BATTERY_SOC_TARGET_ENTITY_ID, None)

    battery_soc = _read_entity(hass, battery_soc_entity, None) if battery_soc_entity else None
    battery_power = _read_entity(hass, battery_power_entity, None) if battery_power_entity else None
    battery_soc_min = get_entry_value(hub_entry, CONF_BATTERY_SOC_MIN, DEFAULT_BATTERY_SOC_MIN)
    battery_soc_target = _read_entity(hass, battery_soc_target_entity, None) if battery_soc_target_entity else None
    battery_soc_hysteresis = get_entry_value(hub_entry, CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS)
    battery_max_charge_power = get_entry_value(hub_entry, CONF_BATTERY_MAX_CHARGE_POWER, None)
    battery_max_discharge_power = get_entry_value(hub_entry, CONF_BATTERY_MAX_DISCHARGE_POWER, None)

    # --- Read max grid import power (entity override → shared hub data → None) ---
    enable_max_import = get_entry_value(hub_entry, CONF_ENABLE_MAX_IMPORT_POWER, True)
    max_import_power_entity = get_entry_value(hub_entry, CONF_MAX_IMPORT_POWER_ENTITY_ID, None)
    if max_import_power_entity:
        max_grid_import_power = _read_entity(hass, max_import_power_entity, None)
    elif enable_max_import:
        hub_rt = hass.data[DOMAIN]["hubs"].get(hub_entry.entry_id, {})
        max_grid_import_power = hub_rt.get("max_import_power", None)
    else:
        max_grid_import_power = None

    # --- Read inverter configuration ---
    inverter_max_power = get_entry_value(hub_entry, CONF_INVERTER_MAX_POWER, None)
    inverter_max_power_per_phase = get_entry_value(hub_entry, CONF_INVERTER_MAX_POWER_PER_PHASE, None)
    inverter_supports_asymmetric = get_entry_value(hub_entry, CONF_INVERTER_SUPPORTS_ASYMMETRIC, False)
    wiring_topology = get_entry_value(hub_entry, CONF_WIRING_TOPOLOGY, DEFAULT_WIRING_TOPOLOGY)

    # --- Read per-phase inverter output entities (optional) ---
    inv_out_a_entity = get_entry_value(hub_entry, CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID, None)
    inv_out_b_entity = get_entry_value(hub_entry, CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID, None)
    inv_out_c_entity = get_entry_value(hub_entry, CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID, None)
    inverter_output_per_phase = None

    if inv_out_a_entity:
        def _read_inverter_output(entity_id):
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

        inv_a = _read_inverter_output(inv_out_a_entity)
        inv_b = _read_inverter_output(inv_out_b_entity)
        inv_c = _read_inverter_output(inv_out_c_entity)
        if inv_a is not None:
            inverter_output_per_phase = PhaseValues(inv_a, inv_b, inv_c)

    # --- Read runtime state from shared hub data ---
    hub_runtime = hass.data[DOMAIN]["hubs"].get(hub_entry.entry_id, {})
    charging_mode = hub_runtime.get("charging_mode", CHARGING_MODE_STANDARD)
    distribution_mode = hub_runtime.get("distribution_mode", DEFAULT_DISTRIBUTION_MODE)
    allow_grid_charging = hub_runtime.get("allow_grid_charging", True)
    power_buffer = hub_runtime.get("power_buffer", 0)

    # Apply power buffer to reduce effective max grid import power
    if max_grid_import_power is not None and power_buffer > 0:
        max_grid_import_power = max(0, max_grid_import_power - power_buffer)

    # --- Block 1: Raw Entity Reads ---
    def _fv(v):
        """Format value for debug: None->'n/a', number->'12.3'."""
        if v is None:
            return "n/a"
        if isinstance(v, (int, float)):
            return f"{v:.1f}"
        return str(v)

    _LOGGER.debug(
        "--- Hub Update --- CT: A=%sA B=%sA C=%sA (%dph, invert=%s) | "
        "Solar: %sW (%s) | Export: %sA/%sW",
        _fv(raw_phase_a), _fv(raw_phase_b), _fv(raw_phase_c),
        consumption_pv.active_count, "on" if invert_phases else "off",
        _fv(solar_production_total), solar_production_entity or "derived",
        _fv(total_export_current), _fv(total_export_power),
    )
    _extra = []
    if battery_soc_entity:
        _bat_dir = "chg" if (battery_power or 0) < 0 else ("dischg" if (battery_power or 0) > 0 else "idle")
        _extra.append(
            f"Bat: {_fv(battery_soc)}%/{_fv(battery_power)}W({_bat_dir}) "
            f"min={_fv(battery_soc_min)}% tgt={_fv(battery_soc_target)}%"
        )
    if inverter_max_power or inverter_max_power_per_phase:
        _extra.append(
            f"Inv: {_fv(inverter_max_power)}W/{_fv(inverter_max_power_per_phase)}W/ph "
            f"{'asym' if inverter_supports_asymmetric else 'sym'} {wiring_topology}"
        )
    _LOGGER.debug(
        "  %s/%s grid_chg=%s buf=%sW max_import=%s%s",
        charging_mode, distribution_mode,
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

    # Build site context
    site = SiteContext(
        voltage=voltage,
        main_breaker_rating=main_breaker_rating,
        grid_current=PhaseValues(raw_phase_a, raw_phase_b, raw_phase_c),
        consumption=PhaseValues(phase_a_consumption, phase_b_consumption, phase_c_consumption),
        export_current=PhaseValues(phase_a_export_current, phase_b_export_current, phase_c_export_current),
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
        charging_mode=charging_mode,
    )

    # Add chargers to site
    from . import get_chargers_for_hub
    hub_entry_id = hub_entry.entry_id if hasattr(hub_entry, 'entry_id') else hub_entry.data.get('hub_entry_id')

    if hasattr(sensor, '_charger_entries'):
        chargers = sensor._charger_entries
    else:
        chargers = get_chargers_for_hub(hass, hub_entry_id)
    
    plug_auto_power = {}  # {entry_id: averaged_power_watts} for auto-adjusted plugs
    for entry in chargers:
        device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        charger_entity_id = entry.data.get(CONF_ENTITY_ID, f"charger_{entry.entry_id}")
        priority = get_entry_value(entry, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY)

        if device_type == DEVICE_TYPE_PLUG:
            # Smart load — binary on/off device with fixed power rating
            # Read power rating: prefer shared charger data, fall back to config
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
                connector_status = "Charging"  # Default to active if we can't determine

            equivalent_current = power_rating / (voltage * phases) if voltage > 0 else 0

            charger = ChargerContext(
                charger_id=entry.entry_id,
                entity_id=charger_entity_id,
                min_current=equivalent_current,
                max_current=equivalent_current,
                phases=phases,
                priority=priority,
                active_phases_mask=connected_to_phase,
                connector_status=connector_status,
                device_type=DEVICE_TYPE_PLUG,
            )
            _LOGGER.debug(
                "  Plug %s: %.0fW on %s prio=%d [%s]%s",
                charger_entity_id, power_rating, connected_to_phase,
                priority, connector_status,
                " (auto-adj)" if entry.entry_id in plug_auto_power else "",
            )
        else:
            # OCPP EVSE — standard charger with current modulation
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

            charger = ChargerContext(
                charger_id=entry.entry_id,
                entity_id=charger_entity_id,
                min_current=min_current,
                max_current=max_current,
                phases=phases,
                priority=priority,
                connector_status=connector_status,
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
                        # Try per-phase attributes first (various naming conventions)
                        attrs = evse_state.attributes
                        l1 = _read_phase_attr(attrs, ('l1_current', 'l1', 'phase_1', 'current_phase_1'))
                        l2 = _read_phase_attr(attrs, ('l2_current', 'l2', 'phase_2', 'current_phase_2'))
                        l3 = _read_phase_attr(attrs, ('l3_current', 'l3', 'phase_3', 'current_phase_3'))

                        if l1 is not None or l2 is not None or l3 is not None:
                            # Per-phase data available from attributes
                            charger.l1_current = l1 or 0
                            charger.l2_current = l2 or 0
                            charger.l3_current = l3 or 0

                            # Clamp per-phase draws at max_current. Some OCPP chargers
                            # (e.g. ZJBeny) intermittently report total 3-phase current
                            # in per-phase attributes (40.5A instead of 13.5A on a 16A
                            # charger). Without clamping, the feedback loop subtracts
                            # 40+ amps from grid CT, massively overcorrecting.
                            for attr in ('l1_current', 'l2_current', 'l3_current'):
                                val = getattr(charger, attr)
                                if val > max_current:
                                    _LOGGER.warning(
                                        "EVSE %s: %s=%.1fA exceeds max_current=%.1fA — "
                                        "clamping (charger may be reporting total instead of per-phase)",
                                        charger_entity_id, attr, val, max_current,
                                    )
                                    setattr(charger, attr, max_current)
                        else:
                            # No per-phase attributes — use entity state as per-phase current
                            current_import = float(evse_state.state)
                            charger.l1_current = current_import
                            if phases >= 2:
                                charger.l2_current = current_import
                            if phases >= 3:
                                charger.l3_current = current_import
                    except (ValueError, TypeError):
                        pass

            _LOGGER.debug(
                "  EVSE %s: %s-%sA %dph(hw) L1->%s/L2->%s/L3->%s mask=%s(%dph) "
                "prio=%d [%s] draw=L1:%s/L2:%s/L3:%s",
                charger_entity_id,
                _fv(min_current), _fv(max_current), phases,
                l1_phase, l2_phase, l3_phase,
                charger.active_phases_mask,
                len(charger.active_phases_mask) if charger.active_phases_mask else 0,
                priority, connector_status,
                _fv(charger.l1_current), _fv(charger.l2_current), _fv(charger.l3_current),
            )

        # Clamp active_phases_mask to only include phases that exist on the site.
        # Prevents a misconfigured 3-phase charger from getting 0A on a 1-phase
        # site (min(A=30, B=0, C=0) = 0). Instead, mask is clamped to "A" and
        # the charger operates as single-phase.
        site_phases = {p for p, v in [("A", site.consumption.a), ("B", site.consumption.b), ("C", site.consumption.c)] if v is not None}
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

    # Subtract charger draws from grid readings before running the engine.
    # Grid CTs measure total site current INCLUDING charger draws. Without this
    # adjustment, the engine double-counts charger power as both "consumption"
    # and "charger demand", leading to under-allocation or false pauses.
    # We reconstruct the raw grid current, subtract charger draws, then re-split
    # into consumption/export — this correctly reveals hidden export on phases
    # where the charger was consuming solar surplus.
    # Map charger L1/L2/L3 draws to site phases A/B/C using phase mapping
    total_phase_a = total_phase_b = total_phase_c = 0.0
    for c in site.chargers:
        a_draw, b_draw, c_draw = c.get_site_phase_draw()
        total_phase_a += a_draw
        total_phase_b += b_draw
        total_phase_c += c_draw

    if total_phase_a > 0 or total_phase_b > 0 or total_phase_c > 0:
        def _adjust_phase(consumption, export, charger_draw):
            if consumption is None:
                return None, None
            # Reconstruct raw grid current (positive = import)
            raw_grid = consumption - (export or 0)
            # Remove charger draw to get "true" grid without charger
            true_grid = raw_grid - charger_draw
            return max(0.0, true_grid), max(0.0, -true_grid)

        adj_cons_a, adj_exp_a = _adjust_phase(site.consumption.a, site.export_current.a, total_phase_a)
        adj_cons_b, adj_exp_b = _adjust_phase(site.consumption.b, site.export_current.b, total_phase_b)
        adj_cons_c, adj_exp_c = _adjust_phase(site.consumption.c, site.export_current.c, total_phase_c)

        # Warn when household consumption gets clamped to 0 by feedback.
        # Normal on solar systems (charger draws from solar, not grid).
        # Suspicious if charger readings are inflated (bug #71).
        for _ph, _raw_cons, _raw_exp, _draw, _adj_cons in [
            ("A", site.consumption.a, site.export_current.a, total_phase_a, adj_cons_a),
            ("B", site.consumption.b, site.export_current.b, total_phase_b, adj_cons_b),
            ("C", site.consumption.c, site.export_current.c, total_phase_c, adj_cons_c),
        ]:
            if _raw_cons is not None and _draw > 0 and _adj_cons == 0 and _raw_cons > 0:
                _raw_grid = _raw_cons - (_raw_exp or 0)
                _LOGGER.warning(
                    "Phase %s: household -> 0 after feedback "
                    "(raw_grid=%.1fA - charger=%.1fA = %.1fA)",
                    _ph, _raw_grid, _draw, _raw_grid - _draw,
                )

        site.consumption = PhaseValues(adj_cons_a, adj_cons_b, adj_cons_c)
        site.export_current = PhaseValues(adj_exp_a, adj_exp_b, adj_exp_c)

        # Update derived solar to match adjusted export
        _solar_note = ""
        if solar_is_derived:
            site.solar_production_total = site.export_current.total * site.voltage
            _solar_note = f" | Solar(derived)={site.solar_production_total:.0f}W"

        # --- Block 2: Post-Feedback Summary ---
        _LOGGER.debug(
            "--- Feedback --- Subtracted A=%.1f B=%.1f C=%.1fA -> "
            "cons=(%s/%s/%s) exp=(%s/%s/%s)%s",
            total_phase_a, total_phase_b, total_phase_c,
            _fv(adj_cons_a), _fv(adj_cons_b), _fv(adj_cons_c),
            _fv(adj_exp_a), _fv(adj_exp_b), _fv(adj_exp_c),
            _solar_note,
        )

    # Compute household_consumption_total when solar entity provides ground truth.
    # Energy balance: solar + battery_power = household + grid_export (after feedback)
    # household = solar + battery_power - grid_export_power
    if not solar_is_derived and solar_production_total > 0:
        export_power_after_feedback = site.export_current.total * site.voltage
        bp = float(battery_power) if battery_power is not None else 0
        site.household_consumption_total = max(0, solar_production_total + bp - export_power_after_feedback)
        _LOGGER.debug(
            "Computed household_consumption_total=%.1fW (solar=%.1fW + bat=%.1fW - export=%.1fW)",
            site.household_consumption_total, solar_production_total, bp, export_power_after_feedback,
        )

    # Compute per-phase household from inverter output entities (after feedback).
    # This gives the engine exact per-phase household for asymmetric inverter limits.
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

    # Calculate targets (includes distribution)
    calculate_all_charger_targets(site)

    # Build per-charger targets dict — these ARE the final allocations
    charger_targets = {c.charger_id: c.allocated_current for c in site.chargers}
    charger_available = {c.charger_id: c.available_current for c in site.chargers}

    # Total allocated current = sum of all charger targets
    total_allocated = sum(charger_targets.values())

    # Compute derived values for hub sensors
    # Grid headroom: how much more we can import per phase before hitting breaker
    grid_headroom_a = max(0, main_breaker_rating - phase_a_consumption) * voltage if phase_a_consumption is not None else 0
    grid_headroom_b = max(0, main_breaker_rating - phase_b_consumption) * voltage if phase_b_consumption is not None else 0
    grid_headroom_c = max(0, main_breaker_rating - phase_c_consumption) * voltage if phase_c_consumption is not None else 0
    site_grid_available_power = round(grid_headroom_a + grid_headroom_b + grid_headroom_c, 0)

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

    # Build result dict
    result = {
        CONF_TOTAL_ALLOCATED_CURRENT: round(total_allocated, 1),
        CONF_PHASES: site.num_phases,
        CONF_CHARGING_MODE: site.charging_mode,
        "calc_used": "calculate_all_charger_targets",

        # Site-level data for hub sensor
        "battery_soc": site.battery_soc,
        "battery_soc_min": site.battery_soc_min,
        "battery_soc_target": site.battery_soc_target,
        "battery_power": battery_power,
        "available_battery_power": available_battery_power,
        # Per-phase available current for EV = breaker headroom + solar surplus
        "site_available_current_phase_a": round(main_breaker_rating - raw_phase_a, 1) if raw_phase_a is not None else 0,
        "site_available_current_phase_b": round(main_breaker_rating - raw_phase_b, 1) if raw_phase_b is not None else 0,
        "site_available_current_phase_c": round(main_breaker_rating - raw_phase_c, 1) if raw_phase_c is not None else 0,
        # Total site available power = sum of per-phase available * voltage
        "total_site_available_power": round(
            (max(0, main_breaker_rating - raw_phase_a) * voltage if raw_phase_a is not None else 0)
            + (max(0, main_breaker_rating - raw_phase_b) * voltage if raw_phase_b is not None else 0)
            + (max(0, main_breaker_rating - raw_phase_c) * voltage if raw_phase_c is not None else 0),
            0,
        ),
        "net_site_consumption": round(
            sum(v for v in (raw_phase_a, raw_phase_b, raw_phase_c) if v is not None) * voltage, 0
        ),
        "site_grid_available_power": site_grid_available_power,
        "site_battery_available_power": available_battery_power,
        "total_evse_power": total_evse_power,
        "solar_surplus_power": round(total_export_power, 0),
        "solar_surplus_current": round(total_export_current, 2),

        # Per-charger targets — these are the final allocations from the engine
        "charger_targets": charger_targets,
        "charger_available": charger_available,
        "distribution_mode": site.distribution_mode,

        # Auto-detected plug power ratings (for updating Device Power slider)
        "plug_auto_power": plug_auto_power,
    }

    return result


__all__ = [
    "SiteContext",
    "ChargerContext",
    "PhaseValues",
    "calculate_all_charger_targets",
    "run_hub_calculation",
]