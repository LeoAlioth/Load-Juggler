"""Portable power station constants — a modulating battery-charging load.

A portable power station (EcoFlow Delta and similar, via a local integration
such as ha-ef-ble) exposes two knobs, and the device type uses both:

- **AC charging speed** (W) — the rate ceiling. The engine's allocation is
  written here, so the station modulates like an EVSE.
- **Backup reserve** (%) — in the station's self-powered mode this is both the
  SOC it grid-charges *up to* and the floor it discharges *down to*. It is
  therefore the real on/off gate: with the reserve below the current battery
  level the station draws nothing from the wall and instead serves its own
  loads from its battery, spending what it stored.

The rate knob cannot express "stop" — a station with a 200 W minimum charge
speed has no zero — so the reserve is what turns charging off. resolve_station_reserve()
owns that decision; the engine only decides how much power the station may take.

Whatever is plugged *into* the station passes through to its outputs and is not
ours to control, so only the charging component counts as this load's managed
draw: ``ac_input_power - ac_output_power``, with the commanded speed as a
fallback when those sensors are missing.
"""

from .common import OperatingMode

# --- Controlled entities (all resolved from the station's device) ---
CONF_STATION_CHARGE_SPEED_ENTITY_ID = "station_charge_speed_entity_id"    # number, W — the rate ceiling
CONF_STATION_RESERVE_ENTITY_ID = "station_reserve_entity_id"              # number, % — backup reserve
CONF_STATION_BATTERY_LEVEL_ENTITY_ID = "station_battery_level_entity_id"  # sensor, % — SOC
CONF_STATION_CHARGE_LIMIT_ENTITY_ID = "station_charge_limit_entity_id"    # number, % — max charge limit (read only)
CONF_STATION_AC_INPUT_ENTITY_ID = "station_ac_input_entity_id"            # sensor, W — total wall draw
CONF_STATION_AC_OUTPUT_ENTITY_ID = "station_ac_output_entity_id"          # sensor, W — pass-through + battery output
CONF_STATION_DEVICE_ID = "station_device_id"                              # device to resolve the above from

# --- Charge rate bounds: configured, not read from the device, so the station
# can be held below what its hardware allows. Runtime sliders override these.
CONF_STATION_MIN_CHARGE_POWER = "station_min_charge_power"
CONF_STATION_MAX_CHARGE_POWER = "station_max_charge_power"
DEFAULT_STATION_MIN_CHARGE_POWER = 200   # W — EcoFlow Delta floor
DEFAULT_STATION_MAX_CHARGE_POWER = 2400  # W
STATION_CHARGE_POWER_STEP = 100          # W — device granularity; also the write deadband

# --- Reserve levels ---
CONF_STATION_NORMAL_RESERVE = "station_normal_reserve"  # % held in reserve day to day
CONF_STATION_STORM_RESERVE = "station_storm_reserve"    # % held when storm reserve is on
DEFAULT_STATION_NORMAL_RESERVE = 30
DEFAULT_STATION_STORM_RESERVE = 80
# Fallback when the station's own Max Charge Limit can't be read.
DEFAULT_STATION_CHARGE_LIMIT = 90

# Storm reserve: fill from any source and hold the charge for an outage. A
# reserve that may only be filled from surplus is not a reserve, so this
# overrides the operating mode while it is on.
CONF_STATION_STORM_RESERVE_ON = "station_storm_reserve_on"

# Operating modes — priority is the distribution urgency tier (1-4). The station
# modulates, so these are the EVSE behaviors; the mapping lives in const/modes.py.
STATION_MODE_STANDARD = OperatingMode(
    key="Standard", label="Standard", priority=1, icon="mdi:flash",
)
STATION_MODE_SOLAR_PRIORITY = OperatingMode(
    key="Solar Priority", label="Solar Priority", priority=2, icon="mdi:leaf",
)
STATION_MODE_SOLAR_ONLY = OperatingMode(
    key="Solar Only", label="Solar Only", priority=3, icon="mdi:solar-power",
)
STATION_MODE_EXCESS = OperatingMode(
    key="Excess", label="Excess", priority=4, icon="mdi:solar-power-variant",
)
OPERATING_MODES_POWER_STATION = [
    STATION_MODE_STANDARD,
    STATION_MODE_SOLAR_PRIORITY,
    STATION_MODE_SOLAR_ONLY,
    STATION_MODE_EXCESS,
]
# Absorbing surplus is the point of the device type, so Excess is the default.
DEFAULT_OPERATING_MODE_POWER_STATION = STATION_MODE_EXCESS


def resolve_station_charge_speed(allocated_power, min_power, max_power, step=STATION_CHARGE_POWER_STEP):
    """Quantise an allocation (W) to a speed the station will accept.

    Returns ``None`` when the allocation cannot sustain the station's minimum
    charge rate — the caller then drops the reserve instead of writing a speed,
    because the rate knob has no zero.

    Floors to ``step`` so the engine never asks for more than it allocated: a
    station rounding 250 W up to 300 W would quietly overdraw the pool.

    Pure function — unit-testable.
    """
    if allocated_power is None or allocated_power < min_power:
        return None
    speed = int(allocated_power // step) * step
    return max(min_power, min(speed, max_power))


def resolve_station_reserve(
    charging, normal_reserve, storm_reserve, charge_limit, storm_on
):
    """Return ``(reserve_percent, label)`` for the station's backup reserve.

    The reserve is the gate. Three states:

    - **storm** — storm reserve is on: hold ``storm_reserve``, filled from any
      source, and don't discharge below it.
    - **charging** — the engine found power for the station: raise the reserve to
      the station's own max charge limit so it accepts the charge. Never above
      that limit; it is the user's battery-health cap.
    - **normal** — nothing to absorb: drop to ``normal_reserve``. Below the
      current SOC this stops the wall draw completely and lets the station spend
      what it stored on its own loads.

    ``storm`` wins over ``charging`` because a storm reserve that yields to a
    cloudy afternoon is not a reserve.

    Pure function — unit-testable.
    """
    if storm_on:
        return storm_reserve, "storm"
    if charging:
        return charge_limit, "charging"
    return normal_reserve, "normal"
