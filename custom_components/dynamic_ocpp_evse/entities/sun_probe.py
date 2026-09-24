"""How the off-grid sun probe's backoff is SHOWN on the load it keeps trying.

Off-grid with no battery and nothing measuring the spare sun, the engine tries
a step and backs off when production does not follow, each failed try in a
row doubling the pause (engine/hub_calculation._apply_sun_probe). Its per-load
state (``LOAD_RT_SUN_PROBE`` in the load's runtime bucket) is shown here, the
way entities/readout.py shows a stuck readout: in words on the status, and as
attributes. Nothing here decides anything.
"""

from __future__ import annotations

from homeassistant.util import dt as dt_util


def sun_probe_attributes(state) -> dict:
    """``sun_probe_failed_tries`` - failed tries in a row (0 once one
    succeeds, and wherever nothing is probed); ``sun_probe_next_try_at`` - the
    earliest the next one may run (UTC), None with no failure."""
    state = state or {}
    return {
        "sun_probe_failed_tries": state.get("failed_tries", 0),
        "sun_probe_next_try_at": state.get("next_try_at"),
    }


def status_with_sun_probe_note(status, state):
    """The status, with "(no spare sun on N tries, next try HH:MM)" while the
    load waits out a backoff pause."""
    at = (state or {}).get("next_try_at")
    if not status or at is None or at <= dt_util.utcnow():
        return status
    tries = state["failed_tries"]
    return (
        f"{status} (no spare sun on {tries} {'try' if tries == 1 else 'tries'}, "
        f"next try {dt_util.as_local(at).strftime('%H:%M')})"
    )
