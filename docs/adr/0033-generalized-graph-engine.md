# ADR-0033: Generalized Graph Engine (Phase a)

Status: proposed (2026-07-20) — design completed via `/grill-with-docs`;
implementation pending. See D13 for the 5-stage migration plan.

## Context

`modex_agent/core/graph/` is nominally a generic state-machine engine but in
practice has only one consumer (`ReActAgent`). Other agents bypass it:
`ExternalCodingAgent` drives a subprocess streaming harness directly;
`SummarizerAgent` is deprecated and unused (separate removal tracked below).
Tool-using agents that need the graph (`ArchiveSummarizer` /
`KnowledgeConsolidator` / `ExperienceReviewAgent`) inherit `ScopedFileAgent`
and internally construct a `ReActAgent` in clean mode — they use the graph
indirectly through ReAct, never as a standalone engine.

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
5. ReAct migrates to the new engine as its kernel; `ExternalCodingAgent`
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

1. **True parallel fan-out via `Task`.** Phase a accepts `Command(goto=list[Task])` as a return value but executes targets sequentially in declared order. Phase c introduces parallel scheduling (`asyncio.gather` over all tasks), per-channel multi-write detection (`LastValue` raises `InvalidUpdateError` when ≥2 writes happen in one superstep; reducer channels fold), and a BSP-style barrier between supersteps. **Node code is unchanged** — Phase a code that returns `Command(goto=[Task(...)])` runs in parallel automatically once the engine is upgraded.
2. **Graph-is-a-Node exercised.** `Graph` is-a `Node` is wired in Phase a
   but no consumer uses it. Phase c introduces nested subgraph patterns:
   outer turn graph embeds inner agent graph; `Command(goto=..., graph=...)`
   cross-graph routing; `ParentCommand` exception for subgraph→parent
   routing.
3. **`GraphDrained` cooperative shutdown.** The exception class exists in
   Phase a but is not raised. Phase c wires it at superstep boundaries to
   support SIGTERM-style cooperative shutdown with checkpoint preservation.
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
6. **BSP superstep loop.** Phase a is sequential execution. Phase c, if
   adopted, replaces the sequential loop with a superstep driver that
   runs all targets of a fan-out concurrently within a barrier-bounded
   step. This is a non-trivial change to `GraphEngine.run_async`. The
   decision (BSP vs keep sequential + opt-in parallel) is deferred to
   Phase c.

### D2 — Node interface: single-method `execute` with structured `NodeResult`

```python
class Node[S](ABC):
    @abstractmethod
    def execute(self, ctx: "GraphContext[S]") -> NodeResult: ...
    # Note: declared as `def`, subclasses may override with `async def`.
    # Engine unifies via `inspect.isawaitable`.
```

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
- **Single-method with structured return.** Admits imperative behavior
  (events, hook calls, state mutation) while keeping the contract crisp
  via `NodeResult`. The `GraphRuntime` ABC (D5) absorbs AOP concerns out
  of the node body, so nodes stop being god nodes by construction.
- **Single-method with structured return.** Admits imperative behavior
  (events, hook calls, state mutation) while keeping the contract crisp
  via `NodeResult`. The `GraphRuntime` ABC (D5) absorbs AOP concerns out
  of the node body, so nodes stop being god nodes by construction.

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

### D4 — State model: Pydantic + Annotated channel bag (Z-style dual-mode)

State is a `GraphState(BaseModel)` subclass. Each field is annotated with
`Annotated[T, ChannelSpec]`; the spec selects the channel type. Fields
without annotation default to `LastValue`.

```python
class ReActTurnState(GraphState):
    current_node: Annotated[ReActNode, LastValue] = ReActNode.START
    iteration: Annotated[int, LastValue] = 0
    llm_response: Annotated[LLMResponse | None, LastValue] = None
    tool_batches: Annotated[list[ToolBatchState], LastValue] = Field(default_factory=list)
    approval: Annotated[ApprovalTransaction | None, LastValue] = None
    messages: Annotated[list[ChatMessage], ReducerChannel(reducer=operator.add)] = Field(default_factory=list)
```

**Dual-mode state access:**

- **Imperative:** `ctx.state.iteration += 1` mutates the Pydantic field
  directly. Bypasses `channel.update`. Snapshot syncs Pydantic fields →
  channels before checkpoint.
- **Declarative:** `return NodeResult(state_update={"x": v})` — engine
  calls `channel.update([v])`, then syncs back to the Pydantic field.

Both modes coexist. ReAct uses imperative (near-zero change from current
`ctx.runtime.state.x = y`, just renamed to `ctx.state.x = y` — ~30
mechanical rename sites). Future workflows may use declarative for
reducer-aware fan-in.

**Snapshot automation:**

```python
class GraphState(BaseModel):
    def checkpoint(self) -> dict[str, JsonValue]:
        self._sync_fields_to_channels()
        return {key: ch.checkpoint() for key, ch in self.__channels__.items()}

    @classmethod
    def from_checkpoint(cls, data: dict[str, JsonValue]) -> Self: ...
```

`ReActSnapshotPolicy._build_payload` (230 lines) +
`state_from_snapshot` (~80 lines) collapse to ~10 lines calling
`state.checkpoint()` / `state.from_checkpoint()`. Net code reduction.

**Channels shipped (exactly 2):**

- `LastValue` — single-writer semantics, default. Phase a does not enforce
  single-writer (no parallel execution); Phase c enforces.
- `ReducerChannel(reducer: Callable[[Any, Any], Any])` — binary operator
  fan-in. Reducers are **not required to be commutative**; documentation
  states that order-sensitive reducers used with parallel fan-out
  produce order-dependent results.

`BaseChannel` ABC is the extension seam. Additional channels (Topic /
Ephemeral / Barrier / etc.) deferred to Phase c per ADR-0007.

**Persistence path unchanged:**

- `TurnSnapshot.state_payload: dict[str, JsonValue]` — unchanged
- `TurnStateStore.save_turn()` — unchanged
- `ReActRuntimeStateCodec.encode_turn/decode_turn` — unchanged (payload
  dict ↔ JSON bytes)
- SQLite schema — unchanged

**Multi-write detection: deferred to Phase c.** Sequential execution in
Phase a has no multi-write scenarios. Phase c adds `LastValue` raising
`InvalidUpdateError` on ≥2 writes per superstep.

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
    async def after_node(self, ctx: "GraphContext", node_name: str, result: "NodeResult") -> None: pass

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

4. **`around` constructs interceptor context internally.** Existing
   interceptors like `around_iteration(ctx, iteration_ctx, body)` need
   typed context objects (`IterationContext` / `LLMCallContext` /
   `ToolCallContext` / `LLMStreamContext`). `ReactGraphRuntime.around(
   scope, ctx, body)` maps `scope` to the correct interceptor method and
   constructs the typed context from `ctx.user_data` (AgentContext)
   internally. The `body` is a zero-arg awaitable closure.

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

### D6 — Routing: four coexisting mechanisms with strict priority

Four routing mechanisms coexist, resolved in strict priority order:

| Priority | Mechanism | Declare site | Use case |
|---|---|---|---|
| 1 (highest) | `Command(goto=...)` | Runtime | Dynamic routing / dynamic fan-out |
| 2 | `transition: str` (static edge lookup) | Build time | Fixed control flow (ReAct's `has_tools` → TOOL) |
| 3 | `add_conditional_edges(src, route_fn, destinations)` | Build time | Multi-candidate path selection (Workflow's `decide → {a, b, c}`) |
| 4 (lowest) | Default edge (`reason=None`) | Build time | Fallback when nothing else matches |

**`Command.goto` has three forms:**

| Form | Semantics | Phase-a behavior | Phase-c behavior |
|---|---|---|---|
| `str` | Dynamic routing to one node | Jump to that node | Same |
| `list[str]` | Sequential multi-target | Execute all nodes in order | Same (or parallel, TBD) |
| `list[Task]` | Dynamic fan-out (map-reduce) | Execute all Tasks sequentially, each with independent state | Execute all Tasks in parallel, each with independent state |

`Task` — a single fan-out task ("execute this node with this state"):

```python
class Task(BaseModel):
    """A single fan-out task. Phase-a: sequential. Phase-c: parallel.
    
    Phase-c upgrade is engine-only — node code returning
    `Command(goto=[Task(...)])` runs in parallel automatically.
    """
    node: str
    state: Any | None = None   # independent state; None = share parent state
```

**`add_conditional_edges` design:**

```python
class Graph[S]:
    def add_conditional_edges(
        self,
        source: str,
        route_fn: Callable[[S], str],
        destinations: dict[str, str] | None = None,
    ) -> None:
        """Conditional edge. route_fn(state) returns a string.
        
        destinations=None: route_fn return value is used directly as node name.
        destinations provided: route_fn return value is a key in destinations,
        the mapped node name is used. This decouples routing logic from
        concrete node names.
        """
```

**Resolution algorithm** (in `GraphEngine._resolve_next`):

1. If `result.command` is not None and `command.goto` is not None → use `goto` (any of the three forms). Done.
2. If `result.transition` is not None → look up static edge by `(current, transition)`. Done.
3. If `current` has a conditional edge and `result.transition is None` → call `route_fn(state)`, look up `destinations`. Done.
4. If `current` has a default edge (`reason=None`) → use it. Done.
5. Raise `RoutingError`.

**ReAct example:** the existing 4-node graph uses only static edges
(`add_edge` with `reason=`). The `add_conditional_edges` and
`Command(goto=...)` mechanisms are available for new workflows
(Plan-Execute, Workflow, MapReduce) without changing ReAct.

This fixes D1-defect-1 (`NodeTransition.target`/`reason` dual routing) by
collapsing to one mechanism per layer: `transition` for static, `command.goto`
for dynamic, `route_fn` for conditional, default edge for fallback. No `target`
field on `NodeResult`.

### D7 — `GraphBubbleUp` exception family

```python
class GraphBubbleUp(Exception): pass

class GraphInterrupt(GraphBubbleUp):
    """HITL suspend. Raised by ctx.interrupt(value). Suspend-without-
    re-execution model: applied state updates persist, resume re-enters
    at the next iteration, NOT by re-running the node body."""

class GraphDrained(GraphBubbleUp):
    """Cooperative shutdown at superstep boundary. Phase c only — class
    exists in Phase a but is never raised."""

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

ExternalCodingAgent is NOT migrated. It remains a subprocess streaming
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
TURN_CANCELLED / RESUME_TOOLS) already exist as enums — they are kept
and used as `add_node(name=...)` / `add_edge(reason=...)` arguments.
`StrEnum` values satisfy the engine's `str` parameters.

### D9.3 — Exit mechanisms (all preserved) + graph result via state field

All current ReAct exit mechanisms are preserved through the migration:

| Exit mechanism | Current | Migrated | Phase |
|---|---|---|---|
| Approval suspend (`GraphInterrupt`) | `raise GraphInterrupt(snapshot)` in ToolNode | `ctx.interrupt(tx)` (raises `GraphInterrupt`, a `GraphBubbleUp` subclass) | a ✅ |
| `max_iterations` | LLMNode checks, returns `transition="max_iterations"` → static edge to END | Same; `max_iterations` configured at `compile()` time | a ✅ |
| `turn_cancelled` | ToolNode checks, returns `transition="turn_cancelled"` → static edge to END | Same | a ✅ |
| `llm_error` | LLMNode checks, returns `transition="llm_error"` → static edge to END | Same | a ✅ |
| `GraphDrained` (cooperative shutdown) | N/A | `GraphBubbleUp` subclass, raised at superstep boundary | c |
| `ParentCommand` (subgraph→parent routing) | N/A | `GraphBubbleUp` subclass | c |

**`GraphInterrupt` suspend-without-re-execution model is preserved.**
Node authors write linear code; already-applied state updates persist
across the interrupt boundary; resume re-enters at the next iteration,
NOT by re-running the node body. This is the existing ModexAgent
behavior and is kept verbatim.

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

**ReAct migration:** `ReActTurnState` gains an explicit
`result: Annotated[AgentResult | None, LastValue] = None` field,
replacing the current `custom[TurnCustomKey.GRAPH_RESULT]` pattern. The
`EndNode` writes `ctx.state.result = assembled_agent_result`. The
`ReActAgent.run()` reads `state.result` after `engine.run_async()`
returns. This is more type-safe than the `custom` dict escape hatch
and is checkpoint-friendly (the result is a regular channel).

```python
class ReActTurnState(GraphState):
    # ... existing fields ...
    result: Annotated[AgentResult | None, LastValue] = None  # new, replaces custom[GRAPH_RESULT]

class EndNode(Node[ReActTurnState]):
    async def execute(self, ctx: GraphContext[ReActTurnState]) -> NodeResult:
        result = self._assemble_result(ctx)
        ctx.state.result = result  # ← written to state
        await ctx.runtime.emit(ReActEvent.FINAL_OUTPUT, result, ctx)
        return NodeResult(transition=None)  # END sentinel

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
| MapReduce (fan-out → fan-in) | `Task` accepted but **sequential** in Phase a; reducer channel for fan-in | a (sequential) / c (parallel) |
| Subroutine (call subgraph as a node) | Graph-is-a-Node type wiring | a (wired) / c (exercised) |
| Graph-of-graphs (outer turn graph embeds inner agent graph) | Graph-is-a-Node + `Command(goto=..., graph=...)` | c |
| HITL suspend/resume (approval) | `GraphInterrupt` + `Command(resume=...)` | a ✅ |
| Cooperative shutdown (SIGTERM) | `GraphDrained` at superstep boundary | c |
| Parallel fan-out with multi-write detection | `Task` parallel + `LastValue` multi-write guard | c |

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

### D14 — Channel codec: Pydantic `model_dump()` / `model_validate()`

State field types that are not plain primitives (`int`/`str`/`list`/
`dict`) must serialize through a channel codec for
`GraphState.checkpoint()` / `from_checkpoint()`.

**Decision:** use Pydantic's built-in `model_dump()` / `model_validate()`
as the universal channel codec. Any state field type that is a Pydantic
`BaseModel` subclass automatically round-trips through
`model_dump()` → JSON-compatible dict → `model_validate()`.

**Prerequisite:** all non-primitive ReAct state types must be Pydantic
`BaseModel`. The following types are currently `@dataclass` and must be
migrated to Pydantic `BaseModel` in Stage 1. **Frozen vs mutable is
decided per type based on whether the approval state machine mutates
the object at runtime:**

- `ApprovalTransaction` → `BaseModel` (**NOT frozen** — the approval
  state machine mutates `decisions` dict externally: `apply_decision`
  updates `approval.decisions[call_id]` from `PENDING` to
  `ALLOWED`/`DENIED`, and `_normalize_batch_decisions` may rewrite
  `ALLOWED` to `PREEMPTED` for atomicity per ADR-0011. Frozen would
  break the state machine.)
- `ApprovalRequestState` → `BaseModel` (**NOT frozen** — kept mutable
  for consistency with `ApprovalTransaction`, though rarely mutated)
- `ToolBatchState` → `BaseModel` (**NOT frozen** — `status` field
  transitions `WAITING` → `COMPLETED`/`FAILED`/`CANCELLED` during
  execution; `operation_id` may be set after construction)
- `ToolCallState` → `BaseModel` (**NOT frozen** — `decision` field
  transitions `PENDING` → `ALLOWED`/`DENIED`/`PREEMPTED`; `status`
  transitions `PENDING` → `ALLOWED`/`DENIED` → `COMPLETED`/`FAILED`;
  `result` is set after tool execution)
- `ToolArguments` → `BaseModel(frozen=True)` (truly immutable — just a
  typed wrapper around tool call arguments, never mutated after
  construction)

**Per rule 12:** "config/value objects use `BaseModel(frozen=True)`;
runtime objects with state/connections are regular classes." These 5
types are runtime state objects that participate in the approval state
machine's mutable transitions — they fall under the "runtime objects"
clause. `ToolArguments` is the exception (leaf value-object, no
runtime mutation). The graph engine's `LastValue` channel does not
enforce immutability — it accepts any type, frozen or mutable; the
frozen decision is the agent layer's, not the graph engine's.

`TurnStateBase` (the framework base) and `ReActTurnState` migrate to
`GraphState(BaseModel)` in Stage 2. `TurnSnapshot` remains a `@dataclass`
(runtime-object container per rule 12, not a state field — it wraps the
payload dict, not the other way around).

**Channel codec registration** (Stage 1):

```python
# modex_agent/agents/react/codec.py
from modex_graph.channel import register_codec, Codec

def _pydantic_codec(model_cls: type[BaseModel]) -> Codec:
    return Codec(
        encode=lambda v: v.model_dump(mode="json"),
        decode=lambda d: model_cls.model_validate(d),
    )

# Register for each non-primitive ReAct state type
register_codec(ApprovalTransaction, _pydantic_codec(ApprovalTransaction))
register_codec(ApprovalRequestState, _pydantic_codec(ApprovalRequestState))
register_codec(ToolBatchState, _pydantic_codec(ToolBatchState))
register_codec(ToolCallState, _pydantic_codec(ToolCallState))
# Primitives (int, str, list[ChatMessage] via reducer) don't need registration
```

`modex_graph` provides the `Codec` type + `register_codec()` API as
part of its public surface; the actual registrations happen in
`modex_agent` (business side). This keeps `modex_graph` free of
`modex_agent` type imports.

**Why Pydantic and not hand-written codecs:** Pydantic v2's
`model_dump(mode="json")` produces JSON-compatible dicts with enum
serialization, nested model expansion, and `Annotated` metadata
respect — exactly what the current 230-line hand-written
`_build_payload` does, but declaratively. The 80-line
`approval_from_snapshot` reverse parse is replaced by
`ApprovalTransaction.model_validate(payload_dict)`. Net code reduction
is the primary benefit; type safety is the secondary benefit.

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

- `ExternalCodingAgent` is not migrated — by design, it is not a
  graph-shaped workflow.
- ADR-0016 (`LoopDetectionHook`) and the new Graph Engine cycle guard
  coexist — ADR-0016 detects ReAct-level semantic loops (repeated
  content + tool calls); the engine cycle guard detects graph-topology
  loops (node A→B→A). Different concerns, no conflict.
- ADR-0025 (`ExecutionStrategy` / `TurnRunner`) is unchanged. The Graph
  Engine is a tool `TurnRunner` may use, not a replacement for the
  strategy abstraction.

## Rejected alternatives

- **Graph as the sole execution substrate (force ExternalCoding into a
  graph).** Rejected — ADR-0025's strategy abstraction is sound;
  ExternalCoding as a subprocess streaming harness does not fit a graph
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
- **BSP supersteps as the default execution model.** Deferred to Phase c
  — adds latency tax for the common sequential-agent case. Phase a is
  sequential; Phase c decides BSP vs keep-sequential + opt-in parallel.
- **Two ABCs (`SyncNode` + `AsyncNode`).** Rejected — duplicates engine
  loop, splits node library, forces adapters at every sync↔async
  boundary.
- **Pure declarative state (no imperative mutate).** Rejected — would
  force ReAct's `ctx.state.x = y` to become `ctx.update("x", y)` or
  `return NodeResult(state_update={"x": y})` at ~30 sites, all
  non-mechanical. Dual-mode preserves ReAct's existing style.

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
