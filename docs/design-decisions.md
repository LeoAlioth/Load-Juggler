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

**Per load, counting back from the next full charge (2026-10-18):** the verdict is not handed to every load at once. Each load takes it only when `starts_early` says so: planned kWh x 1000 >= the load's minimum power x the hours until the battery is next full (`forecast_excess_full_entity_id`, labelled *Next full charge*; Load Insights' sensor of that name, the first hour its SOC forecast reaches full, rounded down to the hour; the pacing horizon stands in when unset), latched per load with a 20 % band. Why: a tank boosts on the setpoint and heats at once whatever the pool holds, so on a midnight verdict with a pool smaller than its element it flapped 42/75 C every grace period (Kozolec, 17-18 Oct) and the battery fell from 76 % to 47 %. The site verdict stays on while at least one load is due; with no loads (a bare hub) it is unchanged. Code: `starts_early`, `_apply_forecast_excess`, `_excess_verdict(site, load)`.  The pool top-up is site-wide, but a load that is not due does not count it (`_excess_permits` takes it off the pool): found live on Kozolec 18 Oct, where the bee room heater being due let the top-up start the boiler.

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
