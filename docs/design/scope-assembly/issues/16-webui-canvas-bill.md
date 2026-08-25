# 16 — WebUI tree canvas + provenance bill REST

**What to build:** The WebUI renders the declared scope tree as a canvas (derived edges from parent references — TopologyCanvas components reused per the SPEC's pattern-reuse note), and serves the per-field provenance bill: every field's effective value with its source layer (framework default / profile / local declaration) plus each component's implementation source (which plugin from which source provided it — the O2 audit surface). The bill recomputes from the YAML declaration per request (pure function, no boot-time cache — a WebUI edit not yet restarted must not show stale provenance) via a new REST endpoint. Editing write-back follows the existing PoolEditor pattern (writes the YAML, restart-effective).

**Blocked by:** 06 (compiler produces provenance data + derived effective values).

**Status:** closed (resolved 2026-08-21)

- [x] Tree canvas renders a declared scope tree with derived parent-child edges and peer links (workspace/pool/agent levels visually distinct)
- [x] REST endpoint serves the provenance bill computed from YAML on request (no cache); every field shows its source layer
- [x] Component implementation source visible per component (O2 override audit: e.g. `edit ← aci`, bundled vs user plugin)
- [x] Editing a declaration in the WebUI writes back to the YAML files (PoolEditor pattern); a not-yet-restarted edit shows in the bill as the on-disk declaration
- [x] Canvas handles pool-as-root declarations (no workspace level) without special-casing
- [x] WebUI tests green; manual visual QA pass on the three shipped pool shapes
