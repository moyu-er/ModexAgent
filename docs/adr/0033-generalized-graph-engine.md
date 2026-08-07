# ADR-0033: Generalized Graph Engine

Status: accepted (2026-07-20). Fully implemented — Phase a delivered,
Phase c prerequisites cleared (per-channel checkpoint repair, AOP
routing documentation, API surface governance), resume-target routing
delivered. This ADR consolidates the original Phase a design and the
Phase c prerequisite refinements (formerly ADR-0034, now archived at
`docs/adr/history/001-graph-engine-phase-c-preliminaries.md`). Phase c
(parallel scheduling) is designed in **ADR-0034 (Parallel Scheduling
Engine)** — `LinearScheduler` (Phase a behavior) remains the default;
`ParallelScheduler` is opt-in via `Graph.compile(scheduler="parallel")`.

**Persistence contract refinement (2026-08-05):** The Node contract,
state model, and persistence layer described in D2/D4/D5/D6/D7 below
were refined after initial implementation. Channels (`LastValue` /
`ReducerChannel`), `NodeResult` / `Command` / `Task`, declarative
deltas, fork/merge, and `SUPERSEDED` / `PENDING` invocation statuses
were removed. The current contract: `execute()` is `async ...
-> None` (void, no `NodeResult`); state is a plain `BaseModel` shared
across nodes with `checkpoint()` = `model_dump(mode="json")`;
`after_node(ctx, node_name)` is a two-arg signature (no result
parameter); routing is via `deliver()` / `submit()` +
`ctx.dispatch(target, state_update=...)`. Persistence uses three stores
(`GraphInstanceStore` / `NodeStateStore` / `DeliverStore`) with full
state snapshots. The authoritative description lives in
`docs/design/graph-orchestration/distributed-persistence.md`. The
decision sections below are updated to reflect the current contract;
historical context is preserved.

## Historical context

Prior to this ADR, the graph engine was a god-module embedded in
`modex_agent/core/graph/` — topology definition, execution, and state
management were entangled in a single `ReActGraph(Graph)` subclass.
ReAct's logic lived in monolithic node methods that also inlined hook
dispatch, control drain, governance, approval, and event emission. This
ADR extracted the engine into the standalone `modex_graph` package,
disassembled the god-module into focused primitives (`Graph[S]` /
`Node[S]` / `GraphEngine` / `GraphContext` / `GraphRuntime`), and
migrated ReAct to a 4-node topology on the new engine.

## Context

`modex_agent/core/graph/` is nominally a generic state-machine engine but in
practice has only one consumer (`ReActAgent`). Other agents bypass it:
`ExternalAgent` drives a subprocess streaming harness directly;
`SummarizerAgent` is deprecated and unused (separate removal tracked below).
Tool-using agents that need the graph (`ArchiveSummarizer` /
`KnowledgeConsolidator` / `ExperienceReviewAgent` — `KnowledgeConsolidator`
was renamed to `CoreMemoryConsolidator` per ADR-0035; the new name is used
in code) inherit `ScopedFileAgent` and internally construct a `ReActAgent`
in clean mode — they use the graph indirectly through ReAct, never as a
standalone engine.

The graph engine has three structural defects that block generalization:

1. **`NodeTransition` dual routing.** `target` is set by the node and ignored
   by the engine (which routes on `reason` only) — except when `target ==
   END`. The two fields must be kept consistent by hand or bugs hide.
2. **ReAct coupling.** `ReActTurnState` fields (`current_node`, `iteration`,
   `llm_response`, `tool_batches`, `approval`) are read directly by
   `LLMNode`/`ToolNode`/`EndNode`, which are god nodes that also inline
   hook dispatch, control drain, governance, attachment enrichment,
   approval, result assembly, and event emission. The "node" concept is
   indistinguishable from "a ReAct phase".
3. **Missing graph capabilities.** No `START` sentinel (hardcoded `"start"`
   string), no parallelism/fan-out, no subgraph nesting
   (Flow-is-a-Node), no channel/reducer, no checkpoint abstraction
   (snapshots are 230 lines of hand-written payload flattening in
   `ReActSnapshotPolicy`), no `compile()`/builder-executor separation,
   no `GraphBubbleUp` exception family.

### Hard constraints (confirmed by user)

1. Phase a ships; Phase c is designed-for-but-deferred (Graph-is-a-Node,
   nested subgraphs, parallel fan-out, multi-write detection).
2. **The Graph Engine is a standalone package `modex_graph`** — a sibling
   of `modex_agent` under `src/`, depending only on the standard library
   + Pydantic. `modex_agent` depends on `modex_graph`; the reverse is
   **forbidden and enforced by architecture guard test**. The package
   boundary is the physical guarantee of "framework-agnostic" — no
   soft constraint, no rule to remember, just an import that cannot
   resolve. See D11.
3. Pydantic-first (per existing type-safety rules).
4. Approval / snapshot / persistence paths may be refactored — the
   "preserve unchanged" constraint from earlier in the session is lifted.
   Net change must be an improvement (e.g. `ReActSnapshotPolicy` 230 lines
   → ~50 lines via per-channel checkpoint).
5. ReAct migrates to the new engine as its kernel; `ExternalAgent`
   does not (it remains a subprocess streaming harness).
6. `SummarizerAgent` + `SummarizerStrategy` ABC + `DefaultSummarizerStrategy`
   + `SummarizerEvent` are deprecated and unused (only `tests/unit/agents/
   test_summarizer_memory_prompt.py` reads a constant). Tracked as a
   separate removal, not part of this ADR's scope.
7. The package must be **generic enough to support flexible orchestration
   and scheduling** beyond ReAct — Plan-Execute / Workflow / MapReduce /
   graph-of-graphs must all be expressible without forking the engine.
   This is the test of "is this design actually a graph engine or just a
   renamed ReAct loop?" — see D12 for the concrete capability checklist.

## Decisions

### D1 — Phase a scope vs Phase c (deferred) — explicitly recorded

**Phase a (this ADR) ships:**

- New `Graph[S]` builder + `Node[S]` ABC + `GraphEngine` with `run` /
  `run_async` dual entry (sync/async unified via `inspect.isawaitable`)
- `NodeResult` + `Command` structured return (transition + state_update +
  dynamic goto + interrupt + resume)
- `GraphContext[S]` carrying state + `GraphRuntime` ABC + `emit` /
  `interrupt` helpers
- `BaseChannel` ABC + `LastValue` + `ReducerChannel` (exactly 2)
- `GraphState(BaseModel)` with `Annotated[T, ChannelSpec]` per-field
  channel declaration; `checkpoint()` / `from_checkpoint()` automate
  per-channel snapshot
- `GraphBubbleUp` exception family: `GraphInterrupt` / `GraphDrained` /
  `ParentCommand` — engine never swallows
- `START` / `END` sentinels (replace hardcoded `"start"` string)
- Static edges (`add_edge(src, dst, reason=None)`) + conditional edges
  (`add_conditional_edges(src, path_fn)` returning str | list[str] |
  list[Task]) — `Task` accepted as a return value but executed sequentially
  in Phase a
- Cycle guard: configurable `max_iterations` + `GraphRecursionError`
- Graph-is-a-Node **type-level** support (`Graph` is a subclass of `Node`)
  but not exercised by any Phase-a consumer (wiring for Phase c)
- Architecture guard test: `core/graph/` must not import any `modex_agent`
  module outside `core/graph/`

**Phase c (deferred, recorded as future work — NOT part of this ADR):**

1. **True parallel fan-out via `Task`.** Phase a accepts `Command(goto=list[Task])` as a return value but executes targets sequentially in declared order. Phase c introduces continuous parallel scheduling (`asyncio.create_task` + `asyncio.wait(FIRST_COMPLETED)`, no batch barrier), generation-based multi-write detection (`LastValue` raises `InvalidUpdateError` when two same-generation instances write the same field; `ReducerChannel` folds), and fork-based state isolation. See ADR-0034. **Node code is unchanged** — Phase a code that returns `Command(goto=[Task(...)])` runs in parallel automatically once the engine is upgraded.
2. **Graph-is-a-Node exercised.** `Graph` is-a `Node` is wired in Phase a
   but no consumer uses it. Phase c introduces nested subgraph patterns:
   outer turn graph embeds inner agent graph; `Command(goto=..., graph=...)`
   cross-graph routing; `ParentCommand` exception for subgraph→parent
   routing.
3. **`GraphDrained` cooperative shutdown.** The exception class exists but
   is not raised. ADR-0034 realized Phase c via continuous scheduling (no
   superstep boundaries), so `GraphDrained` wiring is deferred until a
   SIGTERM-style cooperative shutdown requirement materializes.
4. **Additional channel types.** Only `LastValue` + `ReducerChannel` ship
   in Phase a. Phase c adds channels when real second use cases appear:
   `Topic` (PubSub with dedup, for `Task` fan-out), `EphemeralValue`
   (cleared after consume, for transient computation), `NamedBarrierValue`
   (fan-in synchronization), per ADR-0007 "two real use cases before
   adding an adapter".
5. **Non-ReAct workflows on the graph.** Phase a migrates only ReAct.
   Phase c migrates InputPipeline / Approval state machine / OutputRenderer
   to graph topologies (the "graph-of-graphs" / "图套图" target). Each
   migration is its own ADR.
6. **Parallel scheduling.** Phase a is sequential execution. Phase c
   (ADR-0034) introduces a `ParallelScheduler` with continuous scheduling
   (not BSP supersteps) as an opt-in alternative to `LinearScheduler`.
   The decision: keep sequential as default, parallel as opt-in via
   `Graph.compile(scheduler="parallel")`.

### D2 — Node interface: single async method `execute` (void return)

```python
class Node[S](ABC):
    @abstractmethod
    async def execute(
        self,
        ctx: "GraphContext[S]",
        integrated_input: "IntegratedInput",
    ) -> None: ...
```

`execute` is `async` and returns `None`. Nodes accumulate downstream
delivers via `self.deliver(content, next_node, ctx)` during `execute`;
the engine's `Node.run()` wrapper handles `submit` (dispatch) and
lifecycle persistence after `execute` returns. There is no
`NodeResult`, no `Command`, no `Task` return type.

Rationale:

- **Not a three-step `prep/exec/post` split.** A separate "pure
  computation" step is false for LLM agents — LLM calls have
  network/billing/streaming side effects, so the middle step cannot be
  treated as a pure function. In practice, three-step splits become
  ceremonial: complex nodes bloat the prep/post phases with logic that
  doesn't fit the "pure" middle, defeating the separation.
- **Not a pure functional `State → Partial[State]` signature.** ReAct's
  `LLMNode` must imperatively emit streaming events, call hooks, query
  runtime services. A pure functional signature would force a ReAct
  rewrite that violates the "approval/snapshot may be refactored but
  must improve" constraint.
- **Void return with deliver/submit.** Admits imperative behavior
  (events, hook calls, state mutation) while keeping the contract
  crisp. Downstream routing is explicit: nodes call `deliver()` to
  accumulate payloads, the framework's `submit` step dispatches them.
  The `GraphRuntime` ABC (D5) absorbs AOP concerns out of the node
  body, so nodes stop being god nodes by construction.

### D3 — Sync/async dual mode: `def execute` + engine `run` / `run_async`

```python
class GraphEngine[R, S]:
    async def run_async(self, ctx: GraphContext[S]) -> R: ...
    def run(self, ctx: GraphContext[S]) -> R:
        return asyncio.run(self.run_async(ctx))
```

Engine unifies sync/async node implementations via `inspect.isawaitable`:

```python
result = node.execute(ctx)
if inspect.isawaitable(result):
    result = await result
```

- One ABC, one engine loop, one node library — no `SyncNode`/`AsyncNode`
  split. Borrowed from `anyio`/`httpx`/`starlette` precedent.
- ReAct uses `run_async` (its LLM/tool nodes are async); standalone graph
  users (scripts, CLI, REPL) use `run`.
- `ctx.interrupt(value)` is a sync `raise GraphInterrupt(value, ...)`,
  independent of the node's sync/async mode.

Rejected: two ABCs (`SyncNode` + `AsyncNode` + two engines) — duplicates
the engine loop, splits the node library, forces adapters at every
sync↔async boundary. The unified-ABC cost is one `inspect.isawaitable`
call per node execution (negligible).

### D4 — State model: shared Pydantic `BaseModel` (imperative mutate + full snapshot)

**Current contract (2026-08-05 refinement):** State is a plain
`GraphState(BaseModel)` subclass. Fields are ordinary Pydantic fields —
no `Annotated[T, ChannelSpec]`, no `LastValue` / `ReducerChannel`, no
per-field channel machinery. All nodes share the same `ctx.state`
reference and mutate it imperatively.

```python
class ReActTurnState(GraphState):
    current_node: ReActNode = ReActNode.START
    iteration: int = 0
    llm_response: LLMResponse | None = None
    tool_batches: list[ToolBatchState] = Field(default_factory=list)
    approval: ApprovalTransaction | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    result: AgentResult | None = None
    resume_target: str | None = None
```

**Imperative-only state access:** `ctx.state.iteration += 1` mutates
the Pydantic field directly. There is no declarative
`return NodeResult(state_update=...)` path — `execute` is async void.
Downstream data flows through `deliver()` / `submit()` + the
`DeliverStore` consumption state machine, not through state deltas.

**Snapshot automation (full state, per-channel removed):**

```python
class GraphState(BaseModel):
    def checkpoint(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any]) -> Self:
        return cls.model_validate(data)
```

`ReActSnapshotPolicy._build_payload` (230 lines) +
`state_from_snapshot` (~80 lines) collapsed to ~10 lines calling
`state.checkpoint()` / `state.from_checkpoint()`. Net code reduction.
The persistence layer stores the full snapshot in
`node_states.state_json` on `complete_invocation` /
`suspend_invocation`; recovery rebuilds via `model_validate()`.

**Channels, multi-write detection, fork/merge — removed.** The
`BaseChannel` / `LastValue` / `ReducerChannel` ABC + implementations,
the `WriteConflictDetector` / `GenerationWriteTracker`, the
`InvalidUpdateError` multi-write guard, and `ctx.fork()`-based state
isolation were removed after implementation. ParallelScheduler
(ADR-0034) uses per-task context shells that share `ctx.state` rather
than forking. See `distributed-persistence.md` §15 (removed concepts)
for the full list. The historical channel-based design that this ADR
originally specified is preserved in the rejected-alternatives and
open-questions sections below for traceability.

**Persistence path:**

- `TurnSnapshot.state_payload: dict[str, JsonValue]` — unchanged
  (ReAct turn state).
- `TurnStateStore.save_turn()` — unchanged.
- `ReActRuntimeStateCodec.encode_turn/decode_turn` — unchanged.
- SQLite schema for `TurnSnapshot` — unchanged.
- Graph-level state persistence: three stores
  (`GraphInstanceStore` / `NodeStateStore` / `DeliverStore`) with
  full snapshots in `node_states.state_json`. See
  `distributed-persistence.md` for the authoritative description.

### D5 — `GraphRuntime` ABC: AOP concerns out of the node body

```python
class GraphRuntime(ABC):
    """Framework-defined AOP bridge. Default implementations are no-ops.

    Two layers:
    - Engine-auto-invoked (node-level only): before_node / after_node.
      These are universal graph lifecycle points — every graph has nodes.
    - Node-explicit: dispatch_hook / around / apply_governance /
      drain_control / capture_snapshot / emit. Nodes call these when
      they need business-specific AOP. Iteration-level hooks
      (BEFORE_ITERATION / AFTER_ITERATION) are NOT engine-auto-invoked —
      "iteration" is not a universal graph concept (linear graphs,
      conditional branches have no iterations; only loop graphs like
      ReAct do). Iteration hooks are dispatched explicitly by the
      ReAct node that defines what an "iteration" means.
    """

    # Engine-auto-invoked (2, node-level universal):
    async def before_node(self, ctx: "GraphContext", node_name: str) -> None: pass
    async def after_node(self, ctx: "GraphContext", node_name: str) -> None: pass

    # Node-explicit (6, business-specific):
    async def dispatch_hook(
        self, hook_point: str, ctx: "GraphContext", data: dict | None = None
    ) -> None: pass
    async def around(
        self, scope: str, ctx: "GraphContext", body: Callable[[], Awaitable[Any]]
    ) -> Any: return await body()
    async def apply_governance(self, messages: list, ctx: "GraphContext") -> list: return messages
    async def drain_control(self, ctx: "GraphContext") -> None: pass
    async def capture_snapshot(self, ctx: "GraphContext", reason: str) -> None: pass
    async def emit(self, event_type: str, data: Any, ctx: "GraphContext") -> None: pass
```

**Critical design rules:**

1. **`before_iteration` / `after_iteration` are NOT on `GraphRuntime`.**
   "Iteration" is a ReAct concept (one LLM+TOOL cycle), not a universal
   graph concept. ReAct nodes dispatch `BEFORE_ITERATION` /
   `AFTER_ITERATION` explicitly via `ctx.runtime.dispatch_hook(
   ReActHookPoint.BEFORE_ITERATION, ctx)` at the exact same code points
   as today. This eliminates the highest migration risk (hook timing
   parity) — hook call sites are node-controlled, not engine-controlled,
   so timing is identical before and after migration by construction.

2. **`dispatch_hook` carries a `data: dict | None` parameter.** Existing
   `HookRunner.dispatch(hook_point, ctx, payload=HookPayload(data=...))`
   passes structured data to hooks (e.g. `{"tool_calls": [...]}` for
   `BEFORE_TOOL_EXECUTION`). The `data: dict | None = None` parameter
   preserves this with a generic dict — `ReactGraphRuntime` wraps it
   into `HookPayload(data=data)` when calling `hook_runner.dispatch`.
   The graph engine uses a generic dict, not `HookPayload`, keeping
   the engine free of `modex_agent` type dependencies.

3. **`ReactGraphRuntime` bridges `GraphContext` to `AgentContext` for
   underlying services.** Existing hook implementations expect
   `AgentContext` (with `ctx.runtime`, `ctx.history`, `ctx.emitter`,
   `ctx.runtime.state`). `ReactGraphRuntime`'s methods receive
   `GraphContext` but pass `ctx.user_data` (which holds the
   `AgentContext`) to the underlying `hook_runner.dispatch` /
   `interceptor_chain.around_*` / `governance.apply` /
   `drain_control_channel`. **Hook implementations are completely
   unaware of the migration** — they still receive `AgentContext`, read
   the same fields, observe the same timing. This is the agent layer
   adapting to the graph engine's generic `GraphContext` + `user_data`
   escape hatch, not the graph engine adapting to `AgentContext`. The
   engine stays framework-agnostic; the bridging is entirely in
   `modex_agent/agents/react/runtime.py`.

4. **`around` routes `ITERATION` only.** `TOOL_CALL` and `LLM_STREAM`
   are node-local AOP invoked directly via `InterceptorChain`
   (`tool_executor.py` / `llm_client.py`), not through `ctx.runtime.around`.
   Their typed contexts (`ToolCallContext` / `LLMStreamContext`) carry
   node-local data (`tool_call`, `tool_name`, `arguments`, async
   iterators) that cannot be lifted to the graph-runtime layer without
   violating invariant 1 (`modex_graph` has zero `modex_agent` imports)
   or adding a pure forwarding shell that fails the ADR-0007 deletion
   test. `around` constructs `IterationContext` internally from
   `ctx.user_data` (AgentContext) — `IterationContext` is constructible
   from graph-runtime-layer data, so it goes through `around`.

5. **Hook / interceptor registration surfaces are untouched.** Plugin
   `register_hook()`, `HookRunner.add(HookSpec(...))`,
   `InterceptorChain` construction — all registration paths remain in
   `modex_agent` and are not affected by the graph engine migration.
   The migration only changes *where dispatch happens* (from inline
   `runtime.hooks.dispatch(...)` in node code to
   `ctx.runtime.dispatch_hook(...)` routed through `ReactGraphRuntime`),
   not *how hooks are registered* or *what they receive*.

- `modex_agent` provides `ReactGraphRuntime` bridging to `HookRunner` /
  `InterceptorChain` / `Governance` / `ControlChannel` /
  `SnapshotPolicy` / `TurnStateStore` / `ContentEmitter`.
- Standalone graph user supplies `GraphRuntime()` (all no-ops) or their
  own subclass.
- Engine calls `before_node` / `after_node` at node-entry/exit; default
  no-ops make the engine runnable with zero AOP wiring.
- Nodes access runtime via `ctx.runtime.dispatch_hook(...)` etc. —
  god-node logic moves out of node bodies into runtime implementation.

### D5.1 — `GraphContext` subclassability for type-safe business access

`GraphContext[S]` is a regular class (NOT Pydantic — it holds runtime
objects per rule 12). It is **subclassable** so business modules can
add type-safe accessors:

```python
# modex_graph/ — engine provides the base
class GraphContext[S]:
    state: S
    runtime: GraphRuntime
    user_data: Any
    def fork(self, *, state=None, runtime=None, user_data=None) -> GraphContext[S]: ...
    def emit(self, event_type: str, data: Any) -> None: ...
    def interrupt(self, value: Any) -> NoReturn: ...

# modex_agent/agents/react/ — ReAct subclasses for type-safe access
class ReActGraphContext(GraphContext[ReActTurnState]):
    @property
    def agent_ctx(self) -> AgentContext:
        return self.user_data
    @property
    def tool_manager(self) -> ToolManager:
        return self.agent_ctx.runtime.services.tool_manager
    @property
    def context_manager(self) -> ContextManager:
        return self.agent_ctx.runtime.services.context_manager
    # ... other typed accessors as needed
```

This avoids `cast(AgentContext, ctx.user_data)` at every access site.
Other business modules define their own `GraphContext` subclasses.

### D5.2 — `fork()` shared/isolated semantics

`ctx.fork(state=..., parent=...)` creates a sub-context for `Task`
fan-out. Three layers of sharing:

- **`runtime` shared** (inherited from parent): subtask uses the same
  AOP services (hook_runner / emitter / snapshot_store). AOP services
  are turn-scoped, not task-scoped.
- **`user_data` shared** (inherited from parent): subtask sees the same
  `AgentContext` (or business context). Turn-internal context does not
  change across tasks.
- **`state` isolated** (if a new state is passed): subtask has its own
  state. Imperative mutations (`sub_ctx.state.x = y`) do NOT propagate
  to the parent state. Only `NodeResult.state_update` is merged back to
  the parent via reducer channels. If `state=None` is passed, the
  subtask shares the parent state (mutations propagate directly — use
  with care in Phase a; Phase c parallel execution forbids this).

This three-layer semantics is the contract for `Task`-based fan-out.

### D6 — Routing: deliver / submit (current contract)

**Current contract (2026-08-05 refinement):** Routing is deliver-only.
Nodes call `self.deliver(content, next_node, ctx)` during `execute()`
to accumulate payloads; the engine's `Node.run()` wrapper calls
`self.submit(ctx)` after `execute()` returns, which groups delivers by
`next_node` and calls `ctx.dispatch(target, state_update={"delivered":
payload, ...})` for each group. The scheduler's dispatch handler
records the target and routes the deliver to the target node's
`DeliverStore` via `coordinator.route_deliver(...)`.

```python
class Node[S](ABC):
    def deliver(self, content: Any, next_node: str | None, ctx: "GraphContext[S]") -> None: ...
    def submit(self, ctx: "GraphContext[S]") -> None: ...   # default delegates to _submit
    def _submit(self, ctx: "GraphContext[S]") -> None: ...
```

- `next_node=None` resolves via graph topology (default edge / single
  downstream / END) in `_resolve_default_target`.
- `next_node=GraphNode.END` skips `route_deliver` (END has no
  `DeliverStore`).
- A node that produces no delivers and has no default downstream edge
  raises `RoutingError` ("Node X did not deliver").

**Historical context (preserved for traceability):** The original
Phase-a design specified four coexisting routing mechanisms with
strict priority — `Command(goto=...)`, `transition: str` static-edge
lookup, `add_conditional_edges(route_fn)`, and the default edge. Plus
`NodeResult` (structured return carrying `transition` /
`state_update` / `command`) and `Task` (fan-out unit with isolated
state). These were removed during the persistence-contract
refinement: `execute` became async void, `NodeResult` / `Command` /
`Task` were deleted, and `add_conditional_edges` / `route_fn` were
dropped (no internal caller). Conditional routing is now expressed
via `deliver(next_node=...)` choosing the branch, or via
`state.resume_target` for resume re-entry. The deliver/submit model
is the single routing path; see `distributed-persistence.md` §9
(Deliver routing and consumption) for the authoritative description.

### D7 — `GraphBubbleUp` exception family

```python
class GraphBubbleUp(Exception): pass

class GraphInterrupt(GraphBubbleUp):
    """HITL suspend. Raised by ctx.interrupt(value). Suspend-without-
    re-execution model: applied state updates persist, resume re-enters
    at the next iteration, NOT by re-running the node body."""

class GraphDrained(GraphBubbleUp):
    """Cooperative shutdown. Class exists but is never raised; wiring
    deferred (ADR-0034 uses continuous scheduling, not supersteps)."""

class ParentCommand(GraphBubbleUp):
    """Subgraph→parent routing. Phase c only — class exists in Phase a
    but is never raised."""
```

The engine **never swallows `GraphBubbleUp`** — propagates to the caller.
Architecture guard test enforces this. Reinforces the existing
"never catch and swallow `GraphInterrupt`" rule with a formal hierarchy.

**Suspend-without-re-execution model:** `GraphInterrupt` does NOT
re-execute the node body on resume. Node authors write linear code;
already-applied state updates persist across the interrupt boundary.
This avoids requiring node idempotency — a fragile constraint for nodes
with side effects (LLM calls, tool invocations). This is ModexAgent's
existing behavior, preserved through the migration.

**Resume-target routing (D7 refinement — delivered):** The "resume
logic is carried by graph topology" promise is delivered via a
`resume_target` field on `GraphState` (a plain `str | None` field, not
a channel) consumed by the entry node, not via entry-node
`if state.phase == SUSPENDED` hardcoding. A node that wants to suspend
sets `state.resume_target = "NODE_NAME"` before capturing its
snapshot, then calls `ctx.interrupt(value)`. On re-entry, the entry
node reads `state.resume_target` and routes via
`deliver(content, next_node=state.resume_target, ctx)` (deliver/submit
per D6), then clears the field. Any node can suspend this way and the
entry node routes to it generically — approval is no longer the only
expressible suspend source. The `phase` field remains the node's own
lifecycle marker (node-internal resume detection, e.g. `ToolNode`
checking `state.phase == SUSPENDED` to call
`_resume_suspended_batch`), distinct from `resume_target` (graph-level
routing signal consumed by the entry node).

### D8 — Graph-is-a-Node (wired in Phase a, exercised in Phase c)

`Graph[S]` is a subclass of `Node[S]`. Its `execute(ctx)` runs its own
engine loop on `ctx`. This enables:

- Subgraph patterns (outer turn graph embeds inner agent graph)
- Reusable graph fragments as nodes
- The "graph-of-graphs" / "图套图" target

Phase a wires the type relationship but no consumer uses it. Phase c
migrates InputPipeline / Approval / OutputRenderer to graph topologies,
exercising this capability.

This is the Flow-is-a-Node composition pattern — a graph is itself a
node, enabling zero-ceremony subgraph nesting.

### D9 — Migration: ReAct switches to the new engine as its kernel

- `ReActAgent` / `ReActTurnRunner` construct and run the new `GraphEngine`
  internally — no behavior change visible to `AgentPipeline` or
  `ExecutionStrategy`.
- `ReActTurnState` becomes a `GraphState(BaseModel)` subclass with
  `Annotated[T, LastValue]` fields. ~30 mechanical rename sites
  (`ctx.runtime.state.x = y` → `ctx.state.x = y`).
- `ReActSnapshotPolicy` collapses from ~310 lines to ~50 lines via
  `state.checkpoint()` / `state.from_checkpoint()`.
- `LLMNode` / `ToolNode` / `EndNode` shed AOP code — hook dispatch /
  control drain / snapshot move to `ReactGraphRuntime`. Node bodies
  contain only node-specific business logic.
- `ApprovalTransaction` becomes a `LastValue[ApprovalTransaction | None]`
  channel. `ctx.interrupt(tx)` replaces direct `raise GraphInterrupt`.
  Serialization logic moves into a per-type codec registered with the
  channel — no hand-written `ApprovalSnapshotKey` enum + 80-line
  `serialize_approval` / `approval_from_snapshot` pair.

ExternalAgent is NOT migrated. It remains a subprocess streaming
harness outside the Graph Engine. `ExecutionStrategy` /
`TurnRunner` ABCs (ADR-0025) are unchanged.

### D9.1 — Graph construction layering (no presets in Phase a)

Three layers, strictly separated:

| Layer | Location | Content | Examples |
|---|---|---|---|
| Core engine | `modex_graph/` | Primitives: `Graph`, `Node`, `CompiledGraph`, `GraphEngine`, `Channel`, `GraphState`, `Command`, `Task`, `GraphRuntime`, `GraphBubbleUp` | — |
| **Preset graphs** (deferred) | `modex_graph/presets/` (future) | Reusable topology templates: `LinearGraph`, `LoopGraph`, `MapReduceGraph`, `ConditionalBranchGraph` | Phase c / second-real-use-case |
| Business graphs | `modex_agent/agents/<strategy>/graph.py` | Concrete business-specific topologies | `ReActGraph` (4-node), future `PlanExecuteGraph`, etc. |

**Phase a: no preset graphs.** Per ADR-0007 ("two real use cases before
promoting a seam"), `modex_graph/presets/` is NOT created in Phase a.
Only ReAct exists as a graph consumer; a preset library abstracted from
one consumer would be speculative. Business modules construct their own
`Graph` instances from core primitives.

**Interface is preset-ready:** `Graph` is subclassable
(`class LoopGraph(Graph[S])` is a legal pattern), so Phase c can introduce
presets without engine changes. The decision to defer is about *what to
ship*, not *what the API allows*.

**Business graph convention:** each business module owns its graph
construction (e.g. `agents/react/graph.py` exports `build_react_graph()`).
The graph package never imports or knows about business graph builders.

### D9.2 — String typing: `str` at the engine boundary, `StrEnum` in business modules

The Graph Engine's public API uses `str` for all string-typed parameters
(`hook_point: str`, `scope: str`, `event_type: str`, `transition: str`,
node names). Business modules define their own `StrEnum` and pass enum
values — Python's `StrEnum` is a `str` subclass, so enum values satisfy
the `str` parameter type without engine-side imports.

```python
# modex_graph/runtime.py — engine-side, str parameters, no business imports
class GraphRuntime(ABC):
    async def dispatch_hook(self, hook_point: str, ctx: "GraphContext") -> None: ...
    async def around(self, scope: str, ctx: "GraphContext", body: Callable) -> Any: ...
    async def emit(self, event_type: str, data: Any, ctx: "GraphContext") -> None: ...

# modex_agent/agents/react/constants.py — business-side, typed enums
class ReActHookPoint(StrEnum):
    BEFORE_ITERATION = "before_iteration"
    AFTER_ITERATION = "after_iteration"
    AFTER_LLM_RESPONSE = "after_llm_response"
    BEFORE_TOOL_EXECUTION = "before_tool_execution"
    AFTER_TOOL_EXECUTION = "after_tool_execution"
    FINALIZE_CONTENT = "finalize_content"
    # BEFORE_TURN / AFTER_TURN / FINALLY_TURN are NOT here — they are
    # turn-level hooks dispatched in ReActAgent.run() directly via
    # hook_runner.dispatch(HookPoint.X, agent_ctx), not through the
    # graph runtime.

class ReActEvent(StrEnum):
    START = "start"
    MAX_ITERATIONS = "max_iterations"
    MODEL_OUTPUT = "model_output"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    ITERATION_END = "iteration_end"
    PROGRESS = "progress"
    FINAL_OUTPUT = "final_output"
    ERROR = "error"

class ReActScope(StrEnum):
    ITERATION = "iteration"
    LLM_CALL = "llm_call"
    LLM_STREAM = "llm_stream"
    TOOL_CALL = "tool_call"
    # TURN scope is NOT here — around_turn is dispatched in
    # ReActAgent.run() directly, not through the graph runtime.

# modex_agent/agents/react/nodes/llm.py — usage
await ctx.runtime.dispatch_hook(ReActHookPoint.AFTER_LLM_RESPONSE, ctx)
# ReActHookPoint.AFTER_LLM_RESPONSE is a str subclass, satisfies hook_point: str
```

**`ReactGraphRuntime` maps business StrEnum values to `modex_agent`
enums** (`HookPoint` / `InterceptorScope` / `ReActEvent`):

```python
class ReactGraphRuntime(GraphRuntime):
    HOOK_POINT_MAP: Mapping[str, HookPoint] = {
        ReActHookPoint.BEFORE_ITERATION: HookPoint.BEFORE_ITERATION,
        ReActHookPoint.AFTER_ITERATION: HookPoint.AFTER_ITERATION,
        # ...
    }
```

**No hardcoded strings anywhere:** the engine uses sentinel constants
(`GraphNode.START` / `GraphNode.END`); business modules use `StrEnum`;
the mapping lives in the business runtime implementation.

**ReAct node names and transition reasons** also use `StrEnum`:
`ReActNode` (START/LLM/TOOL/END) and `ReActReason` (NORMAL_START /
HAS_TOOLS / NO_TOOLS / MAX_ITERATIONS / LLM_ERROR / TOOLS_DONE /
TURN_CANCELLED) already exist as enums — they are kept and used as
`add_node(name=...)` / `add_edge(reason=...)` arguments. `StrEnum` values
satisfy the engine's `str` parameters. (Resume routing no longer uses a
static edge — see D7 resume-target routing.)

### D9.3 — Exit mechanisms (all preserved) + graph result via state field

All current ReAct exit mechanisms are preserved through the migration:

| Exit mechanism | Current | Migrated | Phase |
|---|---|---|---|
| Approval suspend (`GraphInterrupt`) | `raise GraphInterrupt(snapshot)` in ToolNode | `state.resume_target = TOOL; ctx.interrupt(tx)` (raises `GraphInterrupt`, a `GraphBubbleUp` subclass; `state.resume_target` channel drives re-entry routing per D7) | a ✅ |
| `max_iterations` | LLMNode checks, returns `transition="max_iterations"` → static edge to END | Same; `max_iterations` configured at `compile()` time | a ✅ |
| `turn_cancelled` | ToolNode checks, returns `transition="turn_cancelled"` → static edge to END | Same | a ✅ |
| `llm_error` | LLMNode checks, returns `transition="llm_error"` → static edge to END | Same | a ✅ |
| `GraphDrained` (cooperative shutdown) | N/A | `GraphBubbleUp` subclass; class exists, never raised (wiring deferred) | c (deferred) |
| `ParentCommand` (subgraph→parent routing) | N/A | `GraphBubbleUp` subclass | c |

**`GraphInterrupt` suspend-without-re-execution model is preserved.**
Node authors write linear code; already-applied state updates persist
across the interrupt boundary; resume re-enters at the next iteration,
NOT by re-running the node body. This is the existing ModexAgent
behavior and is kept verbatim. Re-entry routing is driven by
`state.resume_target` (a plain `str | None` field on `GraphState`,
set before `ctx.interrupt(value)` per the D7 refinement): the entry
node reads it and routes via `deliver(next_node=state.resume_target)`,
replacing the earlier `if state.phase == SUSPENDED` hardcoding.

**Graph result return:**

`GraphEngine.run_async(ctx)` returns `ctx.state` (the final state). The
graph does not have a separate "return value" — the result is a field on
the state, written by the terminal node.

```python
class GraphEngine[S]:
    def __init__(self, graph: CompiledGraph[S]): ...
    async def run_async(self, ctx: GraphContext[S]) -> S:
        # ... loop until END ...
        return ctx.state
    def run(self, ctx: GraphContext[S]) -> S:
        return asyncio.run(self.run_async(ctx))
```

**ReAct migration:** `ReActTurnState` carries an explicit
`result: AgentResult | None = None` field (plain Pydantic field, no
`Annotated` / `LastValue`), replacing the current
`custom[TurnCustomKey.GRAPH_RESULT]` pattern. The `EndNode` writes
`ctx.state.result = assembled_agent_result`. The `ReActAgent.run()`
reads `state.result` after `engine.run_async()` returns. This is more
type-safe than the `custom` dict escape hatch and is checkpoint-friendly
(the result is a regular Pydantic field serialized via `model_dump()`).

```python
class ReActTurnState(GraphState):
    # ... existing fields ...
    result: AgentResult | None = None  # replaces custom[GRAPH_RESULT]

class EndNode(Node[ReActTurnState]):
    async def execute(self, ctx: GraphContext[ReActTurnState], integrated_input: IntegratedInput) -> None:
        result = self._assemble_result(ctx)
        ctx.state.result = result  # ← written to state
        await ctx.runtime.emit(ReActEvent.FINAL_OUTPUT, result, ctx)
        self.deliver(result, GraphNode.END, ctx)  # END sentinel via deliver

# ReActAgent.run()
state = await engine.run_async(graph_ctx)
agent_result = state.result  # ← typed, no cast from custom dict
```

### D10 — `SummarizerAgent` removal (separate, tracked here for visibility)

`SummarizerAgent` + `SummarizerStrategy` ABC + `DefaultSummarizerStrategy`
+ `SummarizerEvent` are deprecated and unused inside `src/`. Only
`tests/unit/agents/test_summarizer_memory_prompt.py` reads a constant.
Removal is a separate PR — listed here for visibility, not part of this
ADR's implementation.

### D11 — `modex_graph` as a sibling package (physical boundary)

The Graph Engine lives at `src/modex_graph/` (sibling of
`src/modex_agent/`), with its own `pyproject.toml` declaring only
`pydantic` (and stdlib) as runtime dependencies. The dependency
direction is one-way:

```
src/modex_graph/    ← depends on: pydantic + stdlib only
       ↑
src/modex_agent/    ← depends on: modex_graph + pydantic + stdlib + ...
       ↑
examples/bot_project/  ← depends on: modex_agent + modex_graph + ...
```

The architecture guard test in `tests/architecture/` enforces:

1. **No file under `src/modex_graph/` may import `modex_agent`.** A
   grep-based guard (`grep -r "from modex_agent" src/modex_graph/` must
   return nothing) plus an import-time guard (a `conftest.py` test that
   sys.modules-blocks `modex_agent` before importing any `modex_graph`
   submodule).
2. **No file under `src/modex_graph/` may import `examples/`.** Same
   guard.
3. `modex_graph`'s `pyproject.toml` must NOT list `modex_agent` as a
   dependency; `modex_agent`'s `pyproject.toml` MUST list `modex_graph`
   as a dependency.

This makes the "framework-agnostic" constraint a physical fact rather
than a convention. A future contributor who tries to import
`modex_agent.hook.HookRunner` from inside `modex_graph` gets an
immediate CI failure, not a code-review debate.

The package is **reusable outside `modex_agent`** — any Python project
that needs a typed graph engine with sync/async dual mode, Pydantic
state, channels, and `GraphBubbleUp` exception family can install
`modex_graph` standalone.

### D12 — Generic orchestration capability checklist (the "is this
actually a graph engine?" test)

To qualify as a general-purpose graph engine (not a renamed ReAct loop),
`modex_graph` must support the following workflow shapes without engine
forks. Each row maps a workflow shape to the engine feature that enables
it. **Phase-a rows must be capable (verified by unit tests); Phase-c
rows must be wired-but-unexercised or deferred per D1.** A non-ReAct
end-to-end example is NOT required for Phase-a merge.

| Workflow shape | Required engine feature | Phase |
|---|---|---|
| Linear chain (A→B→C→END) | `add_edge` static edges | a ✅ |
| Conditional branch (if X then A else B) | `add_conditional_edges` or `Command(goto=...)` | a ✅ |
| Loop (A→B→A until done) | static edge + `transition` routing + cycle guard | a ✅ |
| ReAct (START→LLM→TOOL→END with iteration loop) | all of the above | a ✅ |
| Plan-Execute (Plan→Exec→Reflect→Plan or done) | conditional branch + loop | a ✅ |
| Workflow (DAG with conditional branches) | conditional edges + multiple ENDs | a ✅ |
| MapReduce (fan-out → fan-in) | `Task` accepted but **sequential** in Phase a; reducer channel for fan-in | a (sequential) ✅ / c (parallel) → [ADR-0034](0034-parallel-scheduling-engine.md) |
| Subroutine (call subgraph as a node) | Graph-is-a-Node type wiring | a (wired) / c (exercised) |
| Graph-of-graphs (outer turn graph embeds inner agent graph) | Graph-is-a-Node + `Command(goto=..., graph=...)` | c |
| HITL suspend/resume (approval) | `GraphInterrupt` + `Command(resume=...)` | a ✅ |
| Cooperative shutdown (SIGTERM) | `GraphDrained` (class exists, wiring deferred) | c (deferred) |
| Parallel fan-out with multi-write detection | `Task` parallel + `LastValue` multi-write guard | c → [ADR-0034](0034-parallel-scheduling-engine.md) |

**Phase-a validation criterion**: the engine API must be capable of
expressing the Phase-a rows above without engine forks (verified by unit
tests covering linear / conditional / loop / HITL shapes). A non-ReAct
end-to-end example is **not required** for Phase-a merge — the capability
checklist is the design target, the example is deferred until a real
second consumer arrives (per ADR-0007: two real use cases before
promoting a seam).

### D13 — Migration: 5 internally-verifiable stages (ADR-0025 pattern)

Each stage is independently revertible. Each stage ends with a green
test suite and no behavior change relative to the previous stage.
Stages 0+1 may ship together; Stage 2 ships separately; Stages 3+4 ship
together; Stage 5 is independent.

**Stage 0 — `modex_graph` package lands (framework, zero behavior change).**

- Create `src/modex_graph/` with its own `pyproject.toml` declaring
  only `pydantic` (and stdlib) as runtime dependencies.
- Implement the 8 core types: `Graph`, `Node`, `CompiledGraph`,
  `GraphEngine`, `GraphContext`, `NodeResult` + `Command` + `Task`,
  `BaseChannel` + `LastValue` + `ReducerChannel`, `GraphState`,
  `GraphRuntime`, `GraphBubbleUp` family.
- Unit tests cover all primitives (linear / conditional / loop /
  interrupt / sync / async).
- Architecture guard test enforces `src/modex_graph/` does not import
  `modex_agent` or `examples/`.
- ReAct is untouched; old `src/modex_agent/core/graph/` is untouched.

**Stage 1 — `ReactGraphRuntime` + channel codec adapters (behavior unchanged).**

- New `src/modex_agent/agents/react/runtime.py` implements
  `GraphRuntime`, mapping ReAct `StrEnum` values
  (`ReActHookPoint`/`ReActScope`/`ReActEvent`) to `modex_agent` enums
  (`HookPoint`/`InterceptorScope`/`ReActEvent`).
- New `src/modex_agent/agents/react/codec.py` registers Pydantic
  `model_dump()`/`model_validate()` as the channel codec for ReAct
  state types (see D14).
- Migrate `ApprovalTransaction`, `ToolBatchState`, `ToolCallState`,
  `ApprovalRequestState`, `ToolArguments` from `@dataclass` to Pydantic
  `BaseModel` (frozen) where they are not already. This is required for
  the Pydantic-based channel codec and is a prerequisite for Stage 2.
- ReAct still uses old `core/graph/`; new runtime/codec are unreferenced.
- Validation: ReAct full regression green.

**Stage 2 — `ReActTurnState` becomes `GraphState` subclass; snapshot simplification (behavior unchanged).**

- `ReActTurnState` migrated to `GraphState(BaseModel)` with
  `Annotated[T, LastValue]` / `Annotated[T, ReducerChannel(...)]` per
  field. Add explicit `result: Annotated[AgentResult | None, LastValue]
  = None` field (replaces `custom[TurnCustomKey.GRAPH_RESULT]`).
- `ReActSnapshotPolicy._build_payload` (~230 lines) +
  `serialize_approval` (~30 lines) + `approval_from_snapshot` (~60
  lines) + `state_from_snapshot` (~80 lines) collapse to ~50 lines
  using `state.checkpoint()` / `state.from_checkpoint()`. The old
  `ReActSnapshotPayloadKey` / `ApprovalSnapshotKey` / `ToolBatchSnapshotKey`
  / `ToolCallSnapshotKey` enums are deleted — Pydantic `model_dump()` is
  the codec.
- `ReActRuntimeStateCodec.encode_turn/decode_turn` is essentially
  unchanged (payload `dict[str, JsonValue]` ↔ JSON bytes).
- Validation: snapshot serialize/deserialize round-trip tests pass;
  ReAct full regression green.

**Stage 3 — God node disassembly (behavior unchanged).**

- `LLMNode` / `ToolNode` / `EndNode` / `StartNode` shed AOP code.
  Hook dispatch, interceptor `around`, governance, control drain,
  snapshot capture, emit all move to `ctx.runtime.*` calls (via
  `ReactGraphRuntime`).
- `raise GraphInterrupt(snapshot)` → `ctx.interrupt(tx)`.
- `ctx.runtime.state.x = y` → `ctx.state.x = y` (~30 mechanical
  renames across `nodes/llm.py`, `nodes/tool.py`, `nodes/end.py`,
  `agent.py`).
- `ctx.emitter.emit(ReActEvent.X, ...)` → `ctx.runtime.emit(ReActEvent.X,
  ..., ctx)` (~10 sites).
- `before_iteration` / `after_iteration` hooks move from explicit
  dispatch in `LLMNode` to engine-auto-invoked
  `runtime.before_iteration(ctx)` / `runtime.after_iteration(ctx)`.
  `ReactGraphRuntime` maps these to `HookPoint.BEFORE_ITERATION` /
  `HookPoint.AFTER_ITERATION`. **Timing must be verified identical**
  to current dispatch points (this is the highest-risk change in the
  migration — see Open Questions).
- Validation: ReAct full regression green, including approval
  suspend/resume, max_iterations, turn_cancelled, llm_error paths.

**Stage 4 — ReAct switches to `modex_graph` engine; old `core/graph/` deleted.**

- `ReActAgent.run()` constructs new `Graph` (via
  `build_react_graph().compile(max_iterations=...)`), new `GraphEngine`,
  new `GraphContext` with `ReactGraphRuntime` + `user_data=agent_ctx`.
- `engine.run(ctx)` → `engine.run_async(graph_ctx)`.
- Old `src/modex_agent/core/graph/` directory is deleted. Architecture
  guard test enforces no `modex_agent` file imports
  `modex_agent.core.graph`.
- Validation: ReAct full regression green; architecture guard green.

**Stage 5 — Cleanup (independent PR).**

- Delete `SummarizerAgent` + `SummarizerStrategy` ABC +
  `DefaultSummarizerStrategy` + `SummarizerEvent` (D10). Migrate
  `PROMPT_*` constants if externally referenced (only one test file
  reads `PROMPT_MEMORY_COMPRESSION` — relocate or inline).
- Delete any dead code uncovered by the migration.
- Validation: full test suite green.

### D14 — State serialization: Pydantic `model_dump()` (delivered, simplified)

**Current contract (2026-08-05 refinement):** `GraphState` is a plain
`BaseModel` — no per-field channels, no `__channels__` registry, no
per-channel codec. The single serialization path is
`model_dump(mode="json")` for `checkpoint()` and `model_validate()` for
`from_checkpoint()`. Non-primitive state field types are Pydantic
`BaseModel` subclasses and round-trip automatically.

```python
class GraphState(BaseModel):
    def checkpoint(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any]) -> Self:
        return cls.model_validate(data)
```

All non-primitive ReAct state types were migrated to Pydantic
`BaseModel` (9 types total: `ApprovalTransaction`,
`ApprovalRequestState`, `ToolBatchState`, `ToolCallState`,
`ToolArguments`, plus `CancellationState`, `OperationState`,
`MessageDelta`, `LLMResponse`, `AgentResult`, `TurnIdentity`,
`ToolCall`, `LLMErrorInfo`).

**Frozen vs mutable** is decided per type based on whether the approval
state machine mutates the object at runtime:

- `ApprovalTransaction` → mutable `BaseModel` (`apply_decision` mutates
  `decisions` dict; `_normalize_batch_decisions` may rewrite `ALLOWED`
  to `PREEMPTED` per ADR-0011)
- `ApprovalRequestState` → mutable `BaseModel` (consistency)
- `ToolBatchState` / `ToolCallState` → mutable `BaseModel` (`status` /
  `decision` fields transition during execution)
- `ToolArguments` → `BaseModel(frozen=True)` (leaf value-object)
- `TurnIdentity` / `LLMErrorInfo` → `BaseModel(frozen=True)`
- `AgentResult` / `LLMResponse` / `MessageDelta` / `OperationState` /
  `CancellationState` / `ToolCall` → mutable `BaseModel`

**Per rule 12:** "config/value objects use `BaseModel(frozen=True)`;
runtime objects with state/connections are regular classes." Runtime
state objects that participate in mutable transitions fall under the
"runtime objects" clause.

`TurnStateBase` (the framework base) and `ReActTurnState` are
`GraphState(BaseModel)` subclasses. `TurnSnapshot` remains a `@dataclass`
(runtime-object container, not a state field).

**Historical context (preserved for traceability):** The original
Phase-a design specified a per-channel codec — `BaseChannel.checkpoint()`
per field, with `register_codec(MyType, ...)` registration and a
`TypeAdapter` bridge for stdlib dataclasses. The `react/codec.py` file
with its five `register_codec` calls was dead on arrival
(`encode_value` checked `isinstance(value, BaseModel)` before
`_find_codec`) and was deleted. The channel layer itself
(`BaseChannel` / `LastValue` / `ReducerChannel` / `__channels__`) was
subsequently removed in the persistence-contract refinement; the
per-channel codec collapsed to the single `model_dump()` /
`model_validate()` path that the channel layer had already converged
to internally.

**Why Pydantic and not hand-written codecs:** Pydantic v2's
`model_dump(mode="json")` produces JSON-compatible dicts with enum
serialization, nested model expansion, and `Annotated` metadata
respect — exactly what the former 230-line hand-written
`_build_payload` did, but declaratively. `ReActSnapshotPolicy`
collapsed from ~310 lines to ~50 lines.

### D15 — `modex_graph` API surface governance

`modex_graph`'s public API may include capabilities that no production
consumer currently exercises, provided each capability:

1. Forms part of a **coherent contract** — API elements compose sensibly
   (e.g. `Command.goto=list[Task]` + `Task(state=...)` + `ctx.fork(state=...)`
   + `ReducerChannel` form a complete fan-out/fan-in story).
2. Is **unit-tested in isolation** — every public API element has a
   covering test in `tests/unit/modex_graph/`.
3. Is **not internally contradictory** — e.g. dead `register_codec` calls
   that can never be invoked are defects to fix; unused but coherent API
   elements are not.

This **does not supersede ADR-0007**. ADR-0007 governs seams between
sibling modules inside `modex_agent` — there, speculative abstraction
creates real maintenance cost. `modex_graph` is a standalone package
whose purpose is to be a reusable engine; its API surface is the
contract with future consumers, not an internal seam.

**Workstream (C) — `examples/graph_patterns/`:** three pattern modules
(conditional, retry, map_reduce) exercise the retained API surface with
realistic compositions and prove the engine expresses non-ReAct shapes.
These are **examples, not framework modules** — they live under
`examples/` per ADR-0007 rule 9.

## Consequences

### Positive

- The Graph Engine is a **standalone, framework-agnostic module** —
  reusable outside `modex_agent`, no business-code coupling.
- Adding a new graph-shaped workflow (Plan-Execute, Workflow, MapReduce)
  is "configure a new `Graph` topology + write `Node` subclasses" — no
  engine fork.
- `ReActSnapshotPolicy` collapses from ~310 lines to ~50 lines via
  per-channel checkpoint automation.
- `LLMNode` / `ToolNode` / `EndNode` stop being god nodes — AOP concerns
  move to `GraphRuntime`, node bodies shrink to node-specific logic.
- Sync/async dual mode supports both event-loop-bound agent runtimes and
  standalone script / REPL / CLI usage from one engine.
- `GraphInterrupt` suspend-without-re-execution lets node authors write
  linear code without worrying about idempotency across resume.
- `GraphBubbleUp` formalizes the "never swallow" rule with an exception
  family + architecture guard test.
- Graph-is-a-Node wiring (Phase a) makes Phase c "natural evolution"
  rather than "rewrite".

### Negative

- `ReActTurnState` must become a `GraphState(BaseModel)` subclass — ~30
  mechanical rename sites in `nodes/llm.py` / `nodes/tool.py` /
  `nodes/end.py` / `agent.py`.
- `ReActSnapshotPolicy` is rewritten — net code reduction, but the
  rewrite itself is non-trivial (channel codec registration for
  `ApprovalTransaction` / `ToolBatchState` / `ToolCallState`).
- `NodeResult` / `Command` / `GraphContext` / `GraphRuntime` /
  `BaseChannel` / `LastValue` / `ReducerChannel` / `GraphState` are 8
  new public types — acceptable, all have clear contracts.
- `inspect.isawaitable` per node execution is one reflection call —
  negligible overhead.
- Phase c capabilities (parallel fan-out, multi-write detection, BSP,
  graph-of-graphs) are deferred — users who need them in Phase a must
  wait or fork.

### Neutral

- `ExternalAgent` is not migrated — by design, it is not a
  graph-shaped workflow.
- ADR-0016 (`LoopDetectionHook`) and the new Graph Engine cycle guard
  coexist — ADR-0016 detects ReAct-level semantic loops (repeated
  content + tool calls); the engine cycle guard detects graph-topology
  loops (node A→B→A). Different concerns, no conflict.
- ADR-0025 (`ExecutionStrategy` / `TurnRunner`) is unchanged. The Graph
  Engine is a tool `TurnRunner` may use, not a replacement for the
  strategy abstraction.

## Rejected alternatives

- **Graph as the sole execution substrate (force External into a
  graph).** Rejected — ADR-0025's strategy abstraction is sound;
  External as a subprocess streaming harness does not fit a graph
  shape; forcing it adds ceremony without value.
- **Three-step `prep/exec/post` node interface.** Rejected — a separate
  "pure computation" step is false for LLM agents (LLM calls have side
  effects), and complex nodes bloat prep/post in practice, making the
  split ceremonial.
- **Pure functional `State → Partial[State]` node interface.** Rejected
  — forces ReAct rewrite that violates the "must improve" constraint on
  approval/snapshot.
- **Re-execute-on-resume interrupt model.** Rejected — requires node
  idempotency, fragile for side-effectful nodes. Suspend-without-
  re-execution is cleaner.
- **Large channel type zoo (9+ channels).** Rejected per ADR-0007 —
  only 2 ship in Phase a, additional channels deferred to Phase c when
  real use cases appear.
- **BSP supersteps as the default execution model.** Rejected —
  ADR-0034 chose continuous scheduling (not BSP) as the opt-in parallel
  model. `LinearScheduler` (sequential) remains the default; BSP's
  barrier-bounded superstep imposes head-of-line blocking unsuitable for
  the common sequential-agent case.
- **Two ABCs (`SyncNode` + `AsyncNode`).** Rejected — duplicates engine
  loop, splits node library, forces adapters at every sync↔async
  boundary.
- **Pure declarative state (no imperative mutate).** Rejected — would
  force ReAct's `ctx.state.x = y` to become `ctx.update("x", y)` or
  `return NodeResult(state_update={"x": y})` at ~30 sites, all
  non-mechanical. Dual-mode preserves ReAct's existing style.

### Rejected Phase c triggers

Three candidate "second consumers" to justify starting Phase c were
evaluated and rejected:

1. **InputPipeline migration** — the pipeline is a 25-line linear
   sequence with early-exit. Migrating to a graph replaces 25 simple
   lines with `Graph` + `Node` + `GraphEngine` ceremony — net code
   increase, no new capability.
2. **Approval state machine migration** — approval already uses the
   graph engine's `ctx.interrupt(tx)` → `GraphInterrupt` → resume path.
   Further refactoring would re-touch the approval state machine with
   no offsetting benefit — ReAct's turn stages are stable, not
   dynamically composed.
3. **AgentPipeline / OutputRenderer migration** — `AgentPipeline` is
   imperative orchestration (async stream + per-session lock +
   busy-input 3-mode handling + dedup). Its complexity comes from real
   concurrency concerns that a graph engine does not simplify.

A fourth candidate — multi-agent star topology as a subgraph-nesting
target — was rejected as a category error: star topology is
inter-agent communication (async, isolated state, cross-process);
subgraph nesting is intra-turn control flow (sync, shared state, single
`GraphEngine.run_async`). They are different problems.

**Conclusion:** no real Phase c trigger exists today. Phase c remains
deferred per D12; when a real trigger emerges, design starts from a
clean foundation (per-channel checkpoint delivered, AOP routing
documented, API surface governance in place).

## Relationships to prior ADRs

- **ADR-0007** (zero-usage deep modules with real seams): Graph Engine
  has one current consumer (ReAct) but a real seam (≥2 prospective
  Phase-c consumers). Retained and deepened per this ADR.
- **ADR-0016** (ReAct loop detection): unchanged. ReAct-level semantic
  loop detection and Graph Engine topology cycle guard are different
  concerns.
- **ADR-0025** (ExecutionStrategy / TurnRunner): unchanged. Graph Engine
  is a tool `TurnRunner` may use; strategy abstraction is not replaced.
- **ADR-0028~0031** (persistence schema): unchanged. `TurnSnapshot` /
  `TurnStateStore` / `ReActRuntimeStateCodec` / SQLite schema are all
  preserved. Only `ReActSnapshotPolicy` is rewritten (net code reduction).

## Open questions (to resolve during implementation)

1. **`before_iteration` / `after_iteration` dispatch timing parity
   (Stage 3, highest risk).** Current `LLMNode` explicitly calls
   `runtime.hooks.dispatch(HookPoint.BEFORE_ITERATION, ctx)` at the
   very start of `execute()`, and `AFTER_ITERATION` after the
   iteration's hook chain. After migration, the engine auto-invokes
   `runtime.before_iteration(ctx)` / `runtime.after_iteration(ctx)`
   at engine-loop boundaries. The exact timing must be verified
   identical: `BEFORE_ITERATION` must fire before any LLM-node business
   logic, `AFTER_ITERATION` must fire after the full LLM+TOOL cycle
   completes (not just after the LLM node). If the engine's iteration
   boundary differs from ReAct's current LLM-centric iteration, hooks
   may observe different state. Mitigation: Stage 3 ships with a hook
   timing assertion test that records the `(hook_point, current_node,
   iteration)` triple at every dispatch and compares against a
   pre-migration baseline.

2. **`@dataclass` → Pydantic `BaseModel` migration of ReAct state types
   (Stage 1).** `ApprovalTransaction` / `ToolBatchState` /
   `ToolCallState` / `ApprovalRequestState` / `ToolArguments` are
   currently `@dataclass`. Converting to Pydantic `BaseModel(frozen=True)`
   may surface validation errors for fields currently populated with
   `None` defaults or loose types. Mitigation: convert one type at a
   time with focused regression tests; Pydantic v2's
   `model_config = ConfigDict(arbitrary_types_allowed=True)` is the
   escape hatch for fields holding non-Pydantic types (e.g.
   `LLMResponse`).

3. **Channel codec registration: per-type vs per-field.** D14 specifies
   per-type registration (`register_codec(ApprovalTransaction, ...)`).
   If a state has two fields of the same type that need different
   serialization (e.g. two `ApprovalTransaction` fields with different
   redaction), per-type is insufficient. Decision deferred until a real
   use case appears — per-type is the Phase-a default.

4. **`GraphState` Pydantic v2 `Annotated` metadata reflection.** Pydantic
   v2 exposes field metadata via `model_fields[name].metadata`. The
   `GraphState` base class must walk this metadata to discover
   `LastValue` / `ReducerChannel` specs and instantiate channels at
   `__init__` time. Whether to use `model_fields` introspection or an
   explicit `__channels__: dict[str, ChannelSpec]` class attribute is
   an implementation detail to resolve in Stage 0. The
   `__channels__`-explicit approach is more verbose but decouples from
   Pydantic internals; the `model_fields` approach is more idiomatic
   but couples to Pydantic v2's metadata layout.

5. **Cycle guard vs ReAct loop.** `compile()` offers optional cycle
   detection. ReAct's LLM↔TOOL loop is intentional, not a bug. The
   cycle guard must distinguish "intentional back-edge in graph
   topology" from "unbounded recursion". Phase a's answer:
   `max_iterations` is the runtime safety net; `compile()` cycle
   detection defaults to `warn` (logs, does not raise). Strict cycle
   detection (raise on any back-edge) is opt-in and not used by ReAct.

6. **`ctx.fork()` state isolation vs Pydantic model mutation.** When
   `Task` carries an independent `state`, that state is a
   separate Pydantic model instance. Pydantic v2 `BaseModel` is mutable
   by default; `frozen=True` would block the imperative-mutate mode
   (D4 Z-style). Decision: `GraphState` is NOT frozen (imperative
   mutate allowed); individual value-object fields
   (`ApprovalTransaction`, etc.) ARE frozen per rule 12. The
   `state.result = ...` assignment works because `result` is a field on
   the mutable `GraphState`, holding a frozen value object.

7. **`ReactGraphRuntime` instance lifetime.** One `ReactGraphRuntime`
   per turn (constructed in `ReActAgent.run()`), or one per graph
   execution (constructed in `TurnRunner.process_locked()`)? Phase a
   answer: one per turn — it holds turn-scoped references
   (`hook_runner`, `governance`, `snapshot_policy`, `turn_state_store`,
   `emitter`) that are themselves per-turn. The runtime is NOT
   reused across turns.

8. **Architecture guard test for `modex_graph` isolation.** Two-layer
   guard: (a) grep-based check
   (`grep -rE "from modex_agent|import modex_agent" src/modex_graph/`
   returns nothing); (b) import-time check in `conftest.py` that
   blocks `modex_agent` in `sys.modules` before importing any
   `modex_graph` submodule. Both must be in place by Stage 0.
