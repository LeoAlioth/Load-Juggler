"""Hot water tank constants — climate-entity-driven binary heating load.

The climate entity owns all temperature regulation; Load Juggler only gates
power and writes the setpoint.
"""

from .common import OperatingMode

CONF_CLIMATE_ENTITY_ID = "climate_entity_id"              # HA climate entity (read + control)
CONF_HEATING_ELEMENT_POWER = "heating_element_power"      # Element rating in watts
CONF_TANK_POWER_ENTITY_ID = "tank_power_entity_id"        # Optional live power sensor
CONF_TANK_POWER_DEVICE_ID = "tank_power_device_id"        # Optional device to resolve a power sensor from
CONF_TANK_AWAY_TEMPERATURE = "tank_away_temperature"      # Frost-protection / minimal setpoint
CONF_TANK_NORMAL_TEMPERATURE = "tank_normal_temperature"  # Baseline setpoint
CONF_TANK_BOOST_TEMPERATURE = "tank_boost_temperature"    # High setpoint (surplus available)
# When on, a Solar Priority tank that has dropped below its normal temperature
# is promoted to the Normal urgency tier so it wins contention against other
# solar-priority loads — without changing its source behavior, so it still
# won't drain the battery below its minimum SOC.
CONF_TANK_PRIORITIZE_BELOW_NORMAL = "tank_prioritize_below_normal"
DEFAULT_HEATING_ELEMENT_POWER = 2000      # W
DEFAULT_TANK_AWAY_TEMPERATURE = 30        # °C
DEFAULT_TANK_NORMAL_TEMPERATURE = 45      # °C
DEFAULT_TANK_BOOST_TEMPERATURE = 65       # °C
DEFAULT_TANK_PRIORITIZE_BELOW_NORMAL = True

# Hot water tank operating modes. Each picks a setpoint (away/normal/boost)
# dynamically via resolve_tank_setpoint(); priority is the distribution
# urgency tier (1-4). The behavior each maps to is in const/modes.py.
TANK_MODE_FREEZE_PROTECTION = OperatingMode(
    key="Freeze Protection", label="Freeze Protection", priority=1,
    icon="mdi:snowflake",
)
TANK_MODE_NORMAL = OperatingMode(
    key="Normal", label="Normal", priority=1, icon="mdi:water-boiler",
)
TANK_MODE_SOLAR_PRIORITY = OperatingMode(
    key="Solar Priority", label="Solar Priority", priority=2, icon="mdi:leaf",
)
OPERATING_MODES_HOT_WATER_TANK = [
    TANK_MODE_FREEZE_PROTECTION,
    TANK_MODE_NORMAL,
    TANK_MODE_SOLAR_PRIORITY,
]

# Urgency tier a tank competes at while it is riding surplus (boost setpoint).
# Matches the Excess tier used by EVSEs and plugs: heating past the mode's own
# floor temperature is opportunistic, so it must not outrank must-run loads.
TANK_SURPLUS_URGENCY_TIER = 4
DEFAULT_OPERATING_MODE_HOT_WATER_TANK = TANK_MODE_NORMAL


def resolve_tank_mode_priority(
    mode_key,
    mode_priority,
    current_temp,
    normal_temp,
    prioritize_cold,
    setpoint_label=None,
):
    """Effective urgency tier for a tank load.

    Two adjustments, in precedence order:

    1. Cold promotion — a Solar Priority tank that has dropped below its normal
       setpoint is promoted to the Normal urgency tier so it outranks other
       solar-priority loads when power is contended. Only the tier changes; the
       caller keeps the Solar Priority *behavior*, so the tank still draws from
       solar + above-minimum battery and never deep-cycles the bank below its
       minimum SOC.
    2. Surplus demotion — a tank aiming at its *boost* setpoint is heating past
       the temperature its mode actually asks for, on energy the site would
       otherwise dump. That is opportunistic, so it drops to the Excess tier and
       yields the wire to every must-run load. A cold tank (1) keeps its
       promotion: needing heat outranks having free energy.

    Pure function — unit-testable. Returns ``(effective_priority, elevated)``.
    """
    if (
        prioritize_cold
        and mode_key == TANK_MODE_SOLAR_PRIORITY.key
        and current_temp is not None
        and normal_temp is not None
        and current_temp < normal_temp
    ):
        return TANK_MODE_NORMAL.priority, True
    if setpoint_label == "boost":
        return TANK_SURPLUS_URGENCY_TIER, False
    return mode_priority, False
