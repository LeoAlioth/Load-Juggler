"""Contract guards for "is this sensor reading usable?" — ISSUES.md #31.

Machine-authored tests — not yet human-reviewed.

That question used to be hand-rolled at more than a dozen read sites with five
different answers, and they drifted: some forgot ``state.state is None``, some
forgot the empty string, one forgot both. The membership now lives once, in
``units.UNAVAILABLE_STATES``, reached through ``units.is_unavailable`` (state
objects), ``units.is_unavailable_state`` (a status carried onward as a bare
string) and ``units.state_or_unknown`` (the stand-in when there is no state
object at all). ``units.is_unusable_number`` answers the separate, later
question of whether a parsed reading can be used as a number.

The bug that motivated all of it was the grid CTs. ``_read_grid_phases`` coerced
an unavailable reading to **0 A** and trusted a second, independently
hand-rolled staleness test downstream to overwrite it. 0 A on a grid phase means
"the house is importing nothing", i.e. the whole main breaker is free — so any
divergence between those two tests, in either direction, silently granted full
breaker headroom on a blind site. The reader now propagates its sentinel and
``_resolve_grid_phases`` is the only thing allowed to substitute a value, which
is what the middle section here pins down.

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_availability_contract.py   (standalone, no pytest)
  pytest dev/tests/test_availability_contract.py    (Docker / CI tier)
"""

import ast
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules

load_pure_modules(engine_modules=("hub_calculation",))

from custom_components.dynamic_ocpp_evse import units
from custom_components.dynamic_ocpp_evse.const import (
    CONF_INVERT_PHASES,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    GRID_STALE_TIMEOUT,
)
from custom_components.dynamic_ocpp_evse.engine.readers import (
    _UNAVAILABLE,
    _read_entity,
    _read_grid_phases,
    _resolve_grid_phases,
    _track_grid_stale,
)

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "dynamic_ocpp_evse"

V = 230.0
BREAKER = 25.0


# ---------------------------------------------------------------------------
# Minimal HA doubles — only ``.state``/``.attributes`` and ``states.get`` are
# ever touched, which is exactly why units.py stays importable without HA.
# ---------------------------------------------------------------------------
class FakeState:
    def __init__(self, state, unit="A"):
        self.state = state
        self.attributes = {"unit_of_measurement": unit} if unit else {}


class FakeStates:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


class FakeHass:
    def __init__(self, mapping=None):
        self.states = FakeStates(mapping or {})


class FakeEntry:
    def __init__(self, data):
        self.entry_id = "hub"
        self.data = data
        self.options = {}


def _grid_entry(phase_a="sensor.a", phase_b=None, phase_c=None, invert=False):
    return FakeEntry(
        {
            CONF_PHASE_A_CURRENT_ENTITY_ID: phase_a,
            CONF_PHASE_B_CURRENT_ENTITY_ID: phase_b,
            CONF_PHASE_C_CURRENT_ENTITY_ID: phase_c,
            CONF_INVERT_PHASES: invert,
        }
    )


# ---------------------------------------------------------------------------
# The predicates
# ---------------------------------------------------------------------------

# Every variant that used to appear at some read site. The union is the whole
# point: each of these was rejected by at least one site and accepted by
# another, and the accepting site was the bug.
UNUSABLE_STATE_STRINGS = (None, "", "unknown", "unavailable")


def test_is_unavailable_covers_the_union_of_every_old_variant():
    for value in UNUSABLE_STATE_STRINGS:
        assert units.is_unavailable(FakeState(value)), value
        assert units.is_unavailable_state(value), value


def test_is_unavailable_accepts_a_real_reading():
    for value in ("0", "0.0", "-3.5", "Charging", "on", "Available"):
        assert not units.is_unavailable(FakeState(value)), value
        assert not units.is_unavailable_state(value), value


def test_is_unavailable_treats_a_missing_state_object_as_unavailable():
    # The variant two sites forgot: hass.states.get() returns None for an
    # entity that was renamed, removed, or has not been created yet.
    assert units.is_unavailable(None)


def test_is_unavailable_duck_types_the_state_object():
    class NoStateAttribute:
        pass

    assert units.is_unavailable(NoStateAttribute())


def test_is_unavailable_says_nothing_about_being_numeric():
    # Status-string sensors are legitimate inputs: a connector status, a
    # switch's on/off. Folding a float parse into is_unavailable would break
    # every one of them.
    assert not units.is_unavailable(FakeState("SuspendedEV"))


def test_is_unusable_number_rejects_non_numbers_and_non_finite():
    for value in (None, "6", "unavailable", _UNAVAILABLE, object(), [1.0]):
        assert units.is_unusable_number(value), value
    for value in (float("nan"), float("inf"), float("-inf")):
        assert units.is_unusable_number(value), value


def test_is_unusable_number_accepts_every_real_reading_including_zero():
    for value in (0, 0.0, -0.0, 6, 6.5, -3.2, 1e9):
        assert not units.is_unusable_number(value), value


def test_state_or_unknown_never_invents_a_readable_status():
    for value in (None, ""):
        assert units.state_or_unknown(FakeState(value)) == "unknown"
    assert units.state_or_unknown(None) == "unknown"
    # And whatever it returns must read as unavailable to the predicates, so a
    # status carried onward as a bare string cannot look like a plugged-in car.
    assert units.is_unavailable_state(units.state_or_unknown(None))
    # A real status passes through untouched.
    assert units.state_or_unknown(FakeState("Charging")) == "Charging"


# ---------------------------------------------------------------------------
# _read_entity: the single converting reader
# ---------------------------------------------------------------------------


def test_read_entity_maps_every_unusable_state_to_the_sentinel():
    for value in UNUSABLE_STATE_STRINGS:
        hass = FakeHass({"sensor.x": FakeState(value)})
        assert _read_entity(hass, "sensor.x", 0, unit="A", voltage=V) is _UNAVAILABLE, value
    # No state object at all.
    assert _read_entity(FakeHass(), "sensor.x", 0, unit="A", voltage=V) is _UNAVAILABLE
    # A state that is not a number ("--" is a common placeholder).
    hass = FakeHass({"sensor.x": FakeState("--")})
    assert _read_entity(hass, "sensor.x", 0, unit="A", voltage=V) is _UNAVAILABLE


def test_read_entity_maps_a_non_finite_reading_to_the_sentinel():
    # float("nan") parses perfectly happily, and NaN then makes every safety
    # comparison downstream False. Only the grid block used to catch this; now
    # the sentinel makes it engage the holdover and the stale timeout instead.
    for value in ("nan", "inf", "-inf"):
        hass = FakeHass({"sensor.x": FakeState(value)})
        assert _read_entity(hass, "sensor.x", 0, unit="A", voltage=V) is _UNAVAILABLE, value


def test_read_entity_still_returns_the_default_when_unconfigured():
    # "not configured" and "configured but broken" must stay distinguishable.
    assert _read_entity(FakeHass(), None, default=7) == 7


def test_read_entity_converts_and_keeps_zero_usable():
    hass = FakeHass({"sensor.x": FakeState("1380", unit="W")})
    assert abs(_read_entity(hass, "sensor.x", 0, unit="A", voltage=V) - 6.0) < 1e-9
    hass = FakeHass({"sensor.x": FakeState("0", unit="A")})
    assert _read_entity(hass, "sensor.x", 0, unit="A", voltage=V) == 0.0


# ---------------------------------------------------------------------------
# _read_grid_phases: the landmine
# ---------------------------------------------------------------------------


def test_grid_phases_never_read_zero_amps_for_an_unreadable_ct():
    """The regression this whole issue is about.

    0 A on a grid phase says "importing nothing", which grants the entire main
    breaker as headroom. The reader must refuse to invent it, for every flavour
    of unreadable, so the holdover is what decides.
    """
    entry = _grid_entry()
    for value in UNUSABLE_STATE_STRINGS + ("--", "nan"):
        hass = FakeHass({"sensor.a": FakeState(value)})
        phases = _read_grid_phases(hass, entry, V)
        assert phases[0] is _UNAVAILABLE, value
        assert phases[0] != 0, value
    # Entity configured but no state object at all.
    phases = _read_grid_phases(FakeHass(), entry, V)
    assert phases[0] is _UNAVAILABLE


def test_grid_phases_keep_none_for_an_unconfigured_phase():
    # None and the sentinel must stay distinct: None is "no CT here" (used for
    # the phase count and the off-grid inference), the sentinel is "CT broken".
    hass = FakeHass({"sensor.a": FakeState("5.0")})
    phases = _read_grid_phases(hass, _grid_entry(), V)
    assert phases[0] == 5.0
    assert phases[1] is None and phases[2] is None


def test_grid_phases_read_signed_amps_and_convert_units():
    hass = FakeHass(
        {
            "sensor.a": FakeState("5.0", unit="A"),
            "sensor.b": FakeState("-1380", unit="W"),
        }
    )
    phases = _read_grid_phases(hass, _grid_entry(phase_b="sensor.b"), V)
    assert phases[0] == 5.0
    assert abs(phases[1] - -6.0) < 1e-9  # export keeps its sign


def test_grid_phases_inversion_cannot_turn_the_sentinel_into_a_number():
    hass = FakeHass(
        {"sensor.a": FakeState("5.0"), "sensor.b": FakeState("unavailable")}
    )
    phases = _read_grid_phases(
        hass, _grid_entry(phase_b="sensor.b", invert=True), V
    )
    assert phases[0] == -5.0
    assert phases[1] is _UNAVAILABLE


# ---------------------------------------------------------------------------
# _resolve_grid_phases: the documented failure-mode behaviour
# ---------------------------------------------------------------------------


def test_resolve_leaves_healthy_readings_alone():
    resolved, stale, assumed = _resolve_grid_phases([5.0, -2.0, None], {}, BREAKER)
    assert resolved == [5.0, -2.0, None]
    assert stale is False
    assert assumed == (False, False, False)


def test_resolve_holds_the_last_ema_during_a_brief_dropout():
    # Failure mode 1: the CT blinks. Holding the last known EMA means a brief
    # dropout has no visible effect at all.
    ema = {"grid_0": 7.5, "grid_1": -1.5}
    resolved, stale, assumed = _resolve_grid_phases(
        [_UNAVAILABLE, _UNAVAILABLE, None], ema, BREAKER
    )
    assert resolved == [7.5, -1.5, None]
    assert stale is True
    # A held value is an estimate, NOT the breaker fabrication — so it stays
    # publishable as a measurement and nothing is flagged.
    assert assumed == (False, False, False)


def test_resolve_assumes_the_breaker_on_a_cold_start():
    # Failure mode 2: unavailable from the very first cycle, no EMA history.
    # Worst case on purpose — a fully loaded phase hands out no headroom, where
    # the old 0 A fallback handed out all of it.
    resolved, stale, assumed = _resolve_grid_phases(
        [_UNAVAILABLE, None, None], {}, BREAKER
    )
    assert resolved == [BREAKER, None, None]
    assert stale is True
    assert resolved[0] != 0
    # ...and the substitution is reported, because the number is a safety
    # fabrication and must not be published as a grid measurement.
    assert assumed == (True, False, False)


def test_resolve_is_per_phase():
    # One dead CT must not discard the two good ones, and each dead phase picks
    # its own substitute from its own history.
    ema = {"grid_0": 9.0}
    resolved, stale, assumed = _resolve_grid_phases(
        [_UNAVAILABLE, 3.0, _UNAVAILABLE], ema, BREAKER
    )
    assert resolved == [9.0, 3.0, BREAKER]
    assert stale is True
    # Held, read, assumed — the flag distinguishes all three per phase.
    assert assumed == (False, False, True)


def test_resolve_holds_a_zero_ema_because_zero_can_be_a_real_reading():
    # 0 A is a legitimate measurement (a balanced phase). "Held 0" and
    # "invented 0" are different things, and only the second one is the bug.
    resolved, stale, assumed = _resolve_grid_phases(
        [_UNAVAILABLE], {"grid_0": 0.0}, BREAKER
    )
    assert resolved == [0.0]
    assert stale is True
    assert assumed == (False,)


def test_resolve_never_lets_anything_unusable_reach_the_engine():
    """The structural property that replaces the old two-tests-must-agree hope.

    Whatever a phase reader hands over, every entry coming out of here is a
    usable float or the None that means "no CT on this phase".
    """
    junk = [_UNAVAILABLE, float("nan"), float("inf"), None, "unavailable", 4.0]
    for value in junk:
        for ema in ({}, {"grid_0": 8.0}):
            resolved, _, _ = _resolve_grid_phases([value], dict(ema), BREAKER)
            assert resolved[0] is None or not units.is_unusable_number(resolved[0]), (
                value,
                ema,
            )


def test_resolve_does_not_mutate_its_input():
    raw = [_UNAVAILABLE, None, None]
    _resolve_grid_phases(raw, {}, BREAKER)
    assert raw == [_UNAVAILABLE, None, None]


def test_resolve_flags_a_phase_only_when_it_invented_the_breaker_value():
    """The publisher's signal has to mean exactly one thing.

    Flagged if and only if this phase had no usable reading AND no EMA
    history — i.e. the resolved value is the invented main-breaker worst case,
    which is the one substitute that must not reach the published grid
    measurements. Never flagged for a real reading, for an absent CT, or for a
    held EMA value (a held value is an estimate of what the phase was doing
    moments ago, and blanking the grid sensors through every brief dropout
    would be its own bug).
    """
    raws = [_UNAVAILABLE, 3.0, None, float("nan")]
    for ema in ({}, {"grid_0": 8.0}, {"grid_3": -1.0}):
        resolved, _, assumed = _resolve_grid_phases(raws, dict(ema), BREAKER)
        assert len(assumed) == len(raws)
        for i, (value, flag) in enumerate(zip(resolved, assumed)):
            invented = (
                raws[i] is not None
                and units.is_unusable_number(raws[i])
                and f"grid_{i}" not in ema
            )
            assert flag is invented, (i, ema)
            if flag:
                assert value == BREAKER


# ---------------------------------------------------------------------------
# _track_grid_stale: the >GRID_STALE_TIMEOUT escalation
# ---------------------------------------------------------------------------


def test_stale_timer_reports_nothing_while_the_cts_are_healthy():
    runtime = {}
    assert _track_grid_stale(runtime, False, 1000.0) == 0
    assert "grid_stale_since" not in runtime


def test_stale_timer_grows_across_an_unbroken_outage():
    runtime = {}
    assert _track_grid_stale(runtime, True, 1000.0) == 0  # first stale cycle
    assert _track_grid_stale(runtime, True, 1010.0) == 10.0
    # A brief dropout must NOT trip the escalation — that is the whole point of
    # holding the EMA rather than falling straight to minimum current.
    assert 10.0 <= GRID_STALE_TIMEOUT
    assert _track_grid_stale(runtime, True, 1010.0) <= GRID_STALE_TIMEOUT


def test_stale_timer_crosses_the_timeout_and_forces_the_fallback():
    runtime = {}
    _track_grid_stale(runtime, True, 1000.0)
    duration = _track_grid_stale(runtime, True, 1000.0 + GRID_STALE_TIMEOUT + 1)
    # run_hub_calculation tests `grid_stale_duration > GRID_STALE_TIMEOUT` to
    # drop charging EVSEs to min_current and shed binary loads.
    assert duration > GRID_STALE_TIMEOUT


def test_stale_timer_restarts_after_a_single_healthy_cycle():
    runtime = {}
    _track_grid_stale(runtime, True, 1000.0)
    assert _track_grid_stale(runtime, True, 1000.0 + GRID_STALE_TIMEOUT + 5) > GRID_STALE_TIMEOUT
    # Recovery clears the timer...
    assert _track_grid_stale(runtime, False, 1100.0) == 0
    assert "grid_stale_since" not in runtime
    # ...so a later outage is measured from scratch, not from the old start.
    assert _track_grid_stale(runtime, True, 1101.0) == 0


# ---------------------------------------------------------------------------
# The drift ratchet
# ---------------------------------------------------------------------------

# ``units.py`` owns the definition, so it is the one file allowed to name these
# strings. Everywhere else, a literal "unknown"/"unavailable" in CODE means
# someone is hand-rolling the question again — which is how five different
# answers to it grew in the first place.
#
# Counts may only go DOWN without editing this table. Exempting whole files is
# deliberately not offered: that would have exempted hub_calculation.py, which
# is where the dangerous copy lived.
_UNAVAILABLE_LITERAL_BUDGET = {
    # Two display/vocabulary uses, neither an entity state — one per module
    # since config_flow became a package (the total is unchanged):
    #   errors["base"] = "unknown" is HA's translation key for "unexpected
    #   exception" in a config flow;
    "config_flow/flow.py": 1,
    #   f"- Status: {status or 'unknown'}" on the load Overview page falls back
    #   for OUR OWN computed charging-status string (hass.data load_status),
    #   which no sensor ever publishes — the sibling line in the one-line
    #   summary spells the same fallback "status unknown".
    "config_flow/pages.py": 1,
    # A log-line placeholder for a register we could not read back — formatting
    # only, never compared against anything.
    "control/inverter.py": 1,
}

# The literals whose every code-level use has to be justified. "" is left out:
# it is far too common a string to ratchet on, and is_unavailable is the only
# thing that treats it as a state.
_RATCHETED_LITERALS = frozenset({"unknown", "unavailable"})


def _docstring_node_ids(tree):
    """ids of the string nodes that are docstrings (prose, not code)."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    ids.add(id(first.value))
    return ids


def _unavailable_literals_in_code(path):
    """Every code-level occurrence of a ratcheted state literal, as line numbers.

    Parsed rather than grepped, so comments and docstrings — where these strings
    legitimately appear all over the place, including in this test's own prose —
    cost nothing, and no membership shape can hide from it: ``in (...)``,
    ``not in [...]``, ``== "unavailable"``, a bare ``else "unknown"`` default
    and a dict lookup all reduce to the same string constant in the AST.
    """
    tree = ast.parse(path.read_text())
    skip = _docstring_node_ids(tree)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in _RATCHETED_LITERALS
        and id(node) not in skip
    ]


def test_the_unavailable_membership_lives_only_in_units_py():
    over_budget = {}
    for path in sorted(COMPONENT.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        key = str(path.relative_to(COMPONENT))
        if key == "units.py":
            continue  # the definition itself
        hits = _unavailable_literals_in_code(path)
        budget = _UNAVAILABLE_LITERAL_BUDGET.get(key, 0)
        if len(hits) > budget:
            over_budget[key] = f"{len(hits)} found on line(s) {hits}, budget {budget}"
    assert not over_budget, (
        f"hand-rolled unavailable-state handling: {over_budget} — use "
        f"units.is_unavailable(state) for a state object, "
        f"units.is_unavailable_state(s) for a status already reduced to a "
        f"string, or units.state_or_unknown(state) for the stand-in when there "
        f"is no state object; add to _UNAVAILABLE_LITERAL_BUDGET with a reason "
        f"only if the literal genuinely is not an entity state"
    )


def test_the_definition_is_the_union_and_only_grows():
    # Pinned so shrinking the set is a deliberate, visible edit: dropping a
    # member re-opens exactly the bug each looser site used to have.
    assert set(units.UNAVAILABLE_STATES) >= {None, "", "unknown", "unavailable"}


def test_no_second_definition_of_the_membership_set():
    # A file could dodge the AST ratchet by building its own set from
    # HA's STATE_* constants. There is only ever one set.
    pattern = re.compile(r"STATE_(UNKNOWN|UNAVAILABLE)\b")
    offenders = [
        str(path.relative_to(COMPONENT))
        for path in sorted(COMPONENT.rglob("*.py"))
        if "__pycache__" not in str(path) and pattern.search(path.read_text())
    ]
    assert not offenders, (
        f"{offenders} build their own unavailable-state membership from HA's "
        f"STATE_* constants — route through units.UNAVAILABLE_STATES instead"
    )


def test_grid_phase_reader_does_not_coerce_the_sentinel_away():
    """Source-level guard on the fix, not just on today's behaviour.

    ``_coerce`` inside ``_read_grid_phases`` is precisely the landmine: it turns
    the sentinel into 0 A, and 0 A on a grid phase is full breaker headroom. The
    behavioural tests above would catch it too, but this names the mistake.
    """
    source = (COMPONENT / "engine" / "readers.py").read_text()
    tree = ast.parse(source)
    reader = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_read_grid_phases"
    )
    calls = [
        node.func.id
        for node in ast.walk(reader)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "_coerce" not in calls, (
        "_read_grid_phases must propagate _UNAVAILABLE — coercing it to a "
        "default here reads as 0 A of grid import, i.e. the whole main breaker "
        "free, and leaves the stale holdover with nothing to detect"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Deliberately pytest-free: the pure tier has to run on the developer's
    # machine, which has no pytest (dev/tests/conftest.py imports HA anyway).
    failed = []
    for _name, _fn in sorted(list(globals().items())):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed.append((_name, exc))
            print(f"FAIL {_name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {_name}")
    print(f"\n{'FAILED' if failed else 'OK'} — {len(failed)} failure(s)")
    sys.exit(1 if failed else 0)
