<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-28 | capability-bundles doc sync (ADR-0047) -->

# scope

## Purpose

Scope declaration tree (ADR-0042 / `docs/design/scope-assembly/SPEC.md`, implemented 2026-08-22) — the declaration/validation/compile/bill package behind unified assembly. Pure types, parsing, validation, and compilation: the loader turns one YAML file (`config/scopes/bot.yml` in the bot; any explicit path otherwise) into a frozen ``ScopeSpec`` tree, the validator checks it (two phases), and the compiler produces per-agent ``AssemblySpec``s for the existing ADR-0041 pipeline. Zero runtime wiring lives here — boot consumption is the business layer's job (`bot/service/pool/declaration.py`). Never imports ``modex_graph`` (SPEC N3).

## Key Types

| Type | File | Description |
|------|------|-------------|
| ``ScopeSpec`` / ``ScopeKind`` | ``spec.py`` | A loaded declaration tree — exactly one root form: ``kind=WORKSPACE`` (``WorkspaceSpec`` hosting pools) or ``kind=POOL`` (a single pool IS the root scope, no workspace layer). All frozen Pydantic (``extra="forbid"``) |
| ``WorkspaceSpec`` | ``spec.py`` | Workspace-layer resource selection — ``persistence`` (memory backend), ``paths`` (data-dir layout), ``mcp`` (shared server-name set), hosted ``pools``. Every selection field is ``None = inherit`` the service-level domain config |
| ``PoolSpec`` | ``spec.py`` | One pool tree: a FLAT ``agents`` list with ``parent`` references (nested YAML is loader-flattened sugar) + ``peers`` (cross-pool links, ADR-0019). ``root_agent`` accessor is loud until V3 guarantees exactly one root |
| ``AgentSpec`` | ``spec.py`` | Unified per-node agent declaration (the legacy Main/Sub type split is deleted, ticket 11). Root-ness derives from ``parent is None`` — never declared. Field face: ``toolset`` (``None`` = position-derived), ``capabilities`` (override map — ``false`` forces a capability off, a config mapping forces it on with config; ADR-0047), ``tools``, ``hooks``, ``memory`` (``MemoryDeclaration``), ``approval`` (root-only, V9), ``eager``, ``execution_strategy``/``provider_kind`` |
| ``load_scope_declaration`` / ``load_dynamic_workspace_declarations`` / ``ScopeDeclarationError`` | ``loader.py`` | Explicit file path → ``ScopeSpec`` (never a directory scan). The one exception: runtime-created workspaces persist one file per workspace under ``config/scopes/workspaces/`` (stem = identity, ticket 17) and reload from that directory at boot |
| ``PositionDefaults`` / ``defaults_for_position`` / ``effective_defaults`` / ``memory_config_for_position`` | ``defaults.py`` | The SPEC §3.2 position-derived defaults table: root → archive/core memory family + approval eligibility + eager registration + toolset ``full``; non-root → session-only + lazy + ``read_write``. The dead legacy ``tool_preset`` values land here |
| ``RuleId`` / ``validate_declaration`` / ``validate_effective_configs`` / ``ScopeValidationIssue`` | ``validator.py`` | Two-phase pure validation. Phase 1 (pre-derivation): V1 acyclic, V2 connected, V3 one root per pool, V4 kind hierarchy, V5 peer topology, V7 profile single-level, V10 graph agent references (input face ``GraphAgentReference`` — boot extracts these from loaded graph specs), V11 name uniqueness, V12 external agent with a non-empty explicit ``capabilities`` block (ADR-0047 — external agents are structurally excluded from capability resolution). Phase 2 (compiler output): V6 ``task`` present in child-carrying agents' effective toolsets, V9 non-root approval refused. Result objects — empty list = valid; boot turns non-empty into a startup abort |
| ``Profile`` / ``ProfileStore`` / ``STANDARD_PROFILES`` / ``merge_memory_declarations`` | ``profile.py`` | Named default-combination macros. ``STANDARD_PROFILES`` — the five toolset presets as code-level frozen constants — is the FW default store (the ``config/profiles/`` directory face did not land; no consumer). ``ProfileStore`` refuses nested profile references at construction (V7/N10) |
| ``AgentOverlay`` / ``PoolOverlay`` / ``ScopeOverlay`` / ``apply_scope_overlay`` | ``overlay.py`` | Frozen, closed-schema pre-compile overlays. Purely transforms a loaded ``ScopeSpec`` (peer stripping, agent filtering, tool/memory/prompt/approval overrides) before the unchanged boot validation and compilation path. Overlay ``tools`` entries append to the declared list — the compiler's single merge owns all ``+/-`` processing over the full base (preset, derived, capability-contributed names) |
| ``compile_scope`` / ``ScopeCompilation`` / ``CompiledAgent`` / ``AgentProvenance`` / ``ToolReplacement`` | ``compiler.py`` | The pure compiler: validated tree → per-agent ``AssemblySpec`` + ``EffectiveAgentConfig`` (the V6 input face) + ``AgentProvenance`` (per-field winning layer; per-tool origin — capability-contributed names classify as ``ToolOrigin.CAPABILITY_DERIVED`` carrying the contributing registration name; O3 same-name replacement records ``edit ← aci``; per-agent ``capabilities`` audit list with three-state entries ``auto``/``declared``/``vetoed``, ``registration_source``, and typed contribution rows with ``vouched``/``dropped`` gating). Same inputs → byte-identical outputs. Runs the C0/C1/C2 capability compile protocol (ADR-0047) per agent — C0 enablement resolution (effective set = auto-apply ∆ ``capabilities:`` overrides; external agents structurally excluded; unregistered reference → V13 boot-fail), C1 contributions into the roster merge base (contributed tool/hook names vetoable via ``tools: [-x]``/``hooks: [-y]``; ``tool_replacements`` applied post-merge; tree-derived entries fed through the ``derived_tools`` channel), C2 bind + anchor validation (missing anchor → ``CapabilityError`` boot-fail; contributed hooks survive iff a binding vouches them). Takes a ``registry`` parameter (compile input, not hashed). Knows no capability name — the derived communication entries arrive via the ``subagents`` capability's contributions, and the compiler keeps the legacy ``pool_name`` = root-agent-name convention |
| ``_expand_preset_tool_names`` / ``_merge_tools`` / ``_merge_hooks`` / ``_expand_system_prompt`` / ``_derive_agent_type`` | ``derivation.py`` | The shared spec-derivation core (moved here when the legacy roster road was deleted, ticket 11) — the compiler's single consumer |
| ``spec_hash`` / ``ScopeGenerationTracker`` | ``seam.py`` | The N2 hot-reload seam: cross-process-stable SHA-256 over a compilation's byte-stable face (minus each agent's ``spec.workspace_ctx``) + a per-pool generation counter living OUTSIDE the compiler. Zero runtime consumers by design (restart-effective stands) |

## For AI Agents

- Purity contract: loader/validator/compiler take everything as parameters (including the ``WorkspaceContext`` and ``ProfileStore``); no IO, no singletons, no state. The generation counter is the one stateful object and it wraps the compiler — never inside it.
- The derived communication entries are capability-contributed (the ``subagents`` package's ``contribute`` reads the tree, SPEC §8.4): ``task`` for agents with declared children (direct children only), ``send_to_agent`` for every non-root, ``send_to_peer`` for roots of pools with links. They are never roster-declared and never materialize-time side-registered — resolution happens via the TOOL-slot FW factories in ``plugins/defaults/communication.py``.
- An explicitly declared unprefixed ``tools`` list replaces the preset base WHOLESALE (O4/V8) — including the derived entries; V6 catches child-carrying agents that drop ``task``. Incremental ``+/-`` lists merge over the base.
- Position-derived defaults are NOT transcribed into shipped declarations — only deviations are declared. Split-brain equivalence against the deleted legacy road is frozen in ``tests/unit/scope/`` goldens.
- The provenance bill recomputes from the YAML declaration per request (WebUI ``GET /api/scope/bill``) — there is deliberately no boot-time cache (SPEC §3.4 rule 3 / S2).

## Dependencies

### Internal
- ``modex_agent.plugins.assembly.spec`` — ``AssemblySpec``/``MemoryOverrides`` (the compiler's output face)
- ``modex_agent.plugins.assembly.context`` — ``WorkspaceContext`` (compile parameter)
- ``modex_agent.memory.presets`` — ``main_agent_memory``/``subagent_memory`` (position families)
- ``modex_agent.tools.presets`` — ``ToolPreset`` (position-derived toolset expansion)
- ``modex_agent.persistence.config`` — ``PersistenceBackend`` (workspace resource selection)

### External
- ``pydantic`` — frozen ``BaseModel`` declarations
- ``pyyaml`` — declaration loading
