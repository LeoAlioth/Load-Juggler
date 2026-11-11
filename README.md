# Load Juggler

Load management for Home Assistant. It splits the power a site has to spare - from solar, the battery and the grid
connection - across your EV chargers (OCPP), smart plugs, hot water tank, heat pump and anything else that takes a setpoint,
each in its own operating mode. Grid-tied or off-grid, one to three phases.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=LeoAlioth&repository=Load-Juggler&category=integration)

## What it does

- **Per-load operating modes** - Standard, Solar Priority, Solar Only or Excess, chosen load by load.
- **Priorities that follow the mode** - a more urgent mode is served first; the load's priority number decides within a mode.
- **Phase-aware** - per-load phase mapping, symmetric and asymmetric inverters, circuit groups for loads sharing a breaker.
- **A charger that can switch phases** charges on one phase instead of pausing, and returns to three when they have room.
- **Battery and forecast aware** - SOC thresholds, pacing, early starts on a forecast excess, PV clipping advice, optional writes to the inverter.
- **Dynamic Control** - a switch per load that hands it back to you; what it is left at is a setting.
- **Failsafe** - loads fall back to a safe value when a sensor goes unavailable.
- **Auto-detection** of sensors, phase mapping and charger settings; setup forms open pre-filled from the Energy dashboard (read only).

## Load types

| Type | Controlled through | Modes |
|---|---|---|
| **EVSE** | OCPP 1.6J charging profiles (A or W, auto-detected) | Standard, Solar Priority, Solar Only, Excess |
| **Smart Plug** | on/off switch | Continuous, Solar Priority, Solar Only, Excess |
| **Hot Water Tank** | `climate` or `water_heater` entity - picks an away / normal / boost setpoint | Freeze Protection, Normal, Solar Priority |
| **Power Station** | charge-speed and backup-reserve numbers (EcoFlow and similar) | Standard, Solar Priority, Solar Only, Excess |
| **Variable Load** | any `number` setpoint in A or W, or none (publishes its allowance as sensors) | Standard, Solar Priority, Solar Only, Excess, Hold |
| **SG Ready Heat Pump** | two relays | follows the site (Block / Normal / Recommended / Force) |

## Operating modes

| Mode | Runs when |
|---|---|
| **Standard** / **Continuous** | always - as fast as it can, grid included |
| **Solar Priority** | solar first, at the minimum rate when there is none |
| **Solar Only** | only on solar surplus; no grid import |
| **Excess** | only when the site can no longer place its own production |

Urgency decides who is served first: **Standard / Continuous > Solar Priority > Solar Only > Excess**. Within a mode the
load's priority number decides. Several loads split what is left by the hub's distribution mode: **Shared**, **Priority**,
**Optimized** or **Strict**.

Details, battery behaviour and examples: [Charge Modes Guide](CHARGE_MODES_GUIDE.md), [Distribution Modes Guide](DISTRIBUTION_MODES_GUIDE.md).

## Dashboard card

`type: custom:load-juggler-priorities-card` comes with the integration; nothing to add as a resource. It shows the loads in
lanes by urgency tier, what each draws against what it may, in A or W, and arrows that move a load's configured priority
(`P1`, `P2`, ...) one place. Hover the circle (effective priority) or the `P` number for what each means; click a name for the
load's settings.

## With Load Insights

[Load Insights](https://github.com/LeoAlioth/Load-Insights) forecasts a site's consumption and plans its battery's SOC
target, minimum and daily excess. Load Juggler can use them for pacing and early starts. Neither needs the other.

## Install

Click the HACS button above, or add `https://github.com/LeoAlioth/Load-Juggler` to HACS as a custom repository
(integration). By hand: copy `custom_components/dynamic_ocpp_evse` into your config folder. Restart Home Assistant.

## Quick start

1. Make a template sensor for the maximum import power in W (for example `{{ 6000 }}`, device class Power); a name with
   "Power Limit" is picked up automatically.
2. **Settings > Devices & services > Add integration > Load Juggler**, then **Create a Hub**: grid phase sensors (optional for off-grid),
   main breaker, voltage, import limit, and battery and inverter if you have them.
3. **Add a Load**. OCPP chargers are discovered; confirm it, set the minimum and maximum current and the phase mapping.
4. Press **Reset OCPP EVSE** on a charger, set each load's operating mode, and you are done.

## Works with

- **Grid meters and inverters** - the per-phase readings are found for SolarEdge, Deye / Sunsynk and other Solarman inverters, Sofar, Sungrow, Fronius, Huawei Solar, Enphase and Victron; any current or power sensor works otherwise.
- **Chargers** - anything the [OCPP integration](https://github.com/lbbrhzn/ocpp) supports. Running on live sites: EVBox Elvi, go-eCharger V4 (including its phase switching) and Huawei SCharger.
- **Smart plugs** - any switch; power monitoring is found for Shelly, Sonoff, Tasmota, Kasa and Tuya.

## More

- [Reference](docs/reference.md) - Variable Load, off-grid, several inverters, PV clipping forecast, every setting, services, troubleshooting
- [Charge Modes Guide](CHARGE_MODES_GUIDE.md), [Distribution Modes Guide](DISTRIBUTION_MODES_GUIDE.md), [Release notes](RELEASE_NOTES.md)

## Feedback

It is built from real sites, and more setups make it better: other inverter and meter brands, batteries, one- and three-phase
sites, odd grid limits. Report problems and ideas on [GitHub Issues](https://github.com/LeoAlioth/Load-Juggler/issues), with
your configuration if you can.

## Support

If it saves you money and you want to say thanks: [ko-fi.com/LeoAlioth](https://ko-fi.com/LeoAlioth)

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/LeoAlioth)
