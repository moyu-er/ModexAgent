# Execution strategy refactor — spec

ADR: [ADR-0025](../../adr/0025-execution-strategy-abstraction-and-pipeline-slimming.md)

## Problem

`pool_builder.create_pool` (440 lines) and `AgentPipeline.__init__` (33
params, 80+ line body) unconditionally assemble ReAct-only resources
(`BotModelProvider`, `terminal_manager`, full `_build_tools`,
`_build_skill_manager`, `ApprovalRenderer`, `ApprovalResumer`,
`TurnContextBuilder`, governance, interceptor, hook, control_channel) and
then short-circuit with `if execution_strategy == EXTERNAL_CODING` to swap
in external-coding deps. Four such branches exist today. Adding a third
pool shape means editing all four sites plus `pool_builder` and
`AgentPipeline`.

External_coding pools pay for assembly of resources they never use, and
require a `model.yml` they do not need (else they get a
`_placeholder_model_config` stub).

## Goal

Pool shape (react vs external_coding vs future) is decided by an explicit
`ExecutionStrategy` ABC, not by scattered `if` branches. `pool_builder` and
`AgentPipeline` contain zero strategy-specific branching. Adding a new
strategy = one new file + one registry registration.

## Scope

### In scope

- `ExecutionStrategy` ABC + `ExecutionStrategyRegistry` (framework).
- `TurnRunner` ABC (framework, one method).
- `PoolAssemblyContext` + `StrategyAssembly` (frozen dataclasses).
- `ReactExecutionStrategy` + `ExternalCodingExecutionStrategy`.
- `AgentPipeline` slimmed (33 → 13 params, 5 mirrors → 0, ~200 lines).
- `pool_builder.create_pool` restructured (common + strategy + post, ~150
  lines).
- Rename `ExecutionStrategy` enum → `ExecutionStrategyKind`.
- Rename concrete `TurnRunner` → `ReActTurnRunner`.
- Eliminate four `if execution_strategy == EXTERNAL_CODING` assembly branches
  (pipeline runner selection, pool_config validation, factory builder
  dispatch). Keep `peer_normal.py` reply-contract branch (runtime routing,
  not assembly).

### Out of scope

- Turn-stage abstraction (rejected — see ADR-0025 Non-goals).
- External pool bypassing `AgentPool` (rejected — E1).
- Pushing `dream_engine` into strategy (deferred — F1).
- OpenCode subagent (task tool) child-session support (independent feature,
  tracked separately).
- Microservice split along strategy boundaries (future, this ADR prepares
  the seam).

## Design

See ADR-0025 for the full decision text. Key shapes:

### ExecutionStrategy ABC

```python
# src/modex_agent/multi_agent/execution_strategy.py

class ExecutionStrategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...
    @property
    def supports_subagents(self) -> bool: return True
    @property
    def requires_main_agent_tools(self) -> bool: return True
    @abstractmethod
    async def assemble(self, ctx: PoolAssemblyContext) -> StrategyAssembly: ...
    @abstractmethod
    def validate_pool_spec(self, spec: PoolSpec) -> None: ...
```

### TurnRunner ABC

```python
# src/modex_agent/pipeline/turn_runner_abc.py

class TurnRunner(ABC):
    @abstractmethod
    async def process_locked(
        self,
        input_msg: InputMessage,
        session_id: str,
        route_result: RouteResult | None = None,
        *,
        session: SessionInfo,
    ) -> AgentResult | None: ...
```

### PoolAssemblyContext (frozen @dataclass, ~30 fields)

Input to `assemble()`. Carries common-assembly resources. Strategies must
not mutate. See ADR-0025 D2 for the field list.

### StrategyAssembly (frozen @dataclass)

Output of `assemble()`. Carries:
- `agent: Agent[Any]` (required)
- `turn_runner: TurnRunner` (required)
- `notification_service`, `communication_service`, `target_store` (common,
  required)
- `provider`, `tool_manager`, `skill_manager`, `mcp_manager`,
  `terminal_manager`, `context_manager`, `dream_engine`, `dream_interval`,
  `command_processor`, `control_channel` (react-only, `None` for
  external_coding)
- `backend`, `session_map_store` (external-only, `None` for react)
- `extra_cleanup: tuple[Callable[[], Awaitable[None]], ...] = ()`

### AgentPipeline (slimmed)

13 constructor params (see ADR-0025 D4). Responsibilities: lifecycle,
pre-lock dispatch, session queries, session cleanup, turn_runner delegation.
No strategy-specific collaborators, no mirrors.

### pool_builder.create_pool (restructured)

Three phases: common assembly → strategy.assemble() → common post-assembly.
See ADR-0025 D5.

## File impact

### New files

| File | Purpose |
|---|---|
| `src/modex_agent/multi_agent/execution_strategy.py` | `ExecutionStrategy` ABC + `ExecutionStrategyRegistry` + `PoolAssemblyContext` + `StrategyAssembly` + `default_strategy_registry()` |
| `src/modex_agent/pipeline/turn_runner_abc.py` | `TurnRunner` ABC (1 abstract method + 3 lifecycle + 2 wiring + 12 read-only properties — see ADR-0025 D3 deviations) |
| `src/modex_agent/agents/react/strategy.py` | `ReactExecutionStrategy` |
| `src/modex_agent/agents/external_coding/strategy.py` | `ExternalCodingExecutionStrategy` |
| `tests/unit/multi_agent/test_execution_strategy_registry.py` | Registry tests |
| `tests/unit/agents/react/test_strategy.py` | React strategy assemble tests |
| `tests/unit/agents/external_coding/test_strategy.py` | External strategy assemble tests |
| `tests/architecture/test_no_execution_strategy_branches.py` | Architecture guard: no `if execution_strategy ==` in pool_builder.create_pool or AgentPipeline.__init__ |

### Modified files

| File | Change |
|---|---|
| `src/modex_agent/pipeline/pipeline.py` | Slim `AgentPipeline.__init__` to 13 params, delete 5 mirrors, delete `if is_external` branch |
| `src/modex_agent/pipeline/turn_runner.py` | Rename `TurnRunner` → `ReActTurnRunner`, inherit ABC |
| `src/modex_agent/agents/external_coding/turn_runner.py` | Inherit `TurnRunner` ABC |
| `examples/bot_project/bot/service/pool_builder.py` | Restructure `create_pool` to common + strategy + post; move `_build_llm_provider`/`_build_tools`/`_build_skill_manager`/`_wire_main_pipeline` into `ReactExecutionStrategy`; move `_build_external_coding_deps` content into `ExternalCodingExecutionStrategy` |
| `examples/bot_project/bot/service/_external_coding_wiring.py` | Delete superseded functions (or shrink to thin re-exports during Stage 1–3) |
| `src/modex_agent/multi_agent/factory.py` | `_get_builder` dispatch **Retained** (runtime agent-construction dispatch, not assembly branching — see ADR-0025 D5 deviations) |
| `src/modex_agent/multi_agent/pool_config/store.py` | `external_coding` validation branches **Retained** at store level as defense-in-depth (restored after code review; WebUI write-time tests depend on them) |
| `src/modex_agent/core/constants.py` | Rename `ExecutionStrategy` enum → `ExecutionStrategyKind` |
| `examples/bot_project/bot/service/core.py` | Register strategies via `default_strategy_registry()` (or override) in `BotService.initialize()` |

### Test impact

- All existing react pool tests (unit + integration) must pass unchanged
  through Stages 0–4 (behaviour-equivalent refactor).
- All existing external_coding tests must pass unchanged through Stages 0–4.
- New strategy unit tests: each strategy's `assemble()` produces a
  well-typed `StrategyAssembly` with the right `None`/non-`None` fields.
- New architecture guard test: `pool_builder.create_pool` and
  `AgentPipeline.__init__` contain no `if execution_strategy ==` or
  `if is_external` branches (Stage 4).

## Open questions (to resolve during implementation)

1. **`PoolAssemblyContext` field set.** The ~30 fields listed in ADR-0025 D2
   are derived from the current `create_pool` signature. During Stage 1 we
   will encounter whether some fields are read by only one strategy (in
   which case they arguably belong as strategy-local config, not common
   context). Defer to implementation review.

2. **`emitter_factory` pre-wrapping.** The plan is to wrap
   `_WorkspaceEmitterFactory` *before* `assemble()` so `emitter_factory`
   flows in via `PoolAssemblyContext`. Need to confirm `_wire_main_pipeline`
   line 1040–1050 has no other dependencies on assembly-time-only values.

3. **`AgentPool` subagent fields for external_coding.** During Stage 2 we
   will confirm `_template_registry` and `_materialize_deps` are safe to
   leave empty (not read by the external_coding path). If read, add
   `strategy.supports_subagents` guards at the read sites.

4. **`peer_normal.py:54` retention.** Confirmed in-scope to keep (runtime
   routing, not assembly). Re-verify during Stage 4 cleanup that no other
   assembly-time `execution_strategy` reads exist.

## Verification (all stages passed)

| Stage | Verification | Status |
|---|---|---|
| 0 | New unit tests for ABC + registry pass; existing tests unchanged. | ✅ Passed |
| 1 | Full react pool test suite passes (unit + integration). External_coding tests unchanged (old path). | ✅ Passed |
| 2 | Full external_coding test suite passes. React tests unchanged. | ✅ Passed |
| 3 | `AgentPipeline` unit tests pass with slimmed constructor. Full regression. Mirror setter properties deleted. | ✅ Passed |
| 4 | Architecture guard test passes (no strategy branches in pool_builder/pipeline). Full regression. `_external_coding_wiring` deleted. | ✅ Passed |
| Cleanup 1 | Typed property/setter cleanup — zero `getattr`/`_xxx` on turn_runner/builder/approval. | ✅ Passed |
| Cleanup 2 | external_coding bloat elimination — boots without `model.yml`, 6 objects (down from ~15). | ✅ Passed |

Final regression: 4332 framework tests + 1066 bot tests + 3 external_coding
integration tests pass. mypy 380 baseline errors (zero new).
