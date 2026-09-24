"""How a stuck charger readout is SHOWN - one place for every entity and page.

While the stuck-readout watch (engine/readout_watch.py) judges an EVSE's
current/power reading frozen, the engine controls that charger blind and uses
an ASSUMED draw - the limit the charger last accepted, on the legs the verdict
chose - everywhere it would have used the reading. The figures built on that
assumption are published as estimates rather than blanked, and this module
says how they are marked:

* ``readout_attributes`` - the episode itself, on the charger's Available
  Current sensor (and its Charging Status);
* ``estimate_attributes`` - ``estimated`` / evidence / since, on every figure
  derived from the charger's draw (its Allocated Current and Phase Mask);
* ``hub_estimate_attributes`` - the same for the hub's site totals that net
  the managed draw in or out (Current Managed Power, Household Power), from
  the engine's ``draw_estimated`` map;
* ``status_with_readout_note`` - the plain-words note on the charger's status.

Everything reads the watch's own state dict (the load's runtime bucket), which
the engine keeps; nothing here decides anything. Pure: no Home Assistant, so
the Overview page and the tests can use it as-is.
"""

from __future__ import annotations

# Appended to the charger's status for as long as the episode lasts, so the
# entity a user looks at says it in words: "Charging (readout stuck -
# controlled on assumed current)".
READOUT_STUCK_NOTE = "readout stuck - controlled on assumed current"


def _stuck(watch) -> bool:
    return bool(watch and watch.get("stuck"))


def readout_attributes(watch) -> dict:
    """An EVSE's stuck-readout state as entity attributes.

    * ``readout_stuck`` - the charger's current/power reading is judged frozen
      and the load is being controlled blind;
    * ``readout_stuck_evidence`` - how: ``above_limit`` (it claimed more than
      the charger may deliver) or ``household_lockstep`` (the house load
      followed our commands to this charger while it read nothing);
    * ``readout_stuck_since`` - when that episode began (UTC);
    * ``readout_stuck_value`` - the frozen per-leg reading, L1/L2/L3 in A;
    * ``readout_assumed_current`` - the per-leg draw the engine uses instead;
    * ``readout_normal_gap_seconds`` - the longest this reading has recently
      gone between two values while charging, which sets how long a
      contradiction must last before it counts. None while still learning.

    The episode fields are None whenever the reading is trusted.
    """
    watch = watch or {}
    stuck = _stuck(watch)
    gap = watch.get("normal_gap_s")
    return {
        "readout_stuck": stuck,
        "readout_stuck_evidence": watch.get("stuck_how") if stuck else None,
        "readout_stuck_since": watch.get("stuck_at") if stuck else None,
        "readout_stuck_value": (
            [round(v, 2) for v in watch.get("stuck_value") or ()]
            if stuck else None
        ),
        "readout_assumed_current": (
            [round(v, 2) for v in watch.get("assumed") or ()]
            if stuck else None
        ),
        "readout_normal_gap_seconds": round(gap, 1) if gap is not None else None,
    }


def estimate_attributes(watch) -> dict:
    """Marks a per-charger figure derived from its draw as an estimate while
    the charger is controlled blind: ``estimated`` (always present), and the
    evidence and start of the episode while it is."""
    stuck = _stuck(watch)
    return {
        "estimated": stuck,
        "estimate_evidence": watch.get("stuck_how") if stuck else None,
        "estimated_since": watch.get("stuck_at") if stuck else None,
    }


def hub_estimate_attributes(draw_estimated) -> dict:
    """The same marks for a hub figure that nets in every managed draw.

    ``draw_estimated`` is the engine's per-load map of chargers whose draw is
    assumed this cycle (engine/hub_result.py): load id -> ``{"load", "evidence",
    "since"}``. The figure is an estimate while any entry is there; the
    attributes name the chargers, the evidence paths and the earliest start.
    """
    entries = [e for e in (draw_estimated or {}).values() if e]
    if not entries:
        return {"estimated": False, "estimated_loads": None,
                "estimate_evidence": None, "estimated_since": None}
    starts = [e.get("since") for e in entries if e.get("since") is not None]
    return {
        "estimated": True,
        "estimated_loads": sorted(str(e.get("load")) for e in entries),
        "estimate_evidence": sorted({str(e.get("evidence")) for e in entries}),
        "estimated_since": min(starts) if starts else None,
    }


def status_with_readout_note(status, watch):
    """The charger's status, with the stuck-readout note while blind."""
    if not _stuck(watch) or not status:
        return status
    return f"{status} ({READOUT_STUCK_NOTE})"
