# Design decisions that look like bugs

Deliberate behaviour. Check here before calling one of these a bug.

## Solar modes and excess


Settled with the owner 2026-09-09, from the kozolec diagnostics. The asymmetry below
is DELIBERATE - do not "fix" it for consistency.

**Changed** (`f78519e`): `control/hot_water_tank.resolve_tank_setpoint`'s Solar
Priority branch read SOC alone, so a tank sat at its normal setpoint while the
hub published `excess_available`. It now boosts at/above the target SOC **or**
on the excess verdict, the same test Freeze Protection and Normal already used.
The owner: "this is incorrect, i should trigger on excess still". The *away* floor
below the minimum SOC is NOT yielded to excess - that is about the house's
reserve, not about spare production - and he did not ask for it.

**Considered and DECLINED for now**: the same override on the load BEHAVIOURS.
`calculations/target_calculator._source_limit` gates both solar behaviours on
`_below_soc_target(site)`:

    BEHAVIOR_SOLAR_PRIORITY -> load.min_current   (grid-backed minimum only)
    BEHAVIOR_SOLAR_ONLY     -> 0                  (battery needs to charge)

Letting excess override that gate would hand those loads the SOLAR pool
instead. Quantified on kozolec at the time: `min_current`/0 today against
14.49 A = 3 333 W on phase A. The owner, asked directly with those numbers: "keep as
is for now. jut the tank fix is enough."

So the state to expect: a tank boosts on excess, an EVSE/station/plug in Solar
Priority or Solar Only still defers to the battery until target SOC. If a
future session notices that inconsistency, it is a decision, not an oversight -
re-ask before changing it. "For now" was his wording, so it may come back.

## Excess may start before the battery is full (forecast driven)


Decided by the owner 2026-10-17; the 2026-09-09 note above stays as history.

**What:** with the hub option `forecast_excess_enabled` on, the Excess verdict is also true while the pacing energy (`pacing_energy_entity_id`, labelled *Excess energy*: the planned excess still to use today, until the *Sunset* sensor `pacing_horizon_entity_id`) is above 0.2 kWh and the battery SOC is at/above the minimum, the target or a fixed value (`forecast_excess_soc_source`, `forecast_excess_soc`; the owner kept the fixed value on 2026-10-17) - the day's sun will overfill the battery anyway. It is OR-ed into the hub's `excess_available` and the calculator's `_excess_verdict`, so everything that reads the verdict follows it: tank boost (Freeze Protection, Normal, Solar Priority) and the Excess behaviours (`BEHAVIOR_EXCESS`, `BEHAVIOR_BINARY_EXCESS`). The Excess pool is topped up with the solar the battery is absorbing, at most `planned kWh x 1000 / max(hours left, 1)` W. Code: `calculations/forecast_excess.py`, `_calculate_excess_available`, `engine/hub_calculation._apply_forecast_excess`.

**Per load, counting back from the next full charge (2026-10-18):** the verdict is not handed to every load at once. Each load takes it only when `starts_early` says so: planned kWh x 1000 >= the load's minimum power x the hours until the battery is next full (`forecast_excess_full_entity_id`, labelled *Next full charge*; Load Insights' sensor of that name, the first hour its SOC forecast reaches full, rounded down to the hour; the pacing horizon stands in when unset), latched per load with a band: the planned energy must exceed the load's power by 10 % to start it and 90 % of it keeps it running (2026-10-20, below). Why: a tank boosts on the setpoint and heats at once whatever the pool holds, so on a midnight verdict with a pool smaller than its element it flapped 42/75 C every grace period (Kozolec, 17-18 Oct) and the battery fell from 76 % to 47 %. The site verdict stays on while at least one load is due; with no loads (a bare hub) it is unchanged. Code: `starts_early`, `_apply_forecast_excess`, `_excess_verdict(site, load)`.  The pool top-up is site-wide, but a load that is not due does not count it (`_excess_permits` takes it off the pool): found live on Kozolec 18 Oct, where the bee room heater being due let the top-up start the boiler. **Near-boost hysteresis (2026-10-21):** a thermostat load joins the early-start group from 2 K of headroom to its boost temperature and, once in the group, leaves only under 1 K (`EARLY_MIN_HEADROOM_K` / `EARLY_RELEASE_HEADROOM_K`; the hub remembers last cycle's members in `_early_members`). Kozolec's boiler at 73 C of 75 left and rejoined five times in half an hour (19 Oct) on one line.

**Margins, and a due group's fixed-power members (owner, 2026-10-20):** `starts_early` starts a group when planned kWh x 1000 >= 1.1 x its power x hours to full (`EARLY_START_MARGIN`) and holds it down to 0.9 (`EARLY_START_RELEASE_RATIO`), the power being what its loads really pull (their *Absorption rate*, else the minimum power); the same function serves the clipping source. And a binary Excess load (tank, plug) with `early_start_ok` while `forecast_excess` is on keeps its full rating - it is no longer re-tested against the instantaneous pool (`_mode_source_limit`, the importing guard still applies). Why: the group is judged on ENERGY over the hours to full, the pool is the sun the battery absorbs THIS minute (0.4 kW at dawn). A running tank behind a higher-ranked claim got 0 A and was withdrawn after its minimum run, while an idle one passed (an idle load's availability is not tested against the pool) and started again after its rest: Kozolec's boiler and Bee Room Heater, 20 Oct 08:00-08:47 UTC, five cycles (also 11, 17-18 and 19 Oct). Reproduced in `dev/tests/test_tank_forecast_cycle.py`. Loads that vary their power (chargers, stations) still follow the pool. Not benched on recorded days: verified on the rig and the unit tests, to be looked at live.

**Counted back from the clipping window too (JUG-18, 2026-10-19):** with the hub option `forecast_clip_early_enabled` (off by default) the same group rule has a second energy source: the forecast clip the battery cannot take (`calculations/forecast.unavoidable_clip_kwh`: clipped less the absorbable energy at the charge rate within the pack's capacity, and for today's window the room it has left) and a second deadline, the time the clipping window opens (`clip_window_span`). A load starts early when the clip x 1000 reaches the group's minimum power x those hours - so loads start BEFORE the window, which is what looks odd when the battery is still charging and the surplus verdict is off. Its own latches (`_early_group_clip_on`, `_excess_by_clip_on`), the same SOC gate and blockers; the pool top-up is the clip over the hours to the window's end. The clip figures come from the previous cycle's advice. Code: `_apply_forecast_excess`, `_clip_source`.

**Not changed, still declined:** the Solar Priority and Solar Only LOAD behaviours (`_source_limit` gates both on `_below_soc_target`). The owner declined that on 2026-09-09 and this feature did not reopen it: it reaches only what follows the Excess verdict or the Excess pool. So a Solar Only or Solar Priority EVSE below its target SOC still waits for the battery while an Excess one (or a Normal tank, boosting) starts on the forecast.

**Update, same day:** the owner then allowed a per-load, opt-in catch-up for those two behaviours - see the next section. The hub option above still does not reach them.

**How to apply:** a tank boosting or an Excess load running with the battery below full and a surplus verdict of off is this feature, not a bug - look at `forecast_excess` in the hub data. A Solar Only load idle beside it is the 2026-09-09 decision.

## Solar Only / Solar Priority may start below target SOC (catch-up, per load)


Decided by the owner 2026-10-17, as a narrow exception to the 2026-09-09 decision above (which stays as history). The owner: "Solar Only should start under target SOC if its max power would not be able to otherwise keep the battery from reaching 100 %. Essentially if the excess verdict ends up at a lower SOC than target, and the peak production is higher than the max the EVSE can deliver, we might start charging earlier (though I assume this is a rare case unless the max charge rate is not big)." Then: "make these per load options".

**What:** a load with its own option `catch_up_enabled` (off by default; EVSE, smart load and power station settings pages) in Solar Only or Solar Priority takes the solar pool even below the target SOC while it **can't keep up**: `energy x 1000 / max(hours left, 1)` (the hub's *Excess energy* and *Sunset*) is at least its own maximum power (`max_current x voltage x phases`). Latched per load (start at 1.0 of its maximum power, hold to 0.8, planned energy > 0.2 kWh held to 0.1). Never below the hub's early-Excess gate (`forecast_excess_soc_source`: minimum / target / fixed); with `forecast_excess_enabled` off the floor is the minimum SOC, because a Solar load must never take the battery below its reserve. Code: `calculations/catch_up.py`, `engine/hub_calculation._apply_catch_up`, `_mode_source_limit` (Solar Only 0 -> the pool, Solar Priority its minimum -> the pool, plug Solar Only (`BEHAVIOR_BINARY_ABOVE_TARGET`) -> its rating), all capped at the load's maximum.

**Still true:** below the target the battery's discharge is NOT added (the pool then holds only the sun the battery is absorbing); a load without the option, or one that can keep up, waits for the target exactly as decided on 2026-09-09; tanks are excluded - they follow the Excess verdict, which the forecast-driven Excess feature already starts early. With the default early-Excess source (the target) the floor equals the target and the option does nothing: it takes effect only with the source set to the minimum or a fixed value below the target.

**How to apply:** a Solar Only / Solar Priority load running under the target SOC is a bug only if its `catch_up_enabled` is off, the pacing sensors are unreadable, or the SOC is under the floor. Otherwise it is this feature.

## Solar Only uses the battery above target SOC


Load Juggler counts battery energy above the target SOC as solar surplus: a Solar Only load (e.g. Kozolec's
Pond EVSE) may run from the battery after sunset while the SOC is over target (2026-10-14: "by design").

**Why:** that energy above target was stored from solar and is forecast to be refilled.

**How to apply:** don't flag a Solar Only load running on battery as a bug; only question it when the SOC is
at or below target.

## Config flag meaning comes from its help text


`CONF_ENABLE_MAX_IMPORT_POWER` is labelled "Create max import power limit slider" and only decides whether the slider entity exists. The override sensor (`CONF_MAX_IMPORT_POWER_ENTITY_ID`) applies whenever it is set - its help text says it "takes precedence over both the slider and this checkbox". On 2026-09-07 I read the flag as a feature switch and made it gate the sensor on all three code paths (`565a0bf`); the SE17K's 15-minute block cap went unapplied for a week. The owner caught it from a history export on 2026-09-14; restored in `db56ee4`.

**Why:** the key name suggested "enable the limit" and I never read the contract the user configured against. An unticked box beside a configured sensor is the NORMAL configuration for someone driving the limit from their own sensor - they have no use for a slider - not a leftover. The "ten dropouts a morning" that motivated the change were the SolarEdge integration blinking (same source as the grid CTs), not the cap sensor causing anything.

**How to apply:** before changing what a config key gates or its precedence, read its label and `data_description` in strings.json (and sl.json). When a flag and an entity coexist, ask what the UI told the user each one does. "This looks like a leftover" is a hypothesis to put to the owner, never grounds to change precedence on my own.

## SG Ready heat pump: Block waits, and some sites never Block

Built 2026-10-19 (JUG-24), designed with the owner; `calculations/sg_ready.py`, `engine/hub_calculation._apply_sg_ready`.

- **The 10-minute rule covers all four states, Block included - except that a BREAKER overload does not wait for it.** A Slovenian meter or breaker tolerates more than 110 % of its rating for only about 90 s, so a breaker overload blocks the pump after a 10 s noise filter (`sg_ready.BREAKER_HOLD_S`; the input is already smoothed by the engine EMA, 5.6 s time constant, about 13 s to 90 % of a step, so the total reaction is about 25 s, a quarter of what the breaker allows), whatever state it interrupts. The Block then lasts its minimum. A MAX IMPORT POWER overload (the 15-minute-block limit) blocks only after 2 continuous minutes over it (`IMPORT_HOLD_S`, the owner's figure, resets when it drops under) and does wait for the current state's minimum time.
- **The release margin is the hub's Excess trigger margin** (`site.excess_trigger_margin`, 500 W by default): the integration's existing lead below a limit, there so a threshold exactly at the limit is not tried. No constant of its own.
- **The overload is the household's, with every managed load already off** (`target_calculator.grid_headroom_w`: the breaker less the power buffer, and the max import power). The chargers and other loads are cut first because the pool is empty before the pump is asked. An off-grid site has no such limit, so its pump is never blocked and a Block is released at once.
- **The Normal draw is household.** Only the extra draw above Normal is a managed load, and only while the state is Recommended or Force. A circuit group's limit therefore counts that extra draw alone.
- **Force needs the allocation's grant** (the same phase-is-buying guard and circuit groups as any Excess load), so a pump on an importing phase is not forced although the site has excess.
- **After the allowance is used up the overload goes unanswered**, by design: Normal, a warning in the log and the State sensor's reason.

## Meter-only hybrid at its rating: the saturation latch and its known ceilings

(Moved from the TODO, ex-JUG-9, 2026-10-21; shipped in 2.1.3, no site has this topology yet.) With no solar sensor and no output sensors the sun the house uses itself reaches no meter, so by day the rating cap (`target_calculator._house_on_inverters`) is short by it; `engine/hub_calculation._apply_saturation_latch` now holds the battery at its flow (`SiteContext.battery_discharge_ceiling`) once the grid carries our loads past their grant (`target_calculator.grid_overdraw`), the site's import rose with our loads' growth and the battery, below its rating, stayed flat for `SATURATION_CONFIRM_S` (60 s); it offers the rating again after 15 min, doubling to an hour while the limit answers. `dev/tests/test_gridtied_inverter_pool.py`: 3 kW sun / 1 kW house / 7 kW inverter goes from 30.4 A with 992 W past a 0 W allowance all hour to 26.4 A, past it 288 s of the hour. Known ceilings, all to check against a real meter-only site: a battery that answers the site slower than 60 s is taken for a limit (at 70 s the car held at 9.8 A until the first offer); a battery its own logic holds (BMS, reserve, charge priority, forced charge) is held to the allowance too, and where that leaves less than the car's minimum it stops and is restarted at each offer; each offer costs one detection's overrun (80-96 s). The one-shot YAML scenario `grid-rating-cap-derived-solar-sees-only-the-battery` keeps its known breach - one cycle cannot see the limit.

## Shared battery bank behind parallel inverters: not built, how to settle it

(Moved from the TODO, ex-JUG-16, 2026-10-21; analysed 2026-09-07, no site has this topology yet - tackle it when one turns up.) A 3-phase site built from parallel single-phase hybrids sharing one pack READS correctly: per-phase output sensors give `spans_phase` the right answer, `battery_power_total` sums only power-entity members, and configuring the pack on ONE inverter entry keeps `capacity_total` from being multiplied. The WRITE side cannot fan out, though: both `fleet.split_charge_limit` and the per-inverter SOC fan-out in `_compute_forecast_advice` gate on `m.has_battery and (m.capacity_kwh or 0) > 0`, so an entry declaring `battery_control` with no SOC/power entity of its own receives neither `forecast_charge_limit_w` nor `forecast_battery_max_soc` - Load Juggler throttles one inverter's register while its siblings keep pushing into the same pack. **Whether one write is enough is a hardware question, and the BMS is not the answer to it**: a CAN-connected BMS caps the pack's own charge current but cannot relay a setpoint WE chose - the entire point of the charge advice is holding the pack deliberately BELOW its capability to keep clipping headroom, which no BMS limit expresses. A single write suffices only where the units are parallel/three-phase GROUP-linked and the battery settings live on the master for the whole group (Deye/Sunsynk parallel, a Victron system under one GX/DVCC); independent units on a common DC bus each need their own. **How to settle it in two minutes on a real site**: command a reduced charge limit on one inverter, then read the siblings' registers back and watch the pack's charge power - if the siblings follow, it is group-linked and today's single write is already correct; if only that unit's share of the pack current drops, the fan-out needs building. That experiment is why this waits for a site instead of being fixed blind. **Fix direction**: gate the fan-out on DECLARED `battery_control` rather than on measured battery, and split the advice across the control-bearing members - headroom weighting has nothing to weigh when only one entry carries the capacity, so an equal split or the per-member charge cap as the weight is what a shared pack wants. Config notes for whoever hits this: every entry needs its output entity on its OWN phase (a blank `output` makes `spans_phase` true for all three, and a single-phase load then gets offered the whole fleet), asymmetric ticked on each member, the pack's own charge limit - not one inverter's - on the battery-bearing entry, and be aware that unequal inverter sizes lose power to `inverter_limits`' min-over-phases collapse.

## Off-grid sites calibrate the forecast slowly: do not admit curtailed intervals

(Moved from the TODO, ex-JUG-17, 2026-10-21; established live on Kozolec 2026-09-07. This section exists to stop a future fix, not to request one: do NOT "fix" it by admitting curtailed intervals.) Off-grid there is no export wall, so an array is demand-following: production always equals consumption plus charging. `battery_is_saturated` therefore reads curtailment off the pack, and once the pack is at its full-SOC every interval is excluded from the gain - correctly. Measured that day: pack 99 % against a 97 % full-SOC taking 57 W, AC consumption 3803 W, production 4263 W, forecast 6785 W - the array was throttled by ~3 kW and the exclusion was right. The consequence is that a site whose pack fills by mid-morning can only ever calibrate on its shoulders and its cloudy periods, so the gain moves slowly or not at all and `forecast_gain_days` may sit at 0 for a long time. **That is a true statement about what the site can know, not a defect.** Admitting curtailed intervals would discard exactly the evidence of an optimistic forecast (measured production is suppressed BY the quantity being measured), biasing the gain toward the forecast's own optimism - the same trap as calibrating on clipped kWh, noted under *Forecast calibration* below. The skipped share is published as the accuracy sensor's `gain_skipped_pct` so a starved observer says so rather than looking like a fresh restart. **If a faster off-grid gain is ever actually wanted**, it needs a source the site does not have today - a clear-sky reference, or one small unthrottled reference array - and that is a new input, not a tuning.

## Variable Load: equal per-leg allowances, sized on the legs it draws on

Built 2026-10-21 (JUG-30); `calculations/variable_load.py`, `engine/load_builders._build_variable_load`, `control/variable_load.py`.

- **The allowed current is the same on every leg it is sized on.** The allocation sizes a modulating load with one current per leg, the lowest any of its phases allows, exactly as for an EVSE; so *Allowed Current L1/L2/L3* read alike on the legs it is sized on and 0 on a configured leg it is not, and *Allowed Current* equals them.
- **It is sized on the legs it draws on (owner, 2026-10-21), not on its configured legs.** With a current sensor per leg, the legs over 1 A; while it draws nothing, the legs it last drew on, until the connected sensor goes off (then every configured leg). A power sensor cannot tell legs apart, so with one, or no reading, it is sized on its configured legs. So a charger switched to one phase is a single-phase load, and one stopped in single-phase mode restarts as one.
- **Switching back to more legs overloads for the cycle or two before Load Juggler can see it** (the device switches, not Load Juggler). On the reading that shows more legs the allowance restarts at the minimum and the command goes out at once, without its slot or the one-cycle shortage confirmation: 0 where a new leg has no room for the minimum, else the minimum rising through the normal ramp (`dev/tests/test_variable_load_ha.py`: 16 A on two loaded phases for two cycles, then 0). Without a connected sensor, a device stopped on one phase and switched to three before it starts again runs into the same once.
- **The phase advice reads what all its legs could take while it runs on fewer** (owner, 2026-10-21). Sized on all its legs it reads the allowed power. Sized on fewer, the builder sets `potential_phases_mask` (its configured legs) and `target_calculator._set_potential_current` works out the per-leg current those legs could be given - the physical pool and the mode's source limit on that mask, from the pools left by the loads ranked ahead, as an inactive load's permit is - and the advice reads that x legs x voltage, capped at the device's maximum. Circuit-group limits are not in it: it is a hint, and the allocation applies them once the device has switched. Reading the allowed power instead, a charger on one 16 A leg (3680 W) never reached the default 4000 W multi threshold.
- **Hold writes the setpoint entity's own minimum when it cannot take 0** (its `min` attribute above 0), so such a device may still draw that minimum in Hold; the allocation claims nothing for it.
- **With no current or power sensor the device is booked as drawing the allowance it was last handed** while it is active, as a power station without its AC sensors is booked at its commanded speed. In output-only mode that assumes your automation follows the published allowance.
- **Stale grid CTs: as an EVSE.** Past the timeout a running device (active, drawing over 1 A - its booked allowance counts) keeps its minimum; one that is not running gets 0. The fallback setpoint is NOT written then - it is written only when the entry unloads (the stand-in for a charger's profile running out; a charger's own default after the profile timeout is the device's watchdog, which the re-send interval keeps fed).
