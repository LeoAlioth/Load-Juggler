# Load Juggler — Open Issues

Resolved items are removed — history lives in `git log` and `RELEASE_NOTES.md`.
Watch-only items and refactors live in `TODO.md`.

1. **Icon not shown in HA/HACS** — HA does not load a custom component's own icon file; it must be submitted as a PR to the [Home Assistant brands repo](https://github.com/home-assistant/brands). Brands-ready files sit at the repo root: `icon.png` (256×256) and `icon@2x.png` (512×512).
