"""Read solar forecast series from HA entities for the PV clipping maths.

The pure maths lives in ``calculations/forecast.py`` and takes plain
timezone-aware datetimes. This module is the HA-touching side: it reads the
``watts`` attribute the Open-Meteo Solar Forecast sensors expose (a mapping of
block-start timestamps to average watts), parses and validates it, and sums
the per-array series into one site series. It is also the only place timezone
handling lives — the horizon is the start of the next local day, so tomorrow's
peak is never reserved for twice.

Fail open by design: an unreadable entity or attribute contributes nothing,
which flows through to "nothing to clip" → SOC ceiling 100 % → charge cap
released. A dropped sensor must never clamp the battery; visibility comes from
the hub Status sensor, which names unavailable forecast entities.
"""

import logging
import math
from datetime import timedelta

from homeassistant.util import dt as dt_util

from ..calculations import merge_forecast_series

_LOGGER = logging.getLogger(__name__)


def forecast_window(now=None):
    """The integration window: (now, start of the next local day)."""
    now = now or dt_util.now()
    until = dt_util.start_of_local_day(now) + timedelta(days=1)
    return now, until


def _parse_watts(entity_id, watts):
    """Parse one entity's ``watts`` attribute into {aware datetime: float W}.

    Accepts datetime or ISO-string keys. Naive timestamps, non-finite and
    negative values are dropped with a debug note — one bad entry must not
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


def read_forecast_series(hass, entity_ids, hub_runtime):
    """Read and sum the configured forecast entities into one site series.

    The parse is memoized per entity on the State object's identity — HA
    replaces the immutable State on every update, so an unchanged object means
    an unchanged attribute (a timestamp key would miss two updates landing on
    the same clock tick). The attribute holds 48–200 entries and this runs
    every site refresh (default 2 s), while the forecast integration updates a
    few times per hour. A memo, not a fallback cache: a missing or unavailable
    entity contributes nothing (fail open), it does not serve stale data.
    """
    memo = hub_runtime.setdefault("_forecast_parse_memo", {})
    for stale_id in set(memo) - set(entity_ids):
        del memo[stale_id]

    series_list = []
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            memo.pop(entity_id, None)
            continue
        cached = memo.get(entity_id)
        if cached is not None and cached[0] is state:
            series_list.append(cached[1])
            continue
        series = _parse_watts(entity_id, state.attributes.get("watts"))
        memo[entity_id] = (state, series)
        if series:
            series_list.append(series)
        else:
            _LOGGER.debug(
                "Forecast entity %s has no usable watts data", entity_id
            )
    return merge_forecast_series(series_list)
