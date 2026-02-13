# TODO

- [ ] **Infer phase existence from data** — Remove explicit `num_phases` field. Make `PhaseValues` fields `Optional[float]` (None = phase doesn't exist, 0.0 = exists with no load). `SiteContext.num_phases` becomes a derived `@property`. Test scenarios only list phases that exist. See plan: `.claude/plans/cheerful-riding-muffin.md`
