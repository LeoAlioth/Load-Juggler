"""Off-grid, an inverter output that nothing splits into solar and battery.

Machine-authored tests - not yet human-reviewed.

Off-grid an inverter's output is everything the site draws from it: its
panels' production plus its battery's flow, on either wiring (adf15e1). The
battery power sensor is what takes the two apart - ``fleet.member_solar``
derives the production as output less battery power. With a battery
configured but its power not read (no power sensor, or one unreadable with
nothing to hold), nothing does, and the "solar" is the whole output: at night,
every watt of it the battery's.

That figure stays in the CONTROL path - the off-grid pool is built from it
(``target_calculator._calculate_inverter_limit``, solar + rating with the
battery unread), and taking it away would cut the chargers off. What it must
not be is PUBLISHED as production - on Current Solar Power, the inverter's
Solar Production sensor, the Overview and in long-term statistics - nor learned
from by the forecast observers (gain, peakiness, clipped energy). The same
contract as a production sensor that is configured but dead
(``FleetMember.solar_assumed``): the engine keeps its figure, the publication
reads unknown.

The rig: one inverter entry off-grid, rated 6 kW, output metered on phase A
at 10 A (2300 W), battery SOC read and battery power not, and a per-inverter
forecast device of 7 kW - the shape the gain observer keys on.
"""

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    CONF_BASE_CONSUMPTION,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PHASE_VOLTAGE,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_WIRING_TOPOLOGY,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
    WIRING_TOPOLOGY_PARALLEL,
    WIRING_TOPOLOGY_SERIES,
)

V = 230.0
OUTPUT_A = 10.0
OUTPUT_W = OUTPUT_A * V  # 2300 W out of the inverter, however it is made


def _forecast_device(hass, slug, watts):
    """A per-array Open-Meteo device with a watts-bearing sensor, as the
    integration creates them (test_sensor_update.py's rig). Returns its id."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    source = MockConfigEntry(domain="open_meteo_solar_forecast", title=slug)
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("open_meteo_solar_forecast", slug)},
        name=slug,
    )
    reg = er.async_get(hass).async_get_or_create(
        "sensor",
        "open_meteo_solar_forecast",
        f"{slug}_energy_production_today",
        device_id=device.id,
        config_entry=source,
        suggested_object_id=f"{slug}_energy_production_today",
    )
    hass.states.async_set(
        reg.entity_id, "12.5", {"unit_of_measurement": "kWh", "watts": dict(watts)}
    )
    return device.id


def _rig(hass, slug, topology, soc, *, battery=True):
    """No grid CT; one inverter entry with its output metered on A. With
    ``battery`` its SOC is read and its power is not; without, it has no
    battery entity at all."""
    hub = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=8, title=f"Unsplit {slug}",
        data={CONF_NAME: f"Unsplit {slug}", CONF_ENTITY_ID: f"us_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={CONF_MAIN_BREAKER_RATING: 40, CONF_PHASE_VOLTAGE: int(V),
                 CONF_BASE_CONSUMPTION: 250},
    )
    hub.add_to_hass(hass)
    device_id = _forecast_device(
        hass, f"us_{slug}_array",
        {"2026-08-14T10:00:00+00:00": 7000, "2026-08-14T11:00:00+00:00": 0},
    )
    options = {
        CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: f"sensor.us_{slug}_out_a",
        CONF_INVERTER_MAX_POWER: 6000,
        CONF_WIRING_TOPOLOGY: topology,
        CONF_SOLAR_FORECAST_DEVICE_IDS: [device_id],
    }
    if battery:
        options.update({
            CONF_BATTERY_SOC_ENTITY_ID: f"sensor.us_{slug}_soc",
            CONF_BATTERY_CAPACITY_KWH: 9.5,
            CONF_BATTERY_MAX_CHARGE_POWER: 4000,
        })
    inverter = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=8, title=f"Unsplit Inv {slug}",
        data={CONF_NAME: f"Unsplit Inv {slug}", CONF_ENTITY_ID: f"us_inv_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_INVERTER, CONF_HUB_ENTRY_ID: hub.entry_id},
        options=options,
    )
    inverter.add_to_hass(hass)
    hass.data[DOMAIN] = {"hubs": {hub.entry_id: {"loads": []}}, "loads": {},
                         "load_allocations": {}, "inverters": {}}
    hass.states.async_set(
        f"sensor.us_{slug}_out_a", f"{OUTPUT_A:.1f}",
        {"device_class": "current", "unit_of_measurement": "A"})
    hass.states.async_set(
        f"sensor.us_{slug}_soc", soc,
        {"device_class": "battery", "unit_of_measurement": "%"})
    return hub, inverter


def _sensors(hass, hub, inverter):
    """Current Solar Power and the inverter's Solar Production - the real
    sensors the platform builds."""
    from custom_components.dynamic_ocpp_evse.entities.inverter import (
        INVERTER_SENSOR_DEFINITIONS,
        LoadJugglerInverterDataSensor,
    )
    from custom_components.dynamic_ocpp_evse.sensor import (
        DynamicOcppEvseHubDataSensor,
        HUB_SENSOR_DEFINITIONS,
    )

    hub_defn = next(
        d for d in HUB_SENSOR_DEFINITIONS if d["hub_data_key"] == "solar_power"
    )
    inv_defn = next(
        d for d in INVERTER_SENSOR_DEFINITIONS if d["data_key"] == "solar_w"
    )
    return {
        "hub": DynamicOcppEvseHubDataSensor(hass, hub, "Unsplit", "us", hub_defn),
        "inverter": LoadJugglerInverterDataSensor(hass, inverter, "us_inv", inv_defn),
    }


async def _cycle(hass, hub, inverter):
    """Two cycles a minute apart inside the forecast block (the first only
    stamps the observers' monotonic clock, which freezegun freezes too),
    published as the hub coordinator publishes and read by the sensors on the
    same clock. Returns the solar figure the ENGINE computed with on the last
    cycle, and each sensor's ``(value, available)``."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine import hub_calculation
    from custom_components.dynamic_ocpp_evse.entities.hub import publish_hub_data

    engine_solar = []
    real = hub_calculation.calculate_all_load_targets

    def capture(site):
        engine_solar.append(site.solar_production_total)
        return real(site)

    sensors = _sensors(hass, hub, inverter)
    published = {}
    with patch.object(hub_calculation, "calculate_all_load_targets", capture):
        with freeze_time("2026-08-14 10:30:00+00:00") as frozen:
            hub_calculation.run_hub_calculation(hass, hub)
            frozen.tick(60.0)
            publish_hub_data(
                hass, hub.entry_id, hub_calculation.run_hub_calculation(hass, hub)
            )
            for name, sensor in sensors.items():
                await sensor.async_update()
                published[name] = (sensor.native_value, sensor.available)
    return engine_solar[-1], published


def _observed(hass, hub):
    """What each forecast observer has taken in so far: the gain's measured
    and forecast energy, peakiness's production energy, clipped energy."""
    runtime = hass.data[DOMAIN]["hubs"][hub.entry_id]
    gain = next(iter(runtime["_forecast_gain_observer"].values()))["acc"]
    return {
        "gain actual Wh": gain.get("actual_wh", 0.0),
        "gain forecast Wh": gain.get("forecast_wh", 0.0),
        "peakiness Wh": runtime["_forecast_peak_observer"]["sums"]["wh"],
        "clipped Wh": runtime["_forecast_clipped_observer"]["wh"],
    }


@pytest.mark.parametrize("soc", ["60", "100"], ids=["room", "full"])
@pytest.mark.parametrize(
    "topology", [WIRING_TOPOLOGY_PARALLEL, WIRING_TOPOLOGY_SERIES]
)
async def test_an_output_nothing_splits_publishes_no_solar(hass, topology, soc):
    """Battery configured, its power unread: the output is not production.

    Room in the pack is an honest interval the gain and peakiness observers
    learn from; a full pack is a saturated one where the clipped observer books
    forecast less "production". Before, on either wiring and at either SOC:

    ==========================  ========  =================================
    figure                      before    after
    ==========================  ========  =================================
    Current Solar Power         2300 W    unknown (sensor available)
    inverter Solar Production   2300 W    unknown
    gain, measured energy       38.3 Wh   0 (room)
    peakiness, production       38.3 Wh   0
    clipped energy              78.3 Wh   0 (full: 7000 - 2300 W for 60 s)
    engine's solar              2300 W    2300 W - unchanged
    ==========================  ========  =================================
    """
    slug = f"{topology}{soc}"
    hub, inverter = _rig(hass, slug, topology, soc)

    engine_solar, published = await _cycle(hass, hub, inverter)

    # Unknown, and still available: the producer ran and has no figure.
    assert published == {"hub": (None, True), "inverter": (None, True)}, (
        f"published (value, available): {published}, with nothing separating "
        f"the panels from the battery"
    )
    assert _observed(hass, hub) == {
        "gain actual Wh": 0.0, "gain forecast Wh": 0.0,
        "peakiness Wh": 0.0, "clipped Wh": 0.0,
    }
    # The control figure is untouched: the pool is still built from the output.
    assert engine_solar == pytest.approx(OUTPUT_W)


async def test_a_battery_less_output_is_still_solar(hass):
    """No battery entity at all is no battery (engine/readers: "A member with
    no battery entity has no battery"), so there is nothing in the output but
    the panels, and it publishes as before."""
    hub, inverter = _rig(hass, "pvonly", WIRING_TOPOLOGY_PARALLEL, "0",
                         battery=False)

    _engine_solar, published = await _cycle(hass, hub, inverter)

    assert published == {"hub": (OUTPUT_W, True), "inverter": (OUTPUT_W, True)}


@pytest.mark.parametrize(
    "topology", [WIRING_TOPOLOGY_PARALLEL, WIRING_TOPOLOGY_SERIES]
)
async def test_an_output_nothing_splits_leaves_no_solar_remaining(hass, topology):
    """Battery configured, its power unread: Solar Remaining is unknown too.

    Solar Remaining Power / Current publishes the solar pool's sun share,
    which off-grid is built from the battery's flow (the supply our loads
    hold comes back through it). With the flow unread the pool is empty, and
    the sensors read 0 W beside a Current Solar Power that reads unknown for
    the same reason - nothing splits the output into sun and battery. Read
    through the real hub sensors, on either wiring:

    ==========================  ==========  ==================
    figure                      before      after
    ==========================  ==========  ==================
    Solar Remaining Power       0 W         unknown, available
    Solar Remaining Current     0.0 A       unknown, available
    ==========================  ==========  ==================
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )
    from custom_components.dynamic_ocpp_evse.entities.hub import publish_hub_data
    from custom_components.dynamic_ocpp_evse.sensor import (
        DynamicOcppEvseHubDataSensor,
        HUB_SENSOR_DEFINITIONS,
    )

    hub, _inverter = _rig(hass, f"remaining{topology}", topology, "60")
    with freeze_time("2026-08-14 10:30:00+00:00") as frozen:
        run_hub_calculation(hass, hub)
        frozen.tick(60.0)
        publish_hub_data(hass, hub.entry_id, run_hub_calculation(hass, hub))
        published = {}
        for d in HUB_SENSOR_DEFINITIONS:
            if d["hub_data_key"] in ("available_solar_power", "available_solar_current"):
                sensor = DynamicOcppEvseHubDataSensor(hass, hub, "Unsplit", "us", d)
                await sensor.async_update()
                published[d["hub_data_key"]] = (sensor.native_value, sensor.available)

    assert published == {
        "available_solar_power": (None, True),
        "available_solar_current": (None, True),
    }, f"published (value, available): {published}, nothing splitting the output"
