"""Hot water tank constants — climate-entity-driven binary heating load.

The climate entity owns all temperature regulation; Load Juggler only gates
power and writes the setpoint.
"""

from .common import (
    OPERATING_MODE_FREEZE_PROTECTION,
    OPERATING_MODE_NORMAL,
    OPERATING_MODE_SOLAR_ONLY,
)

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

# Hot water tank operating modes select a setpoint (away/normal/boost)
# dynamically; each is mapped to an engine mode in the HA layer
# (Freeze/Normal → Continuous, Solar Only → Solar Priority).
OPERATING_MODES_HOT_WATER_TANK = [
    OPERATING_MODE_FREEZE_PROTECTION,
    OPERATING_MODE_NORMAL,
    OPERATING_MODE_SOLAR_ONLY,
]
DEFAULT_OPERATING_MODE_HOT_WATER_TANK = OPERATING_MODE_NORMAL
