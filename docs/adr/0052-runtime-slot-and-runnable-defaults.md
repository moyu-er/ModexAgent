# Runtime Slotting and Framework-Runnable Defaults

**Status**: Accepted
**Date**: 2026-09-30

## Context

Two long-standing structural problems:

**Problem one: the agent runtime was not a slot product.** `DefaultAgentFactory` branched internally on a strategy enumeration (react → `ReActAgent` + `ReActTurnRunner`, external → the separate stub-subclass hack `ExternalAwareFactory`). A third party wanting to swap in their own loop (say a simplified loop with custom control) had to either fork the factory or hand-build an agent outside the assembly pipeline — a declaration file could not name a custom `execution_strategy` and have it actually run. The EXECUTION_STRATEGY slot existed in name, but after slot resolution it fell back into a closed-set enumeration branch; the slot's promise was empty. The strategy's capability declarations were also scattered ad-hoc booleans (`supports_subagents` / `requires_main_agent_tools` / `requires_llm_provider`) with no unified contract.

**Problem two: the framework could not run on its own.** The runnable pool shapes (react/external strategies), `create_pool`, the backend factories, the model universe (model.yml parsing, the per-turn model-selection `multi` factory), the input pipeline, and the application-service lifecycle all lived in `examples/bot_project`. The framework's own component registry (`DefaultPlugin`) registered parts, not "things that can boot"; so any new deployment (eval, ACP, third-party bot) had to copy the entire assembly chain out of the example. Additionally, plugin loading used to require the plugin directory on `sys.path` with an importable top-level package name — deployment packaging (wheels, synthetic module-name loading) and plugin identity (qualified name) had no contract.

## Decision

### 1. The agent runtime is an EXECUTION_STRATEGY slot product

- **Resolution by name; enumeration branches deleted.** `DefaultAgentFactory`'s strategy enumeration branches and `ExternalAwareFactory` are deleted outright. The declaration's `execution_strategy` is a **name**, resolved at assembly time through the EXECUTION_STRATEGY slot into an `ExecutionStrategy` instance (`strategy_registry_from_components` derives the runtime strategy registry from the ComponentRegistry, so services do not hand-maintain a second copy).
- **Runtime-construction seam.** Strategies deliver a runtime in two shapes:
  - *Self-contained shape* (external): `assemble_main` directly builds its own agent + turn runner and returns it as `StrategyAssembly.main`;
  - *Native-components shape* (react and third-party custom loops): the strategy returns `StrategyAssembly.runtime_constructor` (the strategy's own `AgentFactory` implementation), and Stage 4 threads it through `assemble_native_agent` — native component resolution (tools/hooks/llm/memory/capabilities) is fully reused, and only the loop itself is the strategy's ("native components + custom loop"). The seam's carrier is `NativeAssemblyInputs.agent_factory` (default = the bundled react path).
- **`RuntimeOwnership` as a fully derived contract (completed form).** The scattered ad-hoc capability booleans converge into one frozen model (`needs_llm_provider` / `needs_main_agent_tools` / `needs_memory` / `supports_approval` / `supports_subagents` / `owns_context`); the vocabulary and resolver live in `scope/runtime_ownership.py` (scope owns the declaration vocabulary; `multi_agent` imports downward, no re-export shim). All six axes have mechanical consumers:

  - Assembly time: `needs_llm_provider` (whether `create_pool` resolves the LLM_PROVIDER slot and whether the native core keeps the provider fallback), `needs_main_agent_tools` (the communication tool surface + `wire_main_pipeline`), `supports_subagents` (the subagent rules in the base `validate_pool_spec`), `needs_memory` / `owns_context` (whether `create_pool`'s native inputs thread framework memory/context products — contract-driven, no longer the strategy's own conscientiousness).
  - **Compile time (the core move)**: `compile_scope` resolves the declared strategy name through the EXECUTION_STRATEGY slot into ownership — the factory's `probe()` must expose a `StrategyManifest` (`StrategyComponentFactory` is the single registration face shared by bundled and third-party strategies). Derivation rules: V12 (EXTERNAL_CAPABILITIES) and the position-default-hook exclusion re-key on `owns_context` (rule ids and message bytes unchanged, pinned by `test_w5_v12_semantics.py`); declaring `memory:`/`memory_system:` with `needs_memory=False` is a compile error; declaring approval on the root with `supports_approval=False` is a compile error; an unregistered plugin strategy name fails at **compile time** (mirroring the V13 precedent — one cycle earlier than assembly-time late slot binding). Resolution order: the registry slot wins first (including overrides of bundled names), the bundled enumeration shapes (the scope-owned constants for react/external, i.e. the same constants both bundled strategies' `ownership` attributes return) act as fallback, and a loud error if neither exists; probing does not enter the compiled artifact (pinned by the spec-hash byte-stability anchor).
- **The `AgentMaterializer` seam.** Template materialization (`AgentTemplate.materialize`) goes through the `AgentMaterializer` ABC owned by `multi_agent`, with the native implementation injected by `plugins/assembly/subagent_materializer.py` — `multi_agent` no longer reaches upward to import assembly code (the layering-tree basis; see ADR-0051).
- **`StrategyAssembly` shared contract shrunk.** The container is now down to: shared pool services (`tool_manager` / `context_manager` / `notification_service` / `target_store` / `control_channel` / `root_provider`) + exactly one per-shape product (`react_products` / `runtime_constructor` / `main`). Dead fields (system-prompt provider, tool/MCP/terminal managers, the dream engine, command processor, hook specs, external backend/session-map products, cleanup hooks) and the loose `external_deps: dict[str, Any]` are all deleted.

### 2. Framework-runnable defaults

The following are promoted from the example to framework ownership; a registry loading only `DefaultPlugin` can assemble a runnable pool:

- **Pool shape strategies**: `plugins/assembly/strategies/` (`react` + `external`), registered via `DefaultPlugin`'s `register_default_strategies`.
- **`create_pool`**: `plugins/assembly/pool_factory.py` — the framework owner of the two-layer construction (generic assembly + strategy assembly).
- **Backend factories**: `plugins/assembly/backend_factory.py` (OpenCode external backend construction).
- **Model universe**: `app/models/` (registry / choice / provider / assembly) + two bundled factories for the `LLM_PROVIDER` slot, `default` (single-provider model.yml) and `multi` (per-turn model selection over a multi-provider registry).
- **Input pipeline skeleton**: `pipeline/input/` (the `InputStage` ABC, envelope, stage skeletons for IM/WebUI/ACP, declarative `order` reordering, slot insertion rules).
- **Application service skeleton**: `app/` — `AppService` (three-root assembly, registry loading, the shared-persistence open pair, lifecycle contract), `AppAssemblyRoots` (explicit three-way separation of config root / resource root / workspace root), `supervisor` (process supervision), `config_domain` (deployment-injected application domain config), and the root `AppConfig` YAML face.
- **`MEMORY_SYSTEM`/`DATA_NAMESPACE` defaults**: the `default` framework memory system and the `default` graph state model (registered by `DefaultPlugin`; the slots have real traffic).

### 3. Plugin packaging contract (qualified-name loading)

- **Three sources**: the project plugin directory (`PluginDiscoveryConfig.project_plugin_paths`, e.g. the bot's `bot_plugins/`), the user directory (`~/.modex_agent/plugins`, **enabled by default**, disable with `AppConfig.user_plugins_enabled: false`; a missing directory is normal), and the installed distribution's `modex_agent.plugins` entry points; plus the framework-bundled `DefaultPlugin`.
- **Qualified-name loading**: each `*.py` in a directory is imported via importlib under a synthetic qualified name (`modex_agent_userplugins_<dir-sha>.<module>`) — no `sys.path` mutation, no importable top-level package-name requirement, no global `plugins` package. The single source of identity is the factory resolved from the registry (stage configuration is always constructed by the factory's own `config_model`; plugin modules are never imported directly).
- **Priority**: user > project > entry_points > bundled, resolved by registry flush, independent of scan order.
- **Channel adapter registration face**: `PluginRegistrationContext.register_channel_adapter` — channel adapters are objects resolved once at service startup from configuration; they do not enter ComponentSlot and land in a separate `ChannelAdapterRegistry`.

### 4. Red anchors (behavior is machine-proven, not asserted)

- `tests/integration/plugins/test_w5_runtime_slot.py` — a third-party minimal-loop strategy declared by name, assembled through the production path `create_pool` → 4-stage pipeline → `assemble_native_agent`, and one turn runs; asserts the runtime constructor is **the strategy's own** (not `ReActTurnRunner`), the descriptor carries the registered name, the ad-hoc capability booleans no longer exist, and all three names (react/external/custom) resolve.
- `tests/unit/scope/test_w5_v12_semantics.py` — the semantics of V12's external-agent capability exclusion pinned (rule id + message bytes; after W5 the trigger key is `owns_context`, message unchanged).
- `tests/unit/scope/test_w5_ownership_derivation.py` — red anchor for ownership's compile-time visibility: an unregistered plugin strategy name fails at compile time; `needs_memory` compile error; a `supports_approval` declaration is accepted (the MED4 friendly-form contract: the bill honestly reports NOT applicable, eligibility is derived via `resolve_strategy_ownership` — the consumption point is the bill face, not a compile rejection); V12/position-hook derived via probe; registry registration takes precedence over the bundled shape; probe does not change the spec-hash.
- **G-CAP1 probe-surface exemption**: ownership probing at compile time goes through `registry.factories(ComponentSlot.EXECUTION_STRATEGY)` (**metadata only, no instantiation; strategy creation stays late-bound at assembly time**) — `verify_slot_gates.py`'s G-CAP1 allow-list contains the two hits in `scope/runtime_ownership.py`; the exclusivity of compile-time slot resolution (CAPABILITY is the only slot that may "resolve/instantiate") is unchanged.
- `tests/architecture/test_no_execution_strategy_branches.py` — no `if execution_strategy ==` branches in assembly and pipeline sources (the whitelist retains only runtime per-target behavior).
- `tests/architecture/test_dependency_tree.py` — the layering ledger is empty (the level legality of strategies and assembly).
- `tests/integration/test_memory_system_slot.py`, `tests/integration/test_yaml_plugin_new_agent.py`, `tests/unit/plugins/test_loader*.py` / `test_app_registry_load.py` — slot traffic and the plugin loading contract.

## Consequences

### Positive

- "Write a new loop" = implement `ExecutionStrategy` + register it in the EXECUTION_STRATEGY slot, reference it by name in the declaration; zero changes to the assembly pipeline, zero framework branches.
- The framework (with only `DefaultPlugin`) is self-sufficiently runnable; the example returns to its "business wiring" identity, and new deployments no longer copy the assembly chain.
- Plugin directories become pure data (any directory name, no import-name requirements); wheel packaging and user-directory plugins both work.

### Negative / behavior changes (recorded honestly)

- **The external main no longer receives a context manager**: the external strategy's self-contained shape declares `owns_context=True`, and `assemble_main` assembles the pipeline with `context_manager=None` and `session_binding_store=None` — the previous path that hung a framework context manager on the external main via the factory branch no longer exists (the external harness owns its own context).
- The deletion of `StrategyAssembly` dead fields is a breaking shrink for any (hypothetical) old-field readers; there are no production callers and no compatibility shim was left (convergence discipline).
- A custom subagent's execution-strategy name still resolves via `strategy_registry`. After W5, the scope-side structural exclusions (validator V12, the compiler's position-default-hook rows, the capability protocol's external short-circuit) no longer key on `provider_kind` — all derive from `owns_context`; the `provider_kind` discriminator survives only in agent_type topology derivation and external backend selection (its true semantics: "which CLI to launch"). external + `memory:`/`memory_system:` or a root approval declaration changes from "silently ineffective" to a compile error (a new fail-loud behavior, see above).

## Related

- ADR-0025 — the ExecutionStrategy abstraction and pipeline slimming (this ADR's predecessor; slotting is its completion)
- ADR-0041 — the unified plugin assembly system (the foundation of ComponentRegistry/slots/pipeline)
- ADR-0042/0043 — the scope declaration tree and the dual execution model (the path a declared `execution_strategy` name takes from compilation to assembly)
- ADR-0047 — capability bundling (how the CAPABILITY slot and the default sets coexist)
- ADR-0051 — the package layering total-order tree (the mechanical basis for strategies'/assembly's level legality)
