"""SG Ready heat pump actuation (JUG-24): switch the two relays to the state the
state machine (calculations/sg_ready.py) chose, one relay per call, and check
the relays against what was asked.

Imports only const/helpers/units/calculations - never entities or engine.
"""

import logging

from .. import units
from ..calculations import sg_ready
from ..const import (
    CONF_SG_RELAY_1_ENTITY_ID,
    CONF_SG_RELAY_2_ENTITY_ID,
    SG_VERIFY_S,
)
from ..helpers import get_entry_value
from . import call_device, stamp_command

_LOGGER = logging.getLogger(__name__)

_MIN_RESEND_S = 10.0  # a command the relays do not obey is repeated at most this often


def relay_entities(entry):
    return (
        get_entry_value(entry, CONF_SG_RELAY_1_ENTITY_ID, None),
        get_entry_value(entry, CONF_SG_RELAY_2_ENTITY_ID, None),
    )


def observed_state(hass, entities):
    """The state the relays read as, or None when either cannot be read."""
    closed = []
    for entity in entities:
        state = hass.states.get(entity) if entity else None
        if units.is_unavailable(state):
            return None
        closed.append(state.state == "on")
    return sg_ready.STATE_OF_RELAYS[tuple(closed)]


async def _drive(hass, rt, entities, current, target) -> bool:
    """Step the relays from ``current`` to ``target`` through relay_path, one
    blocking service call per relay - so a failing second call leaves the mild
    state the path went through. False when a call failed."""
    previous = sg_ready.RELAYS[current]
    for combo in sg_ready.relay_path(current, target):
        index = 0 if combo[0] != previous[0] else 1
        try:
            await call_device(
                hass, rt, "switch", "turn_on" if combo[index] else "turn_off",
                {"entity_id": entities[index]}, "state",
                "on" if combo[index] else "off", blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - the relays stay at a valid combination
            _LOGGER.warning("SG Ready relay command failed (%s): %s", entities[index], err)
            return False
        previous = combo
    return True


def _verify(rt, observed, now) -> None:
    """The relays against the last command: a mismatch that outlasts SG_VERIFY_S
    is a fault (the state machine then goes to Normal); a match clears it."""
    commanded = rt.get("_sg_commanded")
    if commanded is None or observed is None:
        return
    state, since = commanded
    if observed == state:
        rt.pop("sg_relay_fault", None)
    elif now - since > SG_VERIFY_S and not rt.get("sg_relay_fault"):
        rt["sg_relay_fault"] = True
        _LOGGER.warning(
            "SG Ready relays read %s but %s was commanded - falling back to Normal",
            observed, state,
        )


async def send_sg_ready_command(sensor, status, now_mono) -> None:
    """Bring the relays to ``status["state"]`` (the engine's decision this cycle)."""
    entry, rt, hass = sensor.config_entry, sensor._runtime(), sensor.hass
    entities = relay_entities(entry)
    if not all(entities):
        _LOGGER.error("SG Ready %s: both relay switches must be configured", sensor._attr_name)
        return
    observed = observed_state(hass, entities)
    _verify(rt, observed, now_mono)
    if observed is None:
        return  # cannot read them: the engine already holds Normal; nothing to drive

    target = status["state"]
    if status.get("hands_off"):
        if rt.get("manual_until"):
            return  # the user operated the relays: theirs until it times out
        if rt.get("_sg_handsoff_done"):
            return
        rt["_sg_handsoff_done"] = True  # Normal once, then hands off
    else:
        rt.pop("_sg_handsoff_done", None)

    if observed == target:
        rt["_sg_commanded"] = (target, now_mono)
        stamp_command(sensor, now_mono)
        return
    if now_mono - rt.get("_sg_last_send", -float("inf")) < _MIN_RESEND_S:
        return
    rt["_sg_last_send"] = now_mono
    _LOGGER.debug("SG Ready %s: %s -> %s", sensor._attr_name, observed, target)
    ok = await _drive(hass, rt, entities, observed, target)
    rt["_sg_commanded"] = (target, now_mono)
    if not ok:
        rt["sg_relay_fault"] = True
    stamp_command(sensor, now_mono)


async def release_relays(hass, entry, rt=None) -> None:
    """Unload / shutdown: leave the pump in Normal."""
    entities = relay_entities(entry)
    if not all(entities):
        return
    observed = observed_state(hass, entities)
    if observed in (None, sg_ready.NORMAL):
        return
    await _drive(hass, rt if rt is not None else {}, entities, observed, sg_ready.NORMAL)
