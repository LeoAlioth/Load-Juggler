import logging
from .. import units
from ..const import (
    CONF_PLUG_FINISH_BELOW_W,
    CONF_PLUG_FINISH_FOR,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
    CONF_PLUG_RESTART_AFTER,
    CONF_PLUG_RESTART_ON_NO_DRAW,
    CONF_PLUG_SWITCH_ENTITY_ID,
    DEFAULT_PLUG_FINISH_BELOW_W,
    DEFAULT_PLUG_FINISH_FOR,
    DEFAULT_PLUG_RESTART_AFTER,
    DEFAULT_PLUG_RESTART_ON_NO_DRAW,
    PLUG_NO_DRAW_W,
    PLUG_RESTART_MAX_TRIES,
    PLUG_RESTART_MIN_GAP_S,
    PLUG_RESTART_OFF_S,
    PLUG_RESTART_RECOVERY_S,
)
from ..helpers import get_entry_value
from . import stamp_command

_LOGGER = logging.getLogger(__name__)

_RESTART = "restart"  # switch the plug off now: a power cycle starts
_HOLD = "hold"  # a power cycle is keeping the plug off


def _reset_restarts(rt) -> None:
    rt["plug_restart_count"] = 0
    rt["plug_restart_status"] = None
    rt["_plug_restart_gave_up"] = False


def _no_draw_restart(sensor, switch_entity, limit, now):
    """The "Restart if it stops drawing" watch - _RESTART, _HOLD or None.

    A load behind the plug can stop drawing with the relay still on (the Pond
    EVSE cuts out on over-temperature), and a turn_on to a relay that is
    already on does nothing. So a plug Load Juggler permits, whose switch is
    on and whose monitor reads under PLUG_NO_DRAW_W for the "restart after"
    span, is switched off and - PLUG_RESTART_OFF_S later, by the ordinary
    command - on again; the controller sends nothing in between.

    At most one restart per PLUG_RESTART_MIN_GAP_S. After
    PLUG_RESTART_MAX_TRIES in a row it gives up until the load draws for
    PLUG_RESTART_RECOVERY_S or its permit goes away and comes back (toggling
    the option reloads the entry, which starts it over too). An unreadable
    monitor is no evidence of anything, so it never restarts the plug.
    State lives in the load's runtime dict, where the Status sensor reads it.
    """
    entry = sensor.config_entry
    monitor = get_entry_value(entry, CONF_PLUG_POWER_MONITOR_ENTITY_ID, None)
    if not monitor or not get_entry_value(
        entry, CONF_PLUG_RESTART_ON_NO_DRAW, DEFAULT_PLUG_RESTART_ON_NO_DRAW
    ):
        return None
    rt = sensor._runtime()
    rt.setdefault("plug_restart_count", 0)
    if now < rt.get("_plug_restart_until", now):
        return _HOLD
    if limit <= 0:
        rt["_plug_no_draw_since"] = rt["_plug_draw_since"] = None
        _reset_restarts(rt)
        return None

    state = sensor.hass.states.get(switch_entity)
    power = units.read_number(sensor.hass, monitor, units.DOMAIN_WATTS)
    if state is None or state.state != "on" or power is None:
        # Off (the command below switches it on) or unreadable.
        rt["_plug_no_draw_since"] = rt["_plug_draw_since"] = None
        return None

    if power >= PLUG_NO_DRAW_W:
        rt["_plug_no_draw_since"] = None
        if rt.get("_plug_draw_since") is None:
            rt["_plug_draw_since"] = now
        if (
            rt["plug_restart_count"]
            and now - rt["_plug_draw_since"] >= PLUG_RESTART_RECOVERY_S
        ):
            _LOGGER.info(
                "Smart load %s draws again - restart count reset", sensor._attr_name
            )
            _reset_restarts(rt)
        return None

    rt["_plug_draw_since"] = None
    if rt.get("_plug_no_draw_since") is None:
        rt["_plug_no_draw_since"] = now
    after_min = float(
        get_entry_value(entry, CONF_PLUG_RESTART_AFTER, DEFAULT_PLUG_RESTART_AFTER)
    )
    if now - rt["_plug_no_draw_since"] < after_min * 60:
        return None

    count = rt["plug_restart_count"]
    if count >= PLUG_RESTART_MAX_TRIES:
        if not rt.get("_plug_restart_gave_up"):
            rt["_plug_restart_gave_up"] = True
            rt["plug_restart_status"] = f"No draw (gave up after {count} restarts)"
            _LOGGER.warning(
                "Smart load %s still draws nothing after %d restarts - not"
                " restarting it again until it draws, its permit returns or"
                " the option is toggled",
                sensor._attr_name,
                count,
            )
        return None
    if now - rt.get("_plug_restarted_at", -float("inf")) < PLUG_RESTART_MIN_GAP_S:
        return None

    rt["plug_restart_count"] = count + 1
    rt["plug_restart_status"] = f"Restarted (no draw {after_min:g} min)"
    rt["_plug_restarted_at"] = now
    rt["_plug_restart_until"] = now + PLUG_RESTART_OFF_S
    rt["_plug_no_draw_since"] = None
    _LOGGER.info(
        "Smart load %s permitted and on but drawing %.0f W for %g min -"
        " power-cycling it (restart %d of %d)",
        sensor._attr_name,
        power,
        after_min,
        count + 1,
        PLUG_RESTART_MAX_TRIES,
    )
    return _RESTART


def _cycle_running(sensor, switch_entity, now) -> bool:
    """Whether the load behind a switched-on plug is still in its cycle: its
    monitor has not yet read under the "finish below" power for the "finish
    for" time (a washing machine's soak or drain pauses are shorter). Tracked
    on every command, so a cycle that ended long ago lets the plug off at
    once. None when not set up (0 W, or no monitor); False with the switch
    off. An unreadable monitor reads as still running - a missing reading
    must not cut a wash.
    """
    entry = sensor.config_entry
    below = float(get_entry_value(entry, CONF_PLUG_FINISH_BELOW_W, DEFAULT_PLUG_FINISH_BELOW_W) or 0)
    monitor = get_entry_value(entry, CONF_PLUG_POWER_MONITOR_ENTITY_ID, None)
    if below <= 0 or not monitor:
        return None
    rt = sensor._runtime()
    state = sensor.hass.states.get(switch_entity)
    if state is None or state.state != "on":
        rt["_plug_quiet_since"] = None
        return False
    power = units.read_number(sensor.hass, monitor, units.DOMAIN_WATTS)
    if power is None or power >= below:
        rt["_plug_quiet_since"] = None
        return True
    if rt.get("_plug_quiet_since") is None:
        rt["_plug_quiet_since"] = now
    finish_s = 60 * float(get_entry_value(entry, CONF_PLUG_FINISH_FOR, DEFAULT_PLUG_FINISH_FOR) or 0)
    return now - rt["_plug_quiet_since"] < finish_s


async def send_plug_command(sensor, limit: float, now_mono: float) -> None:
    """Send on/off command to a smart load device."""
    # The settings page saves a changed switch to options, so read it there first.
    plug_switch_entity = get_entry_value(sensor.config_entry, CONF_PLUG_SWITCH_ENTITY_ID)
    if not plug_switch_entity:
        _LOGGER.error(f"No switch entity configured for plug {sensor._attr_name}")
        return

    restart = _no_draw_restart(sensor, plug_switch_entity, limit, now_mono)
    if restart == _HOLD:
        # Not stamped either, so the next site cycle asks again and the plug
        # goes back on as soon as the hold is over.
        return

    on = limit > 0 and restart != _RESTART
    # A load Load Juggler would switch off finishes its cycle first.
    running = _cycle_running(sensor, plug_switch_entity, now_mono)
    if running is not None:
        finishing = not on and restart != _RESTART and running
        sensor._runtime()["plug_finish_status"] = "Finishing its cycle" if finishing else None
        if finishing:
            _LOGGER.debug("Smart load %s: finishing its cycle before switching off", sensor._attr_name)
            stamp_command(sensor, now_mono)
            return
    try:
        _LOGGER.debug(
            f"Smart load {sensor._attr_name}: turning {'ON' if on else 'OFF'} (limit={limit}A)"
        )
        await sensor.hass.services.async_call(
            "switch",
            "turn_on" if on else "turn_off",
            {"entity_id": plug_switch_entity},
            blocking=False,
        )
    except Exception as e:
        _LOGGER.warning(
            "Smart load switch command failed for %s: %s", sensor._attr_name, e
        )

    stamp_command(sensor, now_mono)
