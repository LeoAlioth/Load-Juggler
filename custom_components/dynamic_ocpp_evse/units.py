"""Entity-read handling — the one place that knows A/mA/W/kW/V/mV, and the one
place that decides whether a reading is usable at all.

Every physical value Load Juggler reads comes from someone else's integration,
in whatever unit that integration felt like publishing. The config flow is
deliberately permissive about this (a meter's signed power entity is often the
only usable choice for a "current" field, see CONF_PHASE_*_CURRENT_ENTITY_ID),
so the conversion has to be right at every read site.

It used to be spread out: the old ``_read_entity(unit="A")`` contract said
"W → A conversion requires voltage - caller must handle this", and each reader
reimplemented it. The grid-phase reader never did, so a 1300 W meter reading
was treated as 1300 A and multiplied by voltage again — 300 kW of phantom grid
power. Hence one module, pure functions, and an exhaustive test matrix.

``ENTITY_UNIT_CONTRACTS`` below is the declaration the config flow validates
against and the tests hold us to: every unit a field accepts must be a unit
some converter here can actually turn into that field's canonical domain.

``is_unavailable`` / ``is_unusable_number`` are here for the same reason the
converters are: "is this sensor reading usable?" used to be hand-rolled at a
dozen read sites with five different answers, and the safety-relevant one
(the grid CTs) had the loosest. The module stays importable without Home
Assistant — state objects are duck-typed, only ``.state`` is touched — so the
pure test tier can hold these to their contract.
"""

from __future__ import annotations

import math

from .const import (
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
)

# Units the pickers offer and the validator accepts, per physical domain.
CURRENT_UNITS = frozenset({"A", "mA"})
POWER_UNITS = frozenset({"W", "kW"})
VOLTAGE_UNITS = frozenset({"V", "mV"})
SOC_UNITS = frozenset({"%"})

# Canonical domains — what the engine wants a value in, whatever it arrived as.
DOMAIN_AMPS = "A"
DOMAIN_WATTS = "W"
DOMAIN_VOLTS = "V"
DOMAIN_PERCENT = "%"

# conf key → (units the field accepts, canonical domain the engine uses)
ENTITY_UNIT_CONTRACTS: dict[str, tuple[frozenset, str]] = {
    # Grid CTs take power as well as current: a meter's current entity is
    # frequently magnitude-only, and only its signed power entity can show
    # export at all.
    CONF_PHASE_A_CURRENT_ENTITY_ID: (CURRENT_UNITS | POWER_UNITS, DOMAIN_AMPS),
    CONF_PHASE_B_CURRENT_ENTITY_ID: (CURRENT_UNITS | POWER_UNITS, DOMAIN_AMPS),
    CONF_PHASE_C_CURRENT_ENTITY_ID: (CURRENT_UNITS | POWER_UNITS, DOMAIN_AMPS),
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: (CURRENT_UNITS | POWER_UNITS, DOMAIN_AMPS),
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: (CURRENT_UNITS | POWER_UNITS, DOMAIN_AMPS),
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: (CURRENT_UNITS | POWER_UNITS, DOMAIN_AMPS),
    CONF_MAX_IMPORT_POWER_ENTITY_ID: (POWER_UNITS, DOMAIN_WATTS),
    CONF_SOLAR_PRODUCTION_ENTITY_ID: (POWER_UNITS, DOMAIN_WATTS),
    CONF_BATTERY_POWER_ENTITY_ID: (POWER_UNITS, DOMAIN_WATTS),
    CONF_BATTERY_SOC_ENTITY_ID: (SOC_UNITS, DOMAIN_PERCENT),
    CONF_BATTERY_VOLTAGE_ENTITY_ID: (VOLTAGE_UNITS, DOMAIN_VOLTS),
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID: (SOC_UNITS, DOMAIN_PERCENT),
}


# Every state string that means "this sensor has nothing usable to say".
#
# This is the UNION of every variant that used to be hand-rolled at the read
# sites: Home Assistant's own "unavailable"/"unknown", plus the empty string
# and a None state (a restored State, or one from an integration that never
# wrote a value, carries either). The strictest reading wins deliberately —
# each looser site was a bug by this one's standard, and the two that mattered
# most read a hard 0 A off a state this set rejects.
#
# Membership only ever grows here. Adding a variant at a call site is the drift
# this set exists to prevent (dev/tests/test_availability_contract.py).
UNAVAILABLE_STATES = frozenset({None, "", "unknown", "unavailable"})


def is_unavailable_state(value) -> bool:
    """True when a bare state STRING carries no usable reading.

    The string form exists because some statuses outlive the State object they
    came from: an OCPP connector status travels into the pure engine as a plain
    string on ``LoadContext.connector_status`` and is compared again in
    ``control/``. Same membership as :func:`is_unavailable` — one definition,
    two front doors.
    """
    return value in UNAVAILABLE_STATES


def state_or_unknown(state) -> str:
    """The state string, or a stand-in that reads as unavailable.

    For the sites that carry a status ONWARD as a plain string — a log line, a
    ``LoadContext.connector_status`` the pure engine will compare later. They
    must not invent a readable status for a sensor that has none, and they must
    not hand ``None`` to code expecting a string, so they get the same
    ``"unknown"`` Home Assistant itself would have used. It is a member of
    ``UNAVAILABLE_STATES``, so every predicate here agrees about the result.
    """
    if state is None:
        return "unknown"
    value = getattr(state, "state", None)
    return "unknown" if value in (None, "") else value


def is_unavailable(state) -> bool:
    """True when an HA state object carries no usable reading.

    The single definition of "is this sensor reading usable?". Covers a missing
    state object as well as an unusable state string, because every caller
    needs both answers and half of them used to forget one.

    ``state`` is duck-typed (only ``.state`` is read), so this stays HA-free.

    Deliberately says nothing about whether the reading is a NUMBER: plenty of
    inputs are legitimately non-numeric — an OCPP connector status, a switch's
    on/off, a climate hvac_action. :func:`is_unusable_number` answers that
    question instead, one step later, after the parse and conversion.
    """
    if state is None:
        return True
    return is_unavailable_state(getattr(state, "state", None))


def is_unusable_number(value) -> bool:
    """True when ``value`` cannot be used as a number.

    Two ways that happens: it is not a number at all (None, an unavailable
    sentinel, a leftover state string), or it is non-finite. NaN/Inf deserve
    their own check because ``float("nan")`` parses perfectly happily and NaN
    then poisons every comparison it touches silently — ``nan > limit`` and
    ``nan < limit`` are both False, so a NaN reading passes every safety test
    downstream instead of failing one.

    Applied after the parse and unit conversion (division by a small voltage
    can manufacture an Inf that the state string never contained), which is why
    it is a separate predicate from :func:`is_unavailable` rather than folded
    into it.
    """
    if not isinstance(value, (int, float)):
        return True
    return not math.isfinite(value)


def normalize(unit) -> str:
    """Upper-cased unit string; '' for a missing or non-string unit."""
    if not unit or not isinstance(unit, str):
        return ""
    return unit.strip().upper()


def to_amps(value: float, unit, voltage: float = 0.0) -> float:
    """Amps, from amps, milliamps, watts or kilowatts.

    Sign is preserved — on a grid CT it distinguishes import from export.
    An unrecognised (or absent) unit is assumed to be amps already, which is
    what a sensor with no unit_of_measurement almost always is; converting
    would be a guess, and passing it through matches the historical reading.

    ``voltage`` <= 0 leaves power values unconverted rather than dividing by
    zero — the caller's site voltage is missing, not the value.
    """
    u = normalize(unit)
    if u == "MA":
        return value / 1000.0
    if u == "KW":
        return value * 1000.0 / voltage if voltage > 0 else value
    if u == "W":
        return value / voltage if voltage > 0 else value
    return value


def to_watts(value: float, unit, voltage: float = 0.0) -> float:
    """Watts, from watts, kilowatts, amps or milliamps (sign preserved)."""
    u = normalize(unit)
    if u == "KW":
        return value * 1000.0
    if u == "A":
        return value * voltage if voltage > 0 else value
    if u == "MA":
        return value * voltage / 1000.0 if voltage > 0 else value
    return value


def to_volts(value: float, unit) -> float:
    """Volts, from volts or millivolts."""
    if normalize(unit) == "MV":
        return value / 1000.0
    return value
