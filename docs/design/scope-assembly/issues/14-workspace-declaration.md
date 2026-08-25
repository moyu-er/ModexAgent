# 14 — Workspace declaration surface

**What to build:** A workspace's resource selection becomes declarable: memory backend (file/sqlite), path layout, shared infrastructure set (MCP server set, media store, trace facilities), hosted pools list. The business workspace-wiring modules die in favor of the declaration + factories reading `WorkspaceContext`. Absent a workspace declaration, a pool declares itself as the root scope and boots straight through — single-workspace deployments never see the workspace concept (no `workspace.enabled` flag; absence of declaration IS the off state, per N15). Data layout compatibility: no-declaration deployments keep today's default structure.

**Blocked by:** 07 (declaration boot path proven).

**Status:** closed (resolved 2026-08-22)

- [x] Workspace resource selection (backend/MCP set/paths/hosted pools) expressible in YAML and consumed via `WorkspaceContext` by factories
- [x] Workspace wiring modules deleted (deletion ledger names each)
- [x] Pool-as-root declaration (no workspace layer) boots and behaves like today's single-workspace deployment — zero workspace awareness
- [x] Two workspaces with different declared backends coexist without leakage (per-(workspace, pool) data addressing verified)
- [x] Restart round-trip on workspace declarations; data lands in the same locations as today for equivalent configs
- [x] Bot test suite green

## Resolution notes (2026-08-22)

- **(a)** `WorkspaceSpec` (scope/spec.py) gains the resource-selection face:
  `persistence.backend` (PersistenceBackend), `paths.data_dir_name`, and
  `mcp` (the workspace's shared server set — 资源引用 per SPEC §3.7).
  Every field is `None = inherit` the service-level domain config
  (SPEC §3.1 继承父层 + 声明差异); the shipped bot.yml carries the full
  face with values matching the service defaults (data landing
  unchanged). Consumption via `WorkspaceContext`: the 04-chain carrier
  (plugins/assembly/context.py) gains `workspace_spec`, threaded
  resources.py → create_pool → AssemblyContext → `agent_context_chain`;
  a WorkspaceContext-declared factory reads the declared selection
  (tests/unit/plugins/test_context_chain.py::TestWorkspaceSpecConsumption).
  The backend/paths overrides resolve ONCE at boot
  (`apply_workspace_resource_selection` onto the service config view —
  one selection authority, `app_config.persistence.backend`, with the
  declaration as override source); the declared MCP set validates loudly
  at boot (`UnknownMcpServer` on a typo'd name) and scopes the shared
  registry's pre-warm (ADR-0017 machinery unchanged — undeclared servers
  still connect lazily via `acquire`).
- **(b)** Deletion ledger (grep-verified zero code references):
  `stack._build_assembly_deps_for_pools` (the 09-era two-preset caller
  branch — converged into the single position-derived deps road:
  declared roots via `_declared_assembly_deps`, no-declaration fallback
  via `_legacy_root_assembly_deps`), `stack.build_single_workspace_stack`
  (single mechanism: `build_workspace_stack(enabled=<declaration form>)`),
  and the `wiring/pool_wiring.py` MODULE (its two functions folded into
  resources.py, their sole consumer). Legacy-road residue NAMED for
  ticket 17 (deletion ledger): `_wire_pool_to_resources` (legacy
  experience-hook wiring), the pool.yml `PeerLink` synthesis feeding the
  FW resolution service, `UserNoticeCleanupHook` code-wiring +
  `_build_communication` legacy store in create_pool, the `PoolStore`
  disk scan as the pool list source, and `SpecBuilder.from_roster`.
  Media store / trace facilities stay domain-config-selected (§3.7 —
  single media implementation; trace selection is the observability
  domain config), recorded here as the honest scoping.
- **(c)** Pool-as-root boots straight through: `declared_pool_build` /
  `_pool_of` handle the pool form; resources.py's road partition sends
  the single declared pool down the declaration road
  (`_declaration_road_pools`); the stack shape comes from
  `workspace_layer_present` (declaration absence = single-home, N15) and
  the resolved config view is untouched (pool form carries no workspace
  layer → nothing to override). Verified end-to-end in
  test_scope_pivot_wiring (road) + test_workspace_resource_declaration
  (landing = today's single-workspace shape, `workspace_spec=None` on
  the chain).
- **(d)** Two workspaces with different declared backends coexist with
  zero leakage: two declaration-driven resource bundles (sqlite vs file)
  in one process, each with live persistence vs file stores respectively
  and per-(workspace, pool) memory roots asserted
  (test_two_workspaces_different_backends_coexist). Same-service
  dual-backend requires per-workspace declarations — 17's delivery.
- **(e)** Restart round-trip + data landing: the declaration-driven
  landing manifest is asserted EQUAL to the frozen config-driven
  baseline manifest (equivalent configs, split-brain commit pair
  7d5306f7 → this commit); a session written in round 1 survives the
  round-2 declaration-driven rebuild.
- **(f)** Full bot suite green: 2172 passed (+24 vs the 2148
  post-baseline HEAD; +25 added / −1 replaced, zero silent loss —
  collection diff verified).
