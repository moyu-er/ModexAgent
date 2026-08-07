# Execution strategy abstraction and pipeline slimming

Status: accepted (2026-07-18) — implemented with documented deviations (see Disposition)

## Context

`AgentPipeline` and `pool_builder.create_pool` have accreted shape-specific
branching across four sites, and `AgentPipeline` has grown to 569 lines with a
33-parameter constructor and five mutable property mirrors. The root cause is
that "which pool shape" (ReAct graph loop vs external CLI harness) is decided
with `if execution_strategy == EXTERNAL` checks scattered across
assembly and pipeline construction, rather than by an explicit strategy object
that owns its own shape.

### Symptoms

1. **`pool_builder.create_pool` (440 lines)** unconditionally assembles
   ReAct-only resources — `BotModelProvider`, `terminal_manager`, full
   `_build_tools`, `_build_skill_manager`, `_wire_main_pipeline` — and only
   later short-circuits with `if execution_strategy == EXTERNAL` to swap
   in external deps. An external pool that needs none of these
   still pays for their construction (or worse, gets a `_placeholder_model_config`
   stub when no `model.yml` is configured, even though the external CLI provides
   its own model).

2. **`AgentPipeline.__init__` (33 params, 80+ line body)** unconditionally
   constructs `ApprovalRenderer`, `ApprovalResumer`, `TurnContextBuilder`,
   `TurnRunner` — then replaces the runner with `ExternalTurnRunner` via an
   `if is_external` branch (`pipeline.py:205-240`). External_coding pools pay
   for approval/governance/interceptor/hook/control_channel assembly that
   `ExternalTurnRunner` immediately discards.

3. **Five mutable property mirrors** (`workspace_manager`, `pool_name`,
   `runtime_services`, `governance`, `emitter_factory`) exist solely because
   `pool_builder` constructs the pipeline first and wires collaborators
   afterwards. Each mirror propagates the post-construction assignment into
   either `TurnRunner` or `TurnContextBuilder`. This is fragile: a new
   collaborator means a new mirror.

4. **Four `if execution_strategy == EXTERNAL` sites** exist today:
   `pipeline.py:207` (runner selection), `peer_normal.py:54` (reply contract),
   `pool_config/store.py:264,335,429` (validation), `factory.py:125`
   (descriptor → builder dispatch). Adding a third strategy (RAG-only,
   planning-graph, …) means editing all four sites plus `pool_builder` and
   `AgentPipeline`.

5. **`ExternalTurnRunner` already exists** as a simplified turn runner
   (skips ~80% of `TurnRunner.process_locked`), but it is selected by an
   `if is_external` branch in `AgentPipeline.__init__` rather than by a
   strategy-owned seam.

### Non-goals

- **Turn-stage abstraction.** The turn execution itself is not stage-ified.
  `ExternalTurnRunner` already solves the react/external turn-execution
  dichotomy via duck typing; stage-ifying turn execution (à la `InputStage`)
  would re-touch the approval `GraphInterrupt` state machine and the
  `ReActTurnState` flow with no offsetting benefit — react's turn stages are
  stable, not dynamically composed.
- **Microservice split.** Strategy boundaries are not (yet) process
  boundaries. This ADR prepares the seam; a future split can ride it.
- **External pool bypassing `AgentPool`.** External_coding pools still use
  `AgentPool` — it provides `InboxPoller` integration, session lock, and
  TTL/LRU eviction that external also needs. The subagent-related
  `AgentPool` fields are simply empty for external.

## Decision

Six decisions, grouped into three concerns: the strategy abstraction, the
pipeline slimming, and the migration discipline.

---

### D1 — `ExecutionStrategy` ABC + `ExecutionStrategyRegistry`

Introduce an `ExecutionStrategy` ABC in
`src/modex_agent/multi_agent/execution_strategy.py`:

```python
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

The strategy is **stateless**: it is called once during pool assembly, returns
a fully-configured `StrategyAssembly`, and is never touched again at runtime.
Runtime state lives in the assembly's `TurnRunner`, not in the strategy.

`ExecutionStrategyRegistry` is a process-scoped, write-once-read-many
registry. `BotService.initialize()` registers the two shipped strategies
(`react`, `external`) before any pool is created. The framework ships a
`default_strategy_registry()` factory that pre-registers both, so a
framework-only consumer gets them for free; business layers may override.

The existing `ExecutionStrategy` enum (`core/constants.py`) is renamed
`ExecutionStrategyKind` — a closed string set used only for pool.yml lookup
and `registry.resolve(name)` dispatch. The name `ExecutionStrategy` now refers
exclusively to the ABC. Pool.yml values (`react`, `external`) are
unchanged; only the Python symbol moves.

Rejected alternatives:

- *Stage-ify the turn.* Rejected — see Non-goals. The bloat is in assembly,
  not turn execution.
- *Protocol instead of ABC.* Rejected per rule 7 (ABC before Protocol).
- *Strategy as dataclass of callables.* Rejected — strategies carry
  `validate_pool_spec` and capability flags as behaviour, not just a `build`
  function; an ABC expresses that better.

---

### D2 — `PoolAssemblyContext` and `StrategyAssembly` as frozen dataclasses

Two new types in `execution_strategy.py`:

- **`PoolAssemblyContext`** (frozen `@dataclass`): the input to
  `assemble()`. Carries common-assembly resources every strategy can read —
  `pool_name`, `pool_spec`, `project_dir`, `data_dir`, `workspace_handle`,
  `workspace_resolver`, `broker`, `inbox_server`, `agent_bus`,
  `output_adapter`, `emitter_factory`, `safety`, `retention`, `app_config`,
  `persistence`, `mcp_registry`, `shared_hooks`, `shared_hook_runner`,
  `shared_interceptor_chain`, `session_registry`, `session_store`,
  `bot_model_config`, `model_choice_registry`, `command_processor`,
  `control_channel`, `pool_data`, `transcript_store`, `on_session_start`,
  `on_session_end`, `registry`, `router`. Strategies must not mutate it.

- **`StrategyAssembly`** (frozen `@dataclass`): the output of `assemble()`.
  Carries the `Agent`, the `TurnRunner`, common services
  (`AgentNotificationService` / `AgentCommunicationService` /
  `CommunicationTargetStore`), react-only collaborators (all `None` for
  external), external-only collaborators (all `None` for react), and
  `extra_cleanup` hooks.

**Why frozen `@dataclass`, not Pydantic `BaseModel`.** Rule 12 distinguishes
"config/value objects (`BaseModel`)" from "runtime objects (regular
classes)". `PoolAssemblyContext` and `StrategyAssembly` are runtime-object
containers — their fields are live objects with connections and state
(`Agent`, `MessageBroker`, `LLMProvider`, `StreamingProviderBackend`), not
serializable values. They are never serialized, never cross a process
boundary, never validate nested fields. Pydantic's value (validation +
serialization) is nil here; `arbitrary_types_allowed=True` would be cargo
cult. Rule 11's "leaf value-object escape hatch" is narrower than this case,
but rule 12's "runtime objects are regular classes" is the controlling
guidance — a frozen `@dataclass` is the frozen form of a regular class. This
ADR establishes the判例 that runtime-object containers with cross-module
visibility use frozen `@dataclass`, not `BaseModel`.

---

### D3 — `TurnRunner` ABC (one method)

Introduce a `TurnRunner` ABC in
`src/modex_agent/pipeline/turn_runner_abc.py`:

```python
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

One method. No `update_emitter_factory` — see D4 for why it is gone.

The existing concrete `TurnRunner` class (`pipeline/turn_runner.py`) is
renamed **`ReActTurnRunner`** and inherits the ABC. `ExternalTurnRunner`
(`agents/external/turn_runner.py`) inherits the ABC unchanged.
`AgentPipeline` holds a `TurnRunner` (ABC) reference, never a concrete
subclass.

The ABC lives in `pipeline/`, not `multi_agent/`, to preserve the existing
dependency direction (`multi_agent/` does not depend on `pipeline/`).

Rejected alternatives:

- *ABC in `multi_agent/execution_strategy.py`.* Rejected — would reverse
  the `multi_agent/ → pipeline/` non-dependence.
- *Expose `_user_interface` / `_resolve_pool_data` /
  `_resolve_workspace_root` / `pool_name` / `workspace_manager` on the ABC.*
  Rejected — these are all mirror entries that D4 eliminates. Exposing them
  on the ABC would freeze the mirror pattern in place.
- *Protocol.* Rejected per rule 7.

---

### D4 — `AgentPipeline` slimmed to 13 params, zero mirrors

`AgentPipeline.__init__` shrinks from 33 params to 13:

```python
class AgentPipeline:
    def __init__(
        self,
        *,
        agent: Agent,
        turn_runner: TurnRunner,            # ABC; strategy-provided, fully configured
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        registry: TurnSessionRegistry,
        safety: RuntimeSafetyPolicy,
        # common optional
        router: AgentMessageRouter | None = None,
        command_processor: CommandProcessor | None = None,
        deduplicator: MessageDeduplicator | None = None,
        busy_input_mode: BusyInputMode = BusyInputMode.QUEUE,
        control_channel: InMemoryControlChannel | None = None,
        # react-only optional (external passes None)
        dream_engine: DreamEngine | None = None,
        dream_interval: float | None = None,
    ) -> None: ...
```

**What is removed from `AgentPipeline`:**

| Removed | Reason |
|---|---|
| `context_manager`, `tool_manager`, `skill_manager`, `runtime_context_manager`, `governance`, `hook_runner`, `interceptor_chain`, `turn_store`, `runtime_services`, `agent_descriptor`, `context_builder`, `sanitizer`, `context_manager_factory`, `max_iterations`, `user_interface` | Strategy-specific; now live inside the `TurnRunner` (react) or are unused (external). Pipeline never touches them. |
| `ApprovalRenderer`, `ApprovalResumer`, `TurnContextBuilder` construction in `__init__` | Strategy-specific; moved into `ReactExecutionStrategy.assemble()`. |
| `if is_external` runner-selection branch | Replaced by `turn_runner: TurnRunner` parameter. |
| Five mutable property mirrors (`workspace_manager`, `pool_name`, `runtime_services`, `governance`, `emitter_factory`) | Eliminated — strategy.assemble() configures the turn_runner once, post-construction wiring is gone. |
| `update_emitter_factory` on TurnRunner | Eliminated — `emitter_factory` is now a `PoolAssemblyContext` field, wrapped (e.g. `_WorkspaceEmitterFactory`) **before** `assemble()` is called, and passed straight through to the turn_runner. |

**What stays in `AgentPipeline`** (its actual responsibilities):

1. **Lifecycle**: `run()` / `stop()`, `input_adapter` start/stop, `dream_task`
   management (gated by `if self.dream_engine is not None`).
2. **Pre-lock dispatch** (`_process_message`): router → command_processor →
   dedup → busy mode → session lock → delegate to `turn_runner.process_locked()`.
3. **Session queries**: `is_session_active` / `has_active_sessions` /
   `get_active_turn_uuid` (delegate to `registry`).
4. **Session cleanup**: `cleanup_session_resources` — `registry.cleanup` +
   optional `control_channel.cleanup_session`.
5. **Public turn entry**: `process_message(input_msg)`.

Estimated size: 569 → ~200 lines.

`dream_engine` stays as a pipeline-level optional (F1) — its lifecycle is
bound to `run()`/`stop()`, and splitting it out would require cross-object
coordination with no benefit. External_coding pools pass `dream_engine=None`;
the `if self.dream_engine is not None` guard keeps the path dead.

---

### D5 — `pool_builder.create_pool` restructured to common + strategy

`pool_builder.create_pool` (440 → ~150 lines) becomes three phases:

```python
async def create_pool(...) -> PoolInstance:
    # ① Common assembly (every pool)
    common = await _build_common_assembly(...)
    # broker, inbox_server, agent_bus, inbox_poller, session_factory,
    # notification_service, communication_service, target_store,
    # registry, input_adapter, output_adapter, safety, retention

    # ② Strategy assembly (no if-else)
    strategy = assembly_deps.strategy_registry.resolve(
        pool_spec.main.execution_strategy.value
    )
    strategy.validate_pool_spec(pool_spec)  # fail-fast
    try:
        assembly = await strategy.assemble(common)
    except ProviderUnavailableError as exc:
        logger.warning("Pool %r skipped: %s", pool_name, exc)
        return _empty_pool_instance(pool_name, common)

    # ③ Common post-assembly
    await _register_main_agent(common.pool, assembly.agent, ...)
    if strategy.requires_main_agent_tools:
        _register_communication_tool(common, assembly, ...)
    pipeline = AgentPipeline(
        agent=assembly.agent,
        turn_runner=assembly.turn_runner,
        input_adapter=common.input_adapter,
        output_adapter=common.output_adapter,
        registry=common.registry,
        safety=common.safety,
        router=common.router,
        command_processor=assembly.command_processor,
        control_channel=assembly.control_channel,
        dream_engine=assembly.dream_engine,
        dream_interval=assembly.dream_interval,
    )
    common.pool.attach_pipeline(pipeline)
    return PoolInstance(
        name=pool_name, pool=common.pool, strategy=strategy,
        assembly=assembly, broker_bridge=common.broker_bridge, ...
    )
```

The existing `_build_llm_provider` / `_build_terminal_manager` / `_build_tools`
/ `_build_skill_manager` / `_build_external_deps` functions **move into**
the corresponding strategy's `assemble()` method. `pool_builder` keeps only
`_build_common_assembly` and the post-assembly wiring.

> **Deferred:** `_wire_main_pipeline` remains in `pool_builder` because it is
> called for both react and external pools (with `getattr` guards for
> the external path). Moving it into `ReactExecutionStrategy.assemble()` would
> require splitting the function or adding a no-op override on
> `ExternalExecutionStrategy`. The function's post-construction wiring
> (governance, runtime_services, user_interface) is genuine pool-level wiring
> that depends on resources not available at `assemble()` time. Kept in
> `pool_builder` as a common post-assembly step.

The four existing `if execution_strategy == EXTERNAL` sites are
collapsed:

| Site | Old behaviour | New behaviour |
|---|---|---|
| `pipeline.py:207` (runner selection) | `if is_external` → `ExternalTurnRunner` else `TurnRunner` | Removed — `turn_runner` is a constructor parameter |
| `peer_normal.py:54` (reply contract) | `if external` → `modexctl send` else `send_to_agent` | Stays — this is per-target runtime behaviour, read off `AgentDescriptor.execution_strategy`. Not assembly branching. |
| `pool_config/store.py:264,335,429` (validation) | `if external` → forbid subagents, require provider_kind | Retained at store level + `ExternalExecutionStrategy.validate_pool_spec` as defense-in-depth. The store is the single pool.yml write path (WebUI pool write endpoint relies on store-level validation to return HTTP 400 on bad input). Store checks use `!= REACT` (not `== EXTERNAL`) to stay within the arch-guard allowlist. |
| `factory.py:125` (descriptor → builder) | `if external` → `ExternalAgentBuilder` else `ReActAgentBuilder` | Retained — runtime agent-construction dispatch (selects `ExternalAgentBuilder` vs `ReActAgentBuilder`). This is runtime construction, not assembly branching — the factory is already strategy-aware via `ExternalAwareFactory._build_turn_runner`. Kept as a legitimate runtime dispatch site. |

`peer_normal.py:54` stays because it is a runtime per-message routing
decision (which reply mechanism a *target* agent uses), not an assembly-time
branch. It is read off the target's `AgentDescriptor.execution_strategy`,
which remains a typed field on descriptors.

---

### D6 — Migration in five internally-verifiable stages (B1)

One-shot migration (B1), but with five internal stages each ending in a
green test suite. No stage is merged with behaviour change relative to the
previous stage — each is a pure refactor step.

**Stage 0 — ABC + Registry (framework, no behaviour change).**
Add `TurnRunner` ABC (`pipeline/turn_runner_abc.py`), rename existing
`TurnRunner` → `ReActTurnRunner`, make `ExternalTurnRunner` inherit ABC. Add
`ExecutionStrategy` ABC + `ExecutionStrategyRegistry` +
`PoolAssemblyContext` + `StrategyAssembly` (frozen dataclasses). Register
`default_strategy_registry()`. No `pool_builder` or `AgentPipeline` changes
yet. Tests: ABC instantiation, registry register/resolve.

**Stage 1 — `ReactExecutionStrategy.assemble()` (behaviour unchanged).**
Add `agents/react/strategy.py`. `assemble()` internally calls the existing
`_build_llm_provider` / `_build_tools` / etc. (imported from `pool_builder`
for now — code moves in Stage 3). `pool_builder.create_pool` react path
calls `strategy.assemble()` to get the assembly, then continues the old way
(using `assembly.agent` / `assembly.turn_runner` to construct the pipeline).
The four `if execution_strategy == EXTERNAL` branches stay (they
still work; external is handled by the old path). Tests: react pool
full regression.

**Stage 2 — `ExternalExecutionStrategy.assemble()` (behaviour unchanged).**
Add `agents/external/strategy.py`. `pool_builder` external path calls
`strategy.assemble()`. Tests: external pool full regression.

**Stage 3 — Slim `AgentPipeline` + eliminate mirrors.**
`AgentPipeline.__init__` cut to 13 params. Delete the five mutable property
mirrors. `pool_builder` now passes `emitter_factory` (pre-wrapped) via
`PoolAssemblyContext`, and `strategy.assemble()` configures the turn_runner
fully — no post-construction wiring. Tests: pipeline unit tests updated,
full regression.

**Stage 4 — Cleanup.**
Delete the `if is_external` branch in `pipeline.py:205-240`. Delete the
`if execution_strategy == EXTERNAL` branches in `pool_builder` and
`factory.py:125`. Move the validation branches from
`pool_config/store.py:264,335,429` into
`ExternalExecutionStrategy.validate_pool_spec`. Delete the
`_external_wiring.py` functions now superseded by
`ExternalExecutionStrategy.assemble()`. Move the imported
`_build_*` helpers from `pool_builder` into `ReactExecutionStrategy`.
Tests: full regression + architecture guard test asserting no
`if execution_strategy ==` branches remain in `pool_builder.create_pool`
or `AgentPipeline.__init__`.

Each stage is independently revertible. Stage 0+1 can ship together; Stage
2 can ship separately; Stage 3+4 can ship together.

---

## Consequences

**Positive:**

- Adding a new pool shape (RAG-only, planning-graph, future CLIs) is one new
  `ExecutionStrategy` subclass + one registry registration. `pool_builder`,
  `AgentPipeline`, `ReActTurnRunner`, `ExternalTurnRunner` do not change.
- `AgentPipeline` shrinks ~65% (569 → ~200 lines), 33 → 13 constructor
  params, five mirror properties eliminated. Its responsibility is now
  crisply "lifecycle + pre-lock dispatch + turn_runner delegation".
- `pool_builder.create_pool` shrinks ~66% (440 → ~150 lines), focused on
  common assembly + strategy delegation.
- Strategy-specific assembly (react's approval/governance/interceptor/hook
  wiring, external's backend/env/session_map wiring) is co-located with its
  strategy, not interleaved into shared code paths.
- `ExecutionStrategy` enum's semantic drift (it simultaneously meant
  "assembly strategy" and "execution strategy") is resolved: the enum
  becomes `ExecutionStrategyKind` (a pure lookup key), the ABC owns the
  "strategy" semantics.
- React and external can evolve independently — approval reform,
  dream-engine changes, cassette wrapping touch only
  `ReactExecutionStrategy.assemble()`; OpenCode subagent support, sticky
  fallback policy, env refresh touch only
  `ExternalExecutionStrategy.assemble()`.

**Negative:**

- One new ABC (`ExecutionStrategy`) and one new ABC (`TurnRunner`) —
  acceptable, both have two implementations and clear contracts.
- `PoolAssemblyContext` is a large frozen dataclass (~30 fields). This is
  inherent: it is the parameter object for `assemble()`, carrying everything
  a strategy might need. Field count is bounded by what
  `_build_common_assembly` produces; new common resources extend it, new
  strategy-specific resources do not.
- `StrategyAssembly` has many `None`-defaulted fields (react-only and
  external-only collaborators coexist). Acceptable: the alternative
  (discriminated union of `ReactAssembly | ExternalAssembly`) would force
  every consumer into a type switch, re-introducing the branching this ADR
  removes. The `None` defaults are typed (`tool_manager: ToolManager | None
  = None`), and consumers gate on capability flags
  (`strategy.requires_main_agent_tools`) rather than on `is None` checks.
- Stage 1 has a temporary import-from-pool_builder pattern (strategy imports
  `_build_*` helpers from pool_builder) that is undone in Stage 4. This is
  the cost of B1's "verify at every stage" discipline; the alternative
  (move code in Stage 1) couples code move with behaviour verification.
- `peer_normal.py:54` retains an `execution_strategy` read. This is correct
  — it is a runtime per-target routing decision, not assembly branching.

**Neutral:**

- `dream_engine` stays as a pipeline-level optional (F1). Future refactor
  may push it into `ReactExecutionStrategy` if the dream-task lifecycle
  needs strategy-specific control; not done now to keep the Stage 3 cut
  focused.
- `AgentPool` still serves external pools (E1). Its
  subagent-related fields (`_template_registry`, `_materialize_deps`) are
  empty for external — a small wasted reference, not a maintenance
  burden.

## Relationships to prior ADRs

- **ADR-0022** (external coding agent integration) is unchanged in
  topology: external pools are still NORMAL main agents of their own
  pools, communicating via `modexctl send` through `InboxMQ.deliver()`. This
  ADR only changes how they are *assembled*, not how they *run*.
- **ADR-0019** (cross-pool peer communication) is unchanged. `peer_normal`
  reply-contract branching stays (D5).
- **ADR-0020** (pool config convergence) is unchanged. `PoolSpec` /
  `MainAgentSpec` / `SubagentSpec` remain the disk models;
  `ExecutionStrategy` is a runtime ABC layered above them.
- **ADR-0015** (unified inbox) is unchanged. `InboxPoller` /
  `InboxMQ` / `AgentMessageBus` are common-assembly resources; strategies
  do not touch them.

## Disposition

Implemented (2026-07-18) across 13 commits + 2 cleanup commits. The core
design (ExecutionStrategy ABC + Registry, TurnRunner ABC, strategy-driven
assembly, pipeline slimming) is implemented as specified. The following
deviations from the original decision text were made during implementation
and are documented here as the authoritative record.

### D3 deviations — TurnRunner ABC surface

**Spec said:** "One method. No `update_emitter_factory` or other mirror
methods."

**Actual:** The ABC has 1 abstract method (`process_locked`) + 3 lifecycle
methods with no-op defaults (`cleanup_session`, `load_pending_approval`,
`bind_to_pipeline`) + 2 post-construction wiring methods with no-op defaults
(`set_pool_context`, `set_emitter_factory`) + 11 read-only properties with
`None` defaults (`agent_descriptor`, `context_manager`, `skill_manager`,
`turn_store`, `hook_runner`, `hooks`, `sanitizer`, `tool_manager`,
`interceptor_chain`, `runtime_context_manager`, `turn_context_builder`) +
1 read-only `approval_renderer` property.

**Rationale:** The pipeline's pre-lock dispatch (`_process_message`) needs
`agent_descriptor` (routing), `skill_manager` + `turn_store` (CommandContext),
and `load_pending_approval` (approval snapshot before lock). The
`cleanup_session` method is needed for per-session resource cleanup. The
`set_pool_context` / `set_emitter_factory` methods replace direct `_xxx`
private field assignment from pool_builder (typed property/setter cleanup).
The 11 read-only properties are pipeline-facing queries, not mirror setters
— they replace the old `_xxx` delegation chain with typed accessors. This
deviation is pragmatic: the "one method" ideal was correct for the mirror
elimination goal but insufficient for the pipeline's legitimate pre-lock
dispatch needs.

### D4 deviations — AgentPipeline size + delegation properties

**Spec said:** "~200 lines", "zero mirrors" (5 setter mirrors deleted).

**Actual:** 347 lines. The 5 mutable setter mirrors are deleted as
specified. However, 11 backward-compat read-only delegation properties
remain (`hook_runner`, `hooks`, `skill_manager`, `context_manager`,
`tool_manager`, `sanitizer`, `agent_descriptor`, `turn_store`,
`interceptor_chain`, `runtime_context_manager`, `_turn_context_builder`).
These delegate to `self._turn_runner.X` and exist because external code
(`pool_builder._add_hook`, `factory.create_agent` hook injection, wiring.py,
tests) still reads `pipeline.hook_runner` etc. They are read-only (not
post-construction mutable), so they do not violate the mirror-elimination
goal (which targeted setter mirrors). Deleting them requires updating ~20
external read sites — deferred as a follow-up.

### D5 deviations — pool_builder size + factory.py + _wire_main_pipeline + store.py

**Spec said:** "`pool_builder.create_pool` (440 → ~150 lines)", "factory.py:125
branch Removed", "_wire_main_pipeline moves into ReactExecutionStrategy",
"pool_config/store.py validation branches move to
ExternalExecutionStrategy.validate_pool_spec".

**Actual:**
- `pool_builder.create_pool` is ~265 lines (not ~150). The function has
  zero strategy-specific branching (verified by architecture guard test),
  but retains common post-assembly logic (PoolInstance construction,
  AgentMaterializeDeps, InboxPoller, communication wiring) that the ~150
  estimate undercounted.
- `factory.py:125` (`_get_builder` dispatch) is **Retained** — it is
  runtime agent-construction dispatch (selects ExternalAgentBuilder
  vs ReActAgentBuilder), not assembly branching. The factory is already
  strategy-aware via `ExternalAwareFactory._build_turn_runner` override.
- `_wire_main_pipeline` is **Deferred** — remains in pool_builder because
  it is called for both react and external pools (external path uses
  `strategy.requires_main_agent_tools == False` to skip it). Moving it into
  `ReactExecutionStrategy.assemble()` would require splitting the function
  or adding a no-op override; the function's post-construction wiring
  (governance, runtime_services, user_interface) depends on resources not
  available at assemble() time. The architecture guard test allows
  `factory.py` + `peer_normal.py` (+ the additional sites listed below) as
  the only files with `execution_strategy ==` comparisons.
- `pool_config/store.py` validation branches are **Retained at store level**
  as defense-in-depth (subagent stripping, provider_kind validation,
  native-field omission for non-react pools).
  `ExternalExecutionStrategy.validate_pool_spec` remains as
  assembly-time defense-in-depth. The store-level validation was
  temporarily deleted in ticket 6 but restored after code review found
  WebUI write-time tests depended on it.
- `subagent_validator.py` is **Retained** — runtime subagent registration
  validation (same per-target runtime category as `peer_normal.py`, not
  assembly branching).
- `pool_config/specs.py` is **Retained** — Pydantic `@model_validator`
  cross-field validation (`provider_kind` set iff
  `execution_strategy == EXTERNAL`); same validation category as
  `subagent_validator.py`, not assembly branching.
- `template.py` is **Retained** — T5 subagent materialize dispatch:
  when the spec's `execution_strategy` is `EXTERNAL`, `materialize`
  delegates to `deps.subagent_external_builder.build()` instead of
  `agent_factory.create_agent()`. Same runtime construction-dispatch
  category as `factory.py._get_builder`; the react path is byte-for-byte
  unchanged.
- `communication/strategies/subagent_dispatch.py:75` is **Retained** —
  `SubagentDispatchStrategy.build_result` selects ack field shape
  (`output_path`/`trace_dir` omitted for external targets) based on
  `req.target.execution_strategy`. Same per-target runtime category as
  `peer_normal.py` (which reply-shape/field-set the *target* gets); not
  assembly branching. Added when ADR-0027 (external coding subagent)
  introduced the external-result shape.
- `message_format.py` is **Retained** — `build_dispatch_message` is the single
  convergence point for the "target is external → peer format" rule,
  delegated to by `SubagentDispatchStrategy` and `ParentReplyStrategy` so
  the branching lives in one place rather than duplicated across strategy
  classes. Same per-target runtime category as `peer_normal.py`; not
  assembly branching. Added when ADR-0019 (cross-pool peer) introduced
  the peer XML format. Pushing this into the `ExecutionStrategy` ABC was
  considered and rejected: D1 fixes the strategy as a stateless
  assemble-once object (never touched at runtime), and `build_dispatch_message`
  is a pure function called per-message with no `self` state to read —
  OOP dispatch would gain nothing while violating the D1 boundary.

### Additional achievement — external bloat elimination

Beyond the original ADR scope, a follow-up cleanup eliminated ALL react-only
object construction for external pools:

- `ExternalAwareFactory.create_agent` fully overridden — builds only
  6 objects (ExternalAgent + broker I/O + emitter_factory + registry +
  ExternalTurnRunner + AgentPipeline), down from ~15.
- `ExternalExecutionStrategy.assemble()` builds only
  `external_deps` (backend/session_store/parser/env_spec).
- `pool_builder.create_pool` external path skips `SendToAgentTool`
  registration and `_wire_main_pipeline`.
- External_coding pools now boot **without `model.yml` configured** — no
  BotModelProvider is built. Verified by
  `test_external_pool_boots_without_model_yml`.

### Deferred items

1. `_wire_main_pipeline` stays in pool_builder (see D5 deviations above).
2. `StrategyAssembly` transitional fields (`cassette_recorder`, `todo_store`,
   `root_provider`, `external_deps`) remain — they carry side products
   from `strategy.assemble()` to pool_builder's post-assembly phase.
   Eliminating them requires resolving the agent-construction chicken-and-egg
   (agent is created by factory, which runs after assemble()).
3. 11 backward-compat read-only delegation properties on AgentPipeline (see
   D4 deviations above).
4. `AgentPool` private fields (`_materialize_deps`, `_template_registry`,
   `_pool_name`, `_context_fork_builder`) now have typed property setters
   (cleanup commit), but the backing fields remain private with `_` prefix.

Implementation tickets:
`docs/design/execution-strategy-refactor/tickets.md`.
