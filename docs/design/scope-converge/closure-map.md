# Structure Map: scope-converge SPEC

> Source: `docs/design/scope-converge/SPEC.md` (revised 2026-08-18)
> Purpose: design-closure Phase 0 inventory

## Data items

| ID | Name | SPEC location | Role |
|---|---|---|---|
| D1 | AssemblySpec | §6.1, §6.6 | frozen Pydantic input: component names + config + workspace_ctx |
| D2 | AssemblyBuilder | §6.1 | mutable accumulator: holds constructed instances during assembly |
| D3 | AssembledAgent | §6.1 | final output: assembled agent instance |
| D4 | AssemblyContext | §6.5 | layered context: global(ComponentRegistry) + workspace + pool_runtime |
| D5 | PoolRoster | §5.2 | frozen Pydantic: pool-level config (agents, strategy, peers) |
| D6 | AgentRoster | §5.2 | frozen Pydantic: per-agent config (tools, hooks, memory, prompt, llm) |
| D7 | GraphRoster | §5.2 | frozen Pydantic: graph-level config (topology, state_schema) |
| D8 | ComponentFactory | §4.2 | ABC: create(config, ctx) -> instance |
| D9 | SimpleFactory | §4.2 | wraps stateless instances as ComponentFactory |
| D10 | Plugin(ABC) | §4.5 | type-safe plugin entry: register(ctx) -> None |
| D11 | PluginRegistrationContext | §4.5 | collecting facade: register_* methods (10 — SPEC Errata-8) |
| D12 | ComponentRegistry | §4.1 | global singleton: dict[Slot, dict[name, ComponentFactory]] |
| D13 | ComponentSlot | §4.3 | StrEnum closed set: 10 values (SPEC Errata-8) |
| D14 | MemoryOverrides | §5.5 | frozen Pydantic, all-optional: merged with default MemoryConfig |
| D15 | MemoryConfig | §5.5 | existing: from memory_defaults.py (main_agent_memory / subagent_memory) |
| D16 | FieldSpec | §8.2 | modex_graph frozen Pydantic: name/type/item_type/initial |
| D17 | state_schema | §8.2 | dict[str, FieldSpec] or None on GraphSpec |
| D18 | state_schema_compiler | §8.2 | Callable injection: dict[str, FieldSpec] -> type[GraphState] |
| D19 | PoolRuntimeDeps | §6.5 | per-pool runtime objects: tree_manager, control_channel, etc. |
| D20 | WorkspaceContext | §6.6 | identity value object: target/paths/is_home |
| D21 | AgentType | §6.6 | enum: native_main / native_sub / external_main / external_sub |
| D22 | provider_kind | §6.4, §6.6 | v1 hardcoded: "pi" / "opencode" |
| D23 | toolPreset | §5.3 | preset name string, expands to component name list |
| D24 | TriggerConfig | §9 | frozen Pydantic for special agent triggers (enabled, thresholds) |
| D25 | KVStore data | §10.1 | plugin persistent data: typed namespace + Pydantic model |
| D26 | Roster YAML files | §5.1 | pool.yml + templates/*.yml + graphs/*.yml |

## States

| ID | Name | SPEC location | Role |
|---|---|---|---|
| S1 | AgentType | §6.6 | 4 values: native_main/native_sub/external_main/external_sub |
| S2 | Assembly timing | §6.3 | pool-creation (main) vs first-turn (sub) |
| S3 | Workspace lifecycle | §6.5 | materialize → active → evict → re-materialize |
| S4 | Component loading priority | §4.1 | bundled > user > entry_points |
| S5 | Plugin registration | §4.5 | unloaded → registered (via register()) → activated (in registry) |
| S6 | Special agent trigger | §9 | disabled → enabled (in roster) → triggered (by hook/memory) → running → done |
| S7 | Name conflict resolution | §4.1 | same-source: ValueError; cross-source: first-seen-wins |
| S8 | Roster validation timing | §5.2 | structural at load → component-existence at assembly (late binding) |

## Interfaces

| ID | Name | SPEC location | Role |
|---|---|---|---|
| I1 | ComponentFactory.create(config, ctx) | §4.2 | ABC method: returns component instance |
| I2 | Plugin.register(ctx) | §4.5 | ABC method: registers components via ctx |
| I3 | PluginRegistrationContext.register_* | §4.5 | 12 methods: register_tool/hook/memory_provider/etc. |
| I4 | AssemblyStage.process(spec, builder, ctx) | §6.1 | ABC method: modifies builder, returns None |
| I5 | AssemblyPipeline.run(spec, ctx) | §6.1 | runner: creates builder, runs stages, returns AssembledAgent |
| I6 | AssemblyBuilder.build_agent() | §6.1 | terminal: assembles final agent from accumulated parts |
| I7 | ResourceFactory.materialize(ctx) | §6.2, §7 | existing ABC: workspace resource construction |
| I8 | ExecutionStrategy.assemble(ctx) | §6.2, §7 | existing ABC: pool shape construction |
| I9 | RosterLoader | §5.2 | loads YAML → PoolRoster/AgentRoster/GraphRoster |
| I10 | resolve_bundle(namespace) | §4.3, §10.1 | DATA_NAMESPACE first-class accessor |
| I11 | ExternalExecutionStrategy.assemble | §6.4 | converges main+sub, branches on ctx.is_subagent |
| I12 | state_schema_compiler | §8.2 | Callable: dict[str, FieldSpec] -> type[GraphState] |
| I13 | GraphSpecCompiler.compile | §8.2 | existing, extended with state_schema support |
| I14 | assemble_agent(spec, ctx) | §6.3 | reusable function: pipeline.run() wrapper |

## Objects

| ID | Name | SPEC location | Role | Scope |
|---|---|---|---|---|
| O1 | ComponentRegistry | §4.1 | global factory store | global (never destroyed) |
| O2 | AssemblyPipeline | §6.1 | stage runner | global (stateless, reusable) |
| O3 | AssemblyContext | §6.5 | layered deps carrier | per-pool (workspace layer evictable) |
| O4 | AssemblyBuilder | §6.1 | mutable accumulator | per-assembly-call (transient) |
| O5 | PoolRuntimeDeps | §6.5 | per-pool runtime objects | per-pool (destroyed on evict) |
| O6 | WorkspaceResources | §7 | broker/inbox/bus/interceptor | per-workspace (evictable) |
| O7 | AgentPool | §7 | agent container | per-pool (within workspace) |
| O8 | GraphOrchestrator | §7, §8.5 | graph engine orchestrator | per-workspace (built in InfraAssembleStage) |
| O9 | KVStore / MemoryStoreBundle | §10.1 | plugin data storage | per-scope (session/user/global) |
| O10 | NodeRegistry | §8.5 | graph node factory registry | per-workspace |
| O11 | RosterLoader output | §5.2 | parsed roster objects | global (startup-loaded) |
| O12 | Special agent instances | §9 | experience reviewer/compactor/etc. | per-trigger (transient, not pooled) |

## Concerns

| ID | Concern | Path 1 | Path 2 | Justification |
|---|---|---|---|---|
| C1 | Agent assembly | AssemblyPipeline (§6) | Special agent inline (§9) | Different execution models: pipeline builds pooled agents; special agents are transient trigger-based with inline tools |
| C2 | LLM config source | model.yml (§5.7) | LLM_PROVIDER slot (§4.3) | v1 transitional: default "default" component reads model.yml; v2 fully slot-driven |
| C3 | External provider | provider_kind hardcoded (§6.4) | EXTERNAL_PROVIDER slot (deferred) | v1 transitional: hardcode until slot lands in v2 |
| C4 | Governance config | MemoryConfig derivation (§5.5) | GOVERNANCE slot (deferred) | v1 transitional: derived from memory; v2 slot-driven |
| C5 | Graph assembly | InfraAssembleStage (§6.2, §8.5) | (GraphAssembleStage removed) | Converged: graph assembly is workspace-level infra, not agent-level |
| C6 | Component storage | ComponentFactory unified (§4.2) | (instance+factory split removed) | Converged: SimpleFactory wraps stateless; all entries are factories |
| C7 | Subagent assembly timing | pool-creation (main agent) | first-turn (subagent) | Justified: lazy-load for subagents; both call same assemble_agent() |
| C8 | Workspace eviction scope | global layer survives (ComponentRegistry) | workspace+pool destroyed | Justified: different lifetimes; factories stateless, instances per-pool |
| C9 | Config validation | structural at load (§5.2) | component-existence at assembly (§5.2) | Justified: late binding allows roster to reference not-yet-registered components |
| C10 | Special agent trigger | plugin config in roster (§9) | inline construction (§9) | Justified: trigger is configurable (plugin), construction is inline (tools not componentizable) |
