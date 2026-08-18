# Load Juggler — Open Issues

Resolved items are removed — history lives in `git log` and `RELEASE_NOTES.md`.
Watch-only items and refactors live in `TODO.md`.

1. **Icon not shown in HA/HACS** — HA does not load a custom component's own icon file; it must be submitted as a PR to the [Home Assistant brands repo](https://github.com/home-assistant/brands). Brands-ready files sit at the repo root: `icon.png` (256×256) and `icon@2x.png` (512×512).

31. **Unavailability/staleness detection re-implemented ~5× with drifting predicates** — `_read_entity` (includes `None`/`""`), the grid-stale block (adds non-finite), `_check_entity_availability` (omits `None`), station status (omits `""`), `forecast_reader.py`. `_read_grid_phases` coerces `_UNAVAILABLE` to 0 A and relies on the downstream stale block to repair it — an unrepaired 0 A grid reading grants full breaker headroom. One `is_unavailable(state)` helper should own the predicate.

32. **Entity layer: deprecated sensor API, no `available` handling, `None` → `0.0`** — sensors override `state`/`unit_of_measurement`/`device_class` instead of `native_value`/`_attr_native_*`, smuggle `state_class` through `extra_state_attributes` (inconsistently — some W/A sensors get long-term statistics, some don't), no hass.data-fed sensor implements `available` (values freeze silently when the producer stops), and `LoadJugglerHubSensor.state` maps `None` → `0.0` (Site Remaining Power reads 0 W on failure instead of unknown). Tracked as the entity-modernization TODO.
