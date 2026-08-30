# 01 — Registry source-priority inversion (O2)

**What to build:** A user or project plugin registering a component under the same `(slot, name)` as a bundled default now **wins** (source priority: user > project > entry_points > bundled). The override is logged (info) and auditable via `registration_source`. Same-source duplicates and direct `registry.register()` same-name calls still raise `ValueError` (typo defense). This reverses ADR-0041's first-seen-wins (which let bundled always win and blocked user plugins from overriding framework defaults) — recorded in ADR-0042's override-semantics section.

**Blocked by:** None — can start immediately.

**Status:** closed (resolved 2026-08-21)

- [x] Loader flush path resolves cross-source duplicates by source priority (user > project > entry_points > bundled), replacing skip-and-warn
- [x] Overrides log at info level and remain queryable via `registration_source` (existing infrastructure)
- [x] Same-source same-name registration still raises `ValueError` at load time
- [x] Direct `registry.register()` same-name still raises `ValueError`; `overwrite=True` remains test-only
- [x] Behavior-change test: a user plugin overriding a bundled same-name component takes effect after restart
- [x] Existing loader/registry tests updated to the new semantics; full suite green
