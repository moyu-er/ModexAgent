# 06 — ScopeCompiler: tree → per-agent AssemblySpecs + effective toolsets + O3 accounting

**What to build:** A pure-function compiler turns a validated ScopeSpec tree into per-agent `AssemblySpec`s (spec-level equivalent to today's `SpecBuilder.from_roster` output), plus each agent's **effective toolset**: declared tools + position-derived communication tools injected as derived entries (`task` for nodes with declared children, listing direct children only; `send_to_agent` for non-root nodes with parent from the Agent layer; `send_to_peer` for roots with links) + supplement same-name replacements resolved with an auditable record (`edit ← aci`). The per-field provenance data (framework default / profile / local declaration + component source) is produced here for the WebUI bill view. Communication tools are compiler-derived entries in the effective toolset, resolved via TOOL-slot FW factories that read per-agent stores from the Agent layer — one path, not materialize-time side registration.

**Blocked by:** 02 (tree types), 03 (validated input), 04 (factories read the chain).

**Status:** closed (resolved 2026-08-21)

- [x] Compiled per-agent AssemblySpecs are spec-equivalent to today's from_roster output for the same shipped configs (field-by-field comparison test; tools compared modulo the §5.2 derived entries; the opencode root's `[todo]`-vs-`[]` supplement default is the one explicit allowlist — ticket-02 evidence, external roots assemble no tools)
- [x] Effective toolsets include derived communication-tool entries per §5.2's derivation table (task/send_to_agent/send_to_peer; the effective toolset IS the derived spec.tools — the V6 input face)
- [x] Supplement same-name replacement recorded (`edit ← aci`) and included in provenance data (queryable via `AgentProvenance.replacement_of`)
- [x] Per-field provenance (defaults ← profile ← local + component source) emitted as data (tool entries carry per-entry origins; the registry-side O2 implementation source joins at the WebUI bill, ticket 16 — the compiler is the declaration-side half)
- [x] Profile resolution implements inheritance + deep merge, single level, lists wholesale (V7/V8 semantics; v1 binding surface: the resolved toolset preset names the bound profile — root binds `full`, non-root `read_write`; BIZ custom profiles + boot loading are ticket 07)
- [x] Pure functions only: same input tree → byte-identical output (hash stability, feeding ticket 18; `workspace_ctx` excluded from the byte-stable face)
- [x] Unit tests cover the derivation table end to end on a 3-level tree (task lists direct children only, grandchildren excluded; leaf has no task entry at all)
