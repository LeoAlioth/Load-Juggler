"""Per-device command modules (OCPP, smart plug, hot water tank) and
load-update logic (compliance, smoothing, status)."""

import time
from collections import deque
from datetime import datetime, timezone

from homeassistant.core import Context

from .. import units
from ..const import MANUAL_ECHO_S


def stamp_command(sensor, now_mono):
    """The load's command went out (or was settled) this cycle: stamp it on
    the wall clock its last_update attribute reports and on the monotonic one
    the command-interval gate measures from."""
    sensor._last_update = datetime.now(timezone.utc)
    sensor._last_command_time = now_mono


async def call_device(hass, load_rt, domain, service, data, field, value, blocking=False):
    """A service call to a load's device, made under a Context of our own and
    remembered with what it asks for - ``field`` is where the device's state
    shows it ("state", or an attribute) - so manual_change can tell Load
    Juggler's changes from someone else's."""
    context = Context()
    load_rt.setdefault("_lj_contexts", deque(maxlen=10)).append(context.id)
    asked = load_rt.setdefault("_commanded", {}).setdefault((data["entity_id"], field), {})
    asked.pop(value, None)
    asked[value] = time.monotonic()  # the newest last: the command in force
    await hass.services.async_call(domain, service, data, blocking=blocking, context=context)


def _field(state, field):
    return state.state if field == "state" else state.attributes.get(field)


def _same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 0.05
    except (TypeError, ValueError):
        return a == b


def manual_change(load_rt, old, new):
    """What changed on a load's device that Load Juggler did not ask for, as
    "field: old -> new", or None.

    Manual is a change of a field Load Juggler commands, to a value it has not
    asked for (now, or within MANUAL_ECHO_S - a slow device's late report of
    the command before), under a context that is not one of our calls. Not a
    report with nothing in that field, nor a start-up or a return from
    unavailable/unknown: there is no device action in those. A target set
    lower than asked counts only when an HA user set it - a device that keeps
    a lower target than the one asked of it (judge_kept_target, a MELCloud
    poll) reads exactly like a hand on its dial.
    """
    if units.is_unavailable(old) or units.is_unavailable(new):
        return None
    if new.context.id in load_rt.get("_lj_contexts", ()):
        return None
    now = time.monotonic()
    for (entity_id, field), asked in load_rt.get("_commanded", {}).items():
        value, before = _field(new, field), _field(old, field)
        if entity_id != new.entity_id or value is None or _same(value, before):
            continue
        last = next(reversed(asked))
        if _same(value, last) or any(
            _same(value, v) and now - t < MANUAL_ECHO_S for v, t in asked.items()
        ):
            continue
        if field == "temperature" and float(value) < float(last) and new.context.user_id is None:
            continue
        return f"{field}: {before} -> {value}"
    return None
