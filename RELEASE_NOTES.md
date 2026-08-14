# Release Notes

## 2.0.6

### New Features

- **Hot Water Tank device type**: Manage a hot water tank as a binary heating load, driven through a Home Assistant `climate` entity (e.g. a Generic Thermostat). Three configurable setpoints (Away / Normal / Boost) and three operating modes — Freeze Protection, Normal, and Solar Priority — that pick a setpoint based on solar surplus and battery state. The climate entity keeps doing the temperature regulation; Load Juggler decides when heating is allowed and which target to write. See the [Charge Modes Guide](CHARGE_MODES_GUIDE.md#hot-water-tank-modes).
- **Portable Power Station device type**: Manage a portable power station (EcoFlow Delta and similar, through a local integration such as [ha-ef-ble](https://github.com/rabits/ha-ef-ble)) as a modulating load. The engine sets its AC charging speed the way it sets an EVSE's current — same four modes, Excess by default — and manages its **backup reserve** as the on/off gate, because a charge-speed knob with a 200 W floor cannot express "don't charge": dropped below the station's battery level, the reserve stops the wall draw entirely and the station spends what it stored on its own loads. A **Storm Reserve** switch holds a high reserve filled from any source, overriding the mode. Min/max charge power are configured rather than read from the device, so a station can be held below its hardware maximum, and only the charging component of the wall draw (`AC input - AC output`) counts as the load's draw — anything plugged into the station is ordinary household consumption. See the [Charge Modes Guide](CHARGE_MODES_GUIDE.md#power-station-modes).
- **EVSE phase mask sensor**: 3-phase EV chargers get a new sensor showing which site phases the car is actively drawing on (e.g. `A`, `AB`, `ABC`, or `Idle`).
- **Multiple inverters, per-inverter batteries**: an inverter is now its own config entry linked to the hub (*Add Inverter / Home Battery*), with its capacity, topology, output sensors and optionally its own battery. The hub aggregates the fleet — capacity-weighted fleet SOC drives the SOC-gated modes, each battery's charge headroom counts only while *that* battery is below *its* full SOC, and mixed AC-coupled + hybrid sites derive solar correctly per inverter. Existing hubs migrate automatically on startup: the legacy hub-level inverter/battery fields move onto a new inverter entry, hub sensor entity IDs are unchanged (now fleet aggregates). Each inverter device carries its own Solar Production and Battery SOC/Power sensors. **Note:** the *Recommended Battery Max SOC* and *Recommended Battery Charge Limit* sensors moved from the hub to the inverter entries (per battery — that is where the future write-control will act); the fleet values remain available in the hub data for automations.
- **PV clipping forecast (export-limited sites)**: Sites with more PV than they may export (e.g. 15 kWp behind a 5 kW export limit) curtail the midday peak if the home battery fills up on morning production that could have been exported. With a **grid export limit**, a **battery capacity** and one or more **Open-Meteo Solar Forecast** devices configured (the integration creates one device per PV array — select each array's device), the hub computes how much of the remaining day's production cannot be exported or consumed and publishes five advisory sensors: clippable and storable energy (kWh), a **recommended battery max SOC** that reserves exactly the storable amount and self-heals to 100% as the peak passes, a headroom deficit for when the battery is already too full, and a **recommended charge limit** that restricts battery charging to unexportable power only while the headroom is actually at risk. Advisory only — Load Juggler never commands the house battery; feed the values to your inverter with an automation, or wait for the planned battery-inverter device type. See the [README](README.md#pv-clipping-forecast-export-limited-sites).

### Improvements

- **One export number**: the *Excess export threshold* and the forecast's *grid export limit* collapsed into a single **Grid export limit** — the site's physical/contract ceiling. The Excess trigger derives from it as limit − **Excess trigger margin** (new field, default 500 W; inverters curtail just under the limit, so a trigger exactly at it would never fire), and the PV clipping forecast integrates above it. 0 = no export limit: the grid absorbs everything, so Excess never triggers on export and the forecast is off. Existing configs migrate automatically — the limit is seeded as your old threshold + 500 W, keeping the effective trigger point exactly where it was.
- **Excess mode counts the battery as a place to put power**: Excess used to look only at grid export, so a load could start while the home battery still had charge headroom — stealing solar the battery wanted. The trigger is now a single comparison of what the site is *absorbing* (grid export + battery charge power) against what it is *allowed* to absorb (the export allowance + Battery Max Charge Power). Each side drops out when its sink can't absorb: no grid means no export allowance, and no battery — or one at/above its **Battery Full SOC** — means no charge allowance. `battery_max_charge_power` was previously configured but unused; it now does what its name says. Two consequences worth knowing: Excess can finally trigger **off-grid**, where export is always zero and the battery's charge rate is the only signal, and on an ideal hybrid inverter nothing changes at all, since the battery is only bypassed once it is already saturated.
- **Off-grid Excess reads the battery instead of the meter**: With no grid CTs there is no export to measure, so off-grid the decision rests on the battery — Excess engages once it is charging at its configured maximum. Our own loads' draws are added back into the comparison (grid-tied the feedback loop already does this), which turns a running load into a probe: a curtailing inverter ramps up to serve it and the margin settles at the site's real surplus, instead of the load suppressing the very signal that started it and flip-flopping every cycle. It needs no SOC floor — a discharging battery counts as absorbing nothing, so a load that outruns production clears the verdict by itself.
- **One definition of "excess" for every load**: EVSE Excess, smart plug Excess and the hot water tank's boost setpoint now all read the same hub verdict instead of each re-deriving it from export. The hub publishes it, along with the absorbed and capacity figures, for diagnostics.
- **Hot water tank boost now triggers on real surplus**: A tank in Freeze Protection or Normal mode raised its target to the Boost setpoint as soon as grid export exceeded the *heating element's own draw* — around 2 kW, which is no measure of site surplus, so the tank boosted on almost any sunny day. Both modes now boost on the hub's Excess verdict — the same one an Excess-mode EVSE or plug triggers on, hysteresis band included — so the tank only boosts on energy the site genuinely cannot absorb anywhere else.
- **Hot water tank yields power while boosting**: A boosting tank now competes at the **Excess** urgency tier instead of its mode's own — heating past the temperature its mode asks for is opportunistic and must not outrank must-run loads. At its Away/Normal floor it keeps its normal tier, and the cold-tank promotion still wins: a Solar Priority tank below its Normal temperature stays at tier 1 even while boosting.
- **Smoother current transitions**: Updated the deadband to a proper Schmitt trigger, and ramps are now applied even if the available current momentarily drops below the minimum the EVSE can offer — a brief consumption spike now just slows the change down instead of stopping it.
- **Excess mode anti-chatter**: Added a hysteresis band to the export threshold so a load in Excess mode no longer flips on/off when export hovers right at the threshold.
- **Power buffer honored on the breaker limit**: The configured power buffer is now subtracted from the per-phase main-breaker limit as well as the grid-import limit — previously it had no effect on sites without a grid-import limit configured.
- **Off-grid hubs require a battery**: A hub configured without grid CT sensors runs off-grid, where the battery is the primary state signal. Hub setup now requires a battery SOC entity and a battery power entity in that case.
- **Clearer hub sensor names**: The headroom sensors are renamed from "Available …" to "… Remaining Power" (Site Remaining Power, Grid/Solar/Battery Remaining Power, Remaining Current A/B/C) and "Total Managed Power" → "Current Managed Power", to remove ambiguity between power *used*, power *remaining*, and total capacity. Entity IDs are unchanged.
- **Smart plug status sensor**: A smart plug now has a plain "Status" sensor showing `On` / `Off` (plus `Unavailable` / `Not Configured` error states) instead of the EVSE "Charging Status" — the charging-status vocabulary ("Unplugged", "Charging", …) does not apply to a plug.
- **Battery-backed plug Solar Only / Excess by SOC**: When a battery is configured, a smart plug in Solar Only or Excess mode is now driven by battery SOC — the battery is the surplus buffer (stored solar). Solar Only runs the plug whenever SOC is above the minimum; Excess runs it only above the target SOC. This works the same on hybrid grid-tied and off-grid sites. EVSEs are unaffected (they keep modulating).
- **Unified load power — slider plus live measurement**: Smart plugs and hot water tanks each have a power slider (Device Power / Element Power) holding the load's set power, and the hot water tank gets a **new Element Power slider**. When a power-measurement entity is configured, the live measured draw is used directly for the allocated current while the load is on and written back to the slider, so the slider learns and displays the device's real power. Without a measurement entity the slider value is used.

### Bug Fixes

- **Charger auto-reset escalation restored**: The OCPP profile-reset → hard-reset escalation was crashing on a missing import and never ran. Fixed.
- **Grace-period status restored**: The charger status sensor crashed during the solar/excess grace period. Fixed — it now shows the grace countdown.
- **Configured timings now applied**: The site update frequency, solar grace period, and charge pause duration were silently ignored (always using defaults). They are now applied as configured.
- **More reliable startup**: A charger set up before its hub could stay permanently broken; it now retries. Also removed a leaked polling timer when the site update frequency changes, and an extra hub reload on startup.
- **OCPP command reliability**: OCPP charge-rate commands that failed to dispatch were wrongly recorded as sent, which could trigger spurious resets. Fixed.
- **Compliance check vs ramping**: A charger legitimately ramping up or down is no longer wrongly flagged as non-compliant.
- **DST-safe timers**: Pause and grace timers no longer jump by an hour across a daylight-saving transition.
- **Duplicate entity IDs prevented**: The config flow now rejects an entity ID already used by another Load Juggler device (previously a second smart load could silently lose its entities).
- **Circuit groups on partially-metered sites**: A 3-phase load in a circuit group is no longer wrongly capped to zero when the site meters only some phases.
- **Service input validation**: The `set_min_current` / `set_max_current` services now reject a value that would make the minimum exceed the maximum.
- **Fewer false notifications**: Phase-mismatch auto-detect notifications no longer re-fire repeatedly on noisy sites.
- **Robustness**: An invalid phase configuration value no longer crashes the power calculation.
- **Off-grid hub no longer stuck "Initializing"**: A solar entity that was unavailable at startup (e.g. a fresh restart at night) crashed the hub calculation, leaving the hub permanently in "Initializing". Fixed.
- **Hub updates continuously with no loads**: A hub with no loads configured ran its calculation only once and then showed stale values. It now recalculates every scan cycle.
- **Off-grid Site Remaining Power**: On a hub with no grid CTs, Site Remaining Power was clamped to 0 W even with battery and solar available. It now correctly reports grid headroom plus inverter-sourced (solar + battery) power.
- **Inverter capacity honored in headroom**: Site Remaining Power and Battery Remaining Power now subtract the power the inverter is *already* delivering to the household, and are capped by the inverter's rated capacity — previously a 4 kW inverter already supplying 1 kW still reported its full rating as available.
- **Remaining Current A/B/C includes the inverter**: The per-phase remaining-current sensors now report total remaining current per phase (grid headroom + inverter share) and sum to Site Remaining Power ÷ voltage — previously they showed only grid breaker headroom and could contradict Site Remaining Power.
- **Off-grid phase count fixed**: An off-grid site forced all three grid phases to 0, making a 1- or 2-phase site look 3-phase — which split per-phase figures (e.g. Remaining Current A) across phantom phases. The site phase count is now the combination of the configured grid CT and inverter output sensors.
- **Smart plug stuck off fixed**: A plug that was currently off reported its connector status as "Available", which the engine treated as "idle" and excluded from power distribution — so an off plug was never allocated power and could never turn on. Plugs and hot water tanks (which have no connector) are now always eligible for allocation; only EVSEs require a connected car.
- **Hot water tank Solar Only mode fixed**: A tank in Solar Only mode was forbidden from heating whenever battery SOC was below the target, which overrode the tank's own setpoint logic — the away (below-minimum) and normal (below-target) setpoints never took effect. A tank now always heats to the setpoint chosen by its mode; Solar Only correctly heats to the away setpoint below minimum SOC, normal up to target, and boost above.
- **Managed power counts plugs and tanks**: The Current Managed Power sensor only summed EVSE draw — smart plugs and hot water tanks were never given per-phase currents, so they contributed 0 even with power metering configured. Plug and tank draw is now populated (from the power-monitor entity, or the set/element power when on), so it shows in Current Managed Power and is correctly subtracted from household consumption by the feedback loop.
- **No current smoothing on binary loads**: Smart plugs and hot water tanks are on/off loads, but their allocated current was run through the EVSE ramp/deadband smoothing — producing meaningless intermediate values and delaying the off transition. Binary loads now use the engine's target directly; only EVSEs are smoothed.
- **OCPP hard reset fixed**: The hard-reset escalation looked up the reset button by the Load Juggler entity ID instead of the OCPP charger ID, so it never found the button and silently fell back to a profile reset. A stuck charger can now actually be hard-reset.
- **`set_operating_mode` service accepts tank modes**: The service schema rejected the hot water tank modes ("Normal", "Freeze Protection"); they can now be set via the service.
- **Config flow: edited OCPP device ID kept**: Editing the auto-detected OCPP device ID during charger setup was silently discarded and the detected value used instead. The edit is now honored.
- **Status sensor names the missing input**: When a required sensor (solar, battery, grid, inverter output) is unavailable, the hub Status sensor now states exactly which input is needed instead of failing silently.
- **1-phase cars on watt-reporting chargers no longer reset-loop**: On a 3-phase EVSE that takes its limit in watts, a 1-phase car was checked for compliance using the charger's hardware phase count instead of the car's active phases — so a correctly-followed 16 A limit read back as ~5 A and triggered a perpetual non-compliance auto-reset loop. The compliance check now uses the same phase count the limit was sent with.
- **Zero site voltage no longer crashes the calculation**: A site phase voltage configured as 0 (or negative) caused a division-by-zero in the power calculation. It now falls back to 230 V with a warning.

---

## 2.0.5

### Improvements

- **Automatic unit conversion**: Power and current sensors now auto-convert units at runtime:
- **Unit-based entity filtering**: Entity selectors now filter by `unit_of_measurement` instead of `device_class`, allowing selection of sensors from integrations that don't set device_class properly.

---

## 2.0.4

**BREAKING**: Existing 1.x users must **remove and re-add** the integration.

### New Features

- **Per-load operating modes**: each charger/load has its own operating mode instead of a site-wide setting. EVSE modes: Standard, Solar Priority, Solar Only, Excess. Smart Load modes: Continuous, Solar Only, Excess.
- **Mixed-mode operation**: run different chargers in different modes simultaneously (e.g., one charger in Standard while another waits for Solar Only).
- **Multi-charger support** with 4 distribution modes (Shared, Priority, Optimized, Strict).
- **Smart Load support**: non-OCPP controllable loads (e.g., granny charger behind a Shelly smart plug) with binary on/off control, configurable power rating, and phase assignment.
- **Asymmetric inverter support**: flexible power pool across phases.
- **Battery integration**: SOC thresholds, charge/discharge power limits, battery-aware solar derivation.
- **2-phase OBC charger support** (e.g., VW eGolf, eUp, ID.3 base).
- **Charger phase mapping (L1/L2/L3 → A/B/C)** with per-phase constraint enforcement.
- **Power-based charging**: send watts instead of amps via OCPP charge rate unit auto-detection.
- **Per-phase inverter output entities**: optional sensors for each inverter phase with parallel/series wiring topology.
- **Entity auto-detection**: battery, solar, inverter, power monitoring, and wiring topology usually get auto detected.
- **Max import power limiter**: cap grid import independently of breaker rating.
- **HA service actions**: `set_operating_mode`, `set_distribution_mode`, `set_max_current`, `set_min_current`.
- **Available current display** for idle chargers.
- **EVSE charging status sensor** with mode-aware status messages (Battery Priority, Insufficient Solar, No Excess, etc.).
- **Hub status sensor** — shows site configuration health (OK, Initializing, No power measurement, Grid sensors unavailable) with detailed warnings as attributes.
- **Solar/Excess grace period**: configurable hold-at-minimum timer before pausing when conditions drop, preventing rapid on/off cycling.
- **Auto-detect grid CT inversion**: correlates charger draw vs grid current direction, fires persistent notification after repeated inverted readings.
- **Auto-detect phase mapping**: correlates charger draw vs per-phase grid deltas, notifies on wiring mismatch (opt-in).
- **Auto-detect OCPP meter sample interval**: uses charger's `MeterValueSampleInterval` as default update frequency.
- **Auto-detect smart plug power monitor**: discovers power sensors for Shelly, Sonoff, Tasmota, Kasa, Tuya plugs.
- **Circuit groups**: shared breaker limits for co-located loads. Group loads under a sub-breaker with a per-phase current limit. Post-distribution enforcement ensures combined allocation never exceeds the circuit limit.
- **Off-grid support**: grid CT entities are now optional. When no grid CTs are configured, the system infers active phases from inverter output entities and treats grid current as 0A. Solar production is derived from inverter output using a unified formula that works for both grid and off-grid sites.

### Resilience Improvements

- **Grid CT stale detection**: when configured grid CT sensors become unavailable, the system holds the last known EMA value. After 60s of continuous unavailability, all chargers fall to minimum current. Recovery is automatic with a log message.
- **Sensor unavailability handling**: `_UNAVAILABLE` sentinel pattern — solar, battery, and inverter sensors automatically hold their last EMA value during brief unavailability instead of decaying to 0.
- **OCPP/switch error handling**: `set_charge_rate` and plug switch commands wrapped in try-except to prevent update cycle crashes if the OCPP integration restarts.
- **Input validation**: NaN/Inf guard in EMA smoothing, voltage ≤0 fallback to 230V, plug empty-phase crash fix, stale circuit group member filtering.

### Bug Fixes

- Fixed charging instability from feedback loop oscillation (EMA smoothing + Schmitt trigger dead band + dual-frequency updates).
- Fixed grid CT feedback loop overcorrection when charger reports inflated per-phase draws (per-phase draw clamping).
- Fixed battery power sensor false-positive detection matching phone batteries.
- Fixed entity state lookup using shared `hass.data` store instead of entity ID guessing.
- Fixed battery SOC target/min sliders not feeding into calculation engine.
- Fixed EVSE min/max current sliders missing value clamping.
- Fixed connector status "Finishing"/"Faulted" treated as active — now correctly stops allocation and skips OCPP profiles.
- Fixed entity selector clearing (`suggested_value` instead of `default` so X button truly clears).
- Fixed options flow Submit → Next button on non-final steps.
- Fixed config entry not reloading on options change (stale battery sliders persisting).
- Fixed `total_site_available_power` and `available_grid_power` not capped by `max_grid_import_power`.

### UX Improvements
- Redesigned configuration flow with contextual help text, entity pickers, and number selectors.
- Phase mapping fields hidden on single-phase sites to prevent misconfiguration.
- Charger names auto-prettified from OCPP entity IDs.
- Phase B/C sensors hidden on single-phase sites.
- Hub sensor renames: shorter, consistent naming (Current X Power / Available X Power), added Current Solar Power sensor.
- Structured debug logging with human-readable charger names, raw+smoothed value display.
- Charge pause duration in minutes (was seconds) for consistency.
- Default hub name changed to "Site Load Management".

---

## 1.2.1

### Improvements

- Added configurable power buffer for grid protection.
- Default charging mode selection.
- Single-phase operation support.
- Configurable OCPP profile stack level.
- Fixed charging instability on standard charge modes.
- Fixed missing entities during updates.
- Input number slider definitions updated.

---

## 1.1.0

### New Features
- **Excess charging mode**: charge only when total export exceeds a configurable power threshold.
- **Battery configuration**: multi-step config flow with battery SOC entity, power entity, and min/target SOC.
- **Grid charging toggle**: switch to allow/disallow grid charging when battery is present.
- **Grid power entities**: support for selecting power entities (W) instead of only current (A) for grid measurements.

### Improvements
- Additional pattern matching for Deye CT current measurements.
- Added None/NoneType safety checks.
- Config flow refactored into multiple steps.
- Updated config descriptions.

---

## 1.0.7

### Improvements

- Faster detection/refreshing of number of charging phases.
- Fixed initial setup and added reset notice on first configuration.
- Updated English descriptions.
- Added Slovenian translation.

---

## 1.0.0

First public HACS release.

### Features

- OCPP 1.6J charger management via Home Assistant.
- Dynamic current adjustment based on solar production and grid capacity.
- Solar and Eco charging modes.
- Automatic charging phase detection.
- Pause timer functionality.
- Reset/reconfigure button (clears OCPP profiles).
- Minimum current start to prevent oscillation from unknown phase count.
