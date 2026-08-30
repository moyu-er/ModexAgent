# Design Closure Report: scope-converge SPEC

> **Source**: `docs/design/scope-converge/SPEC.md` (revised 2026-08-18, 13 gaps fixed in second round)
> **Method**: 5 parallel dimension tracers (data-flow, lifecycle, convergence, interface, state-machine) + Phase 2 seam analysis + Phase 3 finding verification
> **Date**: 2026-08-18
> **Status**: ALL 13 GAPS FIXED in SPEC §12 (second round). Design is closed.

---

## Dimension Selection Record

| Dimension | Selected | Why |
|---|---|---|
| data-flow | ✅ | Data structures cross boundaries (YAML→roster→spec→builder→agent); KVStore persistence |
| state-machine | ✅ | AgentType enum (4 values); workspace lifecycle (materialize→evict); assembly timing; special agent trigger states |
| interface | ✅ | 14 ABCs/factories/functions; ComponentRegistry; AssemblyPipeline; multi-layer wiring |
| lifecycle | ✅ | 12 in-memory objects with different scopes (global/workspace/pool/transient); workspace eviction |
| convergence | ✅ | 10 concerns with potential dual paths; special agent exception; v1 transitional states |

---

## Closure Matrix Summary

| Dimension | Items Traced | Closed | Gap | Assumption-Closed | Deferred |
|---|---|---|---|---|---|
| data-flow | 26 | 15 | 5 | 4 | 2 |
| lifecycle | 12 | 6 | 3 | 3 | 0 |
| convergence | 10 | 6 | 0 | 2 | 2 |
| interface | 14 | 3 | 8 | 2 | 1 |
| state-machine | 8 | 3 | 5 | 0 | 0 |
| **Total** | **70** | **33** | **21** | **11** | **5** |

---

## Findings (13 gaps, deduplicated across dimensions)

### Architecture-Level Gaps (appear in multiple dimensions)

#### GAP-1: Plugin loading mechanism undefined [CRITICAL]
- **Dimensions**: data-flow (F-DF-1) + interface (F-INT-1, F-INT-2) + state-machine (F-S5-1)
- **Location**: §4.4 (old PluginManager deleted), §4.5 (Plugin.register(ctx)), §4.1 (三源加载)
- **Consequence**: Old PluginManager/PluginLoader/PluginContext deleted ("全删"). No replacement loader named. Nobody discovers Plugin subclasses, instantiates them, creates PluginRegistrationContext, calls register(), or flushes to ComponentRegistry. v1 goal "不改框架代码、只写 YAML + 装插件" (§1) cannot be met — user/entry_point plugins will never load. ComponentRegistry will only contain bundled defaults.
- **Fix**: Define `ComponentRegistryLoader` (or `ComponentRegistry.load()` classmethod) that: (1) scans three sources in priority order, (2) instantiates each Plugin subclass, (3) creates PluginRegistrationContext as context manager, (4) calls `plugin.register(ctx)`, (5) flushes on `__exit__`. Fault-isolated per plugin: one bad plugin doesn't block others. Specify caller (e.g. BotService.initialize before pool creation).

#### GAP-2: AssemblySpec construction + per-component config transport [HIGH]
- **Dimensions**: data-flow (S-DF-1, F-DF-3) + interface (F-INT-5)
- **Location**: §5.2 (AssemblySpec 从 roster 构建), §6.6 (tools: list[str] — names only), §4.2 (create(config, ctx) expects BaseModel)
- **Consequence**: (a) Who builds AssemblySpec from rosters? Not named — no method/interface. (b) AssemblySpec carries tool/hook NAMES (list[str]) but no CONFIGS. ComponentFactory.create(config, ctx) expects a validated BaseModel — for tools/hooks, there's no config to pass. Only system_prompt_config and memory_overrides have config transport. (c) ExecutionStrategy.assemble existing signature takes PoolAssemblyContext (25+ fields); SPEC's AssemblyContext is different — type mismatch, existing strategies would break.
- **Fix**: (a) Name the AssemblySpec builder (e.g. `SpecBuilder.from_roster(pool_roster, agent_roster, workspace_ctx)`). (b) Add `tool_configs: dict[str, dict[str, Any]]` and `hook_configs: dict[str, dict[str, Any]]` to AssemblySpec, OR define that tools/hooks always receive default-constructed config_model. (c) Either make AssemblyContext carry `pool_assembly_ctx: PoolAssemblyContext` field, or redesign AssemblyContext as superset. State the mapping explicitly.

#### GAP-3: Builder cleanup on assembly failure [MEDIUM]
- **Dimensions**: lifecycle (O4) + state-machine (F-S2-1, F-S2-2)
- **Location**: §6.1 AssemblyPipeline.run() — no try/finally
- **Consequence**: Pipeline runs stages sequentially. If stage N crashes, builder has accumulated state from stages 1..N-1 (workspace_resources, infra with broker/inbox/bus). No cleanup path defined. Partially-constructed resources (file handles, DB connections, threads) are orphaned — GC'd eventually but not promptly. Non-atomic, crash-between-steps untraced.
- **Fix**: Add try/except to §6.1: "If any stage raises, pipeline calls `builder.cleanup()` which tears down all accumulated resources in reverse order. Exception re-raised to caller."

#### GAP-4: WorkspaceResources holder after assembly [HIGH]
- **Dimensions**: lifecycle (O6, O8)
- **Location**: §6.2 (InfraAssembleStage produces builder.infra), §6.1 (builder is transient per-call)
- **Consequence**: InfraAssembleStage builds per-workspace resources (broker/inbox/bus/GraphOrchestrator) into builder.infra. But builder is transient — GC'd after pipeline.run() returns. Who holds infra after assembly? If AssembledAgent holds it → scope mismatch (per-workspace resource held by per-pool object). If multiple pools in same workspace, each assembly creates new infra (wrong — should be shared). The SPEC doesn't define the transition from builder to permanent holder.
- **Fix**: Define that InfraAssembleStage only runs for the FIRST pool in a workspace (main agent). The workspace object holds infra. Subsequent pools (subagents) skip InfraAssembleStage and access infra via AssemblyContext.workspace_resources. Add `workspace_resources` to AssemblyContext.

#### GAP-5: DATA_NAMESPACE semantic overload [MEDIUM]
- **Dimensions**: data-flow (F-DF-2, A-DF-1)
- **Location**: §4.3 (DATA_NAMESPACE = type namespace + resolve_bundle), §8.3 (graph state schema types), §9 (TriggerConfig storage), §10.1 (KVStore type registry)
- **Consequence**: DATA_NAMESPACE slot serves three incompatible uses: (1) type registry for KVStore (namespace → Pydantic model), (2) graph state schema custom types, (3) TriggerConfig storage (config, not a type). §9 stores TriggerConfig via `register_namespace`, but register_namespace expects ComponentFactory (§4.5). At runtime, InfraAssembleStage reading DATA_NAMESPACE gets TriggerConfig where it expects a factory — TypeError.
- **Fix**: Either (a) add a dedicated slot (e.g. `TRIGGER_CONFIG`) to ComponentSlot enum, or (b) wrap TriggerConfig in a ComponentFactory whose create() returns the TriggerConfig, or (c) define DATA_NAMESPACE as polymorphic with explicit type tagging and document all uses in §4.3.

### Interface-Level Gaps (single dimension)

#### GAP-6: AssembledAgent type undefined [HIGH]
- **Dimension**: interface (F-INT-3)
- **Location**: §6.1 — "输出: AssembledAgent | 不可变 | 最终组装的 agent 实例". No class block, no fields.
- **Consequence**: build_agent() and assemble_agent() both return AssembledAgent, but its shape is unknown. Downstream consumers (pool registration, turn runner) cannot be verified. Blocks implementation of stages 4-5 and pool-wiring migration.
- **Fix**: Add `class AssembledAgent` block in §6.1 with fields (agent, pool, strategy_result, workspace_resources, infra). Map each field to its consumer.

#### GAP-7: resolve_bundle(namespace) undefined [MEDIUM]
- **Dimension**: interface (F-INT-7)
- **Location**: §4.3, §10.1 — referenced but no signature, no return type, no caller path
- **Consequence**: v1 goal "插件能安全存取自己的会话数据" (§1) cannot be met — plugins have no defined way to access their KVStore data.
- **Fix**: Define `resolve_bundle(namespace: str) -> TypedBundle` on ComponentRegistry. Specify return type and how plugins obtain it.

#### GAP-8: RosterLoader undefined + GraphSpecLoader overlap [MEDIUM]
- **Dimension**: interface (F-INT-6)
- **Location**: §5.2 (RosterLoader), §8.5 (GraphSpecLoader)
- **Consequence**: RosterLoader has no signature. GraphSpecLoader also loads graph YAML — duplicate loading path, relationship between GraphRoster and GraphSpec unclear.
- **Fix**: Define RosterLoader signature. Clarify that GraphRoster (roster-level) is loaded by RosterLoader, GraphSpec (engine-level) is constructed by GraphSpecLoader from GraphRoster — or merge into one loader.

#### GAP-9: ctx.is_subagent not on AssemblyContext [LOW]
- **Dimension**: interface (F-INT-8)
- **Location**: §6.4 (branches on ctx.is_subagent), §6.5 (AssemblyContext has no is_subagent)
- **Consequence**: ExternalExecutionStrategy.assemble references ctx.is_subagent but AssemblyContext doesn't have it. Implementer would hit AttributeError.
- **Fix**: Change §6.4 to branch on `spec.agent_type == AgentType.external_sub` (which IS on AssemblySpec).

#### GAP-10: ResourceFactory.materialize path ambiguity [MEDIUM]
- **Dimension**: interface (F-INT-4)
- **Location**: §6.2 (stage calls ResourceFactory.materialize(ctx) directly)
- **Consequence**: Direct factory call bypasses WorkspaceRegistry's caching/LRU/in-flight dedup. Concurrent materialize of same target would run factory twice and orphan resources. Parameter type mismatch (AssemblyContext vs WorkspaceContext).
- **Fix**: Stage should call `workspace_registry.materialize(ctx.workspace_ctx)` (not raw factory). Add workspace_registry to AssemblyContext or resolve from global layer.

### State-Machine Gaps (crash/error handling)

#### GAP-11: Crash handling systematically unspecified [MEDIUM]
- **Dimensions**: state-machine (F-S2-1, F-S2-2, F-S3-1, F-S3-2, F-S4-1, F-S6-1, F-S6-2, F-S8-1, F-S8-2)
- **Location**: Throughout SPEC — no crash cleanup, error handling, or failure isolation specified
- **Consequence**: (a) Assembly stage crash: partial resources orphaned (GAP-3). (b) Workspace materialize crash: partial infra not cleaned up. (c) Plugin loading failure: one bad plugin may block all others. (d) Special agent crash mid-run: trigger consumed, work lost, no retry. (e) Roster validation failure: behavior undefined (fail fast? skip? default?). (f) Component-not-found at assembly: behavior undefined (abort? substitute? skip?).
- **Fix**: Add a "Failure Handling" section to SPEC: (a) Assembly: try/finally with builder.cleanup(). (b) Materialize: atomic-with-cleanup. (c) Plugin loading: fault-isolated per plugin. (d) Special agent: trigger resets to enabled on crash. (e) Roster validation: fail-fast with clear error. (f) Component-not-found: fatal, no substitution.

#### GAP-12: Special agent re-trigger transition unspecified [LOW]
- **Dimension**: state-machine (F-S6-3)
- **Location**: §9 — state machine ends at "done", no loop-back; but cooldown_turns implies re-trigger
- **Consequence**: Implementer might assume "done" is terminal, preventing re-triggering.
- **Fix**: Add "done → enabled (after cooldown)" transition to S6.

#### GAP-13: AgentType YAML derivation unspecified [LOW]
- **Dimension**: state-machine (F-S1-1)
- **Location**: §6.6 (agent_type: AgentType), §5.1 (pool.yml has execution_strategy + provider_kind)
- **Consequence**: How agent_type is derived from YAML fields not stated. Implementer could derive incorrectly, causing wrong stage subset.
- **Fix**: Add derivation rule: "main_agent_name → native_main (default) or external_main (when provider_kind set); subagents → native_sub or external_sub (same condition)."

---

## Phase 2: Cross-Dimension Seam Analysis

| Seam | Check | Status | Finding |
|---|---|---|---|
| data-flow ↔ lifecycle | If lifecycle releases builder, what happens to data in-flight? | **GAP** | GAP-4: WorkspaceResources (per-workspace) held by transient builder — released on assembly return, should survive |
| state-machine ↔ data-flow | If data-flow persistence broken, does state recovery close? | closed | No persistence in assembly (restart-based, §11) |
| interface ↔ lifecycle | If lifecycle releases builder, do interface callers have valid ref? | **GAP** | GAP-3: build_agent() returns AssembledAgent from builder; if builder is GC'd, AssembledAgent's references may be invalid (ownership transfer undefined) |
| state-machine ↔ lifecycle | If lifecycle releases object mid-transition, what happens to state? | **GAP** | GAP-3 + GAP-11: Assembly stage crash leaves partial state, no cleanup |
| data-flow ↔ interface | If interface returns unexpected data, does consumption close? | **GAP** | GAP-6: AssembledAgent type undefined — consumer cannot be verified |
| error propagation (cross-cutting) | When assembly fails, where does error land? What state left? | **GAP** | GAP-3 + GAP-11: Error propagates to caller, partial resources orphaned |

---

## Phase 3: Finding Verification

All 13 findings re-verified against cited SPEC locations:

| GAP | Location verified | Consequence chain references SPEC | Fix addresses traced gap |
|---|---|---|---|
| GAP-1 | §4.4 "全删" ✓, §4.5 register(ctx) ✓, no loader ✓ | ✓ | ✓ |
| GAP-2 | §5.2 "从 roster 构建" ✓, §6.6 tools: list[str] ✓, §4.2 create(config) ✓ | ✓ | ✓ |
| GAP-3 | §6.1 no try/finally ✓ | ✓ | ✓ |
| GAP-4 | §6.2 builder.infra ✓, §6.1 transient ✓, no post-assembly holder ✓ | ✓ | ✓ |
| GAP-5 | §4.3 DATA_NAMESPACE ✓, §9 TriggerConfig ✓, §8.3 graph types ✓ | ✓ | ✓ |
| GAP-6 | No AssembledAgent class block ✓ | ✓ | ✓ |
| GAP-7 | §4.3, §10.1 referenced, no signature ✓ | ✓ | ✓ |
| GAP-8 | §5.2 RosterLoader ✓, §8.5 GraphSpecLoader ✓ | ✓ | ✓ |
| GAP-9 | §6.4 ctx.is_subagent ✓, §6.5 no field ✓ | ✓ | ✓ |
| GAP-10 | §6.2 direct factory call ✓ | ✓ | ✓ |
| GAP-11 | No crash handling in SPEC ✓ | ✓ | ✓ |
| GAP-12 | §9 cooldown_turns ✓, no loop-back ✓ | ✓ | ✓ |
| GAP-13 | §6.6 agent_type ✓, no derivation rule ✓ | ✓ | ✓ |

All 13 findings pass verification.

---

## Convergence Dimension Summary

All 10 concerns (C1-C10) are closed, assumption-closed, or deferred:

| Concern | Status | Note |
|---|---|---|
| C1 Agent assembly | assumption-closed | Special agent exception bounded (§11 Assumption 3) |
| C2 LLM config source | closed | One path through LLM_PROVIDER slot; model.yml is default factory's internal config source |
| C3 External provider | deferred | v1 hardcode, v2 slot |
| C4 Governance config | deferred | v1 MemoryConfig derivation, v2 slot |
| C5 Graph assembly | closed | Converged to InfraAssembleStage |
| C6 Component storage | closed | Unified ComponentFactory |
| C7 Subagent timing | closed | Eager vs lazy, same interface |
| C8 Workspace eviction | closed | Different lifetimes justified |
| C9 Config validation | closed | Load-time vs assembly-time justified |
| C10 Special agent trigger | assumption-closed | Trigger converged; construction inline under fixed-4 assumption |

**No convergence violations found.** All dual paths are justified by execution-model differences or documented as v1 transitional.

---

## Completion Criterion Check

| Criterion | Met |
|---|---|
| Every structure-map item appears in ≥1 closure-matrix row | ✅ (70 items traced, 70 matrix rows) |
| Every row has a status | ✅ |
| Every gap row has a finding meeting the bar | ✅ (13 findings, all with location + consequence + fix) |
| Every assumption-closed row has its assumption recorded | ✅ |
| Cross-dimension seams traced | ✅ (6 seams, 4 gaps found) |
| Every finding re-verified | ✅ (13/13 verified) |

---

## Severity Summary

| Severity | Count | GAPs |
|---|---|---|
| CRITICAL | 1 | GAP-1 (plugin loader) |
| HIGH | 3 | GAP-2 (spec construction+config), GAP-4 (infra holder), GAP-6 (AssembledAgent type) |
| MEDIUM | 5 | GAP-3 (builder cleanup), GAP-5 (DATA_NAMESPACE overload), GAP-7 (resolve_bundle), GAP-8 (RosterLoader), GAP-10 (materialize path), GAP-11 (crash handling) |
| LOW | 3 | GAP-9 (is_subagent), GAP-12 (re-trigger), GAP-13 (AgentType derivation) |

**Design is NOT fully closed.** 13 gaps found — 1 critical, 3 high, 6 medium, 3 low. The critical gap (GAP-1: plugin loader) blocks the v1 goal. The 3 high gaps (GAP-2, GAP-4, GAP-6) block implementation of core assembly.

However, convergence is fully closed (no dual-path violations), and all gaps have concrete fixes. The design is structurally sound — the gaps are missing definitions, not architectural flaws.
