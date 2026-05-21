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
DEFAULT_HEATING_ELEMENT_POWER = 2000      # W
DEFAULT_TANK_AWAY_TEMPERATURE = 30        # °C
DEFAULT_TANK_NORMAL_TEMPERATURE = 45      # °C
DEFAULT_TANK_BOOST_TEMPERATURE = 65       # °C

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
DEFAULT_OPERATING_MODE_HOT_WATER_TANK = TANK_MODE_NORMAL
