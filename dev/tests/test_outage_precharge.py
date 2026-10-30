"""Expected grid loss window (JUG-23): pre-charge the battery before a planned outage.

Three layers, bottom up: the pure window rule (calculations/outage_window.py), the
hub calculation that acts on it (target SOC raised to full, the clipping advice and
the early starts stood down, the status note), and the SOC write-control that lifts
the slots to full and restores them. The scenarios beside these
(scenarios/features/test_outage_precharge.yaml) show the loads waiting.
"""

from datetime import datetime, timedelta, timezone

from custom_components.dynamic_ocpp_evse.calculations.outage_window import (
    precharge_note,
    precharge_target,
    precharge_window,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)


def _cal(state, start=None, end=None):
    """A calendar entity's state and attributes, as Home Assistant exposes them
    (the running event's, else the next one's)."""
    attrs = {}
    if start is not None:
        attrs["start_time"] = start.isoformat()
    if end is not None:
        attrs["end_time"] = end.isoformat()
    return state, attrs


def _window(state_attrs, now=NOW, lead=6, hold=1, memory=None):
    state, attrs = state_attrs
    return precharge_window(state, attrs, now, lead, hold, memory)


H = timedelta(hours=1)


# --- the pure rule -------------------------------------------------------------


def test_a_calendar_window_inside_the_lead_time_turns_it_on():
    start, end = NOW + 5 * H, NOW + 9 * H
    window, memory = _window(_cal("off", start, end))
    assert window == (start, end)
    assert memory is None  # nothing to hold yet: it has not run


def test_a_calendar_window_beyond_the_lead_time_does_nothing():
    window, _ = _window(_cal("off", NOW + 7 * H, NOW + 9 * H))
    assert window is None


def test_the_lead_time_is_the_settings():
    start = NOW + 3 * H
    assert _window(_cal("off", start, start + H), lead=2)[0] is None
    assert _window(_cal("off", start, start + H), lead=3)[0] is not None


def test_a_running_event_is_on_and_remembers_when_the_hold_ends():
    start, end = NOW - H, NOW + 3 * H
    window, memory = _window(_cal("on", start, end))
    assert window == (start, end)
    assert memory == (end + H, start, end)


def test_the_hold_keeps_it_on_after_the_window_then_releases():
    start, end = NOW - 3 * H, NOW - H / 2  # just ended; the calendar shows no event
    memory = (end + H, start, end)
    # 30 minutes after the end, inside the 1 h hold
    window, kept = _window(("off", {}), memory=memory)
    assert window == (start, end) and kept == memory
    # past the hold: off, and the memory is dropped
    window, kept = _window(("off", {}), now=NOW + H, memory=memory)
    assert window is None and kept is None


def test_a_zero_hold_releases_at_the_end():
    start, end = NOW - H, NOW + H
    _, memory = _window(_cal("on", start, end), hold=0)
    assert memory[0] == end
    assert _window(("off", {}), now=end, memory=memory, hold=0)[0] is None


def test_the_next_event_after_the_hold_does_not_extend_it():
    """An event that moves back out of the lead time is not held by an old
    window: the hold exists only from a window that actually ran."""
    far = _cal("off", NOW + 20 * H, NOW + 22 * H)
    assert _window(far)[0] is None


def test_a_rescheduled_event_drops_a_pending_pre_charge():
    start = NOW + 5 * H
    assert _window(_cal("off", start, start + H))[0] is not None
    later = NOW + 12 * H
    assert _window(_cal("off", later, later + H))[0] is None


def test_unavailable_or_unknown_is_no_window_and_clears_the_hold():
    memory = (NOW + H, NOW - H, NOW)
    for state in ("unavailable", "unknown", None, ""):
        assert _window((state, {"start_time": (NOW + H).isoformat()}), memory=memory) == (
            None,
            None,
        )


def test_a_binary_sensor_on_is_a_window_with_no_look_ahead():
    window, memory = _window(("on", {}))
    assert window == (None, None)
    assert memory == (NOW + H, None, None)  # held from the last cycle it was on
    assert _window(("off", {}))[0] is None  # no next_start: nothing ahead


def test_a_binary_sensor_with_next_start_gets_the_lead_time():
    start, end = NOW + 4 * H, NOW + 6 * H
    attrs = {"next_start": start.isoformat(), "next_end": end.isoformat()}
    assert _window(("off", attrs))[0] == (start, end)
    far = {"next_start": (NOW + 9 * H).isoformat()}
    assert _window(("off", far))[0] is None


def test_naive_local_times_are_read_in_the_local_zone():
    """Home Assistant writes a calendar's times as local 'YYYY-MM-DD HH:MM:SS'."""
    cest = timezone(timedelta(hours=2))
    now = datetime(2026, 8, 14, 10, 0, tzinfo=cest)
    window, _ = precharge_window(
        "off", {"start_time": "2026-08-14 14:00:00", "end_time": "2026-08-14 18:00:00"},
        now, 6, 1,
    )
    assert window[0] == datetime(2026, 8, 14, 14, 0, tzinfo=cest)


def test_garbage_attributes_are_no_look_ahead():
    assert _window(("off", {"start_time": "soon"}))[0] is None
    assert _window(("on", {"start_time": "soon"}))[0] == (None, None)


def test_the_target_is_full_and_never_lower_than_the_owners():
    assert precharge_target(80, 97) == 97
    assert precharge_target(99, 97) == 99


def test_the_status_note():
    cest = timezone(timedelta(hours=2))
    now = datetime(2026, 10, 19, 9, 0, tzinfo=cest)
    at = lambda d, h: datetime(2026, 10, d, h, 0, tzinfo=cest)
    assert precharge_note((at(19, 14), at(19, 18)), now) == (
        "Pre-charging for the planned outage 14:00-18:00"
    )
    assert precharge_note((at(20, 8), at(20, 12)), now) == (
        "Pre-charging for the planned outage 20.10. 08:00-12:00"
    )
    assert precharge_note((None, at(19, 18)), now) == (
        "Pre-charging for the planned outage until 18:00"
    )
    assert precharge_note((None, None), now) == "Pre-charging for the planned outage"


# --- the hub calculation -------------------------------------------------------

CAL = "calendar.planned_outages"
FROZEN = "2026-08-14 08:00:00+00:00"


def _rig(hass, **extra):
    """The forecast rig of test_forecast_excess (a grid CT, a 20 kWh battery, the
    clipping forecast), plus the planned-outage options."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.dynamic_ocpp_evse.const import DOMAIN
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    from .test_sensor_update import (
        _destination_hub,
        _destination_inverter,
        _set_destination_states,
    )

    base = _destination_hub("op")
    hub = MockConfigEntry(
        domain=DOMAIN, entry_id=base.entry_id, version=2, minor_version=4,
        title=base.title, data=dict(base.data),
        options={**base.options, **extra},
    )
    _destination_inverter(hass, hub, "op").add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }

    def states(soc=85):
        _set_destination_states(hass, soc=soc)
        hass.states.async_set(
            "sensor.dst_battery_soc", str(soc),
            {"device_class": "battery", "unit_of_measurement": "%"},
        )

    def calendar(state, start=None, end=None):
        attrs = {} if start is None else {
            "start_time": start.isoformat(), "end_time": end.isoformat()
        }
        hass.states.async_set(CAL, state, attrs)

    def run(at=FROZEN):
        from freezegun import freeze_time

        with freeze_time(at):
            return run_hub_calculation(hass, hub)

    return states, calendar, run


def _with_calendar(**more):
    return {"outage_entity_id": CAL, **more}


async def test_a_window_inside_the_lead_time_prepares_the_site(hass):
    states, calendar, run = _rig(hass, **_with_calendar())
    states()
    calendar("off", NOW + 4 * H, NOW + 8 * H)
    result = run()
    assert result["outage_precharge"] is True
    # the allocation runs on full (97 % by default): the battery wants to charge
    assert result["battery_soc_target"] == 97
    assert result["hub_status"] == "Pre-charging for the planned outage 12:00-16:00"
    # the clipping advice stands down: no ceiling below full, no rate cap
    assert result["forecast_battery_max_soc"] is None
    assert result["forecast_charge_limit_w"] is None
    assert all(
        section.get("forecast_battery_max_soc") is None
        and section.get("forecast_charge_limit_w") is None
        for section in result["inverters"].values()
    )


async def test_without_the_setting_nothing_changes(hass):
    # (target 77: the battery's 85 % is above the 80 % target, so its hysteresis
    # band is in force - the same figure every "unchanged" assertion here means)
    states, calendar, run = _rig(hass)
    states()
    calendar("on", NOW - H, NOW + 4 * H)  # the entity exists, the hub does not use it
    result = run()
    assert result["outage_precharge"] is False
    assert result["battery_soc_target"] == 77
    assert result["hub_status"] == "OK"
    # ... and the clipping advice is there, as it is for a site that uses the input
    assert result["forecast_battery_max_soc"] is not None


async def test_a_window_beyond_the_lead_time_changes_nothing(hass):
    states, calendar, run = _rig(hass, **_with_calendar())
    states()
    calendar("off", NOW + 9 * H, NOW + 12 * H)
    result = run()
    assert result["outage_precharge"] is False
    assert result["battery_soc_target"] == 77
    assert result["forecast_battery_max_soc"] is not None


async def test_an_unavailable_input_is_no_window(hass):
    states, calendar, run = _rig(hass, **_with_calendar())
    states()
    for state in ("unavailable", "unknown"):
        hass.states.async_set(CAL, state)
        result = run()
        assert result["outage_precharge"] is False
        assert result["battery_soc_target"] == 77
        assert result["hub_status"] == "OK"
    # and a hub whose entity does not exist at all
    hass.states.async_remove(CAL)
    assert run()["outage_precharge"] is False


async def test_it_is_back_to_normal_once_the_window_and_the_hold_are_over(hass):
    states, calendar, run = _rig(hass, **_with_calendar(outage_hold_hours=1))
    states()
    start, end = NOW - H, NOW + 2 * H
    calendar("on", start, end)
    assert run()["outage_precharge"] is True  # running

    after = (end + H / 2).isoformat()
    calendar("off")  # the window is over; the calendar has no next event
    result = run(after)
    assert result["outage_precharge"] is True  # inside the hold
    assert result["battery_soc_target"] == 97

    result = run((end + 2 * H).isoformat())
    assert result["outage_precharge"] is False
    assert result["battery_soc_target"] == 77
    assert result["hub_status"] == "OK"
    assert result["forecast_battery_max_soc"] is not None
    # and the memory is gone
    hub_runtime = next(iter(hass.data["dynamic_ocpp_evse"]["hubs"].values()))
    assert hub_runtime.get("_outage_hold") is None


async def test_a_real_fault_stays_the_status(hass):
    """The note stands in for 'OK' only."""
    states, calendar, run = _rig(
        hass, **_with_calendar(), base_consumption_entity_id="sensor.base_gone"
    )
    states()
    calendar("on", NOW - H, NOW + 4 * H)
    hass.states.async_set("sensor.base_gone", "unavailable")
    result = run()
    assert result["outage_precharge"] is True
    assert not result["hub_status"].startswith("Pre-charging")


async def test_an_off_grid_site_ignores_it(hass):
    states, calendar, run = _rig(
        hass, **_with_calendar(), phase_a_current_entity_id=None
    )
    states()
    calendar("on", NOW - H, NOW + 4 * H)
    result = run()
    assert result["outage_precharge"] is False
    assert result["battery_soc_target"] == 77


# --- the SOC write-control -----------------------------------------------------


def _soc(slots=None, normal=None, options=None):
    from .test_inverter_control import _arm_soc, _soc_entry, _soc_hass

    hass = _soc_hass(slots=slots, normal=normal)
    entry = _soc_entry(options)
    return hass, entry, _arm_soc(hass, entry)


def _send_soc(hass, entry, advice, now, lift):
    import asyncio

    from custom_components.dynamic_ocpp_evse.control.inverter import (
        send_inverter_soc_limit,
    )

    asyncio.run(send_inverter_soc_limit(hass, entry, advice, now, lift=lift))


def test_the_slots_are_lifted_to_full_then_restored():
    from custom_components.dynamic_ocpp_evse.const import (
        CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    )

    from .test_inverter_control import NORMAL_ENTITY, SOC_SLOTS, _soc_writes

    hass, entry, _ = _soc(
        slots={eid: 80 for eid in SOC_SLOTS},
        normal=80,
        options={CONF_SOC_LIMIT_NORMAL_ENTITY_ID: NORMAL_ENTITY},
    )
    # a clipping advice of 60 % is standing; the outage lifts over it
    _send_soc(hass, entry, 60.0, 0.0, lift=True)
    assert _soc_writes(hass) == [(eid, 100.0) for eid in SOC_SLOTS]

    for eid in SOC_SLOTS:
        hass.states.set(eid, 100)
    _send_soc(hass, entry, 60.0, 10.0, lift=False)  # window over: the ordinary rule
    assert _soc_writes(hass)[-3:] == [(eid, 60.0) for eid in SOC_SLOTS]

    for eid in SOC_SLOTS:
        hass.states.set(eid, 60)
    _send_soc(hass, entry, None, 20.0, lift=False)  # advice gone: back to the normal
    assert _soc_writes(hass)[-3:] == [(eid, 80.0) for eid in SOC_SLOTS]


def test_a_lift_with_slots_already_full_writes_nothing():
    from .test_inverter_control import SOC_SLOTS, _soc_writes

    hass, entry, rt = _soc()  # slots at 100, normal 100
    _send_soc(hass, entry, None, 0.0, lift=True)
    assert _soc_writes(hass) == []


def test_nothing_is_lifted_while_the_switch_is_off():
    from .test_inverter_control import SOC_SLOTS, _arm_soc, _soc_writes

    hass, entry, _ = _soc(slots={eid: 70 for eid in SOC_SLOTS})
    _arm_soc(hass, entry, enabled=False)
    _send_soc(hass, entry, None, 0.0, lift=True)
    assert _soc_writes(hass) == []


def test_no_lift_is_the_ordinary_behaviour():
    from .test_inverter_control import SOC_SLOTS, _soc_writes

    hass, entry, _ = _soc(slots={eid: 100 for eid in SOC_SLOTS})
    _send_soc(hass, entry, 70.0, 0.0, lift=False)
    assert _soc_writes(hass) == [(eid, 70.0) for eid in SOC_SLOTS]


def test_a_floor_register_is_lifted_and_may_be_lowered_again_only_by_us():
    """A discharge-floor register is only ever written in its safe direction, which
    forbids lowering an above-normal slot - except the lift we wrote ourselves."""
    from custom_components.dynamic_ocpp_evse.const import (
        CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
        CONF_SOC_LIMIT_SEMANTICS,
        SOC_LIMIT_SEMANTICS_FLOOR,
    )

    from .test_inverter_control import NORMAL_ENTITY, SOC_SLOTS, _soc_writes

    options = {
        CONF_SOC_LIMIT_SEMANTICS: SOC_LIMIT_SEMANTICS_FLOOR,
        CONF_SOC_LIMIT_NORMAL_ENTITY_ID: NORMAL_ENTITY,
    }
    hass, entry, rt = _soc(slots={eid: 40 for eid in SOC_SLOTS}, normal=40, options=options)
    _send_soc(hass, entry, None, 0.0, lift=True)
    assert _soc_writes(hass) == [(eid, 100.0) for eid in SOC_SLOTS]

    for eid in SOC_SLOTS:
        hass.states.set(eid, 100)
    _send_soc(hass, entry, None, 10.0, lift=False)
    assert _soc_writes(hass)[-3:] == [(eid, 40.0) for eid in SOC_SLOTS]

    # an owner's own raised slot (a storm hold) is still never lowered
    hass, entry, rt = _soc(slots={eid: 100 for eid in SOC_SLOTS}, normal=40, options=options)
    _send_soc(hass, entry, None, 0.0, lift=False)
    assert _soc_writes(hass) == []
