# Dashboard cards: Priorities (Load Juggler) and Forecast (Load Insights)

Agreed with the owner on 2026-11-01, prototyped live on Kozolec's controls view
(injected into the page, nothing saved). Version 1; both cards replace hand-made
dashboard sections that needed `config-template-card`, `plotly-graph` and helper
`input_select`s.

## Shared approach

- Plain JavaScript custom elements, no build step and no dependencies, one file
  per card under the integration's `frontend/` folder.
- Each integration serves its file and loads it on every dashboard itself
  (`StaticPathConfig` + `add_extra_js_url`, the URL carries a hash of the file
  so a new build replaces a cached one) - no dashboard resource to add by hand.
  Load Insights already does this for `load-insights-card.js`.
- Options are edited in the card's visual editor through `getConfigForm`
  (HA's schema form), so no editor element is written by hand.
- Theme variables only (`--primary-text-color`, `--divider-color`, ...), and HA's
  own `ha-card`, `ha-select` and entities card, so the cards match the dashboard.

## Load Juggler: Priorities card

`type: custom:load-juggler-priorities-card`

Shows how the loads group by operating-mode urgency and how their effective
priorities come out of it, and lets the owner reorder them.

- **Lanes**, one per urgency tier in serving order: Tier 1 Always (Standard /
  Continuous / Normal / Freeze Protection), Tier 2 Solar Priority, Tier 3 Solar
  Only, Tier 4 Excess. Each lane has its own colour; an empty lane says "none".
  The tier is the one the engine sorted the load by this cycle (a tank riding
  surplus sits in Excess), read from the Effective Priority sensor.
- **Rows**: one per load, sorted by effective rank. Stacked one per row by default
  (`stacked: true`); `stacked: false` lays them out as pills that wrap.
  Each row shows
  - the effective rank in a coloured badge (1 = served first),
  - the load's name, with a bolt while it draws; clicking the name opens a popup
    with the load's own entities (mode, Dynamic Control, status, powers,
    temperatures, currents) as HA's entities card - the same controls, live,
  - **draw / allowance**: Allocated Current / Available Current, with a thin bar.
    For an on/off load (smart load, hot water tank / room thermostat) the
    allowance is its configured power (Device Power / Element Power), since the
    raw permit is only a ceiling. An allowance of 0 dims the row and reads
    "not allowed",
  - **up / down arrows around "set N"**, the configured priority.
- **A / W switch** in the header (`unit: A` or `W` sets the start): watts use the
  site voltage the engine measured and the load's phase count; an on/off load's
  allowance in W is its configured power exactly.
- **Arrows**: up moves the load one place earlier in the hub's configured order
  1..N (served sooner), down one place later. Up is disabled on the first load,
  down on the last. A press calls `dynamic_ocpp_evse.move_priority`; the row
  updates when the reloaded loads publish again.
- Options: `title` (default "Priorities"), `stacked` (default true), `unit`
  (default A), `hub` (a hub's entry id, only needed with more than one hub;
  default: the hub of the first load found).

### Load Juggler changes behind it

1. **`move_priority` service** - `entity_id` (any entity of the load) and
   `direction` (`up` / `down`). Swaps the load with its neighbour in the hub's
   configured order and renumbers 1..N through the same helper the hub's
   priority-order page uses (`config_flow.helpers._apply_priority_order`), so
   with numbers already 1..N only the two swapped loads change. Each changed load
   reloads, as after editing it on its Configure page.
2. **Effective Priority sensor attributes** `urgency_tier` (the load's mode
   urgency this cycle, 1-4) and `voltage` (the site voltage, V). Published from
   `engine/hub_result.py` as `load_tier` and `voltage`, both republished to the
   load sensors.
3. **Card registration** in `async_setup`, as Load Insights does it.

## Load Insights: Forecast card

`type: custom:load-insights-forecast-card`

- A header with HA's dropdown listing the forecasts; picking one redraws.
- A chart the card draws itself as SVG, from the sensor's `detailedForecast`
  (future, with `kwh_p10` / `kwh_p90`) and `history` (past) attributes, over the
  last 36 h and the next 36 h: actual as bars, the forecast as a dotted line, the
  p10-p90 range as a band, a "now" line, watts on the y axis.
- Which forecasts: by default every sensor carrying `detailedForecast`, so a new
  one appears by itself. Options (one or the other; both set is a card error):
  - `include`: only these, in this order - also the dropdown's order, and the
    first is shown when the card opens;
  - `exclude`: every forecast except these.
- Names: the entity's friendly name without the integration's device prefix and
  the trailing "forecast".
- Options: `title` (default "Forecast"), `include`, `exclude`, `hours_back` /
  `hours_ahead` (default 36 each).

## Not in v1

- Moving a load into another lane from the card (that is a mode change; the
  popup has the mode select).
- Several hubs in one card.
- A table view of the chart's numbers.
