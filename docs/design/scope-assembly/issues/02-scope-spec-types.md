# 02 — Scope declaration types + YAML loading + position-derived defaults

**What to build:** A nested scope YAML declaration (workspace → pool → agents, agents nestable under agents) loads into a frozen `ScopeSpec` tree. A unified `AgentSpec` (one type for root and non-root) replaces the Main/Sub split conceptually: per-node defaults derive from tree **position** — root gets main-agent defaults (archive/core/experience memory preset, approval eligibility, eager registration), non-root gets subagent defaults (session-only memory, lazy materialize). The `tool_preset` field dies; its values land as position-derived toolset profiles (root → full, non-root → read-write), matching current shipped behavior. Nested YAML is sugar: the spec model stays flat with `parent` references.

**Blocked by:** None — can start immediately.

**Status:** closed (resolved 2026-08-21)

- [x] `ScopeSpec` / `AgentSpec` frozen Pydantic types with parent references (flat model; nested YAML is parse-level sugar)
- [x] YAML loader reads a scope declaration into the frozen tree via an explicit file path (pinned by plan Oracle R2#2: single-file source of truth, never a directory scan; pool-as-root declarations supported via the same explicit-path parameter: no workspace layer required)
- [x] Position-derived defaults table implemented: memory preset, approval eligibility, registration timing (eager/lazy), toolset profile — each overridable per node
- [x] `tool_preset` values mapped to position-derived profiles with behavior parity to shipped configs (split-brain ready)
- [x] Loading an existing shipped pool config (e.g. review) produces an equivalent declaration
- [x] Unit tests cover root/non-root/intermediate-node default derivation + override precedence
- [x] Zero behavior change yet: types and loading only, no consumer wiring
