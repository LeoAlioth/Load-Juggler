"""Per-inverter forecast optimism — the clip integral only.

Open-Meteo's clear-day error is a few percent and is not uniform across arrays,
so each inverter may bias its own forecast up before the clip is integrated.
Two properties matter and neither is obvious:

* the factor is applied PER ARRAY, before the merge — summing scaled arrays is
  not the same as scaling the summed site series once any two arrays differ;
* it reaches the clip integral ONLY. The overnight drop's deadline reads the
  raw series, because that estimate already carries its own early bias
  (FORECAST_EARLY_START_FACTOR) and stacking two margins on one number moves
  the drop earlier for nothing.

Docker / CI tier, alongside test_forecast_clipping.py:
  pytest dev/tests/test_forecast_inflation.py
"""

from datetime import datetime, timedelta, timezone

from custom_components.dynamic_ocpp_evse.calculations import (
    clipping_forecast,
    first_production_at,
    merge_forecast_series,
    scale_forecast_series,
)
from custom_components.dynamic_ocpp_evse.engine import fleet

T0 = datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc)


def _series(*watts, hours=1):
    return {T0 + timedelta(hours=i * hours): w for i, w in enumerate(watts)}


# --- The scaling itself ----------------------------------------------------


def test_zero_returns_the_same_object():
    """An unconfigured site must cost nothing at all — not even a rebuild."""
    s = _series(1000.0, 2000.0)
    assert scale_forecast_series(s, 0) is s
    assert scale_forecast_series(s, None) is s


def test_ten_percent_inflates_every_block():
    s = _series(1000.0, 2000.0)
    out = scale_forecast_series(s, 10)
    assert list(out.values()) == [1100.0, 2200.0]
    assert list(out) == list(s)  # timestamps untouched


def test_a_negative_factor_deflates():
    out = scale_forecast_series(_series(1000.0), -20)
    assert list(out.values()) == [800.0]


def test_the_factor_cannot_drive_production_negative():
    out = scale_forecast_series(_series(1000.0), -250)
    assert list(out.values()) == [0.0]


# --- Per array, before the merge -------------------------------------------


def test_scaling_arrays_separately_is_not_scaling_the_sum():
    """The whole reason the factor lives on the inverter: two arrays with
    different biases cannot be expressed by one factor on the site series."""
    shaded = _series(1000.0)
    clean = _series(3000.0)

    per_array = merge_forecast_series([
        scale_forecast_series(shaded, 20),
        scale_forecast_series(clean, 0),
    ])
    assert list(per_array.values()) == [4200.0]

    # The same total biased once, either way, lands somewhere else entirely.
    site_at_20 = scale_forecast_series(merge_forecast_series([shaded, clean]), 20)
    site_at_0 = merge_forecast_series([shaded, clean])
    assert list(site_at_20.values()) == [4800.0]
    assert list(site_at_0.values()) == [4000.0]


def test_one_uniform_factor_does_coincide():
    """The degenerate case, worth pinning so the per-array rule is not read as
    a claim that the two forms never agree."""
    a, b = _series(1000.0), _series(3000.0)
    per_array = merge_forecast_series([
        scale_forecast_series(a, 15), scale_forecast_series(b, 15)
    ])
    whole = scale_forecast_series(merge_forecast_series([a, b]), 15)
    assert per_array == whole


# --- What it reaches, and what it must not --------------------------------


def test_inflation_grows_the_clip_and_the_reserve_it_buys():
    """5 kW export limit, an array forecast at 6 kW for one hour: 1 kWh clips
    raw, 1.3 kWh once the array is biased 30% optimistic."""
    raw = _series(6000.0)
    until = T0 + timedelta(hours=1)

    plain = clipping_forecast(raw, 5000.0, T0, until)
    biased = clipping_forecast(scale_forecast_series(raw, 30), 5000.0, T0, until)

    assert round(plain.clipped_kwh, 3) == 1.0
    assert round(biased.clipped_kwh, 3) == 2.8
    assert biased.clipped_kwh > plain.clipped_kwh


def test_inflation_can_surface_a_clip_the_raw_forecast_misses():
    """A day forecast just under the limit reserves nothing at all; that is
    exactly the marginal case a low-reading forecast costs you."""
    raw = _series(4900.0)
    until = T0 + timedelta(hours=1)
    assert clipping_forecast(raw, 5000.0, T0, until).clipped_kwh == 0.0
    assert clipping_forecast(
        scale_forecast_series(raw, 10), 5000.0, T0, until
    ).clipped_kwh > 0.0


def test_the_production_deadline_is_computed_on_the_raw_series():
    """The deadline must not move. A block that is below base consumption raw
    and above it inflated would hand the overnight drop an earlier deadline —
    two safety margins stacked on one number."""
    raw = _series(250.0, 4000.0)
    base = 300.0

    # Raw: the first block is below base, so the crossing is the SECOND block.
    assert first_production_at(raw, base, T0, T0 + timedelta(hours=3)) == T0 + timedelta(
        hours=1
    )
    # Inflated 30%, that first block reads 325 W and would cross immediately —
    # which is precisely why the engine hands this function the raw series.
    inflated = scale_forecast_series(raw, 30)
    assert first_production_at(inflated, base, T0, T0 + timedelta(hours=3)) == T0


# --- The per-inverter map --------------------------------------------------


def _member(entry_id, devices, inflation):
    return fleet.FleetMember(
        entry_id=entry_id,
        forecast_device_ids=tuple(devices),
        forecast_inflation=inflation,
    )


def test_only_biased_members_appear_in_the_map():
    """An all-default fleet returns nothing, so the reader skips the scaling
    pass and both series stay the same object."""
    members = [_member("a", ["dev_a"], 0), _member("b", ["dev_b"], 0)]
    assert fleet.forecast_inflation_by_device(members) == {}


def test_each_array_carries_its_own_factor():
    members = [_member("a", ["dev_a"], 20), _member("b", ["dev_b"], 5)]
    assert fleet.forecast_inflation_by_device(members) == {"dev_a": 20.0, "dev_b": 5.0}


def test_a_shared_device_is_claimed_once():
    """Same de-duplication rule as forecast_device_ids — first member wins —
    so a device configured on two inverters cannot be biased twice."""
    members = [_member("a", ["shared"], 20), _member("b", ["shared"], 5)]
    assert fleet.forecast_inflation_by_device(members) == {"shared": 20.0}


def test_an_unbiased_member_still_claims_its_device():
    """First-wins has to hold in both directions: if the unbiased member owns
    the device, the biased sibling must not silently inflate it."""
    members = [_member("a", ["shared"], 0), _member("b", ["shared"], 30)]
    assert fleet.forecast_inflation_by_device(members) == {}
