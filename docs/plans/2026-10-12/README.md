# Plans 2026-10-12 - Load Juggler + Load Insights batch

Written 2026-10-12, approved by the owner; to be RUN when he says (likely end of week, by weekly plan usage).
Every plan is self-contained (an agent can run it cold). Planning agents changed nothing in the repos or on
the sites. Before running: check plan usage (pause at 90 %, CLAUDE.md), `git status` clean in both repos,
dev up to date.

## Run order

| # | Plan | Repo | What | Notes |
|---|------|------|------|-------|
| 1 | [lj-3-verify-shipped.md](lj-3-verify-shipped.md) + [lj-3-verify.py](lj-3-verify.py) | LJ | Check the four "shipped, verify on site" TODO items from history | Read-only. Pond EVSE restart is already on since 10-05 - just check history. Prune/annotate dev/TODO.md after |
| 2 | [li-2-slicing-invariance.md](li-2-slicing-invariance.md) | LI | Re-verify slicing invariance (last 0 on 10-04), fix what differs; recorder flush before each pass; RESTART= dial + fixes | ~30-45 min SLICINGS=all card on the Mac. Doubles as li-1's slicing card |
| 3 | [li-1-daily-repair.md](li-1-daily-repair.md) | LI | Daily repair (c689283) is ALREADY SHIPPED - re-bench on today's code, new tests, docs | REPAIR_SLACK_NOISE=1e9 = off for the ablation; shares the _day_rows persistence with li-2 |
| 4 | [li-3-temperature-evidence.md](li-3-temperature-evidence.md) | LI | Temperature term engages on statistical evidence, not a fixed 336 h | Forecast work re-opened for this + li-4 only |
| 5 | [li-4-house-from-parts.md](li-4-house-from-parts.md) | LI + Kozolec HA | Whole-house forecast built from its parts; Kozolec minimum SOC helper without Pond EVSE + Power Strip | Part B: count the Well Pump again, no tank spreading (decided) - independent, can run first |
| 6 | [li-5-running-markdown.md](li-5-running-markdown.md) | LI | `running_markdown` attribute (running loads, named + unnamed, links), #id on the naming page | English only; sub-meter duplicates fixed in the plan |
| 7 | [lj-1-immediate-cuts.md](lj-1-immediate-cuts.md) | LJ | A cut beyond the dead band goes out at once (cut_now), not at the load's next slot | Watch Solar modes on cloudy days at Home |
| 8 | [lj-7-tank-flapping.md](lj-7-tank-flapping.md) | LJ | the third site's tank 42<->40 (Optimized/Strict rounding bug + denied-idle-at-floor rule); Home's boiler only watched | Install on the third site too (decided) |
| 9 | [lj-6-energy-prefill.md](lj-6-energy-prefill.md) | LJ + LI | Setup-form defaults from the Energy dashboard; LI's model.py split, verbatim copy to LJ | Setup forms only, never Configure pages |
| 10 | [lj-5-household-refactor.md](lj-5-household-refactor.md) | LJ | household = grid + Σ(inverter output − input); wiring_topology deleted; migration step 9 | Biggest; last |

Then: one push per repo, installs on **Home and Kozolec**; the LJ build also on **the third site** (for lj-7, decided 2026-10-12) - LI builds reach the third site only when the owner says. Pushed builds
download and restart HA by themselves (automation "Load Juggler family - download pushed build" +
script.lj_family_restart_after_build, 2 min debounce). Snapshot + NOTES.md per site.

## Status, 2026-10-18

The TODO entries carry the numbers (`JUG-n` in Load Juggler's `dev/TODO.md`, `INS-n` in Load Insights'); the plan ids below stay as they are.

| # | Plan | Status | TODO |
|---|------|--------|------|
| 1 | lj-3 verify shipped | DONE 2026-10-17 (88d577f): item 1 PASS (Optimized) / FAIL (second charger paused), item 2 Home PASS / third site not exercised, item 3 N/A, item 4 PASS | results annotated in JUG-1, JUG-4, JUG-9 |
| 2 | li-2 slicing invariance | not started | INS-13 |
| 3 | li-1 daily repair | repair shipped (c689283); re-bench, tests, docs open | INS-14 |
| 4 | li-3 temperature evidence | not started (`MIN_HOURS = 336` is still the rule) | INS-15 |
| 5 | li-4 house from parts | Part A built 2026-10-17 (2e5ca5f); Part B (Kozolec helper) superseded: the helpers were replaced by the planning sensors 2026-10-17 | - |
| 6 | li-5 running markdown | not started | INS-17 |
| 7 | lj-1 immediate cuts | prototyped on a scratch copy, not in the repo | JUG-2 |
| 8 | lj-7 tank flapping | code DONE 2026-10-18 (db2d02d, with the owner's review); third-site check open | JUG-4 |
| 9 | lj-6 energy prefill | not started | JUG-22 |
| 10 | lj-5 household refactor | not started | JUG-10, JUG-11 |

## the owner's decisions (2026-10-12)

- lj-1: 30 s peaks slightly over the breaker are tolerable (meters average ~90 s, slow-blow fuses) - minimise, not guarantee.
- lj-5: delete wiring_topology; keep the "grid flows through this inverter" tick as a permanent option; inverter-rating cap's house figure from output sensors on every site.
- lj-6: pre-fill only on the initial setup forms, never on options pages (cleared fields stay cleared).
- li-2: yes to the RESTART= dial and to the recorder flush.
- li-4: minimum SOC excludes only the Pond EVSE and the Power Strip ("loads running over target only").
- li-5: English for now; fix the sub-meter duplicate / missing-link gap.
- lj-7: the LJ build with the tank fix ALSO goes to the third site (he is the one on Sequential - Optimized).
- li-4 Part B: the minimum SOC helper COUNTS the Well Pump again (only Pond EVSE + Power Strip excluded) and drops the even spreading of the tanks - every remaining load as forecast hour by hour (the plan's 'literal' version, ~16 % vs 18 % on 10-12).
- li-5: the named-load on/off sensors move onto per-load runs in THIS batch (not a follow-up).
- li-3: run on the plan's defaults (ALPHA 0.005 swept 0.05/0.0005, 3 % floor dropped unless its variant wins, both fixes in, calendars/inputs under the same rule).
- Rejected / parked: LJ battery SOC sanity check (new-install problem, not LJ's job); entity-backed inverter capacity (edge case); LI multi-step pairing and edge congestion - discuss after this batch.

## Open questions for the owner

None (all answered 2026-10-12).

## Also open (not in the batch)

- Kozolec temporary corrected SOC - reverts after the BMS resync (automation.kozolec_bms_soc_resynced_at_full lists the steps).
- 18 Oct check-in (scheduled task kozolec-andrej-checkin-1018).
- Possible LI quirk: PowerSpec.polarity says inverted while .rate already holds HA's corrected sensor (check during lj-6).
- Kozolec automation.track_water_volume_changes logged one str/int compare error after a restart.
