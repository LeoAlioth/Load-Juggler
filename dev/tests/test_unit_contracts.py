"""Contract guards against the "read an entity, forget its unit" bug class.

Machine-authored tests — not yet human-reviewed.

test_units.py checks the converters. These two check that the converters are
actually *reached*, which is where the real bugs were:

1. Every unit the config flow accepts for a field must be declared in
   ENTITY_UNIT_CONTRACTS and must genuinely convert into that field's
   canonical domain. Adding a field that accepts millivolts without teaching
   units.py about millivolts fails here rather than in production.

2. No new hand-rolled ``float(state.state)`` parsing. Each one of those is a
   place where a unit can be forgotten — the grid-phase reader was exactly
   that, for months, while the config flow advertised "A or W, auto-converted".
   New occurrences must either go through units.py or be added to the
   allowlist below with a reason.

Both work by reading the source, so they cover code no runtime test touches.
"""

import re
from pathlib import Path

from custom_components.dynamic_ocpp_evse import const, units

_COMPONENT = Path(__file__).parents[2] / "custom_components" / "dynamic_ocpp_evse"

# Probe values: one physical quantity per domain, in every unit, so a
# conversion that silently passes the number through is caught.
_PROBES = {
    units.DOMAIN_AMPS: {"A": 6.0, "mA": 6000.0, "W": 1380.0, "kW": 1.38},
    units.DOMAIN_WATTS: {"W": 1380.0, "kW": 1.38, "A": 6.0, "mA": 6000.0},
    units.DOMAIN_VOLTS: {"V": 51.2, "mV": 51200.0},
    units.DOMAIN_PERCENT: {"%": 75.0},
}
_EXPECTED = {
    units.DOMAIN_AMPS: 6.0,
    units.DOMAIN_WATTS: 1380.0,
    units.DOMAIN_VOLTS: 51.2,
    units.DOMAIN_PERCENT: 75.0,
}
_VOLTAGE = 230.0


def _convert(value, unit, domain):
    if domain == units.DOMAIN_AMPS:
        return units.to_amps(value, unit, _VOLTAGE)
    if domain == units.DOMAIN_WATTS:
        return units.to_watts(value, unit, _VOLTAGE)
    if domain == units.DOMAIN_VOLTS:
        return units.to_volts(value, unit)
    return value  # percentages are unit-free by construction


def test_every_declared_unit_converts_to_its_canonical_domain():
    for conf_key, (accepted, domain) in units.ENTITY_UNIT_CONTRACTS.items():
        probes = _PROBES[domain]
        for unit in accepted:
            assert unit in probes, (
                f"{conf_key} accepts {unit!r} but this test has no probe for it "
                f"in domain {domain!r} — add one, and make sure units.py can "
                f"convert it"
            )
            got = _convert(probes[unit], unit, domain)
            assert abs(got - _EXPECTED[domain]) < 1e-6, (
                f"{conf_key}: {probes[unit]}{unit} converted to {got}, expected "
                f"{_EXPECTED[domain]}{domain} — units.py cannot handle a unit "
                f"the config flow accepts"
            )


def test_config_flow_unit_validation_matches_the_declared_contracts():
    """The per-step validation maps in config_flow.py must not accept a unit
    the contract table hasn't declared (and therefore nothing converts)."""
    source = (_COMPONENT / "config_flow.py").read_text()
    # Matches lines like "CONF_PHASE_A_CURRENT_ENTITY_ID: _CURRENT_UNITS" and
    # the "_CURRENT_UNITS | _POWER_UNITS" continuation form.
    pattern = re.compile(
        r"(CONF_[A-Z0-9_]+):\s*(_[A-Z_]+_UNITS(?:\s*\|\s*_[A-Z_]+_UNITS)*)"
    )
    sets = {
        "_CURRENT_UNITS": units.CURRENT_UNITS,
        "_POWER_UNITS": units.POWER_UNITS,
        "_SOC_UNITS": units.SOC_UNITS,
        "_VOLTAGE_UNITS": units.VOLTAGE_UNITS,
    }
    seen = 0
    for match in pattern.finditer(source):
        const_name, expr = match.groups()
        accepted = set()
        for name in re.findall(r"_[A-Z_]+_UNITS", expr):
            accepted |= set(sets[name])
        conf_value = getattr(const, const_name)
        assert conf_value in units.ENTITY_UNIT_CONTRACTS, (
            f"{const_name} is unit-validated in config_flow.py but missing from "
            f"units.ENTITY_UNIT_CONTRACTS — declare its canonical domain so the "
            f"conversion is covered"
        )
        declared, _domain = units.ENTITY_UNIT_CONTRACTS[conf_value]
        assert accepted <= set(declared), (
            f"{const_name} accepts {sorted(accepted - set(declared))} in the "
            f"config flow but those units are not declared in "
            f"ENTITY_UNIT_CONTRACTS"
        )
        seen += 1
    assert seen > 5, "unit-validation lines not found — did the pattern drift?"


# A ratchet, not an allowlist: exempting whole files would have exempted
# hub_calculation.py, which is exactly where the forgotten conversion lived.
# Counts may only go DOWN without editing this table.
_RAW_PARSE_BUDGET = {
    # Our own min/max-current number entities, in our own amps.
    "__init__.py": 1,
    # The battery discharge power hint in the hub_inverter form description —
    # only this detected preview text is unit-naive and never stored; the value
    # the user then types into the field is a real engine input, user-vetted.
    # And _entry_sensor_value on the Overview page, which reads back this
    # integration's OWN sensors (our units by construction) for display only
    # and is unit-agnostic on purpose (also passes through status strings).
    "config_flow.py": 2,
    # The offered-current read (amps by OCPP definition) and the
    # offered-power read, which converts through units.to_watts.
    "control/compliance.py": 2,
    # A shared reader: the charge-limit register read-back (same entity, same
    # unit as what we write, by construction) and the battery-voltage read,
    # which converts through units.to_volts right below the parse.
    "control/inverter.py": 1,
    # Same pattern for the station's charge-speed/reserve numbers.
    "control/power_station.py": 1,
    # _read_entity (the one converting reader) plus the EVSE current-import
    # total and power fallbacks (the latter via units.to_watts). Was 4: the
    # grid staleness check used to re-parse the raw state string itself, and
    # now reads the sentinel _read_grid_phases already resolved (ISSUES.md #31).
    "engine/hub_calculation.py": 3,
    # The station status sensor reading the power station's external battery
    # SOC and charge-limit entities — both percentages, so no unit conversion
    # applies.
    "entities/load_sensors.py": 1,
    # RestoreEntity state restoration of values we published ourselves. Was 2
    # (one copy per mixin); the hub and charger mixins now share the single
    # _apply_restored_number() reader, so the ratchet drops to 1.
    "entities/mixins.py": 1,
}


def test_no_new_hand_rolled_state_parsing():
    """Every ``float(x.state)`` is a place a unit can be forgotten.

    New ones must go through units.py. If a new raw parse is genuinely
    correct, lower some other count or add an entry here with the reason —
    the point is that it takes a deliberate edit, not silence.
    """
    over_budget = {}
    for path in sorted(_COMPONENT.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        key = str(path.relative_to(_COMPONENT))
        # Chain-aware on purpose: the likeliest new offender is
        # float(hass.states.get(x).state), which a pattern anchored on a plain
        # identifier walks straight past (it did, when this test was written).
        hits = re.findall(r"float\(.{0,80}?\.state\b", path.read_text())
        budget = _RAW_PARSE_BUDGET.get(key, 0)
        if len(hits) > budget:
            over_budget[key] = f"{len(hits)} found, budget {budget}"
    assert not over_budget, (
        f"new hand-rolled entity parsing: {over_budget} — read through units.py "
        f"(to_amps/to_watts/to_volts) so the sensor's unit is honoured, or "
        f"update _RAW_PARSE_BUDGET with the reason it is safe"
    )
