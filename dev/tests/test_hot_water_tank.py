"""Tests for the hot water tank device type — setpoint resolution.

Machine-authored tests — not yet human-reviewed.

resolve_tank_setpoint is the core new logic: given the tank's operating mode,
the three setpoints, the element power and the hub state, it picks which
setpoint (away / normal / boost) the climate entity should target.
"""

from custom_components.dynamic_ocpp_evse.control.hot_water_tank import resolve_tank_setpoint
from custom_components.dynamic_ocpp_evse.const import (
    TANK_MODE_FREEZE_PROTECTION,
    TANK_MODE_NORMAL,
    TANK_MODE_SOLAR_PRIORITY,
)
from custom_components.dynamic_ocpp_evse.const.hot_water_tank import (
    resolve_tank_mode_priority,
    TANK_SURPLUS_URGENCY_TIER,
)

AWAY, NORMAL, BOOST = 30.0, 45.0, 65.0
ELEMENT_POWER = 2000.0


def _hub(soc=None, soc_min=20, soc_target=80, export=0, excess=False):
    """Build a hub_data dict for resolve_tank_setpoint.

    ``excess`` is the hub's excess verdict — the one number every Excess-mode
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
    # absorb it (e.g. the battery has charge headroom) — no boost.
    result = resolve_tank_setpoint(
        TANK_MODE_FREEZE_PROTECTION.key, AWAY, NORMAL, BOOST, ELEMENT_POWER,
        _hub(soc=None, export=9999, excess=False),
    )
    assert result == (AWAY, "away")


def test_freeze_protection_missing_verdict_falls_back_to_element_power():
    # Hub published no verdict (stale hub_data) — degrade to the old export vs
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
# loads. Only the tier changes — the behavior stays Solar Priority elsewhere.

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
    # Exactly at the setpoint counts as warm — only strictly below promotes.
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
# energy the site would otherwise dump — so it competes at the Excess tier (4)
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
    # Warm tank at/above target SOC — nothing urgent, so the surplus tier wins.
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
