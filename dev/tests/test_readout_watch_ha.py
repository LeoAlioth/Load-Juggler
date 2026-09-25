"""A charger whose readout freezes while it keeps obeying its commands.

Machine-authored tests - not yet human-reviewed.

The engine half of the stuck-readout watch (engine/readout_watch.py has the
rule and its pure tests, dev/tests/test_readout_watch.py). What these pin is the
wiring, on the real site calculation with its feedback loop:

  * the limit the charger ACCEPTED is published for the engine - and only once
    the service call has returned - and forgotten when a profile or hard reset
    hands the charger back to its own default;
  * a reading frozen above what the charger was since told to deliver is
    replaced by the commanded draw, which reconstructs the household exactly
    and keeps a second charger from being sized against phantom headroom
    (the same frozen number, left alone, puts the site over its breaker);
  * the assumption is published as an ESTIMATE, marked so on every entity
    that shows it, the charger's status says it in words, the episode is
    announced with ONE warning, and it shows on the Available Current
    attributes - all of it back to normal when the episode ends;
  * a steady reading on a healthy charger is never controlled blind;
  * the reading moving again - twice - ends blind mode, and so does the car
    leaving.
"""

import logging
import time
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    CONF_CHARGE_PAUSE_DURATION,
    CONF_CHARGE_RATE_UNIT,
    CONF_CHARGER_ID,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_HUB_ENTRY_ID,
    CONF_LOAD_PRIORITY,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_OCPP_DEVICE_ID,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_UPDATE_FREQUENCY,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
    EVSE_RT_COMMANDED_LIMIT,
    EVSE_RT_COMMANDED_RATE_UNIT,
    EVSE_RT_READOUT_WATCH,
)
from custom_components.dynamic_ocpp_evse.engine import readout_watch
from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
    run_hub_calculation,
)

BREAKER = 25.0
HOUSEHOLD = 5.0      # A per phase, the part of the grid reading that is ours to keep
LEARNED_GAP = 10.0   # this charger's reading normally moves every 10 s


def _charger(hub_entry, slug, priority):
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title=slug,
        data={
            CONF_ENTITY_ID: slug,
            CONF_NAME: slug,
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: slug,
            CONF_OCPP_DEVICE_ID: f"{slug}_cp",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: f"sensor.{slug}_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: f"sensor.{slug}_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: priority,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_CHARGE_PAUSE_DURATION: 3,
        },
    )


@pytest.fixture
def site(hass):
    """A 25 A three-phase hub with two 16 A chargers, "first" ranked above
    "second". Returns (hub_entry, first, second)."""
    hub_entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_a",
            CONF_PHASE_B_CURRENT_ENTITY_ID: "sensor.grid_b",
            CONF_PHASE_C_CURRENT_ENTITY_ID: "sensor.grid_c",
            CONF_MAIN_BREAKER_RATING: BREAKER,
            CONF_PHASE_VOLTAGE: 230,
        },
    )
    first = _charger(hub_entry, "first", 1)
    second = _charger(hub_entry, "second", 2)
    hass.data[DOMAIN] = {
        "hubs": {
            hub_entry.entry_id: {
                "entry": hub_entry,
                "loads": [first.entry_id, second.entry_id],
                "distribution_mode": "Priority",
                "allow_grid_charging": True,
                "power_buffer": 0,
            }
        },
        "loads": {
            entry.entry_id: {
                "entry": entry,
                "hub_entry_id": hub_entry.entry_id,
                "dynamic_control": True,
            }
            for entry in (first, second)
        },
    }
    return hub_entry, first, second


def _set_world(hass, first_reading, first_true, status="Charging"):
    """Grid CTs see the TRUTH (household + what "first" really draws; "second"
    is connected and not yet drawing). "first" reports ``first_reading``."""
    for phase in "abc":
        hass.states.async_set(
            f"sensor.grid_{phase}", str(HOUSEHOLD + first_true),
            {"device_class": "current", "unit_of_measurement": "A"},
        )
    for slug, reading, state in (
        ("first", first_reading, status),
        ("second", 0.0, "Charging"),
    ):
        hass.states.async_set(
            f"sensor.{slug}_current_import", str(reading),
            {
                "device_class": "current", "unit_of_measurement": "A",
                "l1_current": reading, "l2_current": reading,
                "l3_current": reading,
            },
        )
        hass.states.async_set(f"sensor.{slug}_status_connector", state)
        hass.states.async_set(
            f"sensor.{slug}_current_offered", "16.0",
            {"device_class": "current", "unit_of_measurement": "A"},
        )


def _runtime(hass, entry):
    return hass.data[DOMAIN]["loads"][entry.entry_id]


def _frozen_long_ago(hass, entry, frozen, commanded, learned=True):
    """The charger was told ``commanded`` and has read ``frozen`` ever since -
    five minutes, thirty times its normal gap. ``learned`` says whether the
    watch has seen enough of this reading to know its cadence."""
    rt = _runtime(hass, entry)
    rt[EVSE_RT_COMMANDED_LIMIT] = commanded
    rt[EVSE_RT_COMMANDED_RATE_UNIT] = "A"
    long_ago = time.monotonic() - 300
    rt[EVSE_RT_READOUT_WATCH] = {
        "value": (frozen, frozen, frozen),
        "changed_at": long_ago,
        "clean": True,
        "ruled_out_since": long_ago,
        "gaps": [LEARNED_GAP] * readout_watch.MIN_GAPS if learned else [],
    }


def _calc(hass, hub_entry, first, second):
    return run_hub_calculation(hass, hub_entry, load_entries=[first, second])


def _fresh_filters(hass, hub_entry):
    """Forget the input EMAs. The feedback subtracts the managed draws through
    the same filter as the grid readings, so a switch from one draw figure to
    another blends in over EMA_TAU_S - right for the site, but a comparison of
    two single cycles has to start both from the same, empty filter."""
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id].pop("_ema_inputs", None)


# ── the command side ─────────────────────────────────────────────────────


async def test_an_accepted_command_is_published_for_the_engine(hass, site):
    """What the watch judges against is what the charger was actually sent -
    recorded after set_charge_rate returned, never before, never on failure."""
    from custom_components.dynamic_ocpp_evse.sensor import LoadJugglerDeviceSensor
    from custom_components.dynamic_ocpp_evse.control.ocpp import send_ocpp_command

    hub_entry, first, _ = site
    _set_world(hass, 10.0, 10.0)
    sensor = LoadJugglerDeviceSensor(hass, first, hub_entry, "first", "first")

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock, side_effect=Exception("charger said no"),
    ):
        await send_ocpp_command(sensor, 12.0, hub_entry, True, time.monotonic())
    assert EVSE_RT_COMMANDED_LIMIT not in _runtime(hass, first)

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await send_ocpp_command(sensor, 12.0, hub_entry, True, time.monotonic())
    assert _runtime(hass, first)[EVSE_RT_COMMANDED_LIMIT] == 12.0
    assert _runtime(hass, first)[EVSE_RT_COMMANDED_RATE_UNIT] == "A"


async def test_the_site_cycle_closes_the_loop(hass, site):
    """Through the real coordinator cycle: the load sends its profile, the
    accepted limit lands in its runtime bucket, and the NEXT engine cycle
    judges the frozen reading against it and goes blind."""
    from custom_components.dynamic_ocpp_evse.sensor import (
        LoadJugglerDeviceSensor,
        async_run_hub_cycle,
    )

    hub_entry, first, second = site
    _set_world(hass, first_reading=16.0, first_true=6.0)
    sensors = [
        LoadJugglerDeviceSensor(hass, entry, hub_entry, entry.title, entry.title)
        for entry in (first, second)
    ]
    processors = hass.data[DOMAIN].setdefault("load_processors", {}).setdefault(
        hub_entry.entry_id, {}
    )
    for sensor in sensors:
        processors[sensor.config_entry.entry_id] = sensor

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await async_run_hub_cycle(hass, hub_entry)
        sent = _runtime(hass, first).get(EVSE_RT_COMMANDED_LIMIT)
        assert sent is not None and sent == sensors[0]._last_commanded_limit

        # The charger obeys a 6 A cut; the reading has not moved since.
        _frozen_long_ago(hass, first, 16.0, 6.0)
        await async_run_hub_cycle(hass, hub_entry)

    assert readout_watch.is_stuck(_runtime(hass, first)[EVSE_RT_READOUT_WATCH])
    attrs = sensors[0].extra_state_attributes
    assert attrs["readout_stuck"] is True
    assert attrs["readout_assumed_current"] == [6.0, 6.0, 6.0]
    # The other charger's attributes say nothing is wrong with IT.
    assert sensors[1].extra_state_attributes["readout_stuck"] is False


async def test_a_profile_reset_forgets_the_recorded_command(hass, site):
    """clear_profile hands the charger back to its own default limit, so the
    last accepted command stops being the limit in force until the next one
    lands - and the watch must not judge the reading against it meanwhile."""
    from custom_components.dynamic_ocpp_evse import async_setup

    hub_entry, first, _ = site
    first.add_to_hass(hass)
    _runtime(hass, first)[EVSE_RT_COMMANDED_LIMIT] = 6.0
    await async_setup(hass, {})
    with patch("custom_components.dynamic_ocpp_evse.Script") as script:
        script.return_value.async_run = AsyncMock()
        await hass.services.async_call(
            DOMAIN, "reset_ocpp_evse", {"entry_id": first.entry_id}, blocking=True
        )
    script.return_value.async_run.assert_awaited_once()
    assert EVSE_RT_COMMANDED_LIMIT not in _runtime(hass, first)


async def test_a_hard_reset_forgets_the_recorded_command(hass, site):
    from custom_components.dynamic_ocpp_evse.control.compliance import (
        perform_hard_reset,
    )
    from custom_components.dynamic_ocpp_evse.sensor import LoadJugglerDeviceSensor

    hub_entry, first, _ = site
    _set_world(hass, 16.0, 6.0)
    hass.states.async_set("button.first_reset", "unknown")
    _runtime(hass, first)[EVSE_RT_COMMANDED_LIMIT] = 6.0
    sensor = LoadJugglerDeviceSensor(hass, first, hub_entry, "first", "first")
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await perform_hard_reset(sensor)
    assert EVSE_RT_COMMANDED_LIMIT not in _runtime(hass, first)


# ── entering blind mode, and what it buys ────────────────────────────────


async def test_a_frozen_readout_is_controlled_blind_inside_the_breaker(
    hass, site, caplog
):
    """"first" was cut to 6 A and obeys; its reading stayed at 16 A.

    Left alone, the frozen 16 A is subtracted from a grid reading that only
    holds 6 A of it: the household reads 0 instead of 5 A, and "second" is
    permitted into current the breaker does not have. Blind, the subtraction
    is the 6 A it was told, the household is exact, and every permit fits."""
    hub_entry, first, second = site
    _set_world(hass, first_reading=16.0, first_true=6.0)

    # Control: the same frozen number, but the watch has never learned this
    # charger's cadence, so it (rightly) does not judge - today's behaviour.
    _frozen_long_ago(hass, first, 16.0, 6.0, learned=False)
    unjudged = _calc(hass, hub_entry, first, second)
    over = HOUSEHOLD + sum(unjudged["load_available"].values())
    assert over > BREAKER, (
        f"the phantom headroom this fixes: permits {unjudged['load_available']} "
        f"on {HOUSEHOLD} A of household against a {BREAKER} A breaker"
    )

    _fresh_filters(hass, hub_entry)
    _frozen_long_ago(hass, first, 16.0, 6.0)
    with caplog.at_level(logging.WARNING):
        blind = _calc(hass, hub_entry, first, second)

    permits = blind["load_available"]
    assert HOUSEHOLD + sum(permits.values()) <= BREAKER, permits
    # The household is reconstructed from the command, exactly: grid headroom
    # is breaker minus the real 5 A, where the frozen number read the whole
    # breaker as free.
    assert blind["available_grid_power"] == (BREAKER - HOUSEHOLD) * 3 * 230
    assert unjudged["available_grid_power"] == BREAKER * 3 * 230
    # The assumption is PUBLISHED as an estimate - the charger's own draw,
    # Current Managed Power and the household all carry the 6 A it was told -
    # and marked as one (the entity half is pinned further down).
    assert blind["load_draw"][first.entry_id] == pytest.approx(3 * 6.0)
    assert blind["total_evse_power"] == pytest.approx(3 * 6.0 * 230)
    assert blind["household_power"] == pytest.approx(3 * HOUSEHOLD * 230)
    assert blind["draw_estimated"][first.entry_id]["evidence"] == "above_limit"
    assert first.entry_id not in (unjudged.get("draw_estimated") or {})
    warnings = [r for r in caplog.records if "readout looks stuck" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert warnings[0].levelno == logging.WARNING

    # Next cycles: still blind, and not a word more.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        again = _calc(hass, hub_entry, first, second)
    assert not [r for r in caplog.records if "readout looks stuck" in r.getMessage()]
    assert HOUSEHOLD + sum(again["load_available"].values()) <= BREAKER
    assert readout_watch.is_stuck(_runtime(hass, first)[EVSE_RT_READOUT_WATCH])


async def test_a_car_under_its_command_behind_a_frozen_reading(hass, site):
    """The case blind mode cannot see: "first" was told 6 A but its car takes
    only 3 A of it (its own limit), and the reading is frozen at 16 A.

    Assuming the full command is the safe side for the RESERVATION, and the
    site holds with every other load at its permit even if the car takes all
    6 A it was told. What it costs is on the feedback side: the household
    reads low by the 3 A the car leaves unused, and that much reaches "first"'s
    OWN permit - which is why it is pinned here as a bound, not ignored. The
    frozen 16 A, left alone, overstates it by 13 A."""
    hub_entry, first, second = site
    commanded, true_draw = 6.0, 3.0
    _set_world(hass, first_reading=16.0, first_true=true_draw)

    _frozen_long_ago(hass, first, 16.0, commanded, learned=False)
    unjudged = _calc(hass, hub_entry, first, second)["load_available"]
    _fresh_filters(hass, hub_entry)
    _frozen_long_ago(hass, first, 16.0, commanded)
    blind = _calc(hass, hub_entry, first, second)["load_available"]

    # Everything else stays inside the breaker at the charger's full command.
    assert HOUSEHOLD + commanded + blind[second.entry_id] <= BREAKER, blind
    # The unseen part of the command is the whole of the error, on "first"
    # itself: its permit exceeds the real headroom by at most that much.
    real_headroom = BREAKER - HOUSEHOLD - blind[second.entry_id]
    assert blind[first.entry_id] <= real_headroom + (commanded - true_draw), blind
    # Left alone, the frozen reading handed the SECOND charger phantom current.
    assert HOUSEHOLD + commanded + unjudged[second.entry_id] > BREAKER, unjudged


async def test_blind_mode_shows_on_the_available_current_attributes(hass, site):
    from custom_components.dynamic_ocpp_evse.sensor import LoadJugglerDeviceSensor

    hub_entry, first, second = site
    _set_world(hass, first_reading=16.0, first_true=6.0)
    sensor = LoadJugglerDeviceSensor(hass, first, hub_entry, "first", "first")
    assert sensor.extra_state_attributes["readout_stuck"] is False

    _frozen_long_ago(hass, first, 16.0, 6.0)
    _calc(hass, hub_entry, first, second)

    attrs = sensor.extra_state_attributes
    assert attrs["readout_stuck"] is True
    assert attrs["readout_stuck_since"] is not None
    assert attrs["readout_stuck_value"] == [16.0, 16.0, 16.0]
    assert attrs["readout_assumed_current"] == [6.0, 6.0, 6.0]
    assert attrs["readout_normal_gap_seconds"] == LEARNED_GAP


async def test_a_steady_reading_on_a_healthy_charger_is_never_blind(hass, site):
    """A car sitting at 10 A by its own choice, reading 10 A bit for bit for
    five minutes against a 16 A limit: steady is not stuck, and its draw is
    published and trusted as it always was."""
    hub_entry, first, second = site
    _set_world(hass, first_reading=10.0, first_true=10.0)
    _frozen_long_ago(hass, first, 10.0, 16.0)
    _runtime(hass, first)[EVSE_RT_READOUT_WATCH]["ruled_out_since"] = None

    for _ in range(5):
        result = _calc(hass, hub_entry, first, second)
    assert not readout_watch.is_stuck(_runtime(hass, first)[EVSE_RT_READOUT_WATCH])
    assert result["load_draw"][first.entry_id] == 30.0


async def test_a_suspended_car_behind_a_frozen_reading_is_booked_at_zero(hass, site):
    """The car finished (SuspendedEV) and the reading stayed at 16 A. Blind,
    it is booked at 0 A - which is also what lets the SuspendedEV idle timer
    run at last; the frozen 16 A kept it from ever starting."""
    hub_entry, first, second = site
    _set_world(hass, first_reading=16.0, first_true=0.0, status="SuspendedEV")
    _frozen_long_ago(hass, first, 16.0, 16.0)

    _calc(hass, hub_entry, first, second)
    rt = _runtime(hass, first)
    assert readout_watch.is_stuck(rt[EVSE_RT_READOUT_WATCH])
    assert rt[EVSE_RT_READOUT_WATCH]["assumed"] == (0.0, 0.0, 0.0)
    assert "_suspended_ev_since" in rt


# ── leaving it ───────────────────────────────────────────────────────────


async def test_blind_mode_ends_once_the_readout_moves_again(hass, site, caplog):
    hub_entry, first, second = site
    _set_world(hass, first_reading=16.0, first_true=6.0)
    _frozen_long_ago(hass, first, 16.0, 6.0)
    _calc(hass, hub_entry, first, second)
    watch = _runtime(hass, first)[EVSE_RT_READOUT_WATCH]
    assert readout_watch.is_stuck(watch)

    # One fresh value is not a reading that moves...
    _set_world(hass, first_reading=6.2, first_true=6.2)
    _calc(hass, hub_entry, first, second)
    assert readout_watch.is_stuck(watch)

    # ...two are.
    _set_world(hass, first_reading=5.9, first_true=5.9)
    with caplog.at_level(logging.INFO):
        recovered = _calc(hass, hub_entry, first, second)
    assert not readout_watch.is_stuck(watch)
    assert recovered["load_draw"][first.entry_id] == pytest.approx(17.7)
    assert any("moving again" in r.getMessage() for r in caplog.records)
    assert watch.get("assumed") is None


async def test_unplugging_ends_blind_mode(hass, site, caplog):
    hub_entry, first, second = site
    _set_world(hass, first_reading=16.0, first_true=6.0)
    _frozen_long_ago(hass, first, 16.0, 6.0)
    _calc(hass, hub_entry, first, second)
    watch = _runtime(hass, first)[EVSE_RT_READOUT_WATCH]
    assert readout_watch.is_stuck(watch)

    _set_world(hass, first_reading=16.0, first_true=0.0, status="Available")
    with caplog.at_level(logging.INFO):
        _calc(hass, hub_entry, first, second)
    assert not readout_watch.is_stuck(watch)
    assert any("leaving blind mode" in r.getMessage() for r in caplog.records)
    # What it learned about the charger outlives the session.
    assert readout_watch.normal_gap(watch) == LEARNED_GAP


# ── what the user sees: estimates, marked, and a status that says so ─────


async def test_while_blind_the_figures_are_estimates_and_the_status_says_so(hass, site):
    """Through the real site cycle and the real entities.

    Blind, Current Managed Power, Household Power and the charger's own
    Allocated Current and Phase Mask carry the ASSUMED draw rather than going
    unknown, and every one of them says ``estimated``; the Charging Status
    says in words that the readout is stuck. The cycle the readout moves again
    (twice), all of it is back to a measurement and a plain status."""
    from custom_components.dynamic_ocpp_evse.entities.hub import (
        HUB_SENSOR_DEFINITIONS,
        LoadJugglerHubDataSensor,
    )
    from custom_components.dynamic_ocpp_evse.entities.load_sensors import (
        LoadJugglerAllocatedCurrentSensor,
        LoadJugglerDeviceStatusSensor,
        LoadJugglerPhaseMaskSensor,
    )
    from custom_components.dynamic_ocpp_evse.entities.readout import READOUT_STUCK_NOTE
    from custom_components.dynamic_ocpp_evse.sensor import (
        LoadJugglerDeviceSensor,
        async_run_hub_cycle,
    )

    hub_entry, first, second = site
    defs = {d["hub_data_key"]: d for d in HUB_SENSOR_DEFINITIONS}
    managed = LoadJugglerHubDataSensor(hass, hub_entry, "Hub", "hub", defs["total_evse_power"])
    household = LoadJugglerHubDataSensor(hass, hub_entry, "Hub", "hub", defs["household_power"])
    solar = LoadJugglerHubDataSensor(hass, hub_entry, "Hub", "hub", defs["solar_power"])
    status = LoadJugglerDeviceStatusSensor(hass, first, hub_entry, "first", "first")
    allocated = LoadJugglerAllocatedCurrentSensor(hass, first, hub_entry, "first", "first")
    mask = LoadJugglerPhaseMaskSensor(hass, first, hub_entry, "first", "first")
    other_status = LoadJugglerDeviceStatusSensor(hass, second, hub_entry, "second", "second")
    readers = (managed, household, solar, status, allocated, mask, other_status)

    processors = hass.data[DOMAIN].setdefault("load_processors", {}).setdefault(
        hub_entry.entry_id, {}
    )
    for entry in (first, second):
        processors[entry.entry_id] = LoadJugglerDeviceSensor(
            hass, entry, hub_entry, entry.title, entry.title
        )

    async def cycle():
        await async_run_hub_cycle(hass, hub_entry)
        for reader in readers:
            reader._read_site_data()

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        # A healthy cycle first: measurements, nothing estimated.
        _set_world(hass, first_reading=6.0, first_true=6.0)
        await cycle()
        assert managed.extra_state_attributes["estimated"] is False
        assert household.extra_state_attributes["estimated"] is False
        assert solar.extra_state_attributes is None      # not a draw-netting figure
        plain = status.native_value
        assert plain == "Charging", plain
        assert READOUT_STUCK_NOTE not in plain
        assert status.extra_state_attributes["readout_stuck"] is False
        assert allocated.extra_state_attributes["estimated"] is False

        # The readout freezes at 16 A after the charger was cut to 6 A.
        _set_world(hass, first_reading=16.0, first_true=6.0)
        _frozen_long_ago(hass, first, 16.0, 6.0)
        await cycle()

        assumed_w = 3 * 6.0 * 230
        assert managed.native_value == pytest.approx(assumed_w)
        attrs = managed.extra_state_attributes
        assert attrs["estimated"] is True
        assert attrs["estimated_loads"] == ["first"]
        assert attrs["estimate_evidence"] == ["above_limit"]
        assert attrs["estimated_since"] is not None
        assert household.native_value == pytest.approx(3 * HOUSEHOLD * 230)
        assert household.extra_state_attributes["estimated"] is True

        assert status.native_value == f"{plain} ({READOUT_STUCK_NOTE})"
        s_attrs = status.extra_state_attributes
        assert s_attrs["readout_stuck"] is True
        assert s_attrs["readout_stuck_evidence"] == "above_limit"
        assert s_attrs["readout_assumed_current"] == [6.0, 6.0, 6.0]
        assert allocated.extra_state_attributes["estimated"] is True
        assert allocated.extra_state_attributes["estimate_evidence"] == "above_limit"
        assert mask.extra_state_attributes["estimated"] is True
        assert mask.native_value == "ABC"
        # The other charger is not the one with the stuck readout.
        assert READOUT_STUCK_NOTE not in other_status.native_value

        # The readout moves again - twice - and everything is a measurement.
        for reading in (6.2, 5.9):
            _set_world(hass, first_reading=reading, first_true=reading)
            await cycle()

    assert managed.native_value == pytest.approx(3 * 5.9 * 230)
    for reader in (managed, household):
        assert reader.extra_state_attributes == {
            "estimated": False, "estimated_loads": None,
            "estimate_evidence": None, "estimated_since": None,
        }
    assert status.native_value == plain
    assert status.extra_state_attributes["readout_stuck"] is False
    assert allocated.extra_state_attributes["estimated"] is False
    assert mask.extra_state_attributes["estimated"] is False


async def test_the_overview_marks_estimates_and_the_stuck_readout(hass, site):
    """The Overview page speaks the same words as the entities: the charger's
    draw and the site totals read "(estimated)", and its status carries the
    stuck-readout note - for as long as the episode lasts."""
    from custom_components.dynamic_ocpp_evse.config_flow import pages
    from custom_components.dynamic_ocpp_evse.entities.readout import READOUT_STUCK_NOTE

    hub_entry, first, _ = site
    runtime = hass.data[DOMAIN]
    runtime["load_status"] = {first.entry_id: "Charging"}
    hub_data = {"draw_estimated": {}}
    assert pages._load_status(runtime, first.entry_id) == "Charging"
    assert pages._estimated(hub_data, first.entry_id) == ""

    _frozen_long_ago(hass, first, 16.0, 6.0)
    _runtime(hass, first)[EVSE_RT_READOUT_WATCH]["stuck"] = True
    hub_data = {"draw_estimated": {first.entry_id: {"load": "first"}}}
    assert pages._load_status(runtime, first.entry_id) == (
        f"Charging ({READOUT_STUCK_NOTE})"
    )
    assert pages._estimated(hub_data, first.entry_id) == " (estimated)"
    assert pages._estimated(hub_data) == " (estimated)"
