"""Unit handling for entity reads — the one place that knows A/mA/W/kW/V/mV.

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
"""

from __future__ import annotations

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
}


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
