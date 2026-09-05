<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-04 | capability section HEAD/TAIL anchors -->

# plugins

## Purpose
Plugin-unified agent assembly system — an 11-slot component-factory registry
(``ComponentRegistry``) where plugins register factories, and a 4-stage
``AssemblyPipeline`` assembles main agents from those factories at pool
construction time. The 11th slot (``CAPABILITY``, ADR-0047) hosts capability
bundles — cross-slot units that contribute tools/hooks/prompt-sections into
the compile product and own their own enablement logic. Replaces the legacy
``PluginContext``/``PluginManager`` system (deleted).

## Key Types

| Type | File | Description |
|------|------|-------------|
| ``ComponentSlot`` | ``abc.py`` | StrEnum of 11 extension slots (``TOOL``, ``HOOK``, ``MEMORY_SYSTEM``, ``LLM_PROVIDER``, ``SYSTEM_PROMPT_PROVIDER``, ``INTERCEPTOR``, ``COMMAND_HANDLER``, ``EXECUTION_STRATEGY``, ``INPUT_STAGE``, ``DATA_NAMESPACE``, ``CAPABILITY``). The set is authoritative — additions/removals require a SPEC errata (``MEMORY_SYSTEM`` added by Errata-7; three slots removed by Errata-8; ``CAPABILITY`` added by ADR-0047) |
| ``CAPABILITY`` slot | ``abc.py`` (``ComponentSlot.CAPABILITY``) | Hosts ``Capability`` bundles — the only compile-time-resolved slot (every other slot stays late-binding at assembly; a mechanical gate in ``scripts/verify_slot_gates.py`` anchors the asymmetry). Resolution goes through ``ComponentRegistry.resolve_capability(name)`` / ``registry.names(ComponentSlot.CAPABILITY)`` |
| ``Capability`` ABC + protocol types | ``capability.py`` | The cross-slot capability-bundle unit (ADR-0047, five-phase protocol: ``applies``/``contribute``/``bind`` compile-time, ``supply`` pool-assembly-time, ``assemble`` agent-assembly-time). Frozen view/contribution types: ``AgentDeclarationView``/``AgentDeclaredFields`` (C0 predicate input — declared state only), ``TreePositionView``, ``CapabilityContribution`` (tools/``tool_replacements``/hooks/sections/``derived_tools``), ``PromptSectionSpec`` + ``SectionPlacement`` (fixed `HEAD`/`TAIL` anchors), ``CapabilityBinding`` (``active_sections`` + ``hooks`` vouching + payload), ``CapabilitySupply`` (pool-supply ABC with async ``start``/``stop`` lifecycle defaults), ``CapabilityWiring`` (prompt providers + per-agent artifacts), ``PoolSupplyView`` (pool-level (agent, config) entries + distilled resource fields), ``FinalRosterView`` (C2 input), ``ToolReplacementSpec``/``DerivedToolSpec``/``DerivedToolOrigin``, ``CapabilityError``. Import-light: no runtime framework imports |
| ``register_capability`` | ``loader.py`` (``PluginRegistrationContext``) | Registration face for the CAPABILITY slot — same source-priority bookkeeping as the factory slots (same-source dup raises; cross-source resolves user > project > entry_points > bundled) |
| ``DATA_NAMESPACE`` slot | ``abc.py`` (``ComponentSlot.DATA_NAMESPACE``) | Type registration for plugin data (KVStore ``TypedBundle``) and graph ``state_schema`` resolution. ``DefaultPlugin`` leaves it empty — plugins register Pydantic model classes on demand; the graph orchestrator consumes them via the injected ``state_schema_compiler`` (SPEC Errata-8 (f)) |
| ``main_agent_memory()`` / ``subagent_memory()`` | ``modex_agent/memory/presets.py`` | Memory presets as **plain functions** (no factory indirection; SPEC Errata-8 (a)). Consumed by BIZ wiring and native_core's ``_merge_memory`` fallback. Formerly registered through a removed modifier slot |
| ``MemoryProvider`` ABC | ``modex_agent/memory/core/provider.py`` | ABC relocated out of ``plugins/abc.py`` (fixes the memory→plugins import inversion; guarded by ``tests/architecture/test_memory_package_isolation.py``) |
| ``MEMORY_SYSTEM`` slot | ``abc.py`` (``ComponentSlot.MEMORY_SYSTEM``) | Produces a ``ContextManager`` instance replacing the entire memory/context system. When ``spec.memory_system`` references a registered factory, ``native_core`` resolves it and uses the factory-produced ``ContextManager`` instead of the default ``MemorySystemContextManager``. This is the largest replacement granularity — users fully own prompt assembly, history, governance, and all memory behavior. No built-in factory registered (``DefaultPlugin`` leaves this slot empty); users register their own. See SPEC Errata-7. |
| ``ComponentFactory`` | ``abc.py`` | ABC — ``create(config, ctx)`` produces one component instance per assembly |
| ``SimpleFactory`` / ``PrototypeFactory`` | ``abc.py`` | The two off-the-shelf factory specializations: ``SimpleFactory`` returns a pre-built SINGLETON instance (stateless hooks, execution-strategy classes, test provider injection — sharing is intentional); ``PrototypeFactory`` builds a FRESH instance per ``create()`` via a zero-arg builder (the TOOL slot's framework defaults — a shared mutable ``Tool`` instance would leak config mutations across agents/pools/workspaces). Both expose ``probe()`` for synchronous introspection (manifests, audits). Dedicated factory subclasses own differentiated construction with internal reuse (e.g. ``BashToolFactory`` returning the pool's shared persistent shell) |
| ``HookFactory`` / ``ReactHookFactory`` / ``MemoryHookFactory`` | ``abc.py`` | Hook-dispatch factory hierarchy (``applies_to`` / ``hook_runner`` / ``priority`` ClassVars; stages read them directly, never ``isinstance``) |
| ``ComponentRegistry`` | ``registry.py`` | Process-wide singleton store: ``register(slot, name, factory)``, ``resolve(slot, name)``, ``resolve_bundle()``. Factories only — no instances, no KVStore ownership, no hot-plug |
| ``TypedBundle`` | ``registry.py`` | Typed KVStore accessor for a plugin namespace (scope-forwarded keys) |
| ``Plugin`` | ``loader.py`` | ABC — ``register(ctx: PluginRegistrationContext)`` registers factories into the registry |
| ``PluginRegistrationContext`` | ``loader.py`` | Registration facade passed to ``Plugin.register()`` |
| ``ComponentRegistryLoader`` | ``loader.py`` | Loads bundled + project + user + entry_points plugins into a ``ComponentRegistry`` (cross-source same ``(slot, name)`` resolves by source priority **user > project > entry_points > bundled** — lower-priority duplicates are skipped with an info log; same-source conflict raises ``ValueError``, per ADR-0042 O2). ``PluginDiscoveryConfig.bundled_factories`` is a ``tuple[Plugin, ...]`` |
| ``AssemblyPipeline`` / ``AssemblyStage`` / ``SupplyInfra`` | ``assembly/pipeline.py`` + ``assembly/context.py`` | Main-agent async pipeline runner + stage ABC + typed supply carrier. Stage matrix: ``native_main``=1→2→3→4, ``external_main``=1→2→3; ``native_sub``/``external_sub``=(none — ``AgentTemplate.materialize`` → ``assemble_native_agent`` / ``assemble_sub``) |
| ``AssemblySpec`` / ``MemoryOverrides`` | ``assembly/spec.py`` | Assembly input spec (per-agent and pool-level component-name references) + per-agent memory config override |
| ``AssemblyContext`` / ``PoolRuntimeDeps`` / ``AgentContext`` | ``assembly/context.py`` | Layered assembly context + pool runtime deps; ``AgentContext`` (the ``WorkspaceContext``/``PoolContext``/``AssemblyContext`` diamond, ticket 04) carries per-invocation data (parent session, invocation id, agent identity, per-agent spec) to factories and to ``ExecutionStrategy.assemble_sub`` (ticket 10) |
| ``AssembledAgent`` / ``AssemblyBuilder`` | ``assembly/builder.py`` | Output container + mutable accumulator with cleanup-on-failure |
| ``DefaultPlugin`` | ``defaults/__init__.py`` | Bundled FW-default plugin. Its ``register_default_*`` groups register tools, hooks, providers, interceptors, commands, and capabilities. ``EXECUTION_STRATEGY``/``INPUT_STAGE`` remain bot-owned; ``MEMORY_SYSTEM``/``DATA_NAMESPACE`` are on-demand plugin slots. |
| ``defaults/capabilities/`` | ``defaults/capabilities/`` | Bundled implementations registered by ``register_default_capabilities``: ``aci``, ``ast_grep``, ``experience``, ``skills``, ``subagents``, ``todo``, and ``tracing``. `skills` auto-applies to every native agent, honors `capabilities: {skills: false}`, and is structurally excluded from external agents; its `SkillsSupply` owns one `SkillCatalog` per effective agent (see `defaults/capabilities/skills/AGENTS.md`). `subagents` auto-applies from tree position; the remaining bundles are declaration-driven. |
| ``NativeAssemblyInputs`` / ``NativeAssemblyResult`` / ``assemble_native_agent`` | ``assembly/native_core.py`` | Unified native-agent core — resolves the 5 per-agent slots (TOOL/LLM_PROVIDER/SYSTEM_PROMPT_PROVIDER/MEMORY_SYSTEM/HOOK), merges memory, dispatches hooks (react/memory dual runner), constructs the descriptor, and calls ``agent_factory.create_agent``. ``NativeAssemblyInputs``/``NativeAssemblyResult`` are regular classes (``__init__`` assignment, not ``@dataclass``). Called by both Stage 4 (main) and ``AgentTemplate.materialize`` (sub). See SPEC Errata-6 (unified core) and Errata-7 (MEMORY_SYSTEM slot). |
| ``assemble_declared_single_agent`` | ``assembly/single_agent.py`` | Poolless root-agent assembly from a compiled declaration, reusing the native core and exposing memory/runtime handles for standalone harnesses. |
| ``LlmDefaults`` | ``assembly/native_core.py`` | Frozen Pydantic ``BaseModel`` (``extra="forbid"``) value object carrying default LLM configuration values (model/temperature/max_output_tokens/reasoning_effort/model_info). |
| ``ContextStrategy`` | ``multi_agent/descriptor.py`` | ``StrEnum`` controlling per-agent context persistence strategy; consumed by ``assemble_native_agent`` when constructing ``AgentDescriptor`` (default ``ContextStrategy.PERSISTENT``). |

## Assembly Pipeline Stages (SPEC §6.3, Errata-5)

The pipeline is a **main-agent orchestrator**: it runs stages 1→2→3→4 for
``native_main`` and stages 1→2→3 for ``external_main``. Subagents are
constructed directly by ``AgentTemplate.materialize`` — their per-invocation
data (``parent_session``, ``invocation_id``, materialize deps) does not fit
the per-pool ``AssemblyContext`` factory contract.

| # | Stage | File | Purpose |
|---|-------|------|---------|
| 1 | ``WorkspaceMaterializeStage`` | ``assembly/stages/workspace_materialize.py`` | Materialize workspace resources (no-op when pre-supplied — prevents recursive single-flight deadlock) |
| 2 | ``InfraAssembleStage`` | ``assembly/stages/infra_assemble.py`` | Supply-mode only: copy the orchestrator's ``SupplyInfra`` to ``builder.infra`` verbatim |
| 3 | ``PoolAssembleStage`` | ``assembly/stages/pool_assemble.py`` | Supply-mode only: resolve EXECUTION_STRATEGY from the registry, await ``assemble_main`` on the supplied ``PoolAssemblyContext`` |
| 4 | ``AgentAssembleStage`` | ``assembly/stages/agent_assemble.py`` | Resolve native component slots, construct the authoritative runtime, and register it. Delegates to ``assemble_native_agent`` (``native_core.py``); ``result.instance`` is the authoritative main agent consumed by ``create_pool`` (SPEC Errata-6). |
## For AI Agents
- Plugin contract: ``def register(ctx: PluginRegistrationContext) -> None:`` — registers ``ComponentFactory`` instances into the registry by ``(slot, name)``.
- ``ComponentRegistry`` holds factories only, never instances. Instances are created per-assembly via ``factory.create(config, ctx)``.
- Singleton-vs-prototype is the factory's private decision: ``SimpleFactory`` for intentional sharing (stateless hooks, strategy classes), ``PrototypeFactory`` for per-assembly construction, a dedicated subclass for differentiated construction with internal reuse. The TOOL slot's framework defaults use prototype semantics — pick ``SimpleFactory`` there only when sharing a heavy resource is the point (and the tool must then be safe to share).
- No hot-plug: components are registered at startup and survive until process exit.
- ``AssemblyPipeline`` is fully async; cleanup-on-failure runs ``builder.cleanup()`` then re-raises (SPEC §6.1: "装配失败不泄漏资源").
- ``AssemblySpec`` carries component-name references (strings); the pipeline resolves them via the registry during assembly.
- Pool-level ``INTERCEPTOR`` and ``COMMAND_HANDLER`` names also travel through ``AssemblySpec``. Bot pipeline wiring clones the shared interceptor chain only when additions are configured and builds a per-pool slash-command processor when ``commands`` is configured.

## Dependencies
- ``pydantic`` — ``BaseModel`` for ``AssemblySpec``, ``MemoryOverrides``, config models
- ``modex_agent.core.scope`` — ``RecordScope`` for ``TypedBundle`` key prefixing
- ``modex_agent.memory.core.split_stores`` — ``KVStore`` ABC for ``TypedBundle``

<!-- MANUAL: -->
