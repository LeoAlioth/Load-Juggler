# LJ-1: A charger's cut goes out at once, not at its next command slot

This is a personal project. Do not log it to any work-context file.

- **Repo:** `/Users/anzek/Projects/Dynamic_OCPP_EVSE`, branch `dev`. Manifest is already `2.1.6`, so there is no version bump.
- **TODO:** `dev/TODO.md:8`, "[BUG] A second car starting overloads the breaker until the first car's cut lands".
- **Decision (Anze, 2026-10-12):** short peaks slightly over the breaker are tolerable. The goal is to shorten them, not to guarantee they never happen. A decrease that matters goes out immediately, outside the load's *Update frequency*.
- **Status:** the plan below was prototyped on a scratch copy of the repo, not on the repo itself. The full suite passed on it (1674, i.e. 1671 + 3 new). The numbers further down come from that prototype.

## Root cause

- **One gate serves every load type.** `entities/load.py:713-723` reads `CONF_UPDATE_FREQUENCY` and returns before any command while `now - _last_command_time < interval`. It runs before the limit is built (`:738-818`) and before dispatch (`:838-886`).
  - Plugs, tanks and stations pass through the same gate.
  - `control/ocpp.py` has no gate of its own. It only skips Finishing/Faulted (`:63-70`) and stamps the slot (`:240`, `control/__init__.py:7-12`).
- **The engine reacts at once, the old charger's command does not.** When a lower-priority car starts, the engine lowers the higher-priority charger's permit (`raw_permit`, `load.py:447`) in the same cycle. That charger's new limit then waits behind the gate for up to 30 s on Andrej's site. Meanwhile the new car's first command goes out at once, because its `_last_command_time` is `-inf` (`load.py:228`).
- **Smoothing adds more lag on the way down.** `control/smoothing.py:91-179` (EMA, then Schmitt, then the ramp, `max_down` at `:157`) keeps the command above the permit for 10-20 s even when a slot is open.
- **Order within one cycle does not matter in practice.** Processors run in `sorted(entry_id)` order (`sensor.py:128`), not by priority, so a start can go out before a cut in the same cycle. The gap is one blocking OCPP call (well under 1 s), while a car takes several seconds to ramp. **Cuts will not be reordered to go first.**

## Questions settled

1. **Every decrease, or only the one a new start depends on?**
   - **Chosen:** every decrease that keeps the charger running, by more than the dead band, measured against the limit the charger last accepted (`_last_commanded_limit`, set at `ocpp.py:232`).
   - It is sent at the engine's permit, floored at the minimum. The smoothing pipeline is re-seeded there, the same way `smoothing.py:62-70` fast-starts from 0.
   - This needs no new state. Tagging which load "depends on" which would need engine-side bookkeeping, and gave the same result in every tested case.
2. **Why not just send each smoothed decrease immediately?** That was prototyped first, and it is worse than today.
   - It sends a command every site cycle while the ramp descends: 4 commands in 8 s.
   - That overflows `readout_watch.COMMAND_MEMORY = 4` (`engine/readout_watch.py:388`). `_limits_between` (`:413-423`) then finds no limit, so the stale 16 A reading is believed again.
   - The household then reads 0, the engine raises the permit, and the breaker carried 22.2 A for 238 s, against 20.6 A for 58 s today.
   - The cut must therefore jump straight to the permit, not walk down the ramp.
3. **Pauses and stops keep the slot.**
   - A cut fires only when `not short` (`load.py:709`), Dynamic Control is on, and there is no pause dwell. It never goes below the minimum.
   - Below the minimum, the existing two-cycle shortage confirmation and the pause both still happen at the slot. `test_a_dip_between_two_commands_does_not_pause_the_second_charger` depends on this and still passes.
4. **Plugs, tanks and power stations are unchanged.**
   - The gate is shared, but only OCPP records `_last_commanded_limit`. Plugs (`plug.py:181/198`), tanks (`hot_water_tank.py:441`) and stations (`power_station.py:148`) only stamp the slot.
   - A plug's or tank's only decrease is a switch-off (to 0), which is a stop.
   - Stations write over BLE and only change on a full device step. Bring them in only if a station ever shares a breaker with chargers.
5. **Readout watch / assumed draw.**
   - A cut updates `EVSE_RT_COMMANDED_LIMIT` (`ocpp.py:238`) immediately, and `track_commands` records it (`readout_watch.py:400-410`).
   - On the next cycle, `_await_new_reading` (`load_builders.py:262-318`) takes the charger's draw to be the new limit, because the reading predates it. A cut is believed as soon as it is read.
   - That is what lets the engine hand the freed room to the new car safely. Jumping straight to the permit sends one command per drop, so `COMMAND_MEMORY` is not exceeded (see Risks).
6. **Compliance (`control/compliance.py`).**
   - On a cut cycle it compares the offered current with the previous command, exactly as it does at a slot.
   - The next check sees the command moved by more than the dead band and skips it as "ramping" (`:183-190`). The mismatch clock is time-based (`:203-206`), so more frequent checks cannot cause a false reset.
   - The tolerance (`:179`) is untouched. That is the separate TODO entry `:9`.
7. **Rate limits.** OCPP 1.6 has no rate limit, and the ocpp integration serialises calls. In the closed loop the cut adds 2 profiles per 18 minutes (Stara: 36 to 38).

**Rejected alternative:** hold a new car's start until the loads ahead have taken their cuts. It delays every start by up to one interval, and Anze chose immediate cuts instead.

## Change (minimal diff)

`control/smoothing.py`: append this.

```python
def cut_now(sensor, raw_permit: float, floor: float, dead_band: float) -> float | None:
    """The level to cut a running charger to at once, outside its command
    slot, or None to wait for the slot as usual. Only a charger records the
    limit it last accepted (control/ocpp.py), so plugs, tanks and stations
    always wait. Straight to the permit, not down the ramp: stepping down sent
    a profile every site cycle, and the readout watch lost the limit the
    reading was taken under."""
    # ponytail: every cut that keeps the load running, not only one a new start
    # depends on; gate it on the limiting pool if Solar-mode dips prove noisy.
    last = sensor._last_commanded_limit
    if last is None:
        return None
    level = max(raw_permit, floor)
    if level >= last - dead_band:
        return None
    sensor._ema_current = sensor._schmitt_current = sensor._rate_limited_current = level
    sensor._schmitt_state = "falling"
    return level
```

`entities/load.py`:

- Add `CONF_FILTER_DEAD_BAND` and `DEAD_BAND` to the `..const` import.
- Import `cut_now` beside `apply_smoothing` (`:33`).
- Move `dynamic_control_on = load_rt.get("dynamic_control", True)` from `:736` up to the gate.
- Replace the gate's `if` at `:717`:

```python
        now_mono = time.monotonic()
        dynamic_control_on = load_rt.get("dynamic_control", True)
        # A cut that keeps the charger running goes out now, not at its slot:
        # the engine has already handed the room to someone else (a second car
        # starting). Raises, pauses and stops keep the slot.
        cut = None
        if not short and dynamic_control_on and self._pause_started_at is None:
            cut = cut_now(
                self, raw_permit, min_charge_current,
                get_entry_value(hub_entry, CONF_FILTER_DEAD_BAND, DEAD_BAND),
            )
            if cut is not None:
                _LOGGER.debug(
                    "Cut for %s goes out before its slot: %.1f A -> %.1f A",
                    self._attr_name, self._last_commanded_limit, cut,
                )
                self._available_current = cut
        if cut is None and now_mono - self._last_command_time < command_interval:
```

After that, the `runnable` branch yields `limit == cut`, and `send_ocpp_command` stamps the slot. A cut therefore restarts the interval, so the next raise comes one full interval later.

Docs and strings, in the same commit:

- **`AGENTS.md:217`:** "Per-load `update_frequency` gates command sends inside the processor." becomes "...inside the processor; a cut that keeps an EVSE running goes out at once (`control/smoothing.cut_now`)."
- **Charger `update_frequency` description**, `translations/en.json:360,794` and `sl.json:360,794`:
  - en: "How often to send updated charging profiles to this charger. A cut the site needs goes out at once."
  - sl: "Kako pogosto se pošiljajo posodobljeni profili polnjenja tej polnilni postaji. Znižanje, ki ga lokacija potrebuje, se pošlje takoj."
  - These are strings only, so no migration is needed.

## Tests

1. **`dev/tests/test_priority_slow_meters.py`** (the closed loop):
   - Add `DEAD_BAND` to the const import.
   - In `World.__init__` add `self.sent = {"stara": 0, "nova": 0}`. In `accept`, `self.sent[data["devid"]] += 1`. In each `_evening` row add `"sent": dict(world.sent)`.
   - In `test_the_second_charger_charges_through_beside_the_first`, replace `<= BREAKER_A + 1.0` (`:313`) with:

     ```python
     peak = max(row["grid"] for row in log)
     assert peak <= BREAKER_A + DEAD_BAND, f"the breaker carried {peak:.1f} A"
     slots = len(log) * CYCLE_S / 30
     for slug in ("stara", "nova"):
         sent = log[-1]["sent"][slug] - log[0]["sent"][slug]
         assert sent <= slots + 3, f"{slug} was sent {sent} profiles in {slots:.0f} slots"
     ```

   - Rewrite the comment at `:307-310`: the cut now lands on the cycle the second car is granted its minimum, and the breaker stays at 20.0 A (20.1 A in Shared).
   - Checked in the prototype: with `cut_now` stubbed out (the current behaviour) it fails "the breaker carried 20.6 A" in Priority and Optimized. With the change, all 21 tests pass.
2. **New `dev/tests/test_cut_before_slot.py`**, the unit test of the gate, with a `SimpleNamespace` charger and `dead_band=0.3`:
   - last 16, permit 12.3 → 12.3, pipeline re-seeded at 12.3, Schmitt falling;
   - permit 2 → minimum 6;
   - 15.8 (inside the dead band), a raise, `last=None`, and last 6.2 with permit 0 → `None`, pipeline untouched.
3. **`dev/tests/test_readout_hunting.py`**, the negative controls that reproduce the field hunting with a safeguard patched off:
   - `_session` with `lockstep_enabled=False` (`:326-329`) and `test_a_slow_meter_hunts_when_its_old_reading_is_believed` (`:522-533`) also patch `custom_components.dynamic_ocpp_evse.entities.load.cut_now` to `lambda *a: None`. The field build had no early cut.
   - Add a docstring line saying so. Without the patch, the early cut alone settles the stuck-at-0 case at 10.8 A (half the room) instead of hunting, so "stops >= 4" no longer holds.

## Release notes and TODO

**`RELEASE_NOTES.md`**, `## 2.1.6` → `### Bug Fixes`. Add as the first bullet, next to the two-charger entries:

> - **A charger's cut no longer waits for its next command when a second car starts**: when a lower-priority charger started, Load Juggler lowered the charger ahead of it at once, but that cut went out only at the charger's next command - up to its *Update frequency* later - while the new car's start went out straight away. On a 20 A site with two go-eChargers commanded every 30 s the breaker carried 21.1 A until the cut landed. A cut that leaves a charger running now goes out on the site update that asks for it, straight to the new allocation rather than stepping down to it, and starts the charger's command interval over; raises, pauses and stops still wait for the interval. Smart plugs, tanks and power stations are unchanged. In the two-charger test the breaker no longer passes 20 A, where it carried 20.6 A for a minute.

**`dev/TODO.md:8`**: replace the entry with a verify entry.

> - [ ] **[BUG] A charger's cut goes out before its command slot: verify on a real site** *(shipped in 2.1.6, closed-loop tests only)* - `control/smoothing.cut_now` sends a running charger's cut on the site cycle that asks for it, at the engine's permit floored at the minimum, and re-seeds the smoothing there; raises, pauses and stops keep the *Update frequency* slot. Check at Home (the EVBox under the block import limit): a big house load switched on mid-charge logs "Cut for ... goes out before its slot" and moves `last_set_current` within one site cycle, and profiles per hour stay near 3600 / update frequency plus a few. On Andrej's two go-eChargers (only once Anze approves an install there): a second car starting keeps the grid CTs within 20.3 A and Stara's cut lands within one site cycle of Nova's start. Known ceiling: on a flickering Solar-mode supply every dip of more than the dead band is a cut, and each cut restarts the interval - if a cloudy day shows more than about 2 cuts per interval, gate `cut_now` on the limiting pool (grid/breaker) rather than every pool.

## Steps

1. Edit `control/smoothing.py` and `entities/load.py` as above.
2. Make the test changes: tests 1, 2 and 3.
3. Update `RELEASE_NOTES.md`, `dev/TODO.md`, the `AGENTS.md` line, and both translation files.
4. Run the commands below, one per call.
5. Commit locally on `dev`, as one commit whose message ends with the test count. **Do not push**: dev auto-deploys.

## Commands

```
.venv/bin/python -m pytest dev/tests/test_cut_before_slot.py -q
.venv/bin/python -m pytest dev/tests/test_priority_slow_meters.py -q
.venv/bin/python -m pytest dev/tests/test_readout_hunting.py -q
.venv/bin/python -m pytest dev/tests -q -n 8
```

Expected: **1674 passed** (1671 before + 3). The full run takes about 30 s.

## Verify on a real site

- **Default, Home only.** Install the dev build the usual HA_Configs way, with snapshots before and after, and note it in `home/NOTES.md`.
  - For one evening, set `custom_components.dynamic_ocpp_evse.entities.load` to debug with `logger.set_level`. This is runtime only and does not persist.
  - While the EVBox charges against the block limit, switch on a 2 kW+ load. Expect the "Cut for ... goes out before its slot" line, and the charger's `last_set_current` dropping within about 2 s of the grid step rather than up to its update frequency.
  - Count the profiles per hour from the ocpp log or the `last_set_current` history. It should be about 3600 / update frequency plus a few.
- **Andrej's site: only if Anze approves an install there.** On the first two-car evening:
  - Reuse `lj-3-verify.py` item 1's both-plugged windows. Max phase on the grid CTs should be ≤ 20.3 A.
  - Stara's `last_set_current` should drop within one site cycle of Nova's first non-zero profile, not up to 30 s later.
- **Kozolec has no EVSE on OCPP** (Pond EVSE is a plug), so there is nothing to check there.

## Risks

- **Solar-mode flicker.**
  - Each dip of more than the dead band below the last command is a cut, and each cut restarts the interval, so raises can lag on a cloudy day. This costs some solar energy; it is not a safety issue.
  - None of the closed loops has a noisy sun, so it is untested. Watch it at Home.
  - Fix if needed: only cut while the grid/breaker pool is the one limiting the charger.
- **`COMMAND_MEMORY = 4`.**
  - A slow meter (120 s) with a 30 s interval already gets about 4 limit changes per reading gap; a cut can make it 5. The reading then predates all remembered commands and is believed as it stands.
  - Watch for "back on its reading" right after a cut. The fix is to raise it to 8 (`readout_watch.py:388`), one constant.
- **Finishing/Faulted with a stale `_last_commanded_limit`.** The cut can fire every cycle, but `send_ocpp_command` returns early (`ocpp.py:63-70`), so no OCPP call is made. Compliance and status just run each cycle. This is harmless.
- **Shared mode at 20.1 A steady** comes from the engine's 0.1 A rounding (9.2 + 9.2 + 1.7). It exists today and is unrelated, so it is left alone. The new assertion allows the dead band for it.
- **Cut ordering within a cycle is not addressed** (start before cut, `sensor.py:128`). The gap is under one OCPP round trip, against a car's multi-second ramp.

## Measured in the prototype

`test_priority_slow_meters` evening, 15 minutes after plug-in:

| Mode / allowance | Peak today | Time over 20 A today | Peak after | Time over after | Stara profiles in 18 min, before / after |
|---|---|---|---|---|---|
| Priority, breaker only | 20.6 A | 60 s | 20.0 A | 0 | 36 / 38 |
| Optimized, breaker only | 20.6 A | 60 s | 20.0 A | 0 | 36 / 38 |
| Shared, breaker only | 20.1 A (rounding) | steady | 20.1 A | steady | 36 / 38 |
| Priority, tight allowance | 18.6 A, allowance over for 898 s | - | 14.2 A, allowance over for 0 s | - | 36 / 38 |

The prototype (scratchpad copy) is temporary. Implement from this file.
