"""Inverter entry constants — battery charge write-control.

The PV clipping forecast produces a *recommended* charge limit per battery
(``forecast_charge_limit_w``): how fast this battery may fill so the midday
production peak still has somewhere to go instead of being clipped at the
export limit. These keys turn that recommendation into an actual write to the
inverter's own charge-current register.

Deliberately charge-RATE only. On a Deye (and most hybrids) the register that
always exists is the maximum battery charge current; a true "stop at X %"
ceiling either doesn't exist or is expressed as time-of-use slots that behave
differently. Slowing the fill is also the gentler control: the battery keeps
charging all day, it just doesn't reach full before the peak.

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

# Runtime keys under hass.data[DOMAIN]["inverters"][entry_id].
INVERTER_RT_CONTROL_ENABLED = "charge_control_enabled"
INVERTER_RT_APPLIED = "charge_control_applied"  # last value written (target units)
INVERTER_RT_LAST_WRITE = "charge_control_last_write"  # monotonic seconds
INVERTER_RT_STATUS = "charge_control_status"
