# PRD: Generalized Graph Engine (Phase a)

Status: ready-for-agent
Related ADR: ADR-0033 (`docs/adr/0033-generalized-graph-engine.md`)

## Problem Statement

As a framework developer, I find that the current graph engine is
nominally generic but in practice only serves ReAct. Other agents bypass
it entirely (`ExternalCodingAgent` drives a subprocess; the deprecated
`SummarizerAgent` calls a provider directly). The engine's types are
coupled to ReAct concepts (`AgentContext` leaks into `Node.execute`),
its nodes are god objects that inline hook dispatch / control drain /
governance / approval / event emission / result assembly, and its state
management relies on 230 lines of hand-written snapshot flattening that
no other workflow can reuse. Adding a new graph-shaped workflow
(Plan-Execute, Workflow, MapReduce) today requires forking the engine
or bypassing it entirely — the framework is becoming customized rather
than general.

## Solution

Extract a standalone `modex_graph` package — a sibling of `modex_agent`
under `src/`, depending only on Pydantic + standard library — that
provides a generalized graph engine with sync/async dual mode,
Pydantic-first typed state with per-field channels, four coexisting
routing mechanisms, a clean AOP bridge (`GraphRuntime`), and
suspend-without-re-execution interrupt semantics. Migrate ReAct to use
this engine as its kernel, collapsing `ReActSnapshotPolicy` from ~310
lines to ~50 lines via per-channel checkpoint automation and shedding
god-node AOP code into the runtime bridge. The package's physical
isolation (architecture guard test enforces no `modex_agent` import)
guarantees the engine stays framework-agnostic as the project evolves,
and its API is designed to support Phase c capabilities (parallel
fan-out, subgraph nesting, graph-of-graphs) without node-code changes.

## User Stories

### Graph construction

1. As a framework developer, I want to build a graph by adding nodes
   and edges imperatively, so that I can express any topology I need.
2. As a framework developer, I want a `compile()` step that validates
   the graph at build time (entry node exists, no dangling edges, node
   names unique), so that build errors surface before runtime.
3. As a framework developer, I want optional cycle detection at compile
   time (warn by default, raise if configured), so that unintentional
   infinite loops are caught early without false-positiving intentional
   back-edges like ReAct's LLM↔TOOL loop.
4. As a framework developer, I want a configurable `max_iterations`
   safety net on the compiled graph, so that runtime infinite loops are
   bounded regardless of cycle detection.
5. As a framework developer, I want `START` and `END` sentinels as
   named constants, so that I never hardcode the string `"start"` or
   rely on magic strings for terminal routing.
6. As a framework developer, I want the compiled graph to be an
   immutable artifact (frozen after `compile()`), so that it is safe to
   cache, serialize, and share across concurrent executions.
7. As a framework developer, I want to subclass `Graph` to create
   reusable topology templates (e.g. `LoopGraph`, `LinearGraph`), so
   that common patterns are expressible without repeating edge
   declarations — even though Phase a ships no preset library itself.

### Node interface

8. As an agent developer, I want to implement a node with a single
   `execute(ctx) -> NodeResult` method, so that the node contract is
   simple and admits both pure-computation and side-effectful work.
9. As an agent developer, I want to implement `execute` as either
   `def` (sync) or `async def` (async), so that I can write simple
   computational nodes without async ceremony and I/O-bound nodes
   with proper async concurrency.
10. As an agent developer, I want the engine to automatically detect
    whether my node is sync or async and handle it uniformly, so that
    I don't have to choose between two different ABCs or wrap my node
    in adapters.
11. As an agent developer, I want my node to return a structured
    `NodeResult` with optional `transition`, `state_update`, and
    `command` fields, so that I can express routing, state mutation,
    and dynamic control flow in one typed return value.
12. As an agent developer, I want to be able to mutate state
    imperatively (`ctx.state.x = y`) for near-zero-change migration of
    existing ReAct code, so that the migration is mechanical rather
    than a rewrite.
13. As an agent developer, I want to be able to update state
    declaratively (`return NodeResult(state_update={"x": v})`) for
    future workflows that benefit from reducer-aware fan-in, so that
    both styles coexist on the same state object.

### Routing

14. As an agent developer, I want static edges (`add_edge(src, dst,
    reason)`) for fixed control flow, so that the graph topology is
    readable at build time.
15. As an agent developer, I want conditional edges
    (`add_conditional_edges(src, route_fn, destinations)`) for
    multi-candidate path selection, so that routing logic is
    declarative and decoupled from node names when desired.
16. As an agent developer, I want dynamic routing (`Command(goto=str)`)
    for runtime-decided next-node selection, so that nodes can branch
    on computed state without pre-declaring all branches.
17. As an agent developer, I want dynamic fan-out (`Command(goto=list[
    Task])`) for map-reduce patterns, so that I can express parallel
    workflows even though Phase a executes them sequentially.
18. As an agent developer, I want the four routing mechanisms to
    coexist with a strict priority resolution (`command.goto` >
    `transition` > `conditional edge` > `default edge`), so that
    routing is unambiguous and predictable.
19. As an agent developer, I want `Task` to carry an independent state
    per fan-out task, so that Phase c can upgrade sequential execution
    to parallel without changing node code.

### State management

20. As an agent developer, I want my state to be a Pydantic `BaseModel`
    subclass with `Annotated[T, ChannelSpec]` per-field declarations,
    so that state is type-safe and each field's update semantics
    (single-writer vs reducer) is declarative.
21. As an agent developer, I want exactly two channel types in Phase a
    (`LastValue` for single-writer, `ReducerChannel` for fan-in), so
    that the cognitive load is minimal while the extension seam
    (`BaseChannel` ABC) is available for future channel types.
22. As an agent developer, I want `state.checkpoint()` to automatically
    serialize every field via its channel, so that snapshot is
    per-channel and does not require 230 lines of hand-written payload
    flattening.
23. As an agent developer, I want `state.from_checkpoint(data)` to
    automatically restore every field via its channel, so that resume
    is symmetric with checkpoint and equally automated.
24. As an agent developer, I want channel codecs to use Pydantic's
    `model_dump()` / `model_validate()` universally, so that
    non-primitive state types serialize declaratively without
    hand-written per-type codecs.
25. As an agent developer, I want multi-write detection deferred to
    Phase c (since Phase a has no parallel execution), so that I am
    not constrained by single-writer semantics I don't need yet.

### AOP integration

26. As an agent developer, I want the engine to auto-invoke
    `runtime.before_node` / `after_node` at well-defined node-entry/exit
    points, so that the universal node-level lifecycle is handled
    uniformly without each node dispatching manually.
27. As an agent developer, I want to explicitly call
    `ctx.runtime.dispatch_hook(hook_point, ctx, data)` /
    `ctx.runtime.around(scope, ctx, body)` /
    `ctx.runtime.apply_governance(messages, ctx)` /
    `ctx.runtime.drain_control(ctx)` /
    `ctx.runtime.capture_snapshot(ctx, reason)` /
    `ctx.runtime.emit(event_type, data, ctx)` from my node, so that
    business-specific AOP (including iteration-level hooks like
    `BEFORE_ITERATION` / `AFTER_ITERATION`) is opt-in and the node body
    shows exactly what AOP it uses. Iteration hooks are NOT
    engine-auto-invoked because "iteration" is not a universal graph
    concept.
28. As an agent developer, I want `hook_point` / `scope` / `event_type`
    to be `str` at the engine boundary, so that the engine has zero
    dependency on business-specific enums.
29. As an agent developer, I want to define my own `StrEnum` for hook
    points / scopes / events in my business module and pass enum values
    to the runtime, so that my code is type-safe while the engine
    stays generic.
30. As an agent developer, I want a `ReactGraphRuntime` implementation
    that bridges `StrEnum` values to `modex_agent`'s `HookPoint` /
    `InterceptorScope` / `ReActEvent` enums, so that ReAct's existing
    AOP wiring works through the new interface.
31. As an agent developer, I want the default `GraphRuntime` to be
    all-no-op, so that a standalone graph user can run the engine with
    zero AOP wiring.
32. As an agent developer, I want `ctx.runtime` methods to be async
    only, so that AOP implementations (which are mostly async — hook
    dispatch, interceptor around, snapshot save) have a natural
    signature; sync nodes that don't need AOP simply don't call them.

### Interrupt and resume

33. As an agent developer, I want to call `ctx.interrupt(value)` to
    suspend execution for HITL approval, so that the suspend mechanism
    is a clean method call rather than a raw `raise`.
34. As an agent developer, I want `GraphInterrupt` to propagate as part
    of a formal `GraphBubbleUp` exception family (`GraphInterrupt` /
    `GraphDrained` / `ParentCommand`), so that the engine never
    swallows control-flow exceptions and the caller can distinguish
    suspend / shutdown / subgraph-routing.
35. As an agent developer, I want the interrupt model to be
    suspend-without-re-execution (already-applied state updates
    persist; resume re-enters at the next iteration, not by re-running
    the node body), so that I can write linear node code without
    worrying about idempotency across resume.
36. As an agent developer, I want resume to re-enter the engine with
    `Command(resume=value)`, so that the resume path is symmetric with
    the interrupt path and the resumed value is available to the node.
37. As an agent developer, I want all existing ReAct exit mechanisms
    preserved through migration (`GraphInterrupt` for approval;
    `max_iterations` / `turn_cancelled` / `llm_error` via static edges
    to END), so that ReAct behavior is identical before and after.

### Subgraph nesting (Graph-is-a-Node)

38. As an agent developer, I want `CompiledGraph` to be a subclass of
    `Node`, so that a graph can be used as a node in another graph
    (subgraph pattern) without adapter ceremony.
39. As an agent developer, I want a subgraph to share its parent's
    `GraphContext` (state / runtime / user_data), so that nesting is
    transparent and the subgraph can read/write parent state.
40. As an agent developer, I want a subgraph's terminal node to write
    results into `ctx.state.result`, so that the parent graph can
    branch on the subgraph's result via state inspection.
41. As an agent developer, I want the engine to return `ctx.state`
    from `run_async()`, so that the graph result is a typed state
    field rather than an untyped return value.

### Package isolation

42. As a framework developer, I want `modex_graph` to be a sibling
    package of `modex_agent` with its own `pyproject.toml` declaring
    only `pydantic` + stdlib as runtime dependencies, so that the
    engine is reusable outside `modex_agent` and the dependency
    direction is physically one-way.
43. As a framework developer, I want an architecture guard test that
    enforces no `modex_graph` file imports `modex_agent` or
    `examples/`, so that the isolation is a CI-checked fact, not a
    convention.
44. As a framework developer, I want a two-layer guard (grep-based +
    import-time `sys.modules` blocking in `conftest.py`), so that both
    static and dynamic import violations are caught.
45. As a standalone user, I want to `pip install modex_graph` and use
    the engine without installing `modex_agent`, so that the engine is
    a genuine independent library.

### ReAct migration

46. As a ReAct maintainer, I want `ReActTurnState` to become a
    `GraphState(BaseModel)` subclass with `Annotated[T, LastValue]`
    per-field declarations, so that snapshot is automated and the
    state is type-safe.
47. As a ReAct maintainer, I want `ReActSnapshotPolicy` to collapse
    from ~310 lines to ~50 lines via `state.checkpoint()` /
    `state.from_checkpoint()`, so that the hand-written payload
    flattening is eliminated.
48. As a ReAct maintainer, I want `LLMNode` / `ToolNode` / `EndNode`
    to shed AOP code (hook dispatch, control drain, governance,
    interceptor around, snapshot capture, emit) into `ReactGraphRuntime`,
    so that node bodies contain only node-specific business logic.
49. As a ReAct maintainer, I want `raise GraphInterrupt(snapshot)` in
    `ToolNode` to become `ctx.interrupt(tx)`, so that the suspend
    mechanism is uniform across all graph consumers.
50. As a ReAct maintainer, I want the ~30 sites of
    `ctx.runtime.state.x = y` to become `ctx.state.x = y`
    (mechanical rename), so that the migration is low-risk and
    reviewable.
51. As a ReAct maintainer, I want `before_iteration` / `after_iteration`
    hooks to remain as explicit `ctx.runtime.dispatch_hook(...)` calls
    in `LLMNode` (NOT engine-auto-invoked), so that hook timing is
    preserved by construction — the dispatch sites are node-controlled
    and identical before and after migration, eliminating the need for
    a hook timing parity test.
52. As a ReAct maintainer, I want `ExternalCodingAgent` to remain
    unchanged (not migrated to the graph), so that the subprocess
    streaming harness is not forced into a graph shape it doesn't fit.
53. As a ReAct maintainer, I want `ApprovalTransaction` /
    `ApprovalRequestState` / `ToolBatchState` / `ToolCallState` migrated
    from `@dataclass` to Pydantic `BaseModel` (NOT frozen — these are
    runtime state objects that the approval state machine mutates:
    `decisions` dict, `status` fields, `decision` fields), so that the
    universal Pydantic channel codec works without hand-written
    serializers while preserving the approval state machine's mutability.
54. As a ReAct maintainer, I want `ToolArguments` migrated to Pydantic
    `BaseModel(frozen=True)` (truly immutable leaf value-object), so
    that the universal codec works for this type too.
55. As a ReAct maintainer, I want `TurnSnapshot` / `TurnStateStore` /
    `ReActRuntimeStateCodec` / SQLite schema all unchanged, so that
    the persistence layer is untouched by the migration.
56. As a ReAct maintainer, I want the old `src/modex_agent/core/graph/`
    directory deleted after Stage 4, so that there is no duplicate
    graph engine in the codebase.

### Graph construction layering

57. As a framework developer, I want business modules to own their
    graph construction (e.g. ReAct builds its 4-node graph in
    `agents/react/`), so that the graph package never imports or knows
    about business graph builders.
58. As a framework developer, I want `Graph` to be subclassable for
    future preset topology templates, so that Phase c can introduce
    `LinearGraph` / `LoopGraph` / `MapReduceGraph` without engine
    changes — even though Phase a ships no presets.

### String typing

59. As an agent developer, I want node names, transition reasons, hook
    points, scopes, and event types to use `StrEnum` in business
    modules (e.g. `ReActNode` / `ReActReason` / `ReActHookPoint` /
    `ReActEvent` / `ReActScope`), so that business code is type-safe
    and free of hardcoded strings.
60. As an agent developer, I want the engine API to accept `str` for
    all these parameters, so that `StrEnum` values (which are `str`
    subclasses) satisfy the API without the engine importing business
    enums.

### Exit mechanisms and graph result

61. As an agent developer, I want the terminal node to write the graph
    result into `ctx.state.result` (a typed field), so that the result
    is checkpoint-friendly and type-safe.
62. As an agent developer, I want `GraphEngine.run_async()` to return
    `ctx.state`, so that I retrieve the result via `state.result`
    rather than via an untyped return value or a `custom` dict escape
    hatch.
63. As a ReAct maintainer, I want `custom[TurnCustomKey.GRAPH_RESULT]`
    replaced by an explicit `result: Annotated[AgentResult | None,
    LastValue] = None` field on `ReActTurnState`, so that the result
    access is typed and the `custom` dict escape hatch is no longer
    needed for this purpose.

### Phase c readiness

64. As a framework developer, I want Phase-a code returning
    `Command(goto=[Task(...)])` to run in parallel automatically once
    Phase c upgrades the engine, so that the upgrade is engine-only
    and node code is unchanged.
65. As a framework developer, I want `GraphDrained` and
    `ParentCommand` exception classes to exist in Phase a (even though
    never raised), so that the `GraphBubbleUp` family is complete and
    Phase c can wire them without adding new exception types.
66. As a framework developer, I want `BaseChannel` to be a public ABC
    with a clear extension contract, so that Phase c can add
    `Topic` / `EphemeralValue` / `NamedBarrierValue` channels when
    real second use cases appear.

## Implementation Decisions

### Module structure

- **New package `modex_graph`** at `src/modex_graph/`, sibling of
  `src/modex_agent/`. Own `pyproject.toml` with `pydantic` + stdlib
  only. Dependency direction: `modex_agent` → `modex_graph` (one-way).
- **`modex_graph` public surface**: `Graph`, `Node`, `CompiledGraph`,
  `GraphEngine`, `GraphContext`, `NodeResult`, `Command`, `Task`,
  `BaseChannel`, `LastValue`, `ReducerChannel`, `GraphState`,
  `GraphRuntime`, `GraphBubbleUp` family, `GraphNode` (START/END
  sentinels), `register_codec` / `Codec` (channel codec registration).
- **ReAct adapter layer** in `modex_agent/agents/react/`:
  `ReactGraphRuntime` (implements `GraphRuntime`), channel codec
  registrations, `build_react_graph()` builder.

### Node interface

Single ABC with one method. The method is declared `def` (not
`async def`); subclasses may override with `async def`. Engine unifies
via `inspect.isawaitable`.

```
Node[S](ABC):
    execute(ctx: GraphContext[S]) -> NodeResult   # def, not async def
```

This is the decision-rich type shape — single method, sync/async
polymorphism via `inspect.isawaitable`, no `SyncNode` / `AsyncNode`
split.

### NodeResult + Command + Task

`NodeResult` is a frozen Pydantic value object with three optional
fields: `transition: str | None`, `state_update: Mapping[str, Any] |
None`, `command: Command | None`.

`Command` is a frozen Pydantic value object bundling optional `goto`
(`str | list[str] | list[Task] | None`), `interrupt` (value to surface
to caller), `resume` (value to apply on resume). State updates live on
`NodeResult`, not on `Command` — separation of routing from state
mutation.

`Task` is a frozen Pydantic value object: `node: str`, `state: Any |
None = None`. Phase a: sequential execution. Phase c: parallel
execution, node code unchanged.

### State model

`GraphState` is a Pydantic `BaseModel` subclass. Each field annotated
with `Annotated[T, ChannelSpec]` is backed by a `BaseChannel`
instance. Fields without annotation default to `LastValue`.

```
GraphState(BaseModel):
    checkpoint() -> dict[str, JsonValue]         # per-channel
    from_checkpoint(data) -> Self                # per-channel
```

Exactly two channel types ship in Phase a:
- `LastValue` — single-writer semantics, default. Phase a does not
  enforce single-writer; Phase c enforces via `InvalidUpdateError`.
- `ReducerChannel(reducer: Callable[[Any, Any], Any])` — binary
  operator fan-in. Reducers are NOT required to be commutative.

`BaseChannel` ABC is the extension seam for Phase c channel types.

Dual-mode state access:
- Imperative: `ctx.state.x = y` mutates the Pydantic field directly.
  Snapshot syncs fields → channels before checkpoint.
- Declarative: `return NodeResult(state_update={"x": v})` — engine
  calls `channel.update([v])`, then syncs back to the field.

Channel codec: Pydantic `model_dump()` / `model_validate()` is the
universal codec. Non-primitive state types must be Pydantic `BaseModel`.

### Routing

Four coexisting mechanisms, strict priority:

1. `Command(goto=...)` (highest) — dynamic routing / fan-out
2. `transition: str` — static edge lookup
3. `add_conditional_edges(src, route_fn, destinations)` — multi-candidate
4. Default edge (`reason=None`) — fallback

`add_conditional_edges` accepts a `route_fn(state) -> str` and optional
`destinations: dict[str, str]` mapping. When `destinations=None`, the
return value is used directly as a node name; when provided, the return
value is a key mapped to a node name (decouples routing logic from
concrete node names).

Resolution algorithm: if `result.command.goto` is set, use it; else if
`result.transition` is set, look up static edge; else if conditional
edge exists for current node, call `route_fn`; else use default edge;
else raise `RoutingError`.

### GraphRuntime ABC

AOP bridge with no-op defaults. Engine auto-invokes 2 node-level
universal lifecycle points; nodes explicitly call 6 business-specific
methods. **No `before_iteration`/`after_iteration` on the ABC** —
"iteration" is a ReAct concept, not a universal graph concept;
iteration hooks are dispatched explicitly by ReAct nodes.

```
GraphRuntime(ABC):
    # Engine-auto-invoked (2, node-level universal):
    before_node(ctx, node_name) -> None
    after_node(ctx, node_name, result) -> None
    # Node-explicit (6, business-specific):
    dispatch_hook(hook_point: str, ctx, data: dict | None = None) -> None
    around(scope: str, ctx, body: Callable[[], Awaitable[Any]]) -> Any
    apply_governance(messages: list, ctx) -> list
    drain_control(ctx) -> None
    capture_snapshot(ctx, reason: str) -> None
    emit(event_type: str, data: Any, ctx) -> None
```

All methods are async (nodes that don't need AOP simply don't call
them). `hook_point` / `scope` / `event_type` are `str` at the engine
boundary; business modules define `StrEnum` and pass enum values.
`dispatch_hook`'s `data` is generic `dict | None` (NOT `HookPayload` —
the engine stays free of `modex_agent` types; `ReactGraphRuntime`
wraps `data` into `HookPayload` internally).

**`ReactGraphRuntime` bridges `GraphContext` to `AgentContext`**: all
methods receive `GraphContext` but extract `ctx.user_data` (which holds
`AgentContext`) and pass it to `hook_runner.dispatch` /
`interceptor_chain.around_*` / `governance.apply` /
`drain_control_channel`. Hook implementations receive `AgentContext`
unchanged — completely unaware of the migration. This is the agent
layer adapting to the graph engine's generic interface, not the graph
engine adapting to `AgentContext`.

### GraphContext subclassability

`GraphContext[S]` is a regular class (NOT Pydantic — holds runtime
objects per rule 12), subclassable for type-safe business access.
`ReActGraphContext(GraphContext[ReActTurnState])` adds `agent_ctx` /
`tool_manager` / `context_manager` properties, avoiding
`cast(AgentContext, ctx.user_data)` at every access site.

### `fork()` shared/isolated semantics

`ctx.fork(state=..., parent=...)` for `Task` fan-out: runtime shared
(turn-scoped AOP); user_data shared (turn context); state isolated if
passed (imperative mutations don't propagate; only `NodeResult.
state_update` merges via reducer).

### GraphBubbleUp exception family

```
GraphBubbleUp(Exception)
├── GraphInterrupt    # HITL suspend, raised by ctx.interrupt(value)
├── GraphDrained      # cooperative shutdown, Phase c only
└── ParentCommand     # subgraph→parent routing, Phase c only
```

Engine never swallows `GraphBubbleUp`. Suspend-without-re-execution
model: already-applied state updates persist; resume re-enters at next
iteration, not by re-running the node body.

### Graph-is-a-Node

`CompiledGraph[S]` is a subclass of `Node[S]`. Subgraph `execute(ctx)`
runs its own engine loop on the shared `ctx`. Subgraph terminal node
writes `ctx.state.result`; subgraph returns `NodeResult(transition=None)`
(parent graph follows default edge). Subgraph shares parent's state /
runtime / user_data.

### Graph result return

`GraphEngine.run_async(ctx) -> S` returns `ctx.state`. Terminal node
writes `ctx.state.result`. Caller reads `state.result`. ReAct's
`custom[TurnCustomKey.GRAPH_RESULT]` replaced by explicit
`result: Annotated[AgentResult | None, LastValue] = None` field on
`ReActTurnState`.

### compile() validation

Build-time checks: exactly one entry node; all edge sources/targets
exist; no dangling edges; node names unique; cycle detection optional
(warn default, raise if configured). `max_iterations` configured at
compile time (default 100).

### String typing convention

Engine API uses `str` for all string-typed parameters. Business modules
define `StrEnum` (`ReActNode`, `ReActReason`, `ReActHookPoint`,
`ReActEvent`, `ReActScope`) and pass enum values — `StrEnum` is a `str`
subclass, so values satisfy the API without engine-side imports.
`ReactGraphRuntime` maps business `StrEnum` values to `modex_agent`
enums (`HookPoint` / `InterceptorScope` / `ReActEvent`).

### Persistence path unchanged

`TurnSnapshot.state_payload: dict[str, JsonValue]` — unchanged.
`TurnStateStore.save_turn()` — unchanged. `ReActRuntimeStateCodec` —
unchanged (payload dict ↔ JSON bytes). SQLite schema — unchanged.
`ReActSnapshotPolicy` rewritten (net code reduction) but produces the
same payload structure.

### 5-stage migration plan

Each stage independently revertible. Each stage ends with green test
suite and no behavior change.

- **Stage 0**: `modex_graph` package lands with 8 core types + unit
  tests + architecture guard. ReAct untouched.
- **Stage 1**: `ReactGraphRuntime` + channel codec adapters. ReAct
  state types (`ApprovalTransaction` / `ToolBatchState` /
  `ToolCallState` / `ApprovalRequestState` / `ToolArguments`) migrated
  from `@dataclass` to Pydantic `BaseModel`. `ApprovalTransaction` /
  `ApprovalRequestState` / `ToolBatchState` / `ToolCallState` are NOT
  frozen (runtime state, mutated by approval state machine);
  `ToolArguments` IS frozen (immutable leaf value-object). ReAct still
  uses old `core/graph/`.
- **Stage 2**: `ReActTurnState` becomes `GraphState` subclass.
  `ReActSnapshotPolicy` collapses (~310 → ~50 lines). Snapshot
  round-trip parity verified.
- **Stage 3**: God node disassembly. AOP moves to `ctx.runtime.*`.
  `raise GraphInterrupt` → `ctx.interrupt(tx)`. `ctx.runtime.state.x`
  → `ctx.state.x` (~30 renames). Hook timing preserved by construction
  (iteration hooks remain node-controlled explicit dispatch, NOT
  engine-auto-invoked).
- **Stage 4**: ReAct switches to `modex_graph` engine. Old
  `src/modex_agent/core/graph/` deleted.
- **Stage 5**: Cleanup (separate PR). `SummarizerAgent` + related
  deprecated classes removed. Dead code cleanup.

Stages 0+1 may ship together; Stage 2 ships separately; Stages 3+4
ship together; Stage 5 is independent.

## Testing Decisions

### What makes a good test

Only test external behavior, not implementation details. For the graph
engine, "external behavior" means: given a graph topology + node
implementations + initial state, the engine produces the expected
final state and emits the expected events in the expected order.
Internal channel mechanics, sync/async detection, edge lookup
algorithms are NOT tested directly — they are exercised through
observable outcomes.

### Seam 1: ReAct full regression (highest seam, behavior preservation)

**Purpose**: verify the migration is behavior-preserving.

**Scope**: all existing ReAct tests in `tests/unit/agents/react/` and
`tests/integration/` must pass unchanged after each stage. This is the
single highest-value seam — if ReAct behavior (approval suspend/resume,
max_iterations, turn_cancelled, llm_error, snapshot serialize/deserialize
round-trip, hook dispatch timing) is identical before and after, the
migration is correct.

**Specialized tests within this seam**:
- *Snapshot round-trip parity* (Stage 2): serialize a `ReActTurnState`
  via the old `ReActSnapshotPolicy._build_payload()` and via the new
  `state.checkpoint()`, assert the payloads are equivalent. Proves the
  230→50 line simplification lost no data.
- *Approval state machine parity* (Stage 2 + 3): verify the full
  approval lifecycle (classify → suspend → external `apply_decision`
  mutates `ApprovalTransaction.decisions` → `_normalize_batch_decisions`
  rewrites `ALLOWED` to `PREEMPTED` → `replace_approval` persists →
  resume → `_resume_suspended_batch` reads mutated decisions → execute
  pre-approved, return errors for denied) works identically before and
  after. This is critical because `ApprovalTransaction` is mutable (not
  frozen) and the state machine depends on that mutability.
- *Hook timing* (Stage 3): hook timing is preserved by construction —
  iteration hooks remain node-controlled explicit dispatch (NOT
  engine-auto-invoked), so no hook timing parity test is needed. The
  dispatch sites are identical before and after migration.

**Prior art**: `tests/unit/agents/react/` (ReAct unit tests),
`tests/unit/memory/test_cleanup.py` (snapshot path tests),
`tests/integration/` (ReAct integration tests).

### Seam 2: `modex_graph` unit tests (new code, engine capability verification)

**Purpose**: verify the new engine's capabilities that ReAct does not
exercise.

**Scope**: unit tests for `modex_graph` covering — linear chain
topology; conditional branch topology; loop topology (with cycle guard);
HITL interrupt + resume; sync-only node execution; async-only node
execution; mixed sync+async node execution in one graph; channel
checkpoint round-trip (LastValue + ReducerChannel); `compile()` build-
time validation (dangling edge / missing entry / duplicate node name /
cycle warn); `Command(goto=str)` dynamic routing;
`Command(goto=list[str])` sequential multi-target;
`Command(goto=list[Task])` sequential fan-out with independent state;
`GraphRuntime` no-op default behavior; `GraphBubbleUp` exception
propagation (engine does not swallow).

**D12 Phase-a validation criterion**: these tests must exist and pass
for Phase-a merge. They are the proof that the engine is general, not
just ReAct-shaped.

**Prior art**: `tests/unit/` structure mirrors `src/` structure; new
`tests/unit/modex_graph/` directory follows the same convention.

### Seam 3: Architecture guard (structural, non-behavioral)

**Purpose**: enforce `modex_graph` package isolation.

**Scope**: two-layer guard in `tests/architecture/`:
- *Grep-based*: scan `src/modex_graph/` for any `from modex_agent` or
  `import modex_agent` or `from examples` — must return nothing.
- *Import-time*: `conftest.py` test that blocks `modex_agent` in
  `sys.modules` before importing any `modex_graph` submodule, proving
  the package loads without `modex_agent` available.

**Prior art**: `tests/architecture/` already has architecture guard
tests (ADR-0025 used this pattern to enforce no
`if execution_strategy ==` branches in pool_builder).

## Out of Scope

- **Phase c capabilities**: true parallel fan-out via `Task` (Phase a
  executes sequentially); `GraphDrained` cooperative shutdown (class
  exists, never raised); `ParentCommand` subgraph→parent routing
  (class exists, never raised); additional channel types (`Topic` /
  `EphemeralValue` / `NamedBarrierValue`); BSP superstep loop;
  graph-of-graphs migration of InputPipeline / Approval state machine
  / OutputRenderer. All recorded as Phase c future work in ADR-0033 D1.
- **Preset graph library**: `modex_graph/presets/` (`LinearGraph` /
  `LoopGraph` / `MapReduceGraph`) is NOT created in Phase a. `Graph`
  is subclassable (interface is preset-ready), but no presets ship
  until a real second consumer appears (ADR-0007).
- **Non-ReAct end-to-end example**: a Plan-Execute or Workflow example
  in `modex_graph/examples/` is NOT required for Phase-a merge. The
  D12 capability checklist is the design target; the example is
  deferred.
- **`ExternalCodingAgent` migration**: `ExternalCodingAgent` remains a
  subprocess streaming harness, NOT migrated to the graph engine. Its
  `ExecutionStrategy` / `TurnRunner` ABCs (ADR-0025) are unchanged.
- **`SummarizerAgent` removal**: tracked separately (ADR-0033 D10),
  executed in Stage 5 as an independent PR. Not part of the graph
  engine work.
- **Multi-write detection**: `LastValue` raising `InvalidUpdateError`
  on ≥2 writes per superstep is deferred to Phase c (no parallel
  execution in Phase a → no multi-write scenarios).
- **`@dataclass` → Pydantic migration of types outside ReAct state**:
  only `ApprovalTransaction` / `ToolBatchState` / `ToolCallState` /
  `ApprovalRequestState` / `ToolArguments` are migrated (required for
  the channel codec). Other `@dataclass` types in the codebase are
  untouched.

## Further Notes

- **ADR-0033** (`docs/adr/0033-generalized-graph-engine.md`) is the
  authoritative design document. This PRD synthesizes the ADR's
  decisions into a spec format; the ADR contains the full rationale,
  rejected alternatives, and Open Questions for implementation.
- **CONTEXT.md** has been updated with new domain vocabulary: Graph
  Engine, Node, NodeResult, Command, Channel, GraphRuntime,
  GraphBubbleUp. The "ReAct Agent" and "Graph" entries were corrected
  to reflect the actual current state (ReAct is one configuration of
  the graph engine, not the only agent runtime; ExternalCodingAgent
  does not use the graph).
- **Highest implementation risk**: Stage 2's snapshot simplification
  (310 → 50 lines via per-channel checkpoint). Mitigation: snapshot
  round-trip parity test (Seam 1 specialized test) comparing old
  `_build_payload()` output to new `state.checkpoint()` output.
- **Second highest risk**: Stage 1's `@dataclass` → Pydantic
  `BaseModel` migration of ReAct state types, especially preserving
  mutability for `ApprovalTransaction` / `ToolBatchState` /
  `ToolCallState` (the approval state machine depends on mutating
  these). Mitigation: convert one type at a time with focused
  regression tests; approval state machine parity test (Seam 1)
  verifies the full lifecycle; Pydantic v2's `arbitrary_types_allowed
  =True` is the escape hatch for fields holding non-Pydantic types.
- **Hook timing risk eliminated**: the original design had
  `before_iteration`/`after_iteration` engine-auto-invoked (highest
  risk). Revised to keep them as node-controlled explicit dispatch —
  timing is preserved by construction, no parity test needed.
- **Architecture rules respected**: ADR-0007 (real seams — the graph
  engine has one current consumer but a real seam with ≥2 prospective
  Phase-c consumers); ADR-0025 (ExecutionStrategy / TurnRunner
  unchanged — the graph engine is a tool TurnRunner may use, not a
  replacement); ADR-0028~0031 (persistence schema unchanged —
  TurnSnapshot / TurnStateStore / codec / SQLite schema all preserved);
  ADR-0016 (ReAct loop detection coexists with engine cycle guard —
  different concerns, no conflict).
- **Domain vocabulary used throughout**: Graph Engine, Node,
  NodeResult, Command, Task, Channel, GraphState, GraphRuntime,
  GraphBubbleUp, GraphInterrupt, CompiledGraph, TurnSnapshot,
  TurnStateStore, ExecutionStrategy, TurnRunner, AgentPipeline,
  ReActTurnState, ApprovalTransaction.
