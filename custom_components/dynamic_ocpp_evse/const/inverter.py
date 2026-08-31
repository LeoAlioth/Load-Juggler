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

# What to write back when the forecast releases the limit (below the battery's
# destination with no clipping expected, control switched off). 0 = restore the
# target entity's own maximum, which is what an unmanaged inverter would sit at.
CONF_CHARGE_LIMIT_NORMAL = "inverter_charge_limit_normal"
DEFAULT_CHARGE_LIMIT_NORMAL = 0

# The lowest value ever WRITTEN while the forecast is holding this battery back,
# in the target register's own units — the same convention as the normal above.
#
# A recommendation of 0 is legitimate: solar below the export threshold with the
# battery already at the reserved ceiling — or simply parked at its destination
# on a day that reserves nothing — means "do not add any more". Written as
# a hard 0 the battery stops charging entirely, so it keeps serving the house
# instead and the SOC sags — until the latch releases and the inverter recharges
# at full rate, which is a sawtooth rather than a hold. A couple of amps is
# enough for the battery to trickle back the house draw and hug the ceiling.
#
# 0 (the default) is exactly the behaviour that existed before this knob: the
# recommendation is applied as-is. It is a device-protection floor and not
# forecast policy — the published advice sensors stay unclamped.
CONF_CHARGE_LIMIT_MINIMUM = "inverter_charge_limit_minimum"
DEFAULT_CHARGE_LIMIT_MINIMUM = 0

# Write pacing. These registers go over Modbus and some firmwares commit them
# to EEPROM, so a value that moves a few watts every cycle is genuinely
# harmful — write only on a real change, and only when it has lasted.
#
# The interval is DIRECTIONAL for the charge rate (see ``control/inverter.py``):
# it is how long a REDUCTION must hold before it is written, since the engaged
# advice is direct feedback on a live meter and a reduction that lasts less than
# this — a kettle, a passing cloud, a car plugging in — is not one the register
# should follow. A rise is not paced by it at all (it is bounded to one Excess
# trigger margin per write instead), and the cap engaging bypasses it as the
# protective transition it is. For the RELEASE ramp, and for the SOC ceiling
# control, it stays a plain minimum time between writes.
#
# The key is unchanged from when this was a symmetric write interval, so no
# stored value migrates: 300 s means the same 300 s, applied to the direction
# where waiting is free.
CONF_CHARGE_CONTROL_INTERVAL = "inverter_charge_control_interval"
DEFAULT_CHARGE_CONTROL_INTERVAL = 300  # s a reduction must hold (and pace a release)
# How far the desired charge limit must sit from the register before a write is
# worth making, in WATTS. Absolute rather than a percentage of the normal value,
# which is what it used to be: a percentage is a fraction of the register's FULL
# span, while the value being corrected while the limit is engaged lives in a
# band one Excess trigger margin wide (the export overshoot above the setpoint,
# which is all there is to correct). On a 187 A register 5 % is 9 A — wider than
# the whole working range, so most of it could not be expressed at all.
#
# Watts, not the register's own unit, so one setting means the same thing on an
# amps register and a watts one; the conversion is the same one the slew step
# uses (``to_target_units``). That was the original reason for the percentage,
# and converting is the better answer to it.
CONF_CHARGE_CONTROL_DEADBAND_W = "inverter_charge_control_deadband_w"
DEFAULT_CHARGE_CONTROL_DEADBAND_W = 100  # W


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

# What a WRITE to the inverter's SOC entities MEANS — and only a write. This
# flag never decides where the pack should go: the destination is always read
# from CONF_SOC_LIMIT_NORMAL_ENTITY_ID when that is set, whatever the hardware
# calls the register (see engine/readers.py). A floor register whose value is
# NOT the owner's charge target simply leaves the source unset and anchors at
# 100 % — that knob already existed, and the flag must not duplicate it.
#
# The distinction it does carry is what the SOC fan-out may put into the slots.
# A genuine ceiling register takes "stop charging at N %", so writing the
# reserve-lowered recommendation into it is the whole point. A Deye slot SOC is
# a DISCHARGE FLOOR plus grid-charge target: the same write tells the inverter
# "grid-charge to N % and never discharge below it" — a reservation written
# into it at night imports toward the reserve instead of holding charging back
# (observed live 2026-08-25). The floor-aware fan-out fix keys on this flag;
# until it ships the flag is declarative, and a floor site keeps its SOC
# control switch off.
#
# The common floor site (a Deye whose one slot value is both the overnight
# floor and the owner's charge target) points the ceiling SOURCE at the slot:
# the reserve is carved below that number, the pack parks there on ordinary
# days, and the band above it stays the export-holding buffer that
# ``recommended_charge_limit``'s engaged feedback fills only while export sits
# over the setpoint.
CONF_SOC_LIMIT_SEMANTICS = "inverter_soc_limit_semantics"
SOC_LIMIT_SEMANTICS_CEILING = "ceiling"   # writes mean "stop charging at"
SOC_LIMIT_SEMANTICS_FLOOR = "floor"       # writes mean "grid-defend this level"
DEFAULT_SOC_LIMIT_SEMANTICS = SOC_LIMIT_SEMANTICS_CEILING
SOC_LIMIT_SEMANTICS_OPTIONS = (
    SOC_LIMIT_SEMANTICS_CEILING,
    SOC_LIMIT_SEMANTICS_FLOOR,
)

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
# The rate the battery is actually PERMITTED to take while this control holds
# its register down, in WATTS — None whenever nothing is being held back (switch
# off, no advice, released). The one runtime key here that the calculation
# engine reads rather than an entity: engine/readers.py picks it up per fleet
# member so the Excess verdict's charge allowance is the permitted rate rather
# than the nameplate one (see engine/fleet.charge_power_total). Watts rather
# than the register's own units, so the engine never has to know about
# CONF_CHARGE_LIMIT_UNIT or the DC battery voltage.
INVERTER_RT_ENFORCED_CHARGE_W = "charge_control_enforced_w"

# The same three for the SOC control — its own arming flag, its own write clock
# and its own standing, because the two controls are armed and paced
# independently even though one cycle worker performs both.
INVERTER_RT_SOC_CONTROL_ENABLED = "soc_control_enabled"
INVERTER_RT_SOC_LAST_WRITE = "soc_control_last_write"  # monotonic seconds
INVERTER_RT_SOC_STATUS = "soc_control_status"
