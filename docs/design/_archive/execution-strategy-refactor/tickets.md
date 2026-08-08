# Tickets: Execution strategy abstraction and pipeline slimming

**Status: ALL 8 TICKETS IMPLEMENTED (2026-07-18) + 2 cleanup passes.**

One-line summary: introduce an `ExecutionStrategy` ABC so pool shapes (react, external, future) are assembled by pluggable strategy objects rather than scattered `if execution_strategy ==` branches, and slim `AgentPipeline` from 33 params / 5 mirrors / 569 lines to 13 params / 0 setter mirrors / 347 lines.

Source spec: `docs/design/execution-strategy-refactor/PRD.md`
Source ADR: `docs/adr/0025-execution-strategy-abstraction-and-pipeline-slimming.md` (see Disposition for implementation deviations)
Source technical spec: `docs/design/execution-strategy-refactor/spec.md`

All tickets are implemented. See ADR-0025 Disposition for documented
deviations from the original spec (TurnRunner ABC surface, pool_builder
size, factory.py retention, _wire_main_pipeline deferral, store.py
validation retention, external bloat elimination).

Glossary: `CONTEXT.md` -> "Execution Strategy", "Pool Assembly", "Turn Runner", "Strategy Assembly".


---

## Add `TurnRunner` ABC + `ExecutionStrategy` ABC + Registry

**What to build:** The framework gains its pool-shape extension point. A `TurnRunner` ABC (one abstract method, `process_locked`) becomes the seam between `AgentPipeline` and concrete turn runners. An `ExecutionStrategy` ABC (`name`, `supports_subagents`, `requires_main_agent_tools`, `assemble(ctx) -> StrategyAssembly`, `validate_pool_spec(spec)`) becomes the seam between `pool_builder.create_pool` and per-shape assembly recipes. An `ExecutionStrategyRegistry` (`register(strategy)`, `resolve(name) -> ExecutionStrategy`) holds strategies process-scoped, write-once-read-many. Two frozen `@dataclass` types carry the assembly contract: `PoolAssemblyContext` (input to `assemble()`, ~30 common-assembly resource fields, strategies must not mutate) and `StrategyAssembly` (output of `assemble()`, carries `Agent` + `TurnRunner` + common services + react-only collaborators + external-only collaborators + `extra_cleanup` hooks, with `None` defaults for the unused strategy's fields). A `default_strategy_registry()` factory returns an empty registry (strategies register themselves in tickets 3 and 4). No existing code changes -- these are pure additions. New unit tests verify the ABCs instantiate, the registry registers/resolves/rejects-unknown-names, and the frozen dataclasses reject mutation.

**Blocked by:** None -- can start immediately.

- [ ] `TurnRunner` ABC defined with one abstract method `process_locked(input_msg, session_id, route_result=None, *, session) -> AgentResult | None`; lives in `pipeline/` to preserve the `multi_agent/ -> pipeline/` non-dependence.
- [ ] `ExecutionStrategy` ABC defined with `name`, `supports_subagents` (default `True`), `requires_main_agent_tools` (default `True`), `assemble(ctx) -> StrategyAssembly` (abstract), `validate_pool_spec(spec)` (abstract); lives in `multi_agent/execution_strategy.py`.
- [ ] `ExecutionStrategyRegistry` defined with `register(strategy)` (rejects duplicate names) and `resolve(name) -> ExecutionStrategy` (raises on unknown).
- [ ] `PoolAssemblyContext` defined as frozen `@dataclass` (~30 fields: `pool_name`, `pool_spec`, `project_dir`, `data_dir`, `workspace_handle`, `workspace_resolver`, `broker`, `inbox_server`, `agent_bus`, `output_adapter`, `emitter_factory`, `safety`, `retention`, `app_config`, `persistence`, `mcp_registry`, `shared_hooks`, `shared_hook_runner`, `shared_interceptor_chain`, `session_registry`, `session_store`, `bot_model_config`, `model_choice_registry`, `command_processor`, `control_channel`, `pool_data`, `transcript_store`, `on_session_start`, `on_session_end`, `registry`, `router`); runtime-object container per rule 12, NOT Pydantic `BaseModel`.
- [ ] `StrategyAssembly` defined as frozen `@dataclass` with required fields (`agent`, `turn_runner`, `notification_service`, `communication_service`, `target_store`), react-only optional fields (`provider`, `tool_manager`, `skill_manager`, `mcp_manager`, `terminal_manager`, `context_manager`, `dream_engine`, `dream_interval`, `command_processor`, `control_channel` -- all `None` for external), external-only optional fields (`backend`, `session_map_store` -- all `None` for react), and `extra_cleanup: tuple[...] = ()`.
- [ ] `default_strategy_registry()` factory returns an empty `ExecutionStrategyRegistry`.
- [ ] Unit tests pass: ABC instantiation, registry register/resolve/unknown-name, frozen dataclass mutation rejected.
- [ ] `mypy src/modex_agent` passes.
- [ ] All existing tests pass unchanged.

---

## Rename concrete `TurnRunner` to `ReActTurnRunner` (wide refactor)

**What to build:** The existing concrete `TurnRunner` class is renamed `ReActTurnRunner` and made to inherit the `TurnRunner` ABC from ticket 1. `ExternalTurnRunner` is made to inherit the same ABC unchanged. All imports across the framework and reference bot are updated. The name `TurnRunner` now unambiguously refers to the ABC; concrete runners are named after their strategy (`ReActTurnRunner`, `ExternalTurnRunner`). This is a mechanical rename whose blast radius fans across framework and business-layer imports -- it is done as a single ticket because the change is contained to Python symbols (no string values, no pool.yml changes) and a temporary backward-compat alias is unnecessary at this scale.

**Blocked by:** "Add `TurnRunner` ABC + `ExecutionStrategy` ABC + Registry".

- [ ] Existing concrete `TurnRunner` class renamed `ReActTurnRunner`, inherits `TurnRunner` ABC.
- [ ] `ExternalTurnRunner` inherits `TurnRunner` ABC; behavior unchanged.
- [ ] All imports of the concrete `TurnRunner` updated to `ReActTurnRunner` (or to the ABC `TurnRunner` where the consumer wants the seam type).
- [ ] `isinstance(ReActTurnRunner(...), TurnRunner)` is `True`.
- [ ] `isinstance(ExternalTurnRunner(...), TurnRunner)` is `True`.
- [ ] `mypy src/modex_agent` passes.
- [ ] All existing tests pass unchanged.

---

## `ReactExecutionStrategy` assembles react pools

**What to build:** A react pool is assembled end-to-end by `ReactExecutionStrategy.assemble()`, not by inline logic in `pool_builder.create_pool`. The strategy's `assemble(ctx)` constructs the `BotModelProvider`, terminal manager, tool/MCP/skill managers, context manager, governance, runtime services, control channel, turn store, command processor, approval renderer, approval resumer, turn context builder, dream engine, and the `ReActTurnRunner` -- wiring every collaborator the runner needs. It returns a `StrategyAssembly` with react-only fields populated and external-only fields `None`. `validate_pool_spec(spec)` is a no-op for react (react accepts any valid `PoolSpec`). The existing `_build_llm_provider` / `_build_terminal_manager` / `_build_tools` / `_build_skill_manager` / `_wire_main_pipeline` helpers are imported from `pool_builder` for now (code moves in the contract ticket) -- `assemble()` calls them. `pool_builder.create_pool` react path calls `registry.resolve("react")`, then `strategy.assemble(ctx)`, then uses `assembly.agent` and `assembly.turn_runner` for the rest of `create_pool` (main-agent registration, pipeline construction). The existing `if execution_strategy == EXTERNAL` branch in `pool_builder` stays for now (external is handled in the parallel ticket). React pool behavior is byte-for-byte unchanged.

**Blocked by:** "Add `TurnRunner` ABC + `ExecutionStrategy` ABC + Registry", "Rename concrete `TurnRunner` to `ReActTurnRunner` (wide refactor)".

- [ ] `ReactExecutionStrategy` implemented; `name = "react"`; `supports_subagents = True`; `requires_main_agent_tools = True`.
- [ ] `assemble(ctx)` calls existing `_build_*` helpers (imported from `pool_builder`) and constructs a `ReActTurnRunner` with all collaborators wired.
- [ ] `assemble(ctx)` returns `StrategyAssembly` with react-only fields non-`None` (`provider`, `tool_manager`, `skill_manager`, `context_manager`, `dream_engine`, `command_processor`, `control_channel`) and external-only fields `None` (`backend`, `session_map_store`).
- [ ] `validate_pool_spec(spec)` is a no-op (react accepts any valid `PoolSpec`).
- [ ] `default_strategy_registry()` registers `ReactExecutionStrategy()`.
- [ ] `pool_builder.create_pool` react path calls `registry.resolve("react")` + `strategy.assemble(ctx)` and uses the returned assembly.
- [ ] All existing react pool unit tests pass unchanged.
- [ ] All existing react pool integration tests pass unchanged.
- [ ] External_coding tests unchanged (old `if` branch still active for external path).

---

## `ExternalExecutionStrategy` assembles external pools

**What to build:** An external pool is assembled end-to-end by `ExternalExecutionStrategy.assemble()`, not by inline logic in `pool_builder.create_pool` or `_external_wiring.py`. The strategy's `assemble(ctx)` constructs the `StreamingProviderBackend`, parser, `ExternalSessionMapStore`, env spec, and the `ExternalAgent` + `ExternalTurnRunner` -- and nothing else (no `BotModelProvider`, no `terminal_manager`, no `_build_tools`, no `_build_skill_manager`, no approval/governance/interceptor/hook/control_channel). It returns a `StrategyAssembly` with external-only fields populated and react-only fields `None`. Provider availability gating (`shutil.which(executable) is None` -> skip pool registration) moves into `assemble()` as a `ProviderUnavailableError` exception, caught by `pool_builder` to produce an empty `PoolInstance`. `validate_pool_spec(spec)` enforces: no subagents, `provider_kind` set (the validation branches currently in `pool_config/store.py` migrate here). `pool_builder.create_pool` external path calls `registry.resolve("external")` + `strategy.assemble(ctx)`. The existing `_external_wiring.py` helpers are imported by the strategy for now (code moves in the contract ticket). External_coding pool behavior is byte-for-byte unchanged.

**Blocked by:** "Add `TurnRunner` ABC + `ExecutionStrategy` ABC + Registry", "Rename concrete `TurnRunner` to `ReActTurnRunner` (wide refactor)".

- [ ] `ExternalExecutionStrategy` implemented; `name = "external"`; `supports_subagents = False`; `requires_main_agent_tools = False`.
- [ ] `assemble(ctx)` calls existing `_external_wiring` helpers (imported for now) and constructs an `ExternalAgent` + `ExternalTurnRunner`.
- [ ] `assemble(ctx)` returns `StrategyAssembly` with external-only fields non-`None` (`backend`, `session_map_store`) and react-only fields `None` (`provider`, `tool_manager`, `skill_manager`, `context_manager`, `dream_engine`, `command_processor`, `control_channel`).
- [ ] `assemble(ctx)` raises `ProviderUnavailableError` when `shutil.which(executable) is None`; `pool_builder` catches it and produces an empty `PoolInstance` with a warning log (behavior equivalent to today's skip-pool path).
- [ ] `validate_pool_spec(spec)` rejects pools with subagents or missing `provider_kind` (the validation logic currently in `pool_config/store.py`).
- [ ] `default_strategy_registry()` registers `ExternalExecutionStrategy()`.
- [ ] `pool_builder.create_pool` external path calls `registry.resolve("external")` + `strategy.assemble(ctx)`.
- [ ] All existing external unit tests pass unchanged.
- [ ] All existing external integration tests pass unchanged.
- [ ] React tests unchanged (react path handled by the parallel ticket).

---

## Slim `AgentPipeline` to 13 params, eliminate mirrors

**What to build:** `AgentPipeline.__init__` shrinks from 33 params to 13 and from 569 lines to ~200, accepting a `turn_runner: TurnRunner` (ABC) parameter instead of constructing runners internally. All strategy-specific collaborators (`context_manager`, `tool_manager`, `skill_manager`, `governance`, `hook_runner`, `interceptor_chain`, `turn_store`, `runtime_services`, `agent_descriptor`, `context_builder`, `sanitizer`, `context_manager_factory`, `max_iterations`, `user_interface`, `runtime_context_manager`) are removed from the constructor -- they now live inside the strategy-provided `TurnRunner`. The `ApprovalRenderer`, `ApprovalResumer`, and `TurnContextBuilder` construction in `__init__` is deleted (moved into `ReactExecutionStrategy.assemble()` in the prior ticket). The `if is_external` runner-selection branch is deleted. The five mutable property mirrors (`workspace_manager`, `pool_name`, `runtime_services`, `governance`, `emitter_factory`) are deleted -- `strategy.assemble()` configures the turn runner fully at assembly time, so post-construction wiring is structurally impossible. `emitter_factory` is pre-wrapped (`_WorkspaceEmitterFactory`) before `strategy.assemble()` is called and passed as a `PoolAssemblyContext` field; strategies pass it straight through to their `TurnRunner`. `update_emitter_factory` on `TurnRunner` is eliminated. `dream_engine` stays as a pipeline-level optional (its lifecycle is bound to `run()`/`stop()`). The pipeline's remaining responsibilities are: lifecycle, pre-lock dispatch, session queries, session cleanup, and delegation of the locked turn to `turn_runner.process_locked()`.

**Blocked by:** "`ReactExecutionStrategy` assembles react pools", "`ExternalExecutionStrategy` assembles external pools" (both strategies must produce `assembly.turn_runner` for the pipeline to consume).

- [ ] `AgentPipeline.__init__` accepts 13 params: `agent`, `turn_runner: TurnRunner`, `input_adapter`, `output_adapter`, `registry`, `safety`, `router`, `command_processor`, `deduplicator`, `busy_input_mode`, `control_channel`, `dream_engine`, `dream_interval`.
- [ ] No `context_manager` / `tool_manager` / `skill_manager` / `governance` / `hook_runner` / `interceptor_chain` / `turn_store` / `runtime_services` / `agent_descriptor` / `context_builder` / `sanitizer` / `context_manager_factory` / `max_iterations` / `user_interface` / `runtime_context_manager` parameter on `AgentPipeline.__init__`.
- [ ] No `ApprovalRenderer` / `ApprovalResumer` / `TurnContextBuilder` construction in `AgentPipeline.__init__`.
- [ ] No `if is_external` branch in `AgentPipeline.__init__`.
- [ ] Five mutable property mirrors deleted (`workspace_manager`, `pool_name`, `runtime_services`, `governance`, `emitter_factory`).
- [ ] `update_emitter_factory` removed from `TurnRunner` ABC and both concrete runners.
- [ ] `emitter_factory` pre-wrapped before `strategy.assemble()`; passed via `PoolAssemblyContext.emitter_factory`.
- [ ] No `pipeline.X = ...` post-construction assignment in `pool_builder` (verified by grep).
- [ ] `AgentPipeline` is ~200 lines (down from 569).
- [ ] All existing pipeline unit tests pass (updated for the new constructor signature).
- [ ] All existing react pool and external pool tests pass unchanged.
- [ ] All existing integration tests pass.

---

## Contract: move helpers into strategies, delete old assembly branches

**What to build:** The expand-contract sequence completes. The `_build_llm_provider` / `_build_terminal_manager` / `_build_tools` / `_build_skill_manager` / `_wire_main_pipeline` helpers move from `pool_builder` into `ReactExecutionStrategy` as private methods. The `_external_wiring.py` content moves into `ExternalExecutionStrategy` as private methods; the file is deleted. The `if execution_strategy == EXTERNAL` builder-dispatch branch in `multi_agent/factory.py` is deleted (strategies build their own agents directly). The three `external` validation branches in `pool_config/store.py` are deleted (migrated to `ExternalExecutionStrategy.validate_pool_spec` in the prior ticket). After this ticket, `pool_builder.create_pool` is ~150 lines organized as common assembly -> `strategy.assemble()` -> common post-assembly, with zero strategy-specific branching. The only remaining `execution_strategy ==` reference in the framework is `peer_normal.py` (runtime per-target routing -- intentionally retained). The temporary import-from-pool_builder pattern in `ReactExecutionStrategy` and `ExternalExecutionStrategy` (introduced in the prior tickets) is undone.

**Blocked by:** "Slim `AgentPipeline` to 13 params, eliminate mirrors".

- [ ] `_build_llm_provider` / `_build_terminal_manager` / `_build_tools` / `_build_skill_manager` / `_wire_main_pipeline` move into `ReactExecutionStrategy` as private methods; no longer importable from `pool_builder`.
- [ ] `_external_wiring.py` content moves into `ExternalExecutionStrategy` as private methods; the file is deleted.
- [ ] `if execution_strategy == EXTERNAL` branch deleted from `multi_agent/factory.py`.
- [ ] Three `external` validation branches deleted from `pool_config/store.py`.
- [ ] `pool_builder.create_pool` is ~150 lines.
- [ ] `grep -r "if.*execution_strategy.*EXTERNAL" src/modex_agent` returns only `peer_normal.py` (the intentionally-retained runtime routing branch).
- [ ] No temporary import-from-pool_builder pattern remains in either strategy.
- [ ] All existing tests pass unchanged.

---

## Rename `ExecutionStrategy` enum to `ExecutionStrategyKind` (wide refactor)

**What to build:** The `ExecutionStrategy` enum in `core/constants.py` is renamed `ExecutionStrategyKind`. The name `ExecutionStrategy` now refers exclusively to the ABC. Pool.yml string values (`react`, `external`) are unchanged -- this is a Python-symbol-only rename. All imports across the framework and reference bot are updated. This is sequenced last among the structural changes because the prior ticket's `if execution_strategy ==` branch deletions still reference the enum; renaming before those deletions would touch the branches twice. After this rename, `ExecutionStrategy` unambiguously means the ABC and `ExecutionStrategyKind` unambiguously means the closed string set used for pool.yml lookup and `registry.resolve(name)` dispatch.

**Blocked by:** "Contract: move helpers into strategies, delete old assembly branches".

- [ ] `ExecutionStrategy` enum in `core/constants.py` renamed `ExecutionStrategyKind`.
- [ ] All imports updated across framework and reference bot.
- [ ] Pool.yml string values (`react`, `external`) unchanged.
- [ ] No reference to the old enum name `ExecutionStrategy` (as an enum) remains; `ExecutionStrategy` now refers exclusively to the ABC.
- [ ] `mypy src/modex_agent` passes.
- [ ] All existing tests pass unchanged.

---

## Architecture guard test

**What to build:** A new architecture guard test asserts the structural invariant that `pool_builder.create_pool` and `AgentPipeline.__init__` source code contain no `if execution_strategy ==` or `if is_external` patterns. The test also asserts that `peer_normal.py` is the only file in `src/modex_agent/` containing `execution_strategy ==` (the intentionally-retained runtime routing branch). This is a structural invariant the existing behavior tests cannot detect -- behavior is preserved even if a branch is re-introduced (it would just be dead code), so the guard catches the regression at the source level. Prior art: `tests/architecture/test_pipeline_modules_no_backref.py` asserts no back-reference from deep modules to `AgentPipeline`; same pattern.

**Blocked by:** "Rename `ExecutionStrategy` enum to `ExecutionStrategyKind` (wide refactor)".

- [ ] New test file `tests/architecture/test_no_execution_strategy_branches.py` exists.
- [ ] Test asserts `pool_builder.create_pool` source contains no `if execution_strategy ==` or `if is_external` pattern (regex on source text).
- [ ] Test asserts `AgentPipeline.__init__` source contains no `if execution_strategy ==` or `if is_external` pattern.
- [ ] Test asserts `peer_normal.py` is the only file in `src/modex_agent/` matching `execution_strategy ==`.
- [ ] Test passes against the current codebase.
- [ ] All existing tests pass unchanged.

---

## Dependency graph

```
1 (ABC + Registry)
|
v
2 (rename TurnRunner)
|---> 3 (ReactExecutionStrategy)        -+
|---> 4 (ExternalStrategy)         |  (3 || 4 parallel)
                                          v
                       5 (slim AgentPipeline)
                       |
                       v
                       6 (contract: move + delete)
                       |
                       v
                       7 (rename enum)
                       |
                       v
                       8 (arch guard test)
```

## Shipping batches

- **Batch A**: ticket 1 (pure additions, zero behavior change). Ship first.
- **Batch B**: tickets 2, 3, 4 (rename + both strategies via ABC, pipeline and pool_builder still old shape). Ship together; 3 and 4 may parallelize.
- **Batch C**: ticket 5 (pipeline slimmed, mirrors gone). Ship after B stabilizes.
- **Batch D**: tickets 6, 7, 8 (contract cleanup, enum rename, arch guard). Ship last.

Each batch is independently revertible. Batch A ships alone; B ships without C (pipeline stays fat but strategies are abstracted); C ships without D (old branches dead but not deleted).

## Worked-example frontier

- After ticket 1: frontier = {2}.
- After ticket 2: frontier = {3, 4} (parallel).
- After 3 AND 4: frontier = {5}.
- After 5: frontier = {6}.
- After 6: frontier = {7}.
- After 7: frontier = {8}.
- After 8: done.
