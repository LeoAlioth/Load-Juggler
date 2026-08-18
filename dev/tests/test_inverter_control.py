"""Tests for the inverter battery charge write-control — control.inverter.

Machine-authored tests — not yet human-reviewed.

The forecast's recommended charge limit only reaches the inverter when the user
armed the control switch, the value moved by more than the deadband, and the
write interval has elapsed. Releasing puts the normal value back exactly once.

**Who calls this** changed and these tests did not have to: the control used to
be ticked by the charge-control sensor's 10 s platform poll, and is now awaited
once per site cycle (default 2 s) as a site-cycle worker — see
``entities/mixins.SiteCycleWorkerMixin``. The pacing is wall-clock, so the two
cadences are the same contract; the section at the bottom drives the control at
both of them and pins that. The entity-level half of the change (registration
with the hub's worker bucket, the write happening through the real coordinator
cycle, and ``async_update`` no longer writing) lives in ``test_sensor_update.py``
where the HA fixtures are, with source-level guards for it here.

These use a hand-rolled fake hass rather than the HA fixtures, so the file runs
in the pure tier too. Runnable two ways:
  python3 dev/tests/test_inverter_control.py   (standalone, no pytest needed)
  pytest dev/tests/test_inverter_control.py    (Docker / CI tier)
"""

import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules  # noqa: E402

# control/inverter.py imports nothing but const/helpers/units — the actuation
# layer's own rule — so it loads without Home Assistant installed.
load_pure_modules(calc_modules=(), control_modules=("inverter",))

from custom_components.dynamic_ocpp_evse.const import (  # noqa: E402
    DOMAIN,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_CHARGE_LIMIT_NORMAL,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_CHARGE_CONTROL_DEADBAND,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    INVERTER_RT_APPLIED,
    INVERTER_RT_CONTROL_ENABLED,
    INVERTER_RT_STATUS,
)
from custom_components.dynamic_ocpp_evse.control.inverter import (  # noqa: E402
    battery_voltage,
    resolve_normal_value,
    send_inverter_charge_limit,
    should_write,
    to_target_units,
)

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "dynamic_ocpp_evse"

TARGET = "number.deye_max_charge_current"


class _States:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)

    def set(self, entity_id, state, **attributes):
        self._states[entity_id] = SimpleNamespace(
            state=str(state), attributes=attributes
        )


class _Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))


class _Hass:
    def __init__(self, states=None):
        self.states = _States(states or {})
        self.services = _Services()
        self.data = {DOMAIN: {"inverters": {}}}


def _entry(options=None):
    return SimpleNamespace(
        entry_id="inv1",
        title="Deye Hybrid",
        data={},
        options={CONF_CHARGE_LIMIT_ENTITY_ID: TARGET, **(options or {})},
    )


def _hass_with_target(current=50.0, maximum=100.0, **extra_states):
    hass = _Hass()
    hass.states.set(TARGET, current, max=maximum)
    for entity_id, (state, attrs) in extra_states.items():
        hass.states.set(entity_id, state, **attrs)
    return hass


def _arm(hass, entry, enabled=True):
    hass.data[DOMAIN]["inverters"][entry.entry_id] = {
        INVERTER_RT_CONTROL_ENABLED: enabled
    }
    return hass.data[DOMAIN]["inverters"][entry.entry_id]


# --- Conversion and helpers ---------------------------------------------------


def test_watts_to_amps_uses_battery_voltage():
    assert to_target_units(5120.0, "A", 51.2) == 100.0


def test_watts_passthrough_when_target_takes_watts():
    assert to_target_units(5120.0, "W", 51.2) == 5120.0


def test_battery_voltage_prefers_live_sensor():
    hass = _Hass()
    hass.states.set("sensor.batt_v", 53.4, unit_of_measurement="V")
    entry = _entry({
        CONF_BATTERY_VOLTAGE_ENTITY_ID: "sensor.batt_v",
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
    })
    assert battery_voltage(hass, entry) == 53.4


def test_battery_voltage_falls_back_to_nominal():
    hass = _Hass()
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 25.6})
    assert battery_voltage(hass, entry) == 25.6


def test_normal_value_defaults_to_target_entity_max():
    hass = _hass_with_target(maximum=120.0)
    assert resolve_normal_value(hass, _entry(), TARGET) == 120.0


def test_configured_normal_value_wins():
    hass = _hass_with_target(maximum=120.0)
    entry = _entry({CONF_CHARGE_LIMIT_NORMAL: 90})
    assert resolve_normal_value(hass, entry, TARGET) == 90.0


# --- Deadband -----------------------------------------------------------------


def test_deadband_blocks_a_small_move():
    assert should_write(current=50.0, desired=52.0, previous_applied=None, deadband=5) is False


def test_deadband_allows_a_real_move():
    assert should_write(current=50.0, desired=44.0, previous_applied=None, deadband=5) is True


def test_unreadable_register_falls_back_to_what_we_applied():
    # The register cannot be read back; compare against our own last write.
    assert should_write(None, 50.0, 50.0, 5) is False
    assert should_write(None, 40.0, 50.0, 5) is True


def test_never_written_and_unreadable_writes():
    assert should_write(None, 40.0, None, 5) is True


# --- The write path -----------------------------------------------------------


def test_nothing_written_while_the_switch_is_off():
    hass = _hass_with_target()
    entry = _entry()
    rt = _arm(hass, entry, enabled=False)

    asyncio.run(send_inverter_charge_limit(hass, entry, 3000.0, 0.0))

    assert hass.services.calls == []
    assert rt[INVERTER_RT_STATUS] == "Off"


def test_armed_control_writes_the_converted_limit():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 51.2})
    rt = _arm(hass, entry)

    # 2560 W at 51.2 V = 50 A, half the register's current 100 A
    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))

    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": TARGET, "value": 50.0})
    ]
    assert rt[INVERTER_RT_APPLIED] == 50.0
    assert rt[INVERTER_RT_STATUS] == "Limiting to 50.0A"


def test_second_cycle_inside_the_interval_does_not_write():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 300,
    })
    _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 1000.0))
    hass.states.set(TARGET, 50.0, max=100.0)
    # A much lower advice, but only 60 s later
    asyncio.run(send_inverter_charge_limit(hass, entry, 1024.0, 1060.0))

    assert len(hass.services.calls) == 1


def test_write_resumes_after_the_interval():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 300,
    })
    _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 1000.0))
    hass.states.set(TARGET, 50.0, max=100.0)
    asyncio.run(send_inverter_charge_limit(hass, entry, 1024.0, 1400.0))

    assert len(hass.services.calls) == 2
    assert hass.services.calls[-1][2]["value"] == 20.0


def test_release_restores_the_normal_value_once():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 0,
    })
    rt = _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))
    hass.states.set(TARGET, 50.0, max=100.0)
    # Forecast has nothing to say any more (evening, or no clipping ahead)
    asyncio.run(send_inverter_charge_limit(hass, entry, None, 10.0))
    hass.states.set(TARGET, 100.0, max=100.0)
    asyncio.run(send_inverter_charge_limit(hass, entry, None, 20.0))

    assert len(hass.services.calls) == 2  # one limit, one restore
    assert hass.services.calls[-1][2]["value"] == 100.0
    assert rt[INVERTER_RT_APPLIED] is None
    assert rt[INVERTER_RT_STATUS] == "Not limiting"


def test_release_without_a_prior_write_leaves_the_register_alone():
    """A register we never touched is the user's — never 'restore' it."""
    hass = _hass_with_target(current=42.0, maximum=100.0)
    entry = _entry()
    rt = _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, None, 0.0))

    assert hass.services.calls == []
    assert rt[INVERTER_RT_STATUS] == "Not limiting"


def test_disarming_restores_the_normal_value():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 0,
    })
    rt = _arm(hass, entry)
    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))

    rt[INVERTER_RT_CONTROL_ENABLED] = False
    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 10.0))

    assert hass.services.calls[-1][2]["value"] == 100.0
    assert rt[INVERTER_RT_APPLIED] is None


def test_no_target_entity_is_a_no_op():
    hass = _Hass()
    entry = SimpleNamespace(entry_id="inv1", title="Advisory", data={}, options={})

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))

    assert hass.services.calls == []


def test_deadband_is_a_percentage_of_the_normal_value():
    """5 % of a 100 A normal is 5 A — a 2 A move is not worth a Modbus write."""
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 0,
        CONF_CHARGE_CONTROL_DEADBAND: 5,
    })
    _arm(hass, entry)

    # 5017.6 W = 98 A, 2 A below the register's 100 A
    asyncio.run(send_inverter_charge_limit(hass, entry, 5017.6, 0.0))
    assert hass.services.calls == []

    # 4608 W = 90 A, a 10 A move
    asyncio.run(send_inverter_charge_limit(hass, entry, 4608.0, 10.0))
    assert len(hass.services.calls) == 1


# --- Cadence independence -----------------------------------------------------
#
# The check moved from a 10 s platform poll to the site cycle, whose default is
# 2 s — five times as many checks. What must NOT change is how often the
# register is actually written, because that is what wears EEPROM. The pacing is
# measured in wall-clock seconds (``now_mono``), never in cycles, and these
# drive the same hour at several cadences to hold it to that.

WRITE_INTERVAL = 300.0
HOUR = 3600.0


def _write_times(cadence_s, duration_s, interval=WRITE_INTERVAL, advice_w=2560.0,
                 enabled=True):
    """Drive the control every ``cadence_s`` for ``duration_s`` of wall clock.

    Returns the times at which a register write actually happened. The fake
    register never moves (nothing applies the write), so the deadband always
    passes and the interval is the only thing pacing the writes — exactly the
    worst case for a fast cadence.
    """
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: interval,
    })
    _arm(hass, entry, enabled=enabled)

    times = []
    seen = 0
    now = 0.0
    while now <= duration_s:
        asyncio.run(send_inverter_charge_limit(hass, entry, advice_w, now))
        if len(hass.services.calls) > seen:
            seen = len(hass.services.calls)
            times.append(now)
        now += cadence_s
    return times


def test_a_five_times_faster_cadence_writes_exactly_as_often():
    """The whole cadence question in one assertion: 1800 checks an hour and 360
    checks an hour produce the same writes, at the same times."""
    assert _write_times(2.0, HOUR) == _write_times(10.0, HOUR)


def test_no_two_writes_are_ever_closer_than_the_interval():
    """For any cadence, including ones that do not divide the interval."""
    for cadence in (0.5, 2.0, 7.0, 10.0, 30.0):
        times = _write_times(cadence, HOUR)
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert gaps, cadence
        assert min(gaps) >= WRITE_INTERVAL, (cadence, gaps)
        # An hour at one write per 300 s: 13 with the first at t=0, one fewer
        # when the cadence's phase pushes the last one past the hour.
        assert 12 <= len(times) <= 13, (cadence, times)


def test_a_whole_interval_of_checks_produces_one_write():
    # 150 cycles at the 2 s default, all inside the first 300 s window.
    assert _write_times(2.0, WRITE_INTERVAL - 2) == [0.0]


def test_the_switch_gate_holds_for_every_cycle_of_an_hour():
    """The opt-in is checked per call, so a faster cadence cannot leak a write."""
    assert _write_times(2.0, HOUR, enabled=False) == []


# --- Source-level guards on the drive mechanism -------------------------------
#
# The entity half of this lives in test_sensor_update.py (it needs a real HA
# entity). These pin the three structural decisions in a tier that runs
# anywhere: nothing polls, only the cycle worker writes, and the worker runs
# after the result it consumes has been published.


def _parse(*parts):
    return ast.parse((COMPONENT.joinpath(*parts)).read_text())


def _assigned_names(tree):
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def _callee_name(node):
    """The bare name of a call's callee: ``f()`` and ``obj.f()`` both give "f"."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _functions_calling(tree, callee):
    """Names of the functions in ``tree`` that call ``callee``."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(inner, ast.Call) and _callee_name(inner) == callee
            for inner in ast.walk(node)
        ):
            hits.append(node.name)
    return hits


def _method(tree, class_name, method_name):
    cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )


def test_the_sensor_platform_declares_no_scan_interval():
    """SCAN_INTERVAL existed for exactly one polling sensor. Re-adding it would
    put every sensor on this platform back on a second, unrelated clock."""
    assert "SCAN_INTERVAL" not in _assigned_names(_parse("sensor.py"))


def test_the_charge_control_sensor_is_a_site_cycle_worker():
    """And the mixin comes before SensorEntity, so its ``_attr_should_poll =
    False`` wins the MRO over the entity base's default of True."""
    tree = _parse("entities", "inverter.py")
    cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name == "LoadJugglerInverterChargeControlSensor"
    )
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "SiteCycleWorkerMixin" in bases, bases
    assert bases.index("SiteCycleWorkerMixin") < bases.index("SensorEntity"), bases


def test_the_worker_mixin_turns_polling_off():
    tree = _parse("entities", "mixins.py")
    cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "SiteCycleWorkerMixin"
    )
    polls = [
        node.value.value
        for node in cls.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_attr_should_poll"
            for t in node.targets
        )
    ]
    assert polls == [False], polls


def test_async_update_refreshes_the_state_and_awaits_nothing():
    """``homeassistant.update_entity`` is a service any automation can call at
    any rate. If it wrote, it would be a second writer on the register — able to
    overlap the cycle's own write and to spend the min-interval budget outside
    the one place that owns it. So it awaits nothing at all."""
    method = _method(
        _parse("entities", "inverter.py"),
        "LoadJugglerInverterChargeControlSensor",
        "async_update",
    )
    awaits = [node for node in ast.walk(method) if isinstance(node, ast.Await)]
    assert awaits == [], [node.lineno for node in awaits]
    assert _callee_name(
        next(node for node in ast.walk(method) if isinstance(node, ast.Call))
    ) == "_read_control_status"


def test_only_the_site_cycle_worker_writes_the_register():
    """One writer, and it is the one the coordinator serializes."""
    callers = {}
    for path in sorted(COMPONENT.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        names = _functions_calling(ast.parse(path.read_text()),
                                   "send_inverter_charge_limit")
        if names:
            callers[str(path.relative_to(COMPONENT))] = names
    assert callers == {"entities/inverter.py": ["_async_site_cycle_work"]}, callers


def test_workers_are_awaited_after_the_result_is_published():
    """The ordering the write depends on: it consumes
    ``published["inverters"][…]["forecast_charge_limit_w"]``, which does not
    exist until publish_hub_data has stored it."""
    fn = next(
        node
        for node in ast.walk(_parse("sensor.py"))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_run_hub_cycle"
    )
    lines = {
        callee: [
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and _callee_name(node) == callee
        ]
        for callee in ("publish_hub_data", "async_run_site_cycle")
    }
    assert len(lines["publish_hub_data"]) == 1, lines
    assert len(lines["async_run_site_cycle"]) == 1, lines
    assert lines["publish_hub_data"][0] < lines["async_run_site_cycle"][0], lines


# --- Runner -------------------------------------------------------------------
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
