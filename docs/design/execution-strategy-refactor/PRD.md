# Execution Strategy Abstraction and Pipeline Slimming

Status: implemented (2026-07-18) — see ADR-0025 Disposition for deviations

Related: ADR-0025 (`docs/adr/0025-execution-strategy-abstraction-and-pipeline-slimming.md`);
ADR-0022 (`docs/adr/0022-external-coding-agent-integration.md` — external_coding topology unchanged,
only assembly changes); ADR-0020 (`docs/adr/0020-pool-config-convergence-and-framework-promotion.md`
— `PoolSpec` / `MainAgentSpec` remain the disk models); ADR-0019 (`docs/adr/0019-cross-pool-peer-communication.md`
— `peer_normal` reply-contract branch retained);
`CONTEXT.md` → "Execution Strategy", "Pool Assembly", "Turn Runner", "Strategy Assembly",
"Pool Instance", "Assembly", "Input Pipeline", "ReAct Agent".

## Problem Statement

A framework developer who wants to add a new pool shape — a RAG-only pool, a
planning-graph pool, or a new external coding CLI — cannot do so without
editing four scattered `if execution_strategy == EXTERNAL_CODING` branches
across `pool_builder.create_pool`, `AgentPipeline.__init__`, the agent
factory, and the pool config validator. The framework has no explicit
extension point for "pool shape"; instead, shape-specific assembly logic is
interleaved with shared assembly logic, and adding a third shape means
threading new branches through all four sites plus the pipeline constructor.

A bot maintainer feels this as bloat: `pool_builder.create_pool` is 440
lines and unconditionally assembles ReAct-only resources
(`BotModelProvider`, `terminal_manager`, full `_build_tools`,
`_build_skill_manager`, `_wire_main_pipeline`) even for external_coding
pools that need none of them. An external_coding pool with no `model.yml`
configured silently receives a `_placeholder_model_config` stub — a
provider the pool will never call, assembled only because the shared
assembly path does not know that external_coding pools bypass the
framework's LLM provider entirely.

`AgentPipeline` is 569 lines with a 33-parameter constructor and five
mutable property mirrors (`workspace_manager`, `pool_name`,
`runtime_services`, `governance`, `emitter_factory`). The mirrors exist
solely because `pool_builder` constructs the pipeline first and wires
collaborators afterwards — a post-construction wiring pattern that
propagates every new collaborator into a new mirror. The pipeline
unconditionally constructs `ApprovalRenderer`, `ApprovalResumer`, and
`TurnContextBuilder` — ReAct-only components — then replaces the turn
runner with `ExternalTurnRunner` via an `if is_external` branch for
external_coding pools, discarding the ReAct-only construction.

An external_coding pool, by design, needs none of: `LLMProvider`,
`ToolManager`, `SkillManager`, `ApprovalRenderer`, `ApprovalResumer`,
`ContextGovernance`, `InterceptorChain`, `HookRunner`,
`InMemoryControlChannel`, `TurnContextBuilder`, `TurnStateStore`,
`AgentRuntimeServices`, `DreamEngine`. Today it pays for the assembly of
all of them.

## Solution

Introduce an explicit `ExecutionStrategy` abstraction: each pool shape
(ReAct graph loop, external CLI harness, future shapes) is a stateless
strategy object that owns its own assembly recipe. `pool_builder` and
`AgentPipeline` contain zero strategy-specific branching — they perform
common assembly and delegate strategy-specific assembly to the strategy
object. Adding a new pool shape is one new strategy subclass plus one
registry registration; no existing code changes.

The strategy is called once during pool assembly, receives a
`PoolAssemblyContext` (common-assembly resources), and returns a
`StrategyAssembly` (fully-configured runtime components including the
`TurnRunner`). The strategy is then never touched again at runtime — all
runtime state lives in the assembly's `TurnRunner`.

`AgentPipeline` shrinks to its actual responsibilities: lifecycle,
pre-lock dispatch (route → dedup → busy mode → session lock), session
queries, session cleanup, and delegation of the locked turn to a
`TurnRunner`. It no longer owns strategy-specific collaborators, no
longer constructs approval/governance/interceptor infrastructure, and no
longer has post-construction mirror properties — the strategy configures
the turn runner fully at assembly time.

A `TurnRunner` ABC (one method, `process_locked`) becomes the seam
between the pipeline and concrete turn runners. `ReActTurnRunner` (renamed
from `TurnRunner`) and `ExternalTurnRunner` both inherit it. The pipeline
holds a `TurnRunner` reference, never a concrete subclass.

From the developer's perspective: to add a new pool shape, they implement
`ExecutionStrategy`, register it, and write a `TurnRunner` subclass. They
do not touch `pool_builder`, `AgentPipeline`, or any existing strategy.

## User Stories

### Framework developers (consumers adding a new pool shape)

1. As a framework developer, I want to add a new pool shape (e.g.
   RAG-only, planning-graph) by implementing one `ExecutionStrategy`
   subclass and registering it, so that I do not need to edit
   `pool_builder`, `AgentPipeline`, or any existing strategy.

2. As a framework developer, I want the `ExecutionStrategy` ABC to
   declare capability flags (`supports_subagents`,
   `requires_main_agent_tools`), so that the framework's common assembly
   can gate shared resources (subagent template registry, communication
   tool registration) without branching on strategy identity.

3. As a framework developer, I want `ExecutionStrategy.assemble()` to
   receive a `PoolAssemblyContext` carrying all common-assembly resources,
   so that my strategy can read shared infrastructure (broker, inbox,
   bus, output adapter) without reaching into `pool_builder` internals.

4. As a framework developer, I want `ExecutionStrategy.assemble()` to
   return a `StrategyAssembly` carrying my strategy's runtime components,
   so that `pool_builder` can wire common post-assembly (main-agent
   registration, pipeline construction) without knowing my strategy's
   internals.

5. As a framework developer, I want `ExecutionStrategy.validate_pool_spec()`
   to be called at startup with my strategy's validation rules, so that
   invalid pool configurations fail fast before any resource is assembled.

6. As a framework developer, I want the `ExecutionStrategyRegistry` to be
   process-scoped and write-once-read-many, so that strategies are
   registered at boot and resolved by name at pool-assembly time without
   runtime mutation.

7. As a framework developer, I want a `default_strategy_registry()`
   factory that pre-registers the shipped strategies (`react`,
   `external_coding`), so that a framework-only consumer gets them without
   business-layer wiring.

8. As a framework developer, I want to override the registry in my
   business layer, so that I can disable a shipped strategy or add a
   custom one before any pool is created.

### Bot maintainers (maintaining the reference bot)

9. As a bot maintainer, I want `pool_builder.create_pool` to be ~150 lines
   organized as common assembly → strategy delegation → common
   post-assembly, so that I can locate shared logic vs strategy-specific
   logic without reading 440 interleaved lines.

10. As a bot maintainer, I want `AgentPipeline` to be ~200 lines with a
    13-parameter constructor, so that I can understand the pipeline's
    responsibilities without scrolling past approval/governance/interceptor
    construction that the pipeline does not own.

11. As a bot maintainer, I want external_coding pools to no longer
    assemble `BotModelProvider`, `terminal_manager`, `_build_tools`,
    `_build_skill_manager`, or `_wire_main_pipeline`, so that an
    external_coding pool without `model.yml` boots without a placeholder
    provider.

12. As a bot maintainer, I want the five mutable property mirrors on
    `AgentPipeline` (`workspace_manager`, `pool_name`, `runtime_services`,
    `governance`, `emitter_factory`) eliminated, so that adding a new
    collaborator does not require a new mirror property.

13. As a bot maintainer, I want `emitter_factory` to be wrapped
    (`_WorkspaceEmitterFactory`) before `strategy.assemble()` is called,
    so that the strategy receives a fully-wrapped emitter factory and the
    pipeline never post-wires it.

14. As a bot maintainer, I want the `if is_external` runner-selection
    branch in `AgentPipeline.__init__` removed, so that the pipeline
    accepts a `TurnRunner` (ABC) parameter and does not branch on strategy
    identity.

15. As a bot maintainer, I want `pool_builder.create_pool` to call
    `strategy.assemble()` for both react and external_coding paths
    uniformly, so that there is one assembly path, not two interleaved
    ones.

16. As a bot maintainer, I want the existing
    `_external_coding_wiring.py` helpers folded into
    `ExternalCodingExecutionStrategy`, so that external-coding assembly
    logic lives with its strategy, not in a separate wiring file.

17. As a bot maintainer, I want the existing `_build_llm_provider`,
    `_build_terminal_manager`, `_build_tools`, `_build_skill_manager`,
    `_wire_main_pipeline` helpers folded into `ReactExecutionStrategy`,
    so that react assembly logic lives with its strategy.

### Agent developers (writing turn runners)

18. As an agent developer, I want a `TurnRunner` ABC with one method
    (`process_locked`), so that my custom turn runner integrates with
    `AgentPipeline` by implementing one contract.

19. As an agent developer, I want the `TurnRunner` ABC to NOT expose
    `update_emitter_factory` or other mirror methods, so that the ABC
    stays minimal and post-construction wiring is structurally impossible.

20. As an agent developer, I want the existing `TurnRunner` class renamed
    to `ReActTurnRunner`, so that the name `TurnRunner` unambiguously
    refers to the ABC and concrete runners are named after their strategy.

21. As an agent developer, I want `ExternalTurnRunner` to inherit the
    `TurnRunner` ABC unchanged, so that its existing simplifications (no
    history, no system prompt, no governance, no approval) are preserved.

### Contributors (reading the codebase)

22. As a contributor, I want the `ExecutionStrategy` ABC and
    `ExecutionStrategyRegistry` in the framework's `multi_agent/` package,
    so that I can find the pool-shape extension point without reading the
    business layer.

23. As a contributor, I want the `TurnRunner` ABC in the framework's
    `pipeline/` package, so that the pipeline→runner seam is co-located
    with the pipeline and the `multi_agent/ → pipeline/` dependency
    direction is preserved.

24. As a contributor, I want `PoolAssemblyContext` and `StrategyAssembly`
    to be frozen dataclasses (not Pydantic `BaseModel`), so that the
    codebase consistently follows rule 12's distinction between
    config/value objects (BaseModel) and runtime-object containers
    (regular classes).

25. As a contributor, I want an architecture guard test asserting that
    `pool_builder.create_pool` and `AgentPipeline.__init__` contain no
    `if execution_strategy ==` or `if is_external` branches, so that
    future regressions are caught at CI time.

26. As a contributor, I want the `ExecutionStrategy` enum renamed to
    `ExecutionStrategyKind`, so that the name `ExecutionStrategy`
    unambiguously refers to the ABC and the enum is clearly a lookup key,
    not a strategy object.

### Runtime behavior (preserved invariants)

27. As a react pool user, I want react pool behavior (turn execution,
    approval suspend/resume, streaming, tool calls, dream consolidation,
    session memory) to be byte-for-byte unchanged after the refactor, so
    that my existing react pools work identically.

28. As an external_coding pool user, I want external_coding pool behavior
    (OpenCode SSE/subprocess fallback, session resume, `modexctl send`
    peer communication, WebUI streaming projection) to be byte-for-byte
    unchanged after the refactor, so that my existing external_coding
    pools work identically.

29. As a cross-pool user, I want peer-pool communication (react →
    external_coding, external_coding → react) to be unchanged, so that
    `send_to_agent` and `modexctl send` continue to interoperate.

30. As a WebUI user, I want `is_session_active`, `has_active_sessions`,
    and `get_active_turn_uuid` queries to continue working for both react
    and external_coding pools, so that the WebUI's active-turn indicator
    is unaffected.

31. As an IM user, I want `/stop` turn cancellation to continue working
    for both react and external_coding pools, so that busy-input
    interruption is unaffected.

32. As a workspace user, I want per-workspace pool data resolution
    (memory, runtime stores, experience) to continue working for both
    react and external_coding pools, so that workspace switching is
    unaffected.

### Non-functional

33. As a maintainer, I want the refactor to ship in five internally
    verifiable stages (each ending in a green test suite), so that I can
    bisect regressions to a single stage and revert independently.

34. As a maintainer, I want each stage to be a pure refactor with no
    behavior change relative to the previous stage, so that I can verify
    equivalence at every step rather than only at the end.

35. As a maintainer, I want the `peer_normal.py` reply-contract branch
    (which decides `modexctl send` vs `send_to_agent` based on the
    target's execution strategy) to be retained, so that runtime
    per-target routing is not confused with assembly-time branching.

## Implementation Decisions

### Architectural decisions (from ADR-0025)

- **`ExecutionStrategy` ABC** (stateless, one call at assembly time):
  `name`, `supports_subagents`, `requires_main_agent_tools`, `assemble(ctx)
  -> StrategyAssembly`, `validate_pool_spec(spec)`. Replaces the
  `ExecutionStrategy` enum (renamed `ExecutionStrategyKind`, a pure
  lookup key for `registry.resolve(name)`).

- **`TurnRunner` ABC** (1 abstract method `process_locked` + 3 lifecycle
  methods with no-op defaults + 2 post-construction wiring methods with
  no-op defaults + 12 read-only properties with `None` defaults): the seam
  between `AgentPipeline` and concrete turn runners. The ABC surface is
  larger than the original "one method" spec (see ADR-0025 D3 deviations)
  because the pipeline's pre-lock dispatch needs `agent_descriptor`/
  `skill_manager`/`turn_store` queries and `load_pending_approval`/
  `cleanup_session` lifecycle methods. Existing `TurnRunner` renamed
  `ReActTurnRunner`; `ExternalTurnRunner` inherits ABC unchanged. ABC lives
  in `pipeline/` to preserve the `multi_agent/ → pipeline/` non-dependence.

- **`PoolAssemblyContext`** (frozen `@dataclass`, ~30 fields): input to
  `assemble()`. Carries common-assembly resources. Strategies must not
  mutate. Runtime-object container — frozen `@dataclass` per rule 12's
  runtime-object exemption, NOT Pydantic `BaseModel`.

- **`StrategyAssembly`** (frozen `@dataclass`): output of `assemble()`.
  Carries `Agent`, `TurnRunner`, common services, react-only collaborators
  (`None` for external_coding), external-only collaborators (`None` for
  react), `extra_cleanup` hooks. The `None` defaults are typed; consumers
  gate on capability flags (`strategy.requires_main_agent_tools`) rather
  than `is None` checks.

- **`AgentPipeline` slimmed** (33 → 13 constructor params, 5 mirror setter
  properties deleted, 347 lines): accepts `turn_runner: TurnRunner` (ABC)
  parameter. Removed: `context_manager`, `tool_manager`, `skill_manager`,
  `governance`, `hook_runner`, `interceptor_chain`, `turn_store`,
  `runtime_services`, `agent_descriptor`, `context_builder`, `sanitizer`,
  `context_manager_factory`, `max_iterations`, `user_interface`,
  `runtime_context_manager`. Removed: `ApprovalRenderer` /
  `ApprovalResumer` / `TurnContextBuilder` construction (moved into
  `ReactExecutionStrategy.assemble()`). Removed: `if is_external`
  runner-selection branch. 11 backward-compat read-only delegation
  properties remain (see ADR-0025 D4 deviations).

- **`pool_builder.create_pool` restructured** (440 → ~265 lines, zero
  strategy-specific branching): three
  phases — common assembly → `strategy.assemble(ctx)` → common
  post-assembly. `_build_llm_provider` / `_build_terminal_manager` /
  `_build_tools` / `_build_skill_manager` / `_wire_main_pipeline` move
  into `ReactExecutionStrategy`. `_external_coding_wiring.py` content
  moves into `ExternalCodingExecutionStrategy`.

- **`emitter_factory` pre-wrapping**: `_WorkspaceEmitterFactory` wrapping
  moves from `_wire_main_pipeline` (post-construction) to before
  `strategy.assemble()`. The wrapped factory is a `PoolAssemblyContext`
  field; strategies pass it straight through to their `TurnRunner`.
  `update_emitter_factory` on `TurnRunner` is eliminated.

- **`dream_engine` stays as pipeline-level optional**: its lifecycle is
  bound to `run()`/`stop()`. External_coding passes `None`; the
  `if self.dream_engine is not None` guard keeps the path dead.

- **`peer_normal.py:54` retained**: the reply-contract branch (which
  decides `modexctl send` vs `send_to_agent` based on the target's
  execution strategy) is runtime per-target routing, not assembly-time
  branching. It stays.

- **`AgentPool` still serves external_coding**: `InboxPoller` integration,
  session lock, TTL/LRU eviction are common needs. Subagent-related
  `AgentPool` fields (`_template_registry`, `_materialize_deps`) are empty
  for external_coding.

### Migration discipline (from ADR-0025 D6)

Five stages, each a pure refactor ending in a green test suite:

1. **Stage 0** — Add `TurnRunner` ABC, rename `TurnRunner` →
   `ReActTurnRunner`, add `ExecutionStrategy` ABC + `ExecutionStrategyRegistry`
   + `PoolAssemblyContext` + `StrategyAssembly`. No `pool_builder` or
   `AgentPipeline` changes.
2. **Stage 1** — Add `ReactExecutionStrategy.assemble()` (calls existing
   `_build_*` helpers imported from `pool_builder`). `pool_builder` react
   path calls `strategy.assemble()`.
3. **Stage 2** — Add `ExternalCodingExecutionStrategy.assemble()`.
   `pool_builder` external path calls `strategy.assemble()`.
4. **Stage 3** — Slim `AgentPipeline.__init__` to 13 params, delete five
   mirror properties, pre-wrap `emitter_factory`.
5. **Stage 4** — Move `_build_*` helpers into strategies, delete
   `_external_coding_wiring.py`, remove residual `if execution_strategy ==`
   assembly branches, rename enum → `ExecutionStrategyKind`, add
   architecture guard test.

### Post-implementation cleanup (beyond original scope)

After the 8-ticket implementation, two cleanup passes eliminated residual
debt:

1. **Typed property/setter cleanup**: replaced all `getattr(turn_runner,
   "_builder")` / `builder._governance =` / `approval._user_interface =`
   private-field access with typed property setters on `TurnContextBuilder`
   (4 properties), `ApprovalRenderer` (2 properties), `TurnRunner` ABC
   (`set_pool_context` / `set_emitter_factory` methods + `approval_renderer`
   property), and `AgentPool` (4 properties). Added `get_session_state()`
   typed accessor on `ContextManager` for test/diagnostic use. Zero
   `getattr` / `type: ignore[attr-defined]` on turn_runner/builder/approval
   in production code.

2. **external_coding react-only bloat elimination**: `ExternalCodingAwareFactory
   .create_agent` fully overridden — builds only 6 objects (ExternalCodingAgent
   + broker I/O + emitter_factory + registry + ExternalTurnRunner + AgentPipeline),
   down from ~15. `ExternalCodingExecutionStrategy.assemble()` builds only
   `external_coding_deps`. `pool_builder.create_pool` external path skips
   `SendToAgentTool` registration and `_wire_main_pipeline`. **External_coding
   pools now boot without `model.yml` configured** — no BotModelProvider is
   built.

### Interfaces modified

- `ExecutionStrategy` ABC (new): `name`, `supports_subagents`,
  `requires_main_agent_tools`, `assemble(ctx) -> StrategyAssembly`,
  `validate_pool_spec(spec)`.
- `TurnRunner` ABC (new): `process_locked(input_msg, session_id,
  route_result=None, *, session) -> AgentResult | None`.
- `ExecutionStrategyRegistry` (new): `register(strategy)`,
  `resolve(name) -> ExecutionStrategy`.
- `AgentPipeline.__init__` (modified): 13 params, accepts `turn_runner:
  TurnRunner` (ABC).
- `pool_builder.create_pool` (modified): three-phase structure, calls
  `strategy.assemble()`.
- `ExecutionStrategy` enum (renamed): `ExecutionStrategyKind`.
- `TurnRunner` concrete class (renamed): `ReActTurnRunner`.

### Decisions rejected (from ADR-0025)

- **Turn-stage abstraction** (à la `InputStage`): rejected — turn
  execution bloat is already solved by `ExternalTurnRunner` duck typing;
  stage-ifying would re-touch approval `GraphInterrupt` state machine with
  no offsetting benefit.
- **Protocol instead of ABC**: rejected per rule 7.
- **External pool bypassing `AgentPool`**: rejected — `AgentPool` provides
  `InboxPoller` integration that external_coding also needs.
- **Pushing `dream_engine` into strategy**: deferred — lifecycle is bound
  to `run()`/`stop()`; splitting requires cross-object coordination with
  no benefit.
- **Pydantic `BaseModel` for `PoolAssemblyContext` / `StrategyAssembly`**:
  rejected — they are runtime-object containers (live objects with
  connections), not serializable values; `arbitrary_types_allowed=True`
  would be cargo cult. Rule 12's runtime-object exemption controls.
- **Discriminated union of `ReactAssembly | ExternalAssembly`**: rejected
  — would force every consumer into a type switch, re-introducing the
  branching this spec removes.

## Testing Decisions

### What makes a good test here

A good test for this refactor verifies **external behavior equivalence**
(turn output, pool assembly output, runtime query results), not
implementation details (which class holds which collaborator). The
refactor is a pure restructuring; no behavior should change. Therefore the
primary test surface is the **existing test suite, unmodified, green**.

### Primary seam: existing test suite (zero modifications)

The highest possible seam — the existing tests already encode the
behavior contract of react pools, external_coding pools, and the pipeline.
If the refactor preserves behavior, they pass unchanged. If it does not,
they fail and pinpoint the regression.

- `tests/unit/pipeline/test_turn_runner.py` — react turn execution
  (approval suspend/resume, governance, hooks, interceptors, context
  assembly).
- `tests/unit/agents/external_coding/test_turn_runner.py` — external turn
  execution (minimal context, `current_input` forwarding, cancellation).
- `tests/unit/agents/external_coding/test_agent.py` —
  `ExternalCodingAgent.run()` (backend delegation, session map
  commit/error, stale-session retry).
- `tests/unit/agents/external_coding/test_builder_external_coding.py` —
  `ExternalCodingAgentBuilder.build_agent()` (collaborator wiring).
- `tests/unit/multi_agent/test_factory_external_coding.py` —
  `DefaultAgentFactory` external_coding dispatch.
- `tests/unit/bot_service/test_pool_builder_external_coding.py` —
  `pool_builder.create_pool` external_coding path (provider availability
  gating, deps assembly).
- `tests/integration/multi_agent/test_cross_pool_external_coding.py` —
  cross-pool end-to-end (react → external_coding, external_coding →
  react, `modexctl send` delivery, WebUI streaming projection).

Prior art: these tests are the existing regression surface. The refactor
must not touch them.

### Secondary seam: new architecture guard test (one new test file)

A new architecture guard test asserts the structural invariant that
`pool_builder.create_pool` and `AgentPipeline.__init__` source code
contain no `if execution_strategy ==` or `if is_external` patterns. This
is a structural invariant the existing behavior tests cannot detect —
behavior is preserved even if a branch is re-introduced (it would just be
dead code), so the guard test catches the regression at the source level.

The guard also asserts that `peer_normal.py` is the only file in the
framework containing `execution_strategy ==` (the intentionally-retained
runtime routing branch).

Prior art: `tests/architecture/` already contains guard tests
(`test_pipeline_modules_no_backref.py` asserts no back-reference from
deep modules to `AgentPipeline`). Same pattern.

### New strategy unit tests

Each strategy's `assemble()` gets a unit test verifying the
`StrategyAssembly` shape:
- `ReactExecutionStrategy.assemble()` produces non-`None` react-only
  fields (`provider`, `tool_manager`, `skill_manager`, `context_manager`,
  `dream_engine`, `command_processor`, `control_channel`) and `None`
  external-only fields (`backend`, `session_map_store`).
- `ExternalCodingExecutionStrategy.assemble()` produces non-`None`
  external-only fields and `None` react-only fields.
- `ExternalCodingExecutionStrategy.validate_pool_spec()` rejects pools
  with subagents or missing `provider_kind`.

Prior art: `tests/unit/agents/external_coding/test_builder_external_coding.py`
already tests builder output shape; the new tests follow the same pattern
at the strategy level.

### What is NOT tested

- Internal collaborator wiring inside a strategy (e.g. whether
  `ReactExecutionStrategy` constructs `ApprovalRenderer` before or after
  `TurnContextBuilder`) — implementation detail, not behavior.
- The exact field count of `PoolAssemblyContext` — evolves with common
  assembly, not a stable contract.
- The order of stages in the migration — a process concern, not a runtime
  invariant.

## Out of Scope

- **Turn-stage abstraction** (à la `InputStage`): the turn execution
  itself is not stage-ified. `ExternalTurnRunner` already solves the
  react/external turn-execution dichotomy.
- **External pool bypassing `AgentPool`**: external_coding pools still use
  `AgentPool` for `InboxPoller` integration and session lifecycle.
- **Pushing `dream_engine` into strategy**: `dream_engine` stays as a
  pipeline-level optional; its lifecycle is bound to `run()`/`stop()`.
- **OpenCode subagent (task tool) child-session support**: independent
  feature, tracked separately. This refactor prepares the seam (strategy
  owns its assembly) but does not implement child-session tracking.
- **Microservice split along strategy boundaries**: future possibility;
  this refactor prepares the seam but does not split processes.
- **`peer_normal.py` reply-contract branch removal**: retained — it is
  runtime per-target routing, not assembly-time branching.
- **Changing pool.yml values**: `react` and `external_coding` string
  values in pool.yml are unchanged; only the Python symbol
  (`ExecutionStrategy` enum → `ExecutionStrategyKind`) moves.

## Further Notes

- **ADR-0025** (`docs/adr/0025-execution-strategy-abstraction-and-pipeline-slimming.md`)
  is the authoritative decision record. This PRD is the user-facing
  synthesis; the ADR holds the full decision text, rejected alternatives,
  and consequence analysis.
- **Implementation tickets** are in
  `docs/design/execution-strategy-refactor/tickets.md` (16 tickets across
  5 stages, with a dependency graph and 4 shipping batches).
- **Technical spec** is in
  `docs/design/execution-strategy-refactor/spec.md` (file impact map,
  open questions, verification matrix).
- **Glossary entries** added to `CONTEXT.md`: "Execution Strategy", "Pool
  Assembly", "Turn Runner", "Strategy Assembly" — see the Language
  section.
- **Rule 12 判例**: `PoolAssemblyContext` and `StrategyAssembly` as
  frozen `@dataclass` (not Pydantic `BaseModel`) establishes the判例 that
  runtime-object containers with cross-module visibility use frozen
  `@dataclass`. This is recorded in ADR-0025 D2 and should be referenced
  in future rule-12 edge cases.
- **Backward compatibility**: pool.yml values are unchanged. The
  `ExecutionStrategy` enum rename is a Python-symbol-only breaking change
  (imports update; string values do not). The `TurnRunner` rename is
  similarly Python-symbol-only. Both are contained within the framework
  package; business-layer code references strategies by string name, not
  by class.
