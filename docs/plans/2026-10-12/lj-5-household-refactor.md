# LJ-5: One household formula, no wiring topology

Repo: Load Juggler `/Users/anzek/Projects/Dynamic_OCPP_EVSE` (branch dev). Anze approved this on 2026-10-12. It
closes two TODO items, *"[BUG] The wiring-topology labels describe the hardware..."* and *"[FEATURE] The household
sum telescopes..."*. It is a personal project, so don't log it to work-context. Commit locally on dev and don't push:
dev auto-deploys. Release section: **2.1.6**. The manifest is already 2.1.6.

## Facts this plan rests on (checked 2026-10-12)

**Every consumer of the flag today.**

| Where | What it does with `wiring_topology` | After this plan |
|---|---|---|
| `calculations/utils.py:107` `compute_household_per_phase` | Two formulas. Parallel: `cons + out − exp`. Series or off-grid: `out − draws`. | Replaced by the single sum |
| `engine/hub_calculation.py:305-347` `_mixed_household_per_phase`, called at `:876` | A "best-effort superposition" of the two formulas for mixed fleets | **Deleted** |
| `engine/fleet.py:396` `fleet_topology`, `:408` `mixed_topologies` | Pick a formula; `site.wiring_topology` | **Deleted** |
| `engine/fleet.py:415` `_battery_output_term` | Series → `bp`; parallel → `max(0, bp)`. Used in the estimate with no output sensors, and for unmetered members | **Deleted**: always the signed `bp` |
| `engine/fleet.py:566` `member_solar` | Series or off-grid → `out − bp`; parallel → `out` | `(out − in) − bp` for every member |
| `engine/fleet.py:378` `sum_outputs(topology=…)` | Sums outputs, filtered by topology for the composite | Replaced by `net_output` |
| `calculations/target_calculator.py:701` `_house_on_inverters` | Off-grid, or series with a per-phase household → `Σ max(0, house − grid import)`. Otherwise the solar balance. | Off-grid, or any per-phase household → the household form |
| `engine/readers.py:716` | Reads the key into `FleetMember.topology` | Reads the input keys instead |
| `engine/hub_calculation.py:1396, 1507, 1549` | `fleet_topology` → SiteContext, plus a log string | Removed |
| `config_flow/schemas.py:438-472`, `pages.py:1001`, `helpers.py:355`, `const/hub.py:25-28`, en/sl `inverter_config` and `inverter_core` | Form field, overview text, legacy import list, constants | Replaced, see below |
| `dev/tests/run_tests.py:302, 333, 385, 421, 509, 526` | The harness simulates each wiring | Arrangement model, see Tests |

**What the flag still answered once the household no longer needs it.** The TODO named two facts:
- *How much of a battery's flow shows in the AC output.* Every remaining consumer asks "is this member's battery flow
  inside its own output − input?". The answer is yes by construction once **a battery is configured on the inverter it
  sits behind**: a hybrid's battery on the hybrid, and an AC-coupled battery with an inverter of its own as an inverter
  entry of its own. Off-grid, LJ already treats parallel exactly like series (`member_solar`, `compute_household_*`).
- *Battery conversion losses.* LJ has no consumer of this. `calculations/forecast.py:48` says charge efficiency is
  deliberately not modelled. LI keeps its own `topology` flag for this: `insights/grid.py:86-101` uses it for
  DC-vs-AC round-trip efficiencies, and that stays LI's business.

**So the flag goes entirely** (decided by Anze, 2026-10-12). No renamed flag, no new labels. Deleting it is also how
the label bug gets fixed.

**Nothing the flag did is lost.** Each former consumer and what replaces it:

| Former consumer | What the flag decided | Replaced by | Same result on today's configs |
|---|---|---|---|
| `compute_household_per_phase`, parallel branch | `cons + out − exp` | `grid + (out − 0) − draws`: a parallel member has no input | Yes, exactly |
| `compute_household_per_phase`, series or off-grid branch | `out − draws`, grid ignored | `grid + (out − grid) − draws`: the grid-flows-through tick makes the input equal the grid, so the grid cancels. Off-grid the grid is 0 | Yes, exactly |
| `_mixed_household_per_phase` with `mixed_topologies` | A guessed sum of the two formulas | The same single sum. Each member adds `out − in`; a PV inverter on a hybrid's load port nets through the hybrid's signed output | Exact for all-series fleets. For mixed fleets the old result was a guess, so the new one is the correct value; a PV inverter on the grid side needs the hybrid's input sensors (migration warns) |
| `fleet_topology` → `site.wiring_topology` | One site-wide word for the calculator and the log | Nothing needs a site-wide word: the household arrives computed, and the cap uses it (next row) | n/a |
| `_house_on_inverters` (`target_calculator:701`) | Series → `Σ max(0, house − grid import)`; otherwise the solar balance | `Σ max(0, house − grid import)` on every site with a per-phase household; off-grid as before | Kozolec yes. Home and Andrej move by sensor noise (decided OK) |
| `member_solar` | Series or off-grid → `out − bp`; parallel → `out` | `(out − in) − bp` for every member | Yes where it runs: all three sites have a solar sensor, so it is unused there. A battery-less parallel member gives `out − 0 − 0`; off-grid gives `out − bp` |
| `_battery_output_term` (estimate, unmetered members) | Series → `bp`; parallel → `max(0, bp)` | The signed `bp` always: a battery belongs to the inverter it sits behind, and an AC-coupled one is its own inverter entry | Yes on all three sites: two have no battery, Kozolec was series |
| `sum_outputs(topology=…)` | Split the outputs for the composite | `net_output(members, grid)` | n/a |
| `solar_is_unsplit` (via `FleetMember.off_grid`) | Only off-grid outputs were treated as carrying the battery flow | Every output carries its own battery's flow | Kozolec: same. Grid-tied: no such site |
| Config form, overview page, hub import list, harness | Asked for, showed, imported and simulated the word | Input sensors and the grid-flows-through tick; the overview shows the input; the import maps series to the tick; the harness gets an `inverters:` list | Migration keeps every site's household |
| Battery conversion losses | Not consumed anywhere in LJ (`forecast.py:48` does not model efficiency) | Nothing needed. LI keeps its own flag for its efficiencies | n/a |

**What else is in the arithmetic.**
- The feedback loop subtracts the same smoothed `managed_draws` the household uses (`hub_calculation.py:1594-1596`),
  and `grid_without_managed_draws` keeps `cons − exp` exact. So `cons_adj − exp_adj = grid − draws`, and the
  household can be built from the **pre-feedback** signed grid plus explicit draws with no change in value.
- Grid CT phase remaps (`_apply_phase_remaps`) permute `site.consumption`/`export` after SiteContext is built. Inverter
  outputs are not remapped.
- The published `household_power` (`hub_result.py:802-831`) uses the identity (grid + solar + battery − managed)
  whenever a solar sensor reads. All three sites have one, so this change does not move the published sensor. The
  per-phase household drives allocation (`_get_household_per_phase`) and the inverter cap.

**The three sites** (diagnostics `HA_Configs/{home,kozolec}/snapshots/2026-10-12/configs/diagnostics.json`; Andrej's
read live, read-only, 2026-10-12):

| Site | Grid | Inverter entry | Today | Migrated to | New formula | Same? |
|---|---|---|---|---|---|---|
| Home | SE M1 currents a/b/c (A, import +, `invert_phases` false) | SE17K, `parallel`, outputs `i1_ac_current_a/b/c`, solar `i1_ac_power`, no battery | `cons_adj + out − exp_adj` | no input | `grid + out − draws` | yes, `cons_adj − exp_adj = grid − draws` |
| Kozolec | none (off-grid) | MultiPlus-II, `series`, output `gx_device_consumption_power_l1` (W, L1 only), solar `kozolec_total_pv_power`, battery | `out − draws` | input = grid meter | `0 + (out − 0) − draws` | yes |
| Andrej | SE M1 power a/b/c (W, `invert_phases` true) | SolarEdge, `parallel`, outputs `i2_ac_current_a/b/c`, solar `i2_dc_power`, no battery | `cons_adj + out − exp_adj` | no input | `grid + out − draws` | yes |

- No site has a mixed fleet, a battery on a parallel member, or a grid-tied series member.
- All three have a solar sensor, so `member_solar` is unused at every site.
- Kozolec's generator on the MultiPlus AC-in is unread. Off-grid the grid term is 0, and when the generator runs its
  power is inside the output, so the house is still `out − draws`.

**Load Insights vocabulary** (`Load_Insights/custom_components/load_insights`): `const.py:59-64`,
`detection.py:_inverter_terms`, `config_flow._inverter_fields`.
- Its formula is already `house = grid meter + Σ (output − input)`.
- It stores the input under an `in_` prefix and labels it **"Grid-side power"**: "what the inverter draws from the
  grid at its own AC input. A PV string inverter has none".
- LJ adopts the same words: **output** and **input (grid side)**, positive when the inverter draws from the grid.
- LI has no "input is the grid meter" option. It reads history and always has the meter's own readings.

## Final formula and data model

```
household[p] = max(0, grid[p] + Σ_inverters (output[p] − input[p]) − managed_draw[p])
```

- `grid[p]`: the signed, smoothed grid reading per phase, **before** the feedback loop and **after** phase remaps.
  Positive means import. Off-grid it is 0 on phases with outputs and None elsewhere. A None phase gives None.
- `output[p]`: the inverter's signed AC output. Positive means delivering. The existing `_read_signed_amps` reads it,
  so a parent whose load port is back-fed reads negative.
- `input[p]`: what the inverter draws at its AC input, signed. Positive means drawing from the grid side. A PV string
  inverter has none (0). A member with **"input is the grid meter"** uses `grid[p]` itself, so its grid term cancels
  exactly, per phase, even under a remap.
- Battery power keeps its convention: positive means discharging. A battery configured on an inverter is that
  inverter's: its flow is inside that inverter's `output − input`.
- One member rule everywhere a fleet output is summed. Each inverter contributes `net = output − input`:
  - household: per phase;
  - derived solar: `max(0, net·V − bp)`;
  - measured fleet output for the coverage gate and headroom: `Σ net·V`.

**Stored keys** (inverter entries):
- `inverter_input_phase_a_entity_id`, `inverter_input_phase_b_entity_id`, `inverter_input_phase_c_entity_id`. They
  are optional, and accept current or power like the outputs (A, mA, W, kW).
- `inverter_input_is_grid`: bool, default False.
- `wiring_topology` is removed, except as a legacy string inside the migration helper.

**FleetMember** (`engine/fleet.py`):
- Removed: `topology` and `off_grid`. The latter's only readers were `member_solar` and `solar_is_unsplit`.
- Added: `input` (smoothed PhaseValues), `input_raw`, `grid_input: bool`.

**fleet.py functions**:
- `member_input(m, grid, raw=False)`: returns `grid` when `m.grid_input`, else `m.input` (or `m.input_raw`).
- `net_output(members, grid, raw=False)`: returns `Σ (output − member_input)` per phase over the members that have
  output sensors, or None when no member has output sensors. It replaces `sum_outputs`.
- `member_solar(m, V, grid)` returns `max(0, net.total·V − (bp or 0))`.
- `solar_total(members, V, grid)`.
- `output_power_measured(members, V, grid)`.
- `output_power_total(..., grid)`: the battery term is always the signed `bp`.
- `output_power_estimate(...)`: the signed `bp`, with no topology.
- `solar_is_unsplit`: no `off_grid` condition.
- Deleted: `fleet_topology`, `mixed_topologies`, `_battery_output_term`, `sum_outputs`.

**SiteContext** (`calculations/models.py`):
- `wiring_topology` is deleted.
- `inverter_output_per_phase` is renamed **`inverter_net_per_phase`** (Σ output − input, A). That is 32 references
  across `custom_components` plus `run_tests.py`.

**utils.py**: `compute_household_per_phase` is replaced by
`household_per_phase(grid, net, draws) -> PhaseValues | None`. It contains one expression and no site, off-grid or
topology argument.

## Change list, file by file

All paths are under `custom_components/dynamic_ocpp_evse/`.

1. `const/hub.py`
   - Delete `CONF_WIRING_TOPOLOGY`, `WIRING_TOPOLOGY_PARALLEL`, `WIRING_TOPOLOGY_SERIES` and `DEFAULT_WIRING_TOPOLOGY`.
   - Add `CONF_INVERTER_INPUT_PHASE_{A,B,C}_ENTITY_ID` and `CONF_INVERTER_INPUT_IS_GRID`.
   - Re-export them through `const/__init__`.
2. `calculations/utils.py`: `household_per_phase` replaces `compute_household_per_phase`, and its 60-line docstring
   shrinks to the formula plus one line on where `grid` and `draws` come from.
3. `calculations/models.py`: delete `wiring_topology`, rename `inverter_net_per_phase`, and fix the comment at `:285`
   (topology-aware → signed battery term).
4. `calculations/target_calculator.py`
   - Drop the `WIRING_TOPOLOGY_SERIES` import.
   - In `_house_on_inverters` the condition becomes `site.is_off_grid or site.household_consumption is not None`, and
     its docstring is rewritten to two cases.
   - Rename the field at `:1020`.
5. `engine/fleet.py`
   - Make the FleetMember and function changes listed above.
   - Rewrite the module docstring's Solar bullet (`out − in − battery` per member, no wiring).
   - Delete the four functions listed above.
6. `engine/readers.py`
   - `_read_fleet_member` reads the input entities exactly like the outputs: `_read_signed_amps`, then
     `_smooth_member_output` under the prefix `inv_in_{entry_id}_`, with `input_raw` coerced. It also sets
     `grid_input`.
   - Drop the topology line `:716`.
   - `_check_entity_availability` gets three more tuples, "Inverter {name} input (L1..L3)".
7. `engine/hub_calculation.py`
   - Delete `_mixed_household_per_phase` (`:305-347`), the `WIRING_TOPOLOGY_*` imports, `fleet_topology` (`:1396`),
     the log string (`:1507`), the field (`:1549`) and `m.off_grid = …` (`:1347`).
   - `inverter_net_per_phase = fleet.net_output(members, smoothed_phases)` at build. That gives presence and totals.
   - `fleet.output_power_total(..., grid=smoothed_phases)`, and both `fleet.solar_total(...)` calls get
     `smoothed_phases`. Sums are remap-invariant, so the pre-remap grid is fine there.
   - `_supply_per_phase` (`:218`): `fleet.net_output(members, raw_phases, raw=True)`.
   - Just before `_apply_feedback_loop`, capture `grid = [None if c is None else c - (e or 0) …]` from the remapped
     `site.consumption`/`export_current`, and pass it to `_apply_household_figures`.
   - There, `net = fleet.net_output(members, grid)`, `site.inverter_net_per_phase = net`, and
     `household = household_per_phase(grid, net, managed_draws)`. The if/else at `:876-882` goes, the log at `:893`
     loses the topology, and both docstrings drop "series / either wiring".
8. `engine/hub_result.py:799`: fix the comment text to `grid + Σ(output − input) − draws`.
9. `units.py:70`: add the three input keys to the unit contract (current or power, `DOMAIN_AMPS`).
10. `config_flow/schemas.py` (`_build_hub_inverter_schema`)
    - Delete `topology_options` and the select.
    - After the three output fields, add three input entity fields with the same `current_power` selector, and
      `vol.Optional(CONF_INVERTER_INPUT_IS_GRID, default=False): bool`.
11. `config_flow/helpers.py`
    - Add the input keys to `_INVERTER_ENTITY_KEYS` and to `_INVERTER_OUTPUT_UNIT_MAP`, renamed `_INVERTER_AC_UNIT_MAP`.
    - New `_validate_inverter_input(hass, entry_id, hub_entry_id, data, errors)`, which sets one of three errors:
      - `input_grid_and_sensors`: the tick plus any input sensor;
      - `input_grid_taken`: another inverter on the hub already has the tick;
      - `input_without_output`: input set but no output sensor.
    - Wire it into `options.py` `async_step_inverter_core` (`validate=`) and `flow.py` `async_step_inverter_config`.
12. `helpers.py`
    - `HUB_INVERTER_IMPORT_FIELDS` keeps the literal `"wiring_topology"` (`LEGACY_WIRING_TOPOLOGY`) so old hubs are
      still stripped.
    - New `legacy_topology_to_input(hass, hub_entry_id, entry_id, options) -> None`. It pops the key, and for
      `"series"` sets `CONF_INVERTER_INPUT_IS_GRID` unless the grid input is taken (see Migration).
    - The migration and `flow.py` `async_step_import` (`:911-922`, after `infer_inverter_features`) both call it.
13. `config_flow/pages.py:1001`
    - The overview shows "input: grid meter" or "input: sensors" (nothing when there is no input) instead of the
      topology word.
    - The reading list at `:810` adds input lines beside the outputs.
14. `config_flow/flow.py:161`: `MINOR_VERSION = 9`.
15. `__init__.py`: add migration step `< 9` (below).
16. `translations/en.json` and `translations/sl.json`, in both `config.step.inverter_config` and
    `options.step.inverter_core`:
    - Delete the `wiring_topology` data and data_description entries.
    - Add the strings below, the three errors under `config.error` and `options.error`, and the battery-page and
      `inverter_core` description lines.
17. Docs:
    - `README.md:126-130`: replace "wiring topology" in the list and the "(a series inverter's output minus its own
      battery flow)" clause with "output less input less its own battery flow".
    - `README.md:243`: the table row becomes "Inverter input phase A/B/C entity" plus "Input is the grid meter".
    - `AGENTS.md:15`: minor version 5 → 9.
    - `AGENTS.md:191`: SiteContext line `inverter_net_per_phase`, with `wiring_topology` removed.
    - `AGENTS.md`, `utils.py` line in Code Structure: `household_per_phase`.

**Strings.** Labels (en / sl):
- `inverter_input_phase_a_entity_id`: "Inverter input sensor for phase A (optional)" / "Senzor vhoda razsmernika za
  fazo A (neobvezno)". B and C are the same.
- `inverter_input_is_grid`: "The grid flows through this inverter, straight from the grid meter" / "Omrežje teče
  skozi ta razsmernik, naravnost z omrežnega števca".

`data_description.inverter_input_phase_a_entity_id`:
- en: "What this inverter takes in at its AC input (grid side), positive while it draws from the grid. A PV string
  inverter has none: leave it empty and its whole output counts. A hybrid the grid flows through has one. The house is
  the grid meter plus what each inverter puts out less what it takes in, so the grid it passes on is not counted
  twice."
- sl: "Kar razsmernik prejema na AC vhodu (omrežna stran), pozitivno, ko jemlje iz omrežja. Nizni (PV) razsmernik ga
  nima: pustite prazno in šteje se ves njegov izhod. Hibrid, skozi katerega teče omrežje, ga ima. Poraba hiše je
  omrežni števec plus to, kar vsak razsmernik da ven, manj tega, kar vzame noter, zato se omrežje, ki ga le prepušča,
  ne šteje dvakrat."

`data_description.inverter_input_is_grid`:
- en: "For a hybrid the grid flows through, when nothing sits between the grid meter and the inverter - no other
  loads, no other inverter. Its input is then the same as the grid meter's reading, so it needs no input sensors.
  Only one inverter can have this."
- sl: "Za hibrid, skozi katerega teče omrežje, kadar med omrežnim števcem in razsmernikom ni ničesar - ne drugih
  porabnikov ne drugega razsmernika. Njegov vhod je tedaj enak odčitku omrežnega števca, zato ne potrebuje
  senzorjev vhoda. To ima lahko le en razsmernik."

`inverter_core.description`:
- en: "The inverter's AC side: capacity, and its output and input sensors per phase."
- sl: "AC stran razsmernika: zmogljivost ter senzorji izhoda in vhoda po fazah."

Battery page description, one sentence appended:
- en: "An AC-coupled battery with an inverter of its own is added as its own inverter."
- sl: "AC-sklopljeno baterijo z lastnim razsmernikom dodajte kot samostojen razsmernik."

Errors:
- `input_grid_and_sensors`: "Choose one: the grid meter, or input sensors." / "Izberite eno: omrežni števec ali
  senzorje vhoda."
- `input_grid_taken`: "Another inverter on this hub already takes its input from the grid meter." / "Drug razsmernik
  na tem merilnem mestu že jemlje vhod z omrežnega števca."
- `input_without_output`: "An input needs this inverter's output sensors too." / "Vhod potrebuje tudi senzorje izhoda
  tega razsmernika."

## Migration (2.8 → 2.9), idempotent

```python
if entry.version == 2 and getattr(entry, "minor_version", 0) < 9:
    data, options = dict(entry.data), dict(entry.options)
    legacy_topology_to_input(hass, data.get(CONF_HUB_ENTRY_ID), entry.entry_id, options)  # pops; series -> tick
    data.pop(LEGACY_WIRING_TOPOLOGY, None)
    hass.config_entries.async_update_entry(entry, data=data, options=options, minor_version=9)
```

- **parallel, or the key absent** → no input. Household = `grid + out − draws`, which is what the parallel formula
  gave today.
- **series** → `inverter_input_is_grid: True`. Household = `0·grid + out − draws`, which is what the series formula
  gave today, grid-tied or off-grid.
- **"Taken" rule**, which handles several series members:
  - The tick goes to one member per hub: the series member with the lowest entry_id.
  - A sibling counts as taking the grid input when it already holds the tick, or still holds legacy `"series"` with a
    lower entry_id.
  - This is deterministic whatever order HA migrates the entries in, and a rerun finds no key, so nothing changes.
  - All-series fleets keep today's `Σ out − draws` exactly. That includes the TODO's SolarEdge on a Deye load port
    configured series/series, where the parent's negative back-feed nets out.
- **Mixed fleets** (series plus parallel) have no exact equivalent: today's composite was a guess.
  - After migration they get `Σ out − draws`, which is exact when the parallel inverter sits on the hybrid's load port.
  - The step logs one WARNING naming the hub: "a PV inverter beside a hybrid: if it feeds the grid side, give the
    hybrid its input sensors".
- **Hub entries** pass through with only the version bump. `strip_hub_inverter_fields` already drops the legacy key at
  setup.
- **Import flow** (old hub → new inverter entry at the current version, so the migration never runs on it): the
  import calls the same `legacy_topology_to_input` on `imported`.

## Tests

### YAML scenarios: `dev/tests/scenarios/features/test_household_arrangements.yaml` (new, all `human_verified: false`)

Every scenario uses the same physical site:
- 3 phases, 230 V, `main_breaker_rating: 40`, `solar_production_direct: true`, symmetric, no inverter rating;
- household 3 A a phase;
- one EVSE, 3-phase, 6-16 A.

Each group must give one allocation.

**Day group**: 6900 W solar, the car in Solar Only, expected `allocated: 7.0` = (6900 − 2070) / 230 / 3, confirmed by
hand on the first run. Six scenarios:
- **arr-a-pv-on-main**: one PV inverter, no input.
- **arr-b-hybrid-grid-tick**: hybrid, input = grid meter, no battery.
- **arr-c-hybrid-input-sensors**: hybrid, input sensors.
- **arr-d-hybrid-plus-unbacked-panel**: hybrid, input sensors, 40 % of the household on the grid-meter bus.
- **arr-e-hybrid-plus-pv-on-main**: hybrid with input sensors, plus a PV inverter on the main bus, solar 50/50.
- **arr-f-pv-on-hybrid-load-port**: hybrid with input sensors, plus a PV inverter on its load port, solar 50/50. The
  hybrid's output reads negative while the PV out-produces the port.

**Night group**: solar 0, battery 80 %, 5000 W discharge on the hybrid, `max_import_power: 0`, household 1 A a phase,
the car in Standard, expected `allocated: 6.2` = (0 + 5000 − 690) / 230 / 3. Three scenarios: **arr-b-night**,
**arr-c-night** and **arr-d-night** (with the main-bus panel).

### Harness (`dev/tests/run_tests.py`)

The YAML `inverters:` list replaces `wiring_topology`:

```yaml
inverters:                # in order; the battery sits behind the first hybrid
  - kind: hybrid          # pv | hybrid
    input: sensors        # hybrid: grid | sensors | none (off-grid)
  - kind: pv
    feeds: port           # pv: main (default) | port (the first hybrid's load port)
    solar_share: 0.5      # default: an even split
main_bus_share: 0.4       # share of the household on the grid-meter bus (default 0)
```

Physics per cycle, per phase, lossless, after the existing `simulate_grid_ct`:
- The total conversion is `C = house + draws − grid`.
- PV out is its solar share.
- `hybrid_out = backed house + draws − Σ PV-on-port out`.
- `hybrid_in = hybrid_out − (C − Σ PV out)`.

From there the harness builds `fleet.FleetMember`s and calls the production functions: `fleet.net_output`,
`household_per_phase`, `fleet.solar_total` and `fleet.output_power_total`. It replaces `simulate_inverter_output` and
the topology branches in `apply_feedback_adjustment`. With no `inverters:` key and `inverter_output_sensors: true`,
it keeps today's auto rule: a battery gives `[hybrid, input: grid]`, otherwise `[pv]`.

**Existing scenarios.** There are 47 `wiring_topology:` lines in 8 files, none human-verified. The conversion is
mechanical:
- `series` grid-tied → `inverters: [{kind: hybrid, input: grid}]`;
- `series` off-grid → `[{kind: hybrid, input: none}]`;
- `parallel` grid-tied → `[{kind: pv}]`;
- `parallel` off-grid → `[{kind: hybrid, input: none}]`. That is what parallel meant off-grid already.

Expectations must not move, except where derived solar now leaves out the grid passing through a grid-input hybrid.
That can only touch `test_grid_inverter_split.yaml`, `test_inverter_rating_cap.yaml` and `test_inverter_headroom.yaml`
(grid-tied, series, solar not direct). Each moved expectation goes into the commit message with its before, after
and reason. Any other moved expectation is a bug.

### Unit tests

- `dev/tests/test_household.py` (new, pure):
  - `household_per_phase` handles a None phase, the clamp, and off-grid zeros.
  - The telescoping property: the six arrangements built as FleetMembers give the physical household per phase,
    equal across all six.
  - Under a phase remap, a grid-input member with permuted grid phases still gives exactly `out − draws` per phase.
- `dev/tests/test_fleet.py`
  - Replace `:430-445` (fleet_topology / mixed) and `test_sum_outputs_per_phase` with tests of `member_input` and
    `net_output` (smoothed and raw).
  - Test `member_solar` for a PV inverter, a hybrid with sensors, a hybrid with the tick (`out − grid − bp`) and
    off-grid.
  - Test that the estimate uses the signed battery.
  - Test that `solar_is_unsplit` holds grid-tied too.
  - Drop the `off_grid` fixtures (`:288`, `:401-404`).
- `dev/tests/test_inverter_output.py`
  - Delete the series-vs-parallel contrasts (`:98-185`, `:215-216`).
  - Keep and rename the cascaded-netting tests (`:223-290`) on `net_output`, plus a parent hybrid with input sensors.
- **HA tests parametrized over topology.** `test_offgrid_{battery_household, parallel_household, solar_household,
  no_battery_solar, unsplit_solar, battery_headroom, household_smoothing, sun_probe}.py`, `test_gridtied_inverter_pool.py`
  and `test_readout_hunting.py`:
  - `series` becomes `CONF_INVERTER_INPUT_IS_GRID: True`, and `parallel` drops the key.
  - The `[series, parallel]` params become `[grid tick, no input]`, with assertions unchanged. They already assert
    the two agree.
- `dev/tests/test_inverter_input_ha.py` (new, small)
  - One hub with a hybrid read through mocked input and output entity states (W and A), with 40 % of the house on the
    meter bus and no solar sensor, so `household_power` is the per-phase sum. It must match the physical household.
  - An unavailable input is held by the stale guard and named on the hub status.
- `dev/tests/test_config_flow_e2e.py`
  - Migration: series → tick and no `wiring_topology`, minor 9.
  - Parallel → no input keys.
  - Two series siblings → the lowest entry_id gets the tick, and rerunning changes nothing.
  - A mixed fleet logs the warning.
  - `test_hub_inverter_auto_import` (`:1756-1800`): series asserts the tick, and `CONF_WIRING_TOPOLOGY` goes from the
    imports.
  - `:1624-1739` asserts input keys instead of topology.
  - Options `inverter_core`: input sensors are stored, and each of the three errors fires.

## RELEASE_NOTES.md (## 2.1.6)

- **New Features**, "Inverter input sensors, and one household sum":
  - An inverter now takes optional per-phase input sensors (grid side), or *The grid flows through this inverter,
    straight from the grid meter*.
  - The house is `grid + Σ (output − input)` on every site, whatever the inverters' arrangement: a PV inverter on the
    grid side, one on a hybrid's load port, a non-backed-up panel before a hybrid, or off-grid.
  - Derived solar and the inverters' measured output use `output − input` too. A hybrid the grid passes through no
    longer counts that import as its own output.
- **Bug Fixes**, "The Wiring topology setting is gone":
  - Its Parallel/Series labels described the hardware while the arithmetic needed the position. A PV inverter on a
    hybrid's load port picked *Parallel* and had its output credited against a grid bus it is not on.
  - Migration: Series became *The grid flows through this inverter, straight from the grid meter* (one inverter per
    hub), and Parallel became no input.
    Every single-inverter site computes the same household as before.
  - Mixed fleets: a PV inverter on the grid side of a hybrid needs the hybrid's input sensors (a warning is logged).
  - A battery set on an inverter is that inverter's own, behind its AC side. An AC-coupled battery with its own
    inverter belongs on its own inverter entry.

## dev/TODO.md

- Remove both items, the `[BUG]` at line 28 and the `[FEATURE]` at line 29. History lives in git and the release
  notes.
- Add nothing. No follow-up is left in LJ.
- LI's `layout` option labels still use the hardware words: "Parallel - …(AC-coupled solar, no battery)". That is
  LI's own wording for its efficiency flag and is left for an LI change; this plan does not edit LI.

## Order of work (each step its own local commit, full suite green before the next)

1. **The pure core**:
   - `utils.household_per_phase`, the fleet member rule (`member_input`, `net_output`, `member_solar`, outputs), and
     the SiteContext rename and deletion;
   - `target_calculator._house_on_inverters`, `hub_calculation` and `readers`;
   - the harness arrangement model and the conversion of the 47 YAML lines;
   - `test_household.py` and `test_fleet.py` / `test_inverter_output.py` updates.
2. **Config**: the const keys, schema, validation, translations en+sl, migration step, import path, pages, units,
   e2e/flow tests, and the topology-parametrized HA tests.
3. **The arrangement scenarios and the HA input test.**
4. **Docs**: RELEASE_NOTES, TODO, README, AGENTS.

## Verification

```bash
cd /Users/anzek/Projects/Dynamic_OCPP_EVSE
.venv/bin/python -m pytest dev/tests -q -n 8
.venv/bin/python -m pytest dev/tests -q -n 8 -k "household or fleet or inverter_output or inverter_input or arrangement or migration or import"
grep -rn -E "wiring_topology|WIRING_TOPOLOGY|fleet_topology|mixed_topologies|_battery_output_term|compute_household_per_phase|sum_outputs|inverter_output_per_phase" custom_components dev/tests
```

- The grep may only hit `LEGACY_WIRING_TOPOLOGY` in `helpers.py`, the migration and import tests, and the
  `inverter_output_phase_*` entity keys. The last grep pattern only matches the old SiteContext name.
- Compare the en/sl key sets with a JSON diff of both files' key paths; there should be no difference.
- Before step 1, record the suite's pass count and every scenario's allocation. After steps 1 and 2, diff them: only
  the three files named under Harness may differ.

## Per-site check after install (Home and Kozolec; Andrej only when Anze says)

Install happens when Anze pushes; the pushed build installs and restarts by itself.

1. Before:
   - `node tools/snapshot.mjs home` and `python3 tools/collect_configs.py home` (and the same for kozolec), into
     `-before-lj-household`.
   - Note the per-phase household. Run `logger.set_level` `custom_components.dynamic_ocpp_evse: debug` for ~2 min and
     read the "Per-phase household" lines, then set it back to warning.
2. After the restart, snapshot and diagnostics into `-after-lj-household`.
   - The hub and inverter entries are at version 2.9.
   - **Home**: inverter options have no `wiring_topology` and no input keys.
   - **Kozolec**: `inverter_input_is_grid: true` and no `wiring_topology`.
   - No repairs, no LJ errors in the log, all entries loaded.
3. Household equality. Another 2 min of debug logging.
   - **Home**: per phase A/B/C should equal `M1 current + I1 current − managed draw` read from states at the same
     moment, and the before values, within sensor noise.
   - **Kozolec**: L1 should equal `gx_device_consumption_power_l1 / 230 − managed draws`.
   - The published `household_power` must not step at the restart. At both sites it is the solar identity and
     untouched.
4. 30 min of normal running: no new pauses or flapping on the EVSE, tank and plugs.
   - At Home the inverter rating cap's house term now comes from the output sensors instead of the AC power sensor.
     It is the same figure to within the V×I vs P difference, and it only binds near 17 kW.
5. Write a dated line in each site's `NOTES.md`.

## Risks

- **Other installs with a parallel member that has a battery.** The battery is now taken as behind that inverter.
  Derived solar becomes `out − bp`, and the estimate's charging is no longer ignored. No site of Anze's has this. The
  release note tells others to give an AC-coupled battery its own inverter entry.
- **Mixed fleets can't be migrated exactly.** After migration they get `Σ out − draws`, which is right when the PV
  inverter sits on the hybrid's load port. The migration logs a warning, and the release note covers it.
- **The tick is only right with nothing between the meter and the hybrid.** The field description says so. The
  validation enforces one tick per hub and no tick together with sensors.
- **Input sign.** Hybrids publish grid-port power with either sign, and there is no invert option. A wrong sign shows
  as a household off by twice the import, and the fix is a template sensor. The description states "positive while
  it draws from the grid".
- **Derived solar and output total change** for grid-tied, grid-input hybrids without a solar sensor: the import is
  no longer counted as production. This is physically right, and only the three unverified scenario files named
  above can move.
- **`_house_on_inverters` at Home and Andrej** now follows the per-phase household, which carries the floor hold, so
  it falls more slowly after a load drops. That is conservative, and it only matters near the inverter rating, which
  neither site reaches (Home 17 kW, Andrej 15 kW with a 7.8 kW export limit).

## Open decisions for Anze

None open. Anze decided all three on 2026-10-12:

1. **Delete `wiring_topology` entirely.** The per-inverter input setup replaces what it did; the "Nothing the flag did
   is lost" table above maps every former consumer to its replacement.
2. **The grid-flows-through tick (`inverter_input_is_grid`) is a permanent option**, not a migration artefact. It is
   labelled plainly in en and sl: for a hybrid the grid flows through, when nothing sits between the grid meter and the
   inverter.
3. **The inverter rating cap's house figure comes from the output sensors on every site** (`_house_on_inverters` uses
   the per-phase household wherever one exists).
