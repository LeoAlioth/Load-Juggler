"""Tanks on the forecast verdict must not start and stop in lockstep on a surplus
that cannot carry them.

Machine-authored tests - not yet human-reviewed.

Kozolec (off-grid, a Victron pack), 20 Oct 2026, 08:00-08:47 UTC: SOC 82 % over
a 55 % target, 450 W of sun on a 90 W house, the planning sensor saying 6.6 kWh
still to place and the pack next full in 3 h. The 1.886 kW boiler and the 1.03 kW
Bee Room Heater started in the same second, took 2.6-2.9 kW out of the pack for
the tanks' five-minute minimum, were withdrawn together ("Waiting for Power"),
rested five minutes and started again - five cycles in 47 minutes (11 Oct, 17-18
Oct and 19 Oct the same).

Everything here runs the REAL site cycle against a small off-grid world, as
test_offgrid_battery_headroom.py does, with two tanks in the car's place.
"""

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import INPUT_GRID, input_options, with_inverter

from custom_components.dynamic_ocpp_evse import sensor as sensor_platform
from custom_components.dynamic_ocpp_evse.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_CLIMATE_ENTITY_ID,
    CONF_CONNECTED_TO_PHASE,
    CONF_DEVICE_TYPE,
    CONF_EARLY_RATE_ENTITY_ID,
    CONF_EARLY_ROOM_ENTITY_ID,
    CONF_ENTITY_ID,
    CONF_FORECAST_EXCESS_ENABLED,
    CONF_HEATING_ELEMENT_POWER,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_LOAD_PRIORITY,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PACING_ENERGY_ENTITY_ID,
    CONF_PACING_HORIZON_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_TANK_AWAY_TEMPERATURE,
    CONF_TANK_BOOST_TEMPERATURE,
    CONF_TANK_NORMAL_TEMPERATURE,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DEVICE_TYPE_HOT_WATER_TANK,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
    TANK_MODE_NORMAL,
)

from .closed_loop import clocked

V = 230.0
CYCLE_S = float(DEFAULT_SITE_UPDATE_FREQUENCY)


class Tank:
    """One tank: its element draws ``element_w`` while its setpoint is above its water."""

    def __init__(self, name, *, element_w, away, normal, boost, water_c, priority,
                 room_kwh=None, rate_kw=None):
        self.name, self.element_w = name, element_w
        self.away, self.normal, self.boost = away, normal, boost
        self.water_c, self.priority = water_c, priority
        self.room_kwh, self.rate_kw = room_kwh, rate_kw
        self.setpoint = normal
        self.climate = f"climate.{name}"

    @property
    def heating(self):
        return self.setpoint > self.water_c

    @property
    def w(self):
        return self.element_w if self.heating else 0.0


def boiler(**kw):
    return Tank("boiler", element_w=1886.0, away=7.0, normal=42.0, boost=75.0,
                water_c=67.9, priority=7, **kw)


def heater(**kw):
    return Tank("heater", element_w=1026.0, away=7.0, normal=18.0, boost=24.0,
                water_c=20.0, priority=6, **kw)


class World:
    """Kozolec's morning: a 90 W house, a little sun, and a battery that carries
    the rest."""

    def __init__(self, hass, tanks, *, solar_w=450.0, house_w=90.0, soc=82.0,
                 planned_kwh=6.6, hours_to_full=3.0):
        self.hass, self.tanks = hass, tanks
        self.solar_w, self.house_w, self.soc = solar_w, house_w, soc
        self.planned_kwh, self.hours_to_full = planned_kwh, hours_to_full

    @property
    def battery_w(self):
        # Off-grid, + is discharge: the pack carries what the sun does not.
        return self.house_w + sum(t.w for t in self.tanks) - self.solar_w

    def publish(self):
        set_state = self.hass.states.async_set
        set_state("sensor.solar_production", str(self.solar_w),
                  {"device_class": "power", "unit_of_measurement": "W"})
        set_state("sensor.battery_power", str(round(self.battery_w, 1)),
                  {"device_class": "power", "unit_of_measurement": "W"})
        set_state("sensor.battery_soc", str(self.soc),
                  {"device_class": "battery", "unit_of_measurement": "%"})
        set_state("sensor.planned_kwh", str(self.planned_kwh),
                  {"device_class": "energy", "unit_of_measurement": "kWh"})
        full = dt_util.utcnow() + timedelta(hours=self.hours_to_full)
        set_state("sensor.next_full", full.isoformat(), {"device_class": "timestamp"})
        for t in self.tanks:
            if t.room_kwh is not None:
                set_state(f"sensor.{t.name}_room", str(t.room_kwh),
                          {"device_class": "energy", "unit_of_measurement": "kWh"})
            if t.rate_kw is not None:
                set_state(f"sensor.{t.name}_rate", str(t.rate_kw),
                          {"device_class": "power", "unit_of_measurement": "kW"})
            set_state(t.climate, "heat", {
                "temperature": t.setpoint,
                "current_temperature": t.water_c,
                "hvac_action": "heating" if t.heating else "idle",
                "min_temp": 5, "max_temp": 80,
            })

    async def accept(self, domain, service, data=None, *args, **kwargs):
        if domain == "climate" and service == "set_temperature":
            for t in self.tanks:
                if data.get("entity_id") in (t.climate, [t.climate]):
                    t.setpoint = float(data["temperature"])


async def _morning(hass, tanks, *, minutes=47, **world):
    hub = with_inverter(hass, MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_VOLTAGE: V,
            CONF_MAIN_BREAKER_RATING: 40,
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.solar_production",
            CONF_INVERTER_MAX_POWER: 15000,
            **input_options(INPUT_GRID),
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.battery_power",
            CONF_BATTERY_MAX_DISCHARGE_POWER: 9000,
            CONF_BATTERY_MAX_CHARGE_POWER: 3000,
            CONF_FORECAST_EXCESS_ENABLED: True,
            CONF_PACING_ENERGY_ENTITY_ID: "sensor.planned_kwh",
            CONF_PACING_HORIZON_ENTITY_ID: "sensor.next_full",
        },
    ))
    entries = []
    for t in tanks:
        entries.append(MockConfigEntry(
            domain=DOMAIN, version=2, minor_version=2, title=t.name,
            data={
                CONF_ENTITY_ID: t.name,
                CONF_NAME: t.name,
                ENTRY_TYPE: ENTRY_TYPE_LOAD,
                CONF_DEVICE_TYPE: DEVICE_TYPE_HOT_WATER_TANK,
                CONF_CLIMATE_ENTITY_ID: t.climate,
                CONF_HUB_ENTRY_ID: hub.entry_id,
            },
            options={
                CONF_CONNECTED_TO_PHASE: "A",
                CONF_LOAD_PRIORITY: t.priority,
                CONF_HEATING_ELEMENT_POWER: t.element_w,
                CONF_TANK_AWAY_TEMPERATURE: t.away,
                CONF_TANK_NORMAL_TEMPERATURE: t.normal,
                CONF_TANK_BOOST_TEMPERATURE: t.boost,
                **({CONF_EARLY_ROOM_ENTITY_ID: f"sensor.{t.name}_room"} if t.room_kwh is not None else {}),
                **({CONF_EARLY_RATE_ENTITY_ID: f"sensor.{t.name}_rate"} if t.rate_kw is not None else {}),
            },
        ))
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {
            "entry": hub, "loads": [e.entry_id for e in entries],
            "distribution_mode": "Priority", "allow_grid_charging": True,
            "power_buffer": 0, "battery_soc_min": 10, "battery_soc_target": 55,
        }},
        "loads": {e.entry_id: {
            "entry": e, "hub_entry_id": hub.entry_id, "dynamic_control": True,
            "operating_mode": TANK_MODE_NORMAL.key,
        } for e in entries},
    }
    procs = hass.data[DOMAIN].setdefault("load_processors", {}).setdefault(hub.entry_id, {})
    for t, e in zip(tanks, entries):
        procs[e.entry_id] = sensor_platform.LoadJugglerDeviceSensor(hass, e, hub, t.name, t.name)
    w = World(hass, tanks, **world)
    log = []
    with clocked(w.accept) as clock:
        for _ in range(int(minutes * 60 / CYCLE_S)):
            w.publish()
            await sensor_platform.async_run_hub_cycle(hass, hub)
            log.append((clock.now, [t.setpoint for t in tanks], w.battery_w))
            clock.now += CYCLE_S
    return log


def _boosts(log, i):
    """How many times tank ``i`` was sent to its boost temperature."""
    return sum(1 for a, b in zip(log, log[1:]) if a[1][i] != b[1][i] and b[1][i] > a[1][i])


def _show(log):
    print([(round(t), s, round(b)) for t, s, b in log][::6])


async def test_one_tank_with_no_room_or_rate_fields_boosts_and_stays(hass):
    """The plain case, for reference: a tank with no Load Insights fields is due on
    the forecast verdict (6.6 kWh at its 1.886 kW over 3 h) and simply runs."""
    log = await _morning(hass, [boiler()])
    _show(log)
    assert _boosts(log, 0) <= 1


async def test_a_group_the_paced_rate_cannot_carry_does_not_start(hass):
    """Kozolec's morning: the boiler (room 2.23 kWh, rate 1.583 kW) and the Bee
    Room Heater (room 4.27 kWh, rate 0.52 kW) are the early-start group, 6.5 of the
    6.6 planned kWh between them, 3 h to full: 2.17 kW of pace against a group that
    pulls 2.10 kW. Inside the 10 % band the group does not start - before, it
    started on the verdict, the boiler was withdrawn after its minimum run by the
    pool the heater had already claimed, and it started again after its rest."""
    log = await _morning(hass, [boiler(room_kwh=2.23, rate_kw=1.583),
                                heater(room_kwh=4.27, rate_kw=0.52)])
    _show(log)
    assert (_boosts(log, 0), _boosts(log, 1)) == (0, 0)
    assert all(setpoints == [42.0, 18.0] for _, setpoints, *_ in log)


async def test_a_due_group_runs_through_without_cycling(hass):
    """The same morning with 8 kWh planned and 3.0 + 5.0 kWh of room: 2.67 kW of
    pace against 2.10 kW (a 27 % margin). Both start together and stay on - not
    one withdrawal while the pace holds above 0.9 of the group's power."""
    log = await _morning(hass, [boiler(room_kwh=3.0, rate_kw=1.583),
                                heater(room_kwh=5.0, rate_kw=0.52)], planned_kwh=8.0)
    _show(log)
    assert all(setpoints == [75.0, 24.0] for _, setpoints, *_ in log)
