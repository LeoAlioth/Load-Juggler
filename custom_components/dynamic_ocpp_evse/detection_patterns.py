"""Auto-detection patterns for grid CTs and plug monitors.

**GRID_CT**: per-brand entries in detection priority order, each a
``patterns`` dict with keys phase_a / phase_b / phase_c and the ``unit`` the
entities publish. Tried in order (watts first, see ``_power_first``) - the
first complete 3-phase match wins; the hub setup page then offers the
meter's own watts in place of amps where its device publishes both
(``config_flow/helpers.py`` ``_power_beside``). An entry whose ids are also
some other device's carries a ``device`` test: given the matched entity's
device registry entry (None for an entity without a device), whether that
device is the meter. The page drops a match whose device fails it.

**PLUG_POWER_MONITOR**: a single ``pattern`` regex per entry. First match wins.

To add a brand, add its entries to GRID_CT at its priority, its name first.
"""
import re


def _victron_gx(device_type: str):
    """A device test: victron_gx's, or ha-victron-mqtt's, device of that type.
    Both (the victron_mqtt library) identify a device as
    <installation>_<device type>_<instance> - grid_30, system_0."""
    own = re.compile(rf"_{device_type}_\d+$")
    return lambda device: device is not None and any(
        domain in ("victron_gx", "victron_mqtt") and own.search(identifier)
        for domain, identifier in device.identifiers)


GRID_CT = [
    # SolarEdge - the SolarEdge Modbus Multi integration. Meter entities carry
    # an 'm' prefix (m1_ac_current_a), the inverter's an 'i' prefix.
    {
        "name": "SolarEdge",
        "patterns": {
            "phase_a": r'sensor\..*m.*ac_current_a.*',
            "phase_b": r'sensor\..*m.*ac_current_b.*',
            "phase_c": r'sensor\..*m.*ac_current_c.*',
        },
        "unit": "A",
    },
    # Solarman / Deye - Deye, Sunsynk and others on the Solarman data logger;
    # names come from the integration's inverter definition file. The CT
    # POWER entities are signed (negative while exporting); the CT CURRENT
    # entities of the same name are magnitude-only, so picking those makes
    # export invisible - no Excess mode, and exported power counted as
    # household consumption. Power first, current only as a fallback for
    # definitions that don't publish it. "External CT1 Power" in ha-solarman,
    # "External CT L1 Power" in StephanJoubert's Solarman (deye_sg04lp3).
    {
        "name": "Solarman/Deye - external CTs (power)",
        "patterns": {
            "phase_a": r'sensor\..*_external_ct(?:_l)?1_power$',
            "phase_b": r'sensor\..*_external_ct(?:_l)?2_power$',
            "phase_c": r'sensor\..*_external_ct(?:_l)?3_power$',
        },
        "unit": "W",
    },
    {
        "name": "Solarman/Deye - internal CTs (power)",
        "patterns": {
            "phase_a": r'sensor\..*_internal_ct(?:_l)?1_power$',
            "phase_b": r'sensor\..*_internal_ct(?:_l)?2_power$',
            "phase_c": r'sensor\..*_internal_ct(?:_l)?3_power$',
        },
        "unit": "W",
    },
    # "Grid L1 Power" in ha-solarman: the grid meter's in deye_p3, deye_hybrid,
    # kstar_hybrid, megarevo_r-3h and afore_hybrid, but afore_BNTxxxKTL-2mppt
    # (Afore BNT T4, a string inverter with no meter) computes it from its own
    # output current. ha-solarman names every definition's sensors <device> +
    # name, so only the device's model - the definition's, else its file
    # name's - tells them apart. Anchored on the power suffix: unanchored, and
    # sorted first as a watts entry, it matched any "grid_l1" reading -
    # sensor.victron_grid_l1_voltage on a Victron.
    {
        "name": "Solarman/Deye - grid power (individual phases)",
        "patterns": {
            "phase_a": r'sensor\..*_grid_l1_power$',
            "phase_b": r'sensor\..*_grid_l2_power$',
            "phase_c": r'sensor\..*_grid_l3_power$',
        },
        "unit": "W",
        "device": lambda device: device is None or device.model != "BNTXXXKTL-2MPPT",
    },
    {
        "name": "Solarman/Deye - external CTs (current)",
        "patterns": {
            "phase_a": r'sensor\..*_external_ct1_current.*',
            "phase_b": r'sensor\..*_external_ct2_current.*',
            "phase_c": r'sensor\..*_external_ct3_current.*',
        },
        "unit": "A",
    },
    {
        "name": "Solarman/Deye - internal CTs (current)",
        "patterns": {
            "phase_a": r'sensor\..*_internal_ct1_current.*',
            "phase_b": r'sensor\..*_internal_ct2_current.*',
            "phase_c": r'sensor\..*_internal_ct3_current.*',
        },
        "unit": "A",
    },
    # Fronius - HA core, Fronius Solar API; a SmartMeter is required for
    # per-phase grid readings. Older entity ids read current_ac_phase_1; core
    # names them from its translations now, "Current phase 1" on a device
    # named after the meter model ("Smart Meter 63A"). That spelling only on
    # a smart_meter device: a Zaptec charger's "Current phase 1" (and
    # "Available current phase 1") is spelled the same.
    {
        "name": "Fronius SmartMeter",
        "patterns": {
            "phase_a": r'sensor\..*(?:_current_ac|smart_meter.*_current)_phase_1$',
            "phase_b": r'sensor\..*(?:_current_ac|smart_meter.*_current)_phase_2$',
            "phase_c": r'sensor\..*(?:_current_ac|smart_meter.*_current)_phase_3$',
        },
        "unit": "A",
    },
    # Huawei Solar - wlcrs/huawei_solar over Modbus TCP; a power meter is
    # required for per-phase grid readings.
    {
        "name": "Huawei - power meter",
        "patterns": {
            "phase_a": r'sensor\..*power_meter_phase_a_current$',
            "phase_b": r'sensor\..*power_meter_phase_b_current$',
            "phase_c": r'sensor\..*power_meter_phase_c_current$',
        },
        "unit": "A",
    },
    # Enphase Envoy - HA core, local gateway; per-phase sensors are disabled
    # by default and need consumption CTs. The NET reading is what crosses
    # the grid connection, signed; "current_power_consumption_l1" beside it is
    # what the house draws, never negative. Core names the phases l1..l3.
    {
        "name": "Enphase Envoy - net consumption per phase",
        "patterns": {
            "phase_a": r'sensor\.envoy.*_current_net_power_consumption_l1$',
            "phase_b": r'sensor\.envoy.*_current_net_power_consumption_l2$',
            "phase_c": r'sensor\.envoy.*_current_net_power_consumption_l3$',
        },
        "unit": "W",
    },
    # Victron - sfstar/hass-victron over Modbus TCP (Cerbo GX / Venus GX). It
    # sets the ids itself: victron_grid_l1_current on unit 0/100/225,
    # victrongrid_l1_current30 on any other (a grid meter's unit is 30, 31,
    # ...), victron_grid_l1_current_30 before February 2026. Anchored on the
    # victron prefix: ha-solarman's "Grid L1 Current" is spelled the same and
    # is an inverter's own output current (deye_string, sofar_hybrid, ...).
    {
        "name": "Victron",
        "patterns": {
            "phase_a": r'sensor\.victron_?grid_l1_current(?:_?\d+)?$',
            "phase_b": r'sensor\.victron_?grid_l2_current(?:_?\d+)?$',
            "phase_c": r'sensor\.victron_?grid_l3_current(?:_?\d+)?$',
        },
        "unit": "A",
    },
    # Victron GX - HA core's victron_gx and tomer-w/ha-victron-mqtt, over the
    # GX's MQTT. A grid meter publishes "Power on L1" / "Current on L1" (W, A;
    # the power + from the grid, - feeding in) on a device named after its
    # product and instance - sensor.et340_energy_meter_id_30_power_on_l1 - or
    # its custom name. An AC load or heat pump meter says the same, so the
    # device's identifier (grid_30) decides. The watts themselves, not amps
    # for _power_beside to upgrade: phases' REJECT turns down every reading
    # whose id says "energy", so on an "... Energy Meter" it finds no watts.
    {
        "name": "Victron GX - grid meter",
        "patterns": {
            "phase_a": r'sensor\..*_power_on_l1$',
            "phase_b": r'sensor\..*_power_on_l2$',
            "phase_c": r'sensor\..*_power_on_l3$',
        },
        "unit": "W",
        "device": _victron_gx("grid"),
    },
    # ... and the GX's own "Grid power L1" (system device, "Victron Venus"):
    # the grid meter's reading when there is one, so after it.
    {
        "name": "Victron GX - system",
        "patterns": {
            "phase_a": r'sensor\..*_grid_power_l1$',
            "phase_b": r'sensor\..*_grid_power_l2$',
            "phase_c": r'sensor\..*_grid_power_l3$',
        },
        "unit": "W",
        "device": _victron_gx("system"),
    },
    # Sofar Solar - the grid meter's readings at the PCC: "ActivePower_PCC_R"
    # in both Solarman integrations' sofar_g3hyd (OEM platforms included:
    # ZCS, Turbo Energy, ...), "Active power PCC L1" in HA core's sofar (kW).
    # The power is signed, the current not. ha-solarman's sofar_hybrid
    # (HYD-ES, single phase) has no per-phase grid reading: its meter is the
    # one "Grid power"; "Grid L1 Current" is the inverter's.
    {
        "name": "Sofar - grid power (PCC)",
        "patterns": {
            "phase_a": r'sensor\..*_active_?power_pcc_(?:l1|r)$',
            "phase_b": r'sensor\..*_active_?power_pcc_(?:l2|s)$',
            "phase_c": r'sensor\..*_active_?power_pcc_(?:l3|t)$',
        },
        "unit": "W",
    },
    {
        "name": "Sofar - grid current (PCC)",
        "patterns": {
            "phase_a": r'sensor\..*_current_pcc_(?:l1|r)$',
            "phase_b": r'sensor\..*_current_pcc_(?:l2|s)$',
            "phase_c": r'sensor\..*_current_pcc_(?:l3|t)$',
        },
        "unit": "A",
    },
    # Sungrow - mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant (SH hybrid
    # series), per-phase grid readings from its meter.
    {
        "name": "Sungrow - meter current",
        "patterns": {
            "phase_a": r'sensor\..*meter_phase_a_current$',
            "phase_b": r'sensor\..*meter_phase_b_current$',
            "phase_c": r'sensor\..*meter_phase_c_current$',
        },
        "unit": "A",
    },
]

# Smart plug / smart load power monitoring (typically in watts).
PLUG_POWER_MONITOR = [
    # Shelly plugs (shelly integration)
    {"name": "Shelly Plug", "pattern": r'sensor\.shelly.*plug.*power$'},
    {"name": "Shelly 1PM", "pattern": r'sensor\.shelly.*1pm.*power$'},
    {"name": "Shelly PM Mini", "pattern": r'sensor\.shelly.*pm.*mini.*power$'},
    # Sonoff plugs (eWeLink / SonoffLAN integration)
    {"name": "Sonoff Plug", "pattern": r'sensor\.sonoff.*(?:pow|plug|s[234]0).*power$'},
    # Tasmota plugs (tasmota integration)
    {"name": "Tasmota Power", "pattern": r'sensor\.tasmota.*power$'},
    # TP-Link Kasa plugs
    {"name": "TP-Link Kasa", "pattern": r'sensor\..*kasa.*(?:current_consumption|power)$'},
    # Tuya smart plugs
    {"name": "Tuya Plug", "pattern": r'sensor\..*tuya.*plug.*(?:power|current_consumption)$'},
    # Generic - match entity names with "plug" + "power" (broad fallback)
    {"name": "Generic (plug power)", "pattern": r'sensor\..*plug.*power$'},
]


def _power_first(pattern_sets: list) -> list:
    """Order watt-based pattern sets ahead of amp-based ones.

    A grid CT's POWER entity is signed - negative while exporting - but the
    CURRENT entity from the same meter is very often magnitude-only. Picking
    the latter makes export structurally invisible: the export term is always
    zero, so grid-side Excess can never trigger and exported power is counted
    as household consumption. Neither is detectable at config time, which is
    why the preference belongs here rather than in a warning.

    A stable sort, so brand priority still decides within each group and a
    current-only meter is still detected - just after every power option has
    been ruled out.
    """
    return sorted(pattern_sets, key=lambda p: 0 if p.get("unit") == "W" else 1)


PHASE_PATTERNS = _power_first(GRID_CT)
