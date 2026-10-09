"""Tests for the hot water tank device type - setpoint resolution.

Machine-authored tests - not yet human-reviewed.

resolve_tank_setpoint is the core new logic: given the tank's operating mode,
the three setpoints, the element power and the hub state, it picks which
setpoint (away / normal / boost) the climate entity should target.
"""

import asyncio

from custom_components.dynamic_ocpp_evse.control.hot_water_tank import (
    resolve_tank_setpoint,
    send_hot_water_tank_command,
)
from custom_components.dynamic_ocpp_evse.const import (
    BEHAVIOR_BINARY_EXCESS,
    CONF_BINARY_MIN_OFF_TIME,
    CONF_CLIMATE_ENTITY_ID,
    CONF_SOLAR_GRACE_PERIOD,
    CONF_TANK_AWAY_TEMPERATURE,
    CONF_TANK_BOOST_TEMPERATURE,
    CONF_TANK_NORMAL_TEMPERATURE,
    DOMAIN,
    TANK_MODE_FREEZE_PROTECTION,
    TANK_MODE_NORMAL,
    TANK_MODE_SOLAR_PRIORITY,
)
from custom_components.dynamic_ocpp_evse.const.hot_water_tank import (
    resolve_tank_mode_priority,
    tank_boost_is_opportunistic,
    TANK_SURPLUS_URGENCY_TIER,
)
from custom_components.dynamic_ocpp_evse.engine.load_builders import (
    _build_hot_water_tank_load,
)
from custom_components.dynamic_ocpp_evse.entities.load import min_off_hold

from .test_water_heater_tank import FakeEntry, FakeHass, FakeSensor, FakeState

AWAY, NORMAL, BOOST = 30.0, 45.0, 65.0
ELEMENT_POWER = 2000.0


def _hub(soc=None, soc_min=20, soc_target=80, export=0, excess=False):
    """Build a hub_data dict for resolve_tank_setpoint.

    ``excess`` is the hub's excess verdict - the one number every Excess-mode
    load reads (see calculations.excess_margin). Pass None to simulate a
    hub that published no verdict, which exercises the element-power fallback.
    """
    return {
        "battery_soc": soc,
        "battery_soc_min": soc_min,
        "battery_soc_target": soc_target,
        "total_export_power": export,
        "excess_available": excess,
    }


# --- Freeze Protection: away, raised to boost on surplus ---
#   Surplus means the hub reported excess (its absorption capacity is used up),
#   or the battery is over its target SOC. The element's own draw is NOT a test.

def test_freeze_protection_no_surplus_is_away():
    for hub in (_hub(), _hub(soc=10), _hub(soc=50, export=0)):
        result = resolve_tank_setpoint(
            TANK_MODE_FREEZE_PROTECTION.key, AWAY, NORMAL, BOOST, ELEMENT_POWER, hub
        )
        assert result == (AWAY, "away")


def test_freeze_protection_over_target_soc_is_boost():
    result = resolve_tank_setpoint(
        TANK_MODE_FREEZE_PROTECTION.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=85, soc_target=80, export=0),
    )
    assert result == (BOOST, "boost")


def test_freeze_protection_hub_excess_is_boost():
    # On-grid, no battery: the hub's excess verdict alone lifts it to boost.
    result = resolve_tank_setpoint(
        TANK_MODE_FREEZE_PROTECTION.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=None, excess=True),
    )
    assert result == (BOOST, "boost")


def test_freeze_protection_export_without_excess_is_away():
    # Plenty of export in absolute terms, but the hub says the site can still
    # absorb it (e.g. the battery has charge headroom) - no boost.
    result = resolve_tank_setpoint(
        TANK_MODE_FREEZE_PROTECTION.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=None, export=9999, excess=False),
    )
    assert result == (AWAY, "away")


def test_freeze_protection_missing_verdict_falls_back_to_element_power():
    # Hub published no verdict (stale hub_data) - degrade to the old export vs
    # element test rather than stranding the tank at its floor forever.
    assert resolve_tank_setpoint(
        TANK_MODE_FREEZE_PROTECTION.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=None, export=ELEMENT_POWER + 500, excess=None),
    ) == (BOOST, "boost")
    assert resolve_tank_setpoint(
        TANK_MODE_FREEZE_PROTECTION.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=None, export=ELEMENT_POWER - 500, excess=None),
    ) == (AWAY, "away")


# --- Solar Priority: setpoint follows the battery SOC band ---
#   below min SOC      → away
#   between min/target → normal
#   at or above target → boost

def test_solar_priority_below_target_boosts_on_the_hubs_excess_verdict():
    """The battery has priority until target - unless there is nothing left to
    give it.

    Excess means the pack is already taking every watt it is PERMITTED to take,
    so "the battery first" has nothing to protect and the surplus should go
    into hot water. This branch read SOC alone until 2026-09-09, and the live
    kozolec case is the shape: SOC 78 % against an 87 % target, the pack
    pulling 3 332 W against its 3 000 W allowance, the hub publishing excess -
    and the tank sitting at 42 C.
    """
    result = resolve_tank_setpoint(
        TANK_MODE_SOLAR_PRIORITY.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=78, soc_min=54, soc_target=87, excess=True),
    )
    assert result == (BOOST, "boost")


def test_solar_priority_below_target_without_excess_stays_normal():
    """The mirror: no surplus, so the battery keeps its priority."""
    result = resolve_tank_setpoint(
        TANK_MODE_SOLAR_PRIORITY.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=78, soc_min=54, soc_target=87, excess=False),
    )
    assert result == (NORMAL, "normal")


def test_solar_priority_below_MINIMUM_soc_ignores_excess():
    """The away floor is not yielded to surplus, deliberately.

    Below the minimum SOC the tank drops to its away temperature to leave
    energy for the house - a decision about the battery's reserve, not about
    whether production is spare. Excess does not lift it.
    """
    result = resolve_tank_setpoint(
        TANK_MODE_SOLAR_PRIORITY.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=40, soc_min=54, soc_target=87, excess=True),
    )
    assert result == (AWAY, "away")


def test_solar_priority_below_min_soc_is_away():
    result = resolve_tank_setpoint(
        TANK_MODE_SOLAR_PRIORITY.key, AWAY, NORMAL, BOOST, ELEMENT_POWER, _hub(soc=15)
    )
    assert result == (AWAY, "away")


def test_solar_priority_between_min_and_target_is_normal():
    result = resolve_tank_setpoint(
        TANK_MODE_SOLAR_PRIORITY.key, AWAY, NORMAL, BOOST, ELEMENT_POWER, _hub(soc=50)
    )
    assert result == (NORMAL, "normal")


def test_solar_priority_at_or_above_target_is_boost():
    for soc in (80, 95):
        result = resolve_tank_setpoint(
            TANK_MODE_SOLAR_PRIORITY.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
            _hub(soc=soc),
        )
        assert result == (BOOST, "boost")


def test_solar_priority_no_battery_defaults_normal():
    result = resolve_tank_setpoint(
        TANK_MODE_SOLAR_PRIORITY.key, AWAY, NORMAL, BOOST, ELEMENT_POWER, _hub(soc=None)
    )
    assert result == (NORMAL, "normal")


# --- Normal: normal setpoint, raised to boost on surplus ---

def test_normal_no_surplus_is_normal():
    result = resolve_tank_setpoint(
        TANK_MODE_NORMAL.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=50, export=0),
    )
    assert result == (NORMAL, "normal")


def test_normal_hub_excess_is_boost():
    result = resolve_tank_setpoint(
        TANK_MODE_NORMAL.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=50, excess=True),
    )
    assert result == (BOOST, "boost")


def test_normal_export_without_excess_is_normal():
    # Normal shares Freeze Protection's surplus test, so export the site can
    # still absorb leaves it at the normal setpoint.
    for export in (ELEMENT_POWER + 500, 12500):
        result = resolve_tank_setpoint(
            TANK_MODE_NORMAL.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
            _hub(soc=50, export=export, excess=False),
        )
        assert result == (NORMAL, "normal")


def test_normal_soc_over_target_is_boost():
    result = resolve_tank_setpoint(
        TANK_MODE_NORMAL.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=85, soc_target=80, export=0),
    )
    assert result == (BOOST, "boost")


def test_normal_no_battery_low_export_is_normal():
    result = resolve_tank_setpoint(
        TANK_MODE_NORMAL.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=None, export=0),
    )
    assert result == (NORMAL, "normal")


def test_normal_no_battery_excess_is_boost():
    result = resolve_tank_setpoint(
        TANK_MODE_NORMAL.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=None, export=14000, excess=True),
    )
    assert result == (BOOST, "boost")


def test_normal_offgrid_full_battery_boosts_without_export():
    """Off-grid: export is always ~0; the SOC > target clause carries boost."""
    result = resolve_tank_setpoint(
        TANK_MODE_NORMAL.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=90, soc_target=80, export=0),
    )
    assert result == (BOOST, "boost")


# --- Cold-tank priority promotion ---------------------------------------------
#
# resolve_tank_mode_priority promotes a Solar Priority tank below its normal
# temperature to the Normal urgency tier (1) so it outranks other solar-priority
# loads. Only the tier changes - the behavior stays Solar Priority elsewhere.

SOLAR = TANK_MODE_SOLAR_PRIORITY.key
SOLAR_TIER = TANK_MODE_SOLAR_PRIORITY.priority   # 2
NORMAL_TIER = TANK_MODE_NORMAL.priority          # 1


def test_promotion_cold_solar_priority_tank_is_elevated():
    assert resolve_tank_mode_priority(SOLAR, SOLAR_TIER, 38, 45, True) == (
        NORMAL_TIER,
        True,
    )


def test_promotion_warm_tank_keeps_tier():
    assert resolve_tank_mode_priority(SOLAR, SOLAR_TIER, 47, 45, True) == (
        SOLAR_TIER,
        False,
    )


def test_promotion_at_normal_temp_is_not_elevated():
    # Exactly at the setpoint counts as warm - only strictly below promotes.
    assert resolve_tank_mode_priority(SOLAR, SOLAR_TIER, 45, 45, True) == (
        SOLAR_TIER,
        False,
    )


def test_promotion_disabled_toggle_keeps_tier():
    assert resolve_tank_mode_priority(SOLAR, SOLAR_TIER, 38, 45, False) == (
        SOLAR_TIER,
        False,
    )


def test_promotion_only_applies_to_solar_priority():
    # A Normal-mode tank is already tier 1; promotion logic must not touch it.
    assert resolve_tank_mode_priority(
        TANK_MODE_NORMAL.key, NORMAL_TIER, 38, 45, True
    ) == (NORMAL_TIER, False)


def test_promotion_missing_temperature_keeps_tier():
    # Climate entity not reporting a current temperature → no promotion.
    assert resolve_tank_mode_priority(SOLAR, SOLAR_TIER, None, 45, True) == (
        SOLAR_TIER,
        False,
    )


# --- Surplus demotion --------------------------------------------------------
#
# A tank aiming at its boost setpoint is heating past what its mode asks for, on
# energy the site would otherwise dump - so it competes at the Excess tier (4)
# instead of its own, and yields the wire to every must-run load.

FREEZE = TANK_MODE_FREEZE_PROTECTION.key
FREEZE_TIER = TANK_MODE_FREEZE_PROTECTION.priority   # 1


def test_boosting_freeze_protection_tank_drops_to_excess_tier():
    assert resolve_tank_mode_priority(
        FREEZE, FREEZE_TIER, 32, 45, True, "boost"
    ) == (TANK_SURPLUS_URGENCY_TIER, False)


def test_boosting_normal_tank_drops_to_excess_tier():
    assert resolve_tank_mode_priority(
        TANK_MODE_NORMAL.key, NORMAL_TIER, 47, 45, True, "boost"
    ) == (TANK_SURPLUS_URGENCY_TIER, False)


def test_boosting_solar_priority_tank_drops_to_excess_tier():
    # Warm tank at/above target SOC - nothing urgent, so the surplus tier wins.
    assert resolve_tank_mode_priority(
        SOLAR, SOLAR_TIER, 47, 45, True, "boost"
    ) == (TANK_SURPLUS_URGENCY_TIER, False)


def test_cold_promotion_outranks_surplus_demotion():
    # A cold Solar Priority tank keeps tier 1 even while boosting: needing heat
    # beats merely having free energy available.
    assert resolve_tank_mode_priority(SOLAR, SOLAR_TIER, 38, 45, True, "boost") == (
        NORMAL_TIER,
        True,
    )


def test_away_and_normal_setpoints_keep_the_mode_tier():
    for label in ("away", "normal"):
        assert resolve_tank_mode_priority(FREEZE, FREEZE_TIER, 32, 45, True, label) == (
            FREEZE_TIER,
            False,
        )
        assert resolve_tank_mode_priority(SOLAR, SOLAR_TIER, 47, 45, True, label) == (
            SOLAR_TIER,
            False,
        )


def test_missing_label_keeps_the_mode_tier():
    # Callers that don't pass a label (older call sites) behave as before.
    assert resolve_tank_mode_priority(FREEZE, FREEZE_TIER, 47, 45, True) == (
        FREEZE_TIER,
        False,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
# --- Opportunistic boost: refusable, but never below the mode's own floor -----
#
# tank_boost_is_opportunistic decides whether a boosting tank is allocated as
# BEHAVIOR_BINARY_EXCESS (gated on real surplus) or keeps BEHAVIOR_FULL_POWER
# (unconditional). resolve_tank_setpoint returns "boost" on the surplus verdict
# ALONE, with no temperature test, so the floor guard here is what stops a
# 25 C frost-protection element being refused power.

FREEZE = TANK_MODE_FREEZE_PROTECTION.key
NORMALK = TANK_MODE_NORMAL.key


def test_boost_above_the_away_floor_is_opportunistic():
    # Freeze Protection asks for `away`; past it, boost heat is free energy.
    assert tank_boost_is_opportunistic(FREEZE, "boost", 35.0, 30.0) is True


def test_boost_below_the_away_floor_is_frost_protection():
    # THE GUARD: the label says boost, the tank is cold, the element must run.
    assert tank_boost_is_opportunistic(FREEZE, "boost", 25.0, 30.0) is False


def test_boost_exactly_at_the_floor_is_opportunistic():
    assert tank_boost_is_opportunistic(FREEZE, "boost", 30.0, 30.0) is True


def test_a_normal_mode_tank_below_its_normal_floor_still_runs():
    assert tank_boost_is_opportunistic(NORMALK, "boost", 38.0, 42.0) is False


def test_a_normal_mode_tank_above_its_floor_is_opportunistic():
    assert tank_boost_is_opportunistic(NORMALK, "boost", 45.0, 42.0) is True


def test_the_away_and_normal_setpoints_are_never_opportunistic():
    # Only the boost label rides surplus; the mode's own floor is must-run.
    assert tank_boost_is_opportunistic(FREEZE, "away", 35.0, 30.0) is False
    assert tank_boost_is_opportunistic(NORMALK, "normal", 45.0, 42.0) is False


def test_solar_priority_is_left_alone():
    # Already BEHAVIOR_SOLAR_PRIORITY, and its boost is SOC-driven.
    assert tank_boost_is_opportunistic(SOLAR, "boost", 45.0, 42.0) is False


def test_an_unknown_temperature_is_never_opportunistic():
    # A missing reading must not be what gates a must-run element.
    assert tank_boost_is_opportunistic(FREEZE, "boost", None, 30.0) is False
    assert tank_boost_is_opportunistic(FREEZE, "boost", 35.0, None) is False


# ---------------------------------------------------------------------------
# The setpoint holds: a flipping verdict must not flip the thermostat
# ---------------------------------------------------------------------------
#
# Kozolec, 3 Oct 2026: the tank followed the Excess verdict cycle by cycle, so
# a verdict sitting at its edge wrote 75 -> 42 -> 75 C on nearly every 5 s
# cycle - 113 setpoint changes that day, the element's relay switching every
# 5-20 s. A label change now holds for the tank's minimum off time, and leaving
# boost is ridden through its solar grace period like any solar load's dip.

CLIMATE = "climate.kozolec_boiler"
CYCLE_S = 5.0
HOLD_S = 5 * 60.0  # the tank's binary_min_off_time and solar_grace_period


def _kozolec_tank():
    """The Kozolec tank: Solar Priority, 42 / 75 C, 5 min hold and grace."""
    hass = FakeHass({CLIMATE: FakeState(
        "heat", current_temperature=64.2, temperature=42.0, min_temp=7, max_temp=80,
    )})
    hass.data[DOMAIN]["loads"]["tank"]["operating_mode"] = TANK_MODE_SOLAR_PRIORITY.key
    entry = FakeEntry({CONF_CLIMATE_ENTITY_ID: CLIMATE})
    entry.options = {
        CONF_TANK_AWAY_TEMPERATURE: 15.0,
        CONF_TANK_NORMAL_TEMPERATURE: 42.0,
        CONF_TANK_BOOST_TEMPERATURE: 75.0,
        CONF_BINARY_MIN_OFF_TIME: 5.0,
        CONF_SOLAR_GRACE_PERIOD: 5.0,
    }
    return hass, FakeSensor(hass, entry)


def _setpoints(verdicts, soc=16.0):
    """Drive the command path once per 5 s cycle with ``verdicts``; returns
    ``[(t, setpoint written)]``."""
    hass, sensor = _kozolec_tank()
    written = []
    for i, excess in enumerate(verdicts):
        t = i * CYCLE_S
        hass.services.calls.clear()
        hub = _hub(soc=soc, soc_min=10, soc_target=88, excess=excess)
        asyncio.run(send_hot_water_tank_command(sensor, 8.3, hub, t))
        for domain, service, data in hass.services.calls:
            if service == "set_temperature":
                written.append((t, data["temperature"]))
    return written


def _changes(written):
    return [
        (t, sp) for (t, sp), (_, prev) in zip(written[1:], written) if sp != prev
    ]


def test_a_verdict_flipping_every_cycle_moves_the_setpoint_at_most_once_per_hold():
    """Twenty minutes of a verdict flipping on every cycle: one change at most,
    and never two inside one hold window. It used to be 239."""
    written = _setpoints([i % 2 == 0 for i in range(240)])
    changes = _changes(written)
    assert len(changes) <= 20 * 60 / HOLD_S
    for (t1, _), (t2, _) in zip(changes, changes[1:]):
        assert t2 - t1 >= HOLD_S


def test_a_sustained_change_still_goes_through_after_the_hold():
    """Boost from the start; the verdict then drops for good at 10 min, and
    returns for good at 20 min. Boost rides the dip for the grace period and
    then goes to normal; normal holds for the minimum time and then boosts."""
    verdicts = [True] * 120 + [False] * 120 + [True] * 120
    written = _setpoints(verdicts)
    assert written[0] == (0.0, 75.0)
    assert _changes(written) == [
        (600.0 + HOLD_S, 42.0),            # dip ridden through the grace
        (600.0 + 2 * HOLD_S, 75.0),        # back up once normal has held
    ]


def test_the_minimum_soc_floor_is_never_held():
    """The away setpoint below the battery's minimum SOC protects the house's
    reserve, not surplus: it acts on the cycle it happens, mid-hold or not."""
    hass, sensor = _kozolec_tank()
    calls = hass.services.calls
    asyncio.run(send_hot_water_tank_command(
        sensor, 8.3, _hub(soc=16, soc_min=10, soc_target=88, excess=True), 0.0))
    calls.clear()
    asyncio.run(send_hot_water_tank_command(
        sensor, 8.3, _hub(soc=9, soc_min=10, soc_target=88, excess=True), CYCLE_S))
    assert ("climate", "set_temperature",
            {"entity_id": CLIMATE, "temperature": 15.0}) in calls


# ---------------------------------------------------------------------------
# Leaving boost goes straight to the floor, never through away or off
# ---------------------------------------------------------------------------
#
# Andrej's site, 4 Oct 2026: water_heater.tc (Normal mode, 42 / 46 C) went
# 46 -> 40 at 13:49 and 40 -> 42 at 13:54 - its lowest target for five minutes
# between boost and normal - and 46 -> 40 -> 46 -> 40 through 11:21-12:13. Home's
# workshop boiler went 80 heat -> off -> 80 heat -> off. The label held at boost,
# so the allocator still sized the tank as opportunistic and withdrew its power
# with the surplus, and a tank denied power was switched off however warm it was.

TICK_S = 15.0


def _tank(entity_id, mode, temp, away, normal, boost, min_temp, **attrs):
    device = FakeState(
        "off" if entity_id.startswith("climate.") else "auto",
        current_temperature=temp, temperature=normal, min_temp=min_temp, max_temp=80,
        **attrs,
    )
    hass = FakeHass({entity_id: device})
    hass.data[DOMAIN]["loads"]["tank"]["operating_mode"] = mode
    entry = FakeEntry({CONF_CLIMATE_ENTITY_ID: entity_id})
    entry.options = {
        CONF_TANK_AWAY_TEMPERATURE: away,
        CONF_TANK_NORMAL_TEMPERATURE: normal,
        CONF_TANK_BOOST_TEMPERATURE: boost,
        CONF_BINARY_MIN_OFF_TIME: 5.0,
        CONF_SOLAR_GRACE_PERIOD: 5.0,
    }
    return hass, device, FakeSensor(hass, entry)


def _replay(entity_id, surplus, mode=TANK_MODE_NORMAL.key, temp=44.0,
            away=40.0, normal=42.0, boost=46.0, min_temp=40, **attrs):
    """One 15 s cycle per ``surplus`` verdict: the builder sizes the tank from
    the label last written, the allocator grants a must-run tank its rating and
    an opportunistic one its rating only on the surplus, the minimum off time
    holds a shed permit, and the command path writes. Returns what the device
    was set to, change by change - a temperature, or "off" - and the labels."""
    hass, device, sensor = _tank(entity_id, mode, temp, away, normal, boost, min_temp, **attrs)
    off_since, shown, labels = None, [], []
    for i, excess in enumerate(surplus):
        t = i * TICK_S
        load = _build_hot_water_tank_load(hass, sensor.config_entry, 230.0, "tank_1", 1)
        granted = (
            0.0 if load.mode_behavior == BEHAVIOR_BINARY_EXCESS and not excess
            else load.max_current
        )
        permit, off_since, _ = min_off_hold(granted, off_since, t, 300.0)
        hass.services.calls.clear()
        asyncio.run(send_hot_water_tank_command(
            sensor, permit, {"excess_available": excess}, t))
        for _domain, service, data in hass.services.calls:
            if service == "set_temperature":
                device.attributes["temperature"] = data["temperature"]
            elif service == "set_hvac_mode":
                device.state = data["hvac_mode"]
        now = "off" if device.state == "off" else device.attributes["temperature"]
        if not shown or shown[-1] != now:
            shown.append(now)
        labels.append(hass.data[DOMAIN]["loads"]["tank"]["tank_setpoint_label"])
    return shown, labels


MIN = 60 / TICK_S  # cycles a minute


def _minutes(*spans):
    return [v for minutes, v in spans for _ in range(int(minutes * MIN))]


def test_a_water_heater_leaving_boost_goes_straight_to_normal():
    shown, _ = _replay("water_heater.tc", _minutes((10, True), (15, False)))
    assert shown == [46.0, 42.0]


def test_a_thermostat_leaving_boost_goes_straight_to_normal_not_off():
    shown, _ = _replay("climate.workshop_boiler", _minutes((10, True), (15, False)),
                       temp=60.0, away=10.0, boost=80.0, min_temp=7)
    assert shown == [80.0, 42.0]


def test_a_dip_inside_the_grace_comes_back_to_boost_without_passing_away():
    # The label rides the dip at boost; the device waits at its floor - the
    # surplus it was boosting on is gone - and boosts again once it is back.
    shown, labels = _replay("water_heater.tc",
                            _minutes((10, True), (2, False), (10, True)))
    assert shown == [46.0, 42.0, 46.0]
    assert set(labels) == {"boost"}


def test_a_denied_tank_the_device_says_heats_is_not_held_at_its_floor():
    """Above its normal floor on the last poll, but the heat pump says it heats
    (cooled past its own hysteresis since): denied, it goes to its lowest
    target, not to a floor target it would heat to."""
    def denied(status):
        hass, device, sensor = _tank("water_heater.tc", TANK_MODE_NORMAL.key, 44.0,
                                     40.0, 42.0, 46.0, 40, status=status)
        _build_hot_water_tank_load(hass, sensor.config_entry, 230.0, "tank_1", 1)
        asyncio.run(send_hot_water_tank_command(sensor, 0.0, {"excess_available": False}, 0.0))
        return [d["temperature"] for _, s, d in hass.services.calls if s == "set_temperature"]
    assert denied("idle") == []            # at its floor, already set to it
    assert denied("heat_water") == [40]    # heating at the floor target: its lowest


def test_freeze_protection_leaves_boost_for_its_own_floor_away():
    shown, _ = _replay("climate.workshop_boiler", _minutes((10, True), (15, False)),
                       mode=FREEZE, temp=60.0, away=30.0, boost=80.0, min_temp=7)
    assert shown == [80.0, 30.0]


def _denied(entity_id, temp, hub=None, mode=TANK_MODE_NORMAL.key):
    hass, _device, sensor = _tank(entity_id, mode, temp, 30.0, 42.0, 65.0, 7)
    asyncio.run(send_hot_water_tank_command(sensor, 0.0, hub or {}, 0.0))
    return [(service, data) for _domain, service, data in hass.services.calls]


def test_a_tank_denied_power_above_its_floor_waits_at_the_floor():
    assert _denied("climate.tank", 50.0) == [
        ("set_temperature", {"entity_id": "climate.tank", "temperature": 42.0}),
        ("set_hvac_mode", {"entity_id": "climate.tank", "hvac_mode": "heat"}),
    ]


def test_a_tank_denied_power_below_its_floor_is_switched_off():
    # Genuinely no power for a tank that would heat: it must stop drawing.
    assert _denied("climate.tank", 38.0) == [
        ("set_hvac_mode", {"entity_id": "climate.tank", "hvac_mode": "off"}),
    ]
    assert _denied("water_heater.tank", 38.0) == [
        ("set_temperature", {"entity_id": "water_heater.tank", "temperature": 7}),
    ]


def test_a_tank_denied_power_with_no_temperature_is_switched_off():
    # An unknown temperature must not be what keeps a denied element heating.
    assert _denied("climate.tank", None) == [
        ("set_hvac_mode", {"entity_id": "climate.tank", "hvac_mode": "off"}),
    ]


def test_solar_priority_below_the_minimum_soc_waits_at_away():
    # The battery's minimum SOC is away's own condition: away, never normal.
    calls = _denied("climate.tank", 50.0, _hub(soc=5, soc_min=10, soc_target=80),
                    TANK_MODE_SOLAR_PRIORITY.key)
    assert calls[0] == (
        "set_temperature", {"entity_id": "climate.tank", "temperature": 30.0})
