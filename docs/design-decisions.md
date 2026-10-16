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
