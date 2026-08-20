"""Inverter entry constants — battery charge and SOC write-control.

The PV clipping forecast produces two recommendations per battery: a *charge
limit* (``forecast_charge_limit_w``) — how fast this battery may fill so the
midday production peak still has somewhere to go instead of being clipped at
the export limit — and a *max SOC* (``forecast_battery_max_soc``), the ceiling
that leaves exactly that much headroom. These keys turn either recommendation
into an actual write to the inverter.

The two controls are separate because the registers behind them are shaped
differently. The charge RATE is one register that every hybrid exposes (Deye:
maximum battery charge current), so it is one entity. The SOC ceiling on a Deye
is not a register at all — it lives in the *time-of-use slots*, six `number`
entities that each carry their own "charge up to %", so that control fans one
value out across as many entities as the user names. Neither replaces the
other: slowing the fill is gentler, a ceiling is absolute, and a site may arm
one, both or neither.

Everything here is optional. With no target entity configured, the inverter
entry stays advisory — exactly as before write-control existed.
"""

# The number entity to write: the inverter's max battery charge current/power.
CONF_CHARGE_LIMIT_ENTITY_ID = "inverter_charge_limit_entity_id"

# What that register expects. Deye exposes DC amps at battery voltage, others
# expose watts — the advice is computed in watts and converted on write.
CONF_CHARGE_LIMIT_UNIT = "inverter_charge_limit_unit"
CHARGE_LIMIT_UNIT_AMPS = "A"
CHARGE_LIMIT_UNIT_WATTS = "W"
DEFAULT_CHARGE_LIMIT_UNIT = CHARGE_LIMIT_UNIT_AMPS

# Battery DC voltage for the W↔A conversion. A live voltage sensor is more
# accurate across the charge curve; the nominal value is the fallback.
CONF_BATTERY_VOLTAGE_ENTITY_ID = "battery_voltage_entity_id"
CONF_BATTERY_NOMINAL_VOLTAGE = "battery_nominal_voltage"
DEFAULT_BATTERY_NOMINAL_VOLTAGE = 51.2  # V — 16S LFP, the common hybrid pack

# What to write back when the forecast releases the limit (evening, no
# clipping expected, control switched off). 0 = restore the target entity's
# own maximum, which is what an unmanaged inverter would sit at.
CONF_CHARGE_LIMIT_NORMAL = "inverter_charge_limit_normal"
DEFAULT_CHARGE_LIMIT_NORMAL = 0

# Write pacing. These registers go over Modbus and some firmwares commit them
# to EEPROM, so a value that moves a few watts every cycle is genuinely
# harmful — write only on a real change, and not more often than this.
CONF_CHARGE_CONTROL_INTERVAL = "inverter_charge_control_interval"
DEFAULT_CHARGE_CONTROL_INTERVAL = 300  # s between writes
CONF_CHARGE_CONTROL_DEADBAND = "inverter_charge_control_deadband"
DEFAULT_CHARGE_CONTROL_DEADBAND = 5  # % of the normal value

# --- Battery SOC ceiling control ---------------------------------------------
#
# The SOC twin of the charge-rate control above, and deliberately a LIST of
# target entities: on a Deye the ceiling is expressed per time-of-use slot, so
# holding the battery at 80 % means writing 80 to every slot the battery may
# charge in. One value, N entities — a single-entity field would silently
# control a sixth of the day.
CONF_SOC_LIMIT_ENTITY_IDS = "inverter_soc_limit_entity_ids"

# Where the "normal" ceiling comes from while the forecast is not holding the
# battery back. An entity rather than a stored number, so whatever already owns
# the slots — a seasonal automation, a cheap-tariff schedule, the user's own
# input_number — keeps owning them: we only ever push it DOWN (see the min() in
# control/inverter.py), and a change to this entity propagates on the next
# cycle. Unconfigured, or configured and unreadable, is not a guess: the
# constant below stands in for the first, and the second defers all writes.
CONF_SOC_LIMIT_NORMAL_ENTITY_ID = "inverter_soc_limit_normal_entity_id"
DEFAULT_SOC_LIMIT_NORMAL = 100.0  # % — where an unmanaged battery ceiling sits

# Deadband for the SOC fan-out, in SOC points, applied PER TARGET. Fixed rather
# than configurable: a percentage-of-a-percentage would be a confusing setting,
# and 1 point is both the resolution these slots accept and small enough that
# every meaningful move gets through. Same EEPROM reasoning as above — a slot
# already within a point of the desired ceiling is not worth a Modbus write.
SOC_LIMIT_DEADBAND = 1.0

# Runtime keys under hass.data[DOMAIN]["inverters"][entry_id].
INVERTER_RT_CONTROL_ENABLED = "charge_control_enabled"
INVERTER_RT_APPLIED = "charge_control_applied"  # last value written (target units)
INVERTER_RT_LAST_WRITE = "charge_control_last_write"  # monotonic seconds
INVERTER_RT_STATUS = "charge_control_status"

# The same three for the SOC control — its own arming flag, its own write clock
# and its own standing, because the two controls are armed and paced
# independently even though one cycle worker performs both.
INVERTER_RT_SOC_CONTROL_ENABLED = "soc_control_enabled"
INVERTER_RT_SOC_LAST_WRITE = "soc_control_last_write"  # monotonic seconds
INVERTER_RT_SOC_STATUS = "soc_control_status"
