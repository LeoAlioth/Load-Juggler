"""Expected grid loss window (JUG-23, pure Python).

A hub setting points at a ``calendar`` or ``binary_sensor`` entity that says when
the grid is about to be lost (a DSO's planned outage). While a window is within
its lead time, running, or inside the hold after it, the home battery is
pre-charged so the site enters the outage full. This module only decides WHETHER
the pre-charge is on; the engine acts on it (engine/hub_calculation.py,
control/inverter.py).

What is read, all from the entity's state and attributes - the Juggler knows no
DSO:

* calendar (Home Assistant): state ``on`` while an event is running; the
  attributes ``start_time`` / ``end_time`` (local ``YYYY-MM-DD HH:MM:SS``) are
  those of the running event, else of the NEXT one - which gives the look-ahead.
* binary_sensor: ``on`` = a window is running. There is no look-ahead unless the
  entity carries ``next_start`` (and optionally ``next_end``) attributes; then
  the lead time applies to it too.

The only state kept is the hold: ``memory`` is ``(hold_until, start, end)`` of the
last running window, returned for the caller to keep in ``hub_runtime`` - nothing
is persisted, so a restart inside the hold simply ends it. An unavailable or
unknown entity is no window at all, and drops the hold.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..units import is_unavailable_state


def _when(value, tz):
    """A datetime from an attribute (a datetime or an ISO-like string), naive
    values taken as local time; None when it is not one."""
    if isinstance(value, str) and value.strip():
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=tz)


def precharge_window(state, attrs, now, lead_hours, hold_hours, memory=None):
    """``(window, memory)``: ``window`` is ``(start, end)`` (either may be None)
    while the battery should be pre-charged, else None.

    On from ``lead_hours`` before an upcoming window's start, through the window,
    to ``hold_hours`` after it ends. ``now`` is timezone-aware local time.
    """
    state = state.lower() if isinstance(state, str) else state
    if is_unavailable_state(state):
        return None, None
    attrs = attrs or {}
    tz = now.tzinfo
    lead = timedelta(hours=max(float(lead_hours or 0), 0.0))
    hold = timedelta(hours=max(float(hold_hours or 0), 0.0))
    start = _when(attrs.get("start_time", attrs.get("next_start")), tz)
    end = _when(attrs.get("end_time", attrs.get("next_end")), tz)

    if state == "on":
        # Running. Only now is there a known end for the hold to count from; a
        # window with no end (a bare binary_sensor) holds from the last cycle it
        # was on.
        until = (end if end is not None and end > now else now) + hold
        return (start, end), (until, start, end)
    if start is not None and start > now and start - lead <= now:
        return (start, end), None
    if memory is not None and now < memory[0]:
        return (memory[1], memory[2]), memory
    return None, None


def precharge_target(target, full):
    """The battery SOC target the allocation runs on while pre-charging: "full"
    (the hub's Full SOC), never lower than the owner's own target. Below it the
    battery counts as wanting to charge, so Solar Only and Solar Priority loads
    wait for it exactly as they do below the ordinary target."""
    return max(target, full)


def precharge_note(window, now):
    """The hub status text while pre-charging, e.g. "Pre-charging for the planned
    outage 14:00-18:00" (a date is added when the window is not today)."""
    start, end = window
    if start is not None and end is not None:
        span = f"{start:%H:%M}-{end:%H:%M}"
    elif end is not None:
        span = f"until {end:%H:%M}"
    else:
        span = ""
    day = start if start is not None else end
    if span and day is not None and day.date() != now.date():
        span = f"{day:%d.%m.} {span}"
    note = "Pre-charging for the planned outage"
    return f"{note} {span}" if span else note
