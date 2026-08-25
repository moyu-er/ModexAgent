# 11 — All pools on scope declarations; legacy path deleted (contract)

**What to build:** Every shipped pool boots from scope declarations; the legacy roster path dies entirely — `MainAgentSpec`/`SubagentSpec` type split, `main_agent_name` (and its directory-name default), the `tool_preset` field, and the old `RosterLoader`/`SpecBuilder.from_roster` path are deleted (no compat shims per convergence rule 2). This is the contract ticket: expand phase over, one path remains. After this, "declare a new agent" means writing YAML + (optionally) a plugin — the design's core promise, demoable end to end.

**Blocked by:** 09, 10 (all glue migrated; nothing references the legacy types).

**Status:** closed (resolved 2026-08-22)

- [x] All shipped pools (default/coder/review/office/opencode...) boot from scope YAML
- [x] `MainAgentSpec`, `SubagentSpec`, `main_agent_name` parsing, `tool_preset` field, legacy roster→spec path: deleted from the codebase (grep-clean)
- [x] Demo criterion: defining a brand-new agent (nested subagent with custom toolset) requires only YAML + optional plugin — no framework or business code change
- [x] Same-pool NORMAL peer concept removed with the type split (external pools = single-node trees + peer links; comm_kind residue swept)
- [x] Full bot + framework test suites green on the single path
- [x] Net code accounting vs pre-W1 baseline published (deletion ledger totals)
