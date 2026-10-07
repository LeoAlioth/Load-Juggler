"""Read solar forecast series from HA entities for the PV clipping maths.

The pure maths lives in ``calculations/forecast.py`` and takes plain
timezone-aware datetimes. This module is the HA-touching side: it reads the
``watts`` attribute the Open-Meteo Solar Forecast sensors expose (a mapping of
block-start timestamps to average watts), parses and validates it, and sums
the per-array series into one site series. It is also the only place timezone
handling lives - every horizon boundary is a local midnight, so one day's peak
is never reserved for twice (``forecast_windows``).

Fail open by design: an unreadable entity or attribute contributes nothing,
which flows through to "nothing to clip" → SOC ceiling 100 % → charge cap
released. A dropped sensor must never clamp the battery; visibility comes from
the hub Status sensor, which names unavailable forecast entities. One
exception: a source that stops delivering keeps contributing the last series
it read for up to FORECAST_HOLD_S after that read (``read_forecast_series_pair``)
- an internet outage takes the forecast integration with it, and the forecast
it last fetched still describes the day.
"""

import logging
import math
from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..const import FORECAST_HOLD_S
from ..calculations import (
    FORECAST_LOOKAHEAD_DAYS,
    merge_forecast_series,
    scale_forecast_series,
)
from .. import units

_LOGGER = logging.getLogger(__name__)

# The Open-Meteo device sensor to prefer when several carry a watts series.
_FORECAST_TODAY_HINT = "energy_production_today"


def resolve_forecast_sensor(hass, device_id, held=()):
    """Pick the one ``watts``-bearing sensor of a forecast device, or None.

    A forecast device (one Open-Meteo Solar Forecast config entry per PV
    array) exposes several sensors carrying the same ``watts`` series - today,
    tomorrow, current power. Exactly one must be read per device or the array
    is double-counted. Prefer the "energy production today" sensor; otherwise
    the first watts-bearing sensor in deterministic order.

    ``held`` - the entities with a forecast held for them
    (``read_forecast_series_pair``) - count as watts-bearing: an unavailable
    entity carries no attributes at all, so without this the device would
    resolve to nothing and its held forecast could never be read.
    """
    registry = er.async_get(hass)
    candidates = []
    for entry in er.async_entries_for_device(registry, device_id):
        if entry.domain != "sensor":
            continue
        state = hass.states.get(entry.entity_id)
        if entry.entity_id not in held and (
            state is None or not isinstance(state.attributes.get("watts"), dict)
        ):
            continue
        candidates.append(entry.entity_id)
    if not candidates:
        return None
    candidates.sort()
    for entity_id in candidates:
        if _FORECAST_TODAY_HINT in entity_id:
            return entity_id
    return candidates[0]


def configured_forecast_sensors(hass, device_ids, legacy_entity_ids=None, held=()):
    """The sensor entity_ids to read: one per configured forecast device,
    plus any directly-configured legacy entities (pre-device-selector
    installs). A device whose sensors expose no watts data contributes
    nothing - fail open, visibility via the hub Status sensor - unless a
    forecast is held for one of them (``held``, see resolve_forecast_sensor)."""
    entity_ids = []
    for device_id in device_ids or []:
        entity_id = resolve_forecast_sensor(hass, device_id, held)
        if entity_id is not None:
            entity_ids.append(entity_id)
        else:
            _LOGGER.debug(
                "Forecast device %s has no watts-bearing sensor", device_id
            )
    for entity_id in legacy_entity_ids or []:
        if entity_id not in entity_ids:
            entity_ids.append(entity_id)
    return entity_ids


def forecast_windows(now=None):
    """The candidate integration windows, nearest first.

    ``[(now, tonight's midnight), (tonight's midnight, tomorrow's midnight), …]``
    - the remainder of today, then one whole local day per lookahead day. The
    pure side picks between them (``select_clipping_window``); all this owns is
    the local-day arithmetic, which is why it lives here and not in
    ``calculations/``.

    Every boundary is a real local midnight - ``start_of_local_day`` applied to
    the following midday, never a 24-hour offset - so on the two DST days a year
    one window is 23 or 25 hours long and the windows still meet exactly. Adding
    a day's worth of seconds instead would land at 23:00 or 01:00 and leave a
    gap between one window's end and the next one's start, where a block of clip
    would belong to neither.

    ``now`` is a parameter so tests need no wall clock.
    """
    now = now or dt_util.now()
    boundary = _next_local_midnight(dt_util.start_of_local_day(now))
    windows = [(now, boundary)]
    for _ in range(FORECAST_LOOKAHEAD_DAYS):
        following = _next_local_midnight(boundary)
        windows.append((boundary, following))
        boundary = following
    return windows


def _next_local_midnight(day_start):
    """The local midnight after ``day_start``, DST-safe (see forecast_windows)."""
    return dt_util.start_of_local_day(day_start + timedelta(days=1, hours=12))


def _parse_watts(entity_id, watts):
    """Parse one entity's ``watts`` attribute into {aware datetime: float W}.

    Accepts datetime or ISO-string keys. Naive timestamps, non-finite and
    negative values are dropped with a debug note - one bad entry must not
    discard the rest of the series.
    """
    series = {}
    if not isinstance(watts, dict):
        _LOGGER.debug(
            "Forecast entity %s: watts attribute is %s, not a mapping",
            entity_id,
            type(watts).__name__,
        )
        return series
    for key, value in watts.items():
        ts = key if not isinstance(key, str) else dt_util.parse_datetime(key)
        if ts is None or ts.tzinfo is None:
            _LOGGER.debug(
                "Forecast entity %s: dropping entry with naive/unparseable key %r",
                entity_id,
                key,
            )
            continue
        try:
            power = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(power) or power < 0:
            continue
        series[ts] = power
    return series


def read_forecast_series_pair(hass, entity_ids, hub_runtime, inflation_by_entity=None):
    '''``(raw, inflated, by_entity)`` - both site series, and their parts.

    Two series out of ONE read, because the two consumers want different
    numbers from the same forecast. The clip integral may be biased optimistic
    per array (a learned gain, once the observers earn one); the deadline
    (``first_production_at``) must not be, since it already carries its own
    early bias. Reading twice would double the parse and could straddle a
    forecast update, so the per-array series are kept and merged twice instead.

    ``inflation_by_entity`` maps a forecast entity to its inverter's percent.
    Falsy, or all zeros, returns the SAME object for both - so a site with
    nothing configured takes exactly the path it took before this existed.

    The third element is the per-entity map the merges were built from. The
    gain observer needs it to split the site forecast back out per inverter
    (each array belongs to one), and rebuilding it there would parse every
    entity a second time.

    The parse is memoized per entity on the State object's identity - HA
    replaces the immutable State on every update, so an unchanged object means
    an unchanged attribute (a timestamp key would miss two updates landing on
    the same clock tick). The attribute holds 48–200 entries and this runs
    every site refresh (default 2 s), while the forecast integration updates a
    few times per hour.

    The memo is not the fallback; ``_forecast_held`` is. Each entity's last
    usable series is kept there with the time it was last read, and an entity
    that is unavailable or has no usable data contributes that series instead,
    for up to FORECAST_HOLD_S after that read - the same series a fresh read
    of it would give, used the same way. Past that it contributes nothing, as
    any missing entity always did (fail open). In memory only: a restart
    starts with nothing held. The read time shows in the diagnostics dump.
    '''
    memo = hub_runtime.setdefault("_forecast_parse_memo", {})
    held = hub_runtime.setdefault("_forecast_held", {})
    for cache in (memo, held):
        for stale_id in set(cache) - set(entity_ids):
            del cache[stale_id]

    now = dt_util.utcnow()
    by_entity = {}
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        series = None
        if units.is_unavailable(state):
            memo.pop(entity_id, None)
        else:
            cached = memo.get(entity_id)
            if cached is not None and cached[0] is state:
                series = cached[1]
            else:
                series = _parse_watts(entity_id, state.attributes.get("watts"))
                memo[entity_id] = (state, series)
                if not series:
                    _LOGGER.debug(
                        "Forecast entity %s has no usable watts data", entity_id
                    )
        if series:
            held[entity_id] = (now, series, False)
            by_entity[entity_id] = series
            continue
        if entity_id not in held:
            continue
        read_at, series, holding = held[entity_id]
        if (now - read_at).total_seconds() > FORECAST_HOLD_S:
            del held[entity_id]
            _LOGGER.info(
                "Forecast entity %s: the forecast held since %s has expired,"
                " continuing without it",
                entity_id,
                read_at,
            )
            continue
        if not holding:
            _LOGGER.info(
                "Forecast entity %s has no forecast: using the one last read at"
                " %s, for up to %d h",
                entity_id,
                read_at,
                FORECAST_HOLD_S // 3600,
            )
            held[entity_id] = (read_at, series, True)
        by_entity[entity_id] = series

    raw = merge_forecast_series(list(by_entity.values()))
    if not any((inflation_by_entity or {}).get(e) for e in by_entity):
        return raw, raw, by_entity
    inflated = merge_forecast_series([
        scale_forecast_series(series, (inflation_by_entity or {}).get(entity_id) or 0)
        for entity_id, series in by_entity.items()
    ])
    return raw, inflated, by_entity
