"""The closed-loop rigs the HA-tier tests share.

Machine-authored - not yet human-reviewed.

``close_loop`` runs the production site cycle - ``run_hub_calculation`` and
the real permit pipeline (``control.smoothing.apply_smoothing``) - against a
plant the test supplies: each cycle the plant moves its cars on the commands
in force and sets its meters, the engine sizes the permits on what they read,
and the pipeline turns those into the next commands.

``clocked`` is for the rigs that run the whole hub cycle
(``sensor.async_run_hub_cycle``) instead, load processor and OCPP call
included, on a monotonic clock the test advances.
"""

import time
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    CONF_CHARGER_ID,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_HUB_ENTRY_ID,
    CONF_LOAD_PRIORITY,
    CONF_NAME,
    CONF_PHASES,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
)
from custom_components.dynamic_ocpp_evse.control import compliance, status
from custom_components.dynamic_ocpp_evse.control.smoothing import apply_smoothing
from custom_components.dynamic_ocpp_evse.engine import (
    auto_detect,
    hub_calculation,
    hub_result,
    load_builders,
    readers,
)
from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
    run_hub_calculation,
)
from custom_components.dynamic_ocpp_evse.entities import load as load_entity

V = 230.0
DT = 2                                # the default site cycle, seconds
AMPS = {"device_class": "current", "unit_of_measurement": "A"}
WATTS = {"device_class": "power", "unit_of_measurement": "W"}
BATTERY_PCT = {"device_class": "battery", "unit_of_measurement": "%"}
# The hub's SOC floor and target as its number entities hold them.
SOC_BOUNDS = {"battery_soc_min": 20, "battery_soc_target": 50}


def evse_entry(hub, slug, lo=6, hi=32, priority=1):
    """A 1-phase ``lo``→``hi`` A EVSE named ``slug``: it reads its draw from
    ``sensor.<slug>_current`` and its connector from
    ``sensor.<slug>_status_connector``."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=slug,
        data={CONF_NAME: slug,
              CONF_ENTITY_ID: slug,
              ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_CHARGER_ID: slug,
              CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: f"sensor.{slug}_current",
              CONF_HUB_ENTRY_ID: hub.entry_id},
        options={
            CONF_LOAD_PRIORITY: priority,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: lo,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: hi,
            CONF_PHASES: 1,
        },
    )


def slew(draw, target, step):
    """A car's draw one cycle on: toward ``target``, by at most ``step``."""
    return draw + max(-step, min(step, target - draw))


def over_w(trace, key, limit_a):
    """How far ``row[key]`` peaks above ``limit_a``, in W (0 if never)."""
    return max(0.0, max(row[key] - limit_a for row in trace)) * V


async def close_loop(hass, hub, evses, cycles, plant, *, at,
                     hub_data=None, load_data=None):
    """Close the loop: engine permit → permit pipeline → car → meters → engine.

    Adds ``hub`` and ``evses`` (one entry, or a list) to hass and seeds
    hass.data as setup does; ``hub_data`` / ``load_data`` add to the hub's and
    every load's bucket. From ``at``, each cycle the clock moves on ``DT``,
    ``plant(i, command)`` moves the cars on the command in force (0 before the
    first) and sets the meters, and returns that cycle's row; the site cycle
    runs and the row gains its ``result`` and ``permit``. Command and permit
    are one value per EVSE, a list when ``evses`` is a list. One row per cycle.
    """
    single = not isinstance(evses, list)
    loads = [evses] if single else evses
    hub.add_to_hass(hass)
    for entry in loads:
        entry.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {
            "loads": [e.entry_id for e in loads], **(hub_data or {}),
        }},
        "loads": {e.entry_id: {
            "entry": e, "hub_entry_id": hub.entry_id, "dynamic_control": True,
            **(load_data or {}),
        } for e in loads},
        "load_allocations": {e.entry_id: 0 for e in loads},
        "inverters": {},
    }
    # apply_smoothing keeps its state on the load entity and touches only these.
    permit_states = [
        SimpleNamespace(
            _attr_name=e.data[CONF_ENTITY_ID], _ema_current=None,
            _schmitt_current=None, _schmitt_state="rising",
            _rate_limited_current=0.0,
        )
        for e in loads
    ]

    trace = []
    commands = [0.0] * len(loads)
    with freeze_time(at) as frozen:
        for i in range(cycles):
            frozen.tick(DT)
            row = plant(i, commands[0] if single else commands)
            result = run_hub_calculation(hass, hub)
            # The load processor's own rounding (entities/load.py).
            permits = [
                round(result["load_available"][e.entry_id], 1) for e in loads
            ]
            commands = [
                apply_smoothing(state, permit, False, hub)
                for state, permit in zip(permit_states, permits)
            ]
            row.update(i=i, result=result,
                       permit=permits[0] if single else permits)
            trace.append(row)
    return trace


# Every module of the hub cycle that reads time.monotonic().
_CLOCKED = (
    load_builders, hub_calculation, readers, hub_result, auto_detect,
    load_entity, status, compliance,
)


class Clock:
    """``time`` as the modules above see it: monotonic() is ours to advance,
    everything else is the real module."""

    def __init__(self, start):
        self.now = start

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


@contextmanager
def clocked(accept, *patches):
    """The hub cycle's modules on a ``Clock`` from 10 000 s, which it yields,
    and every service call it makes answered by ``accept`` - plus any further
    ``patches``, all undone on the way out."""
    clock = Clock(10_000.0)
    with ExitStack() as stack:
        for module in _CLOCKED:
            stack.enter_context(patch.object(module, "time", clock))
        stack.enter_context(patch(
            "homeassistant.core.ServiceRegistry.async_call",
            new_callable=AsyncMock, side_effect=accept,
        ))
        for extra in patches:
            stack.enter_context(extra)
        yield clock
