# 01 — `modex_graph` package + engine primitives

**What to build:** A standalone `src/modex_graph/` package (sibling of `src/modex_agent/`) providing a generalized graph engine with all core primitives. The package depends only on Pydantic + standard library. End-to-end demoable: a simple graph executes with typed Pydantic state, supports both sync and async nodes in the same graph, supports HITL interrupt + resume, supports four routing mechanisms (static edge / conditional edge / dynamic goto / fan-out via `Task`), supports per-channel checkpoint round-trip, and is validated by `compile()` at build time. The package is genuinely framework-agnostic — an architecture guard test enforces zero `modex_agent` imports.

**Blocked by:** None — can start immediately.

**Status:** completed (commit 9be20d83)

## Acceptance criteria

- [ ] `src/modex_graph/` exists with its own `pyproject.toml` declaring only `pydantic` (and stdlib) as runtime dependencies
- [ ] `modex_agent`'s `pyproject.toml` lists `modex_graph` as a dependency; the reverse is forbidden
- [ ] Public surface exports: `Graph`, `Node`, `CompiledGraph`, `GraphEngine`, `GraphContext`, `NodeResult`, `Command`, `Task`, `BaseChannel`, `LastValue`, `ReducerChannel`, `GraphState`, `GraphRuntime`, `GraphBubbleUp`, `GraphInterrupt`, `GraphDrained`, `ParentCommand`, `GraphNode` (START/END sentinels), `register_codec`, `Codec`, `RoutingError`, `GraphRecursionError`
- [ ] `Node[S]` ABC has single `execute(ctx) -> NodeResult` method declared as `def` (not `async def`); subclasses may override with `async def`
- [ ] `GraphEngine.run_async(ctx) -> S` and `GraphEngine.run(ctx) -> S` both work; engine unifies sync/async node implementations via `inspect.isawaitable`
- [ ] `Graph.compile(max_iterations=100) -> CompiledGraph` validates: exactly one entry node, all edge sources/targets exist, no dangling edges, node names unique; cycle detection optional (warn default, raise if configured)
- [ ] **`max_iterations` has two layers**: (1) engine-level safety net — `compile(max_iterations=N)` sets a hard recursion limit, exceeding it raises `GraphRecursionError` (abnormal exit, prevents infinite loops); (2) node-level graceful exit — nodes check business iteration count and return `transition=...` to route to END via static edge (produces normal `AgentResult` with appropriate `stop_reason`). Both coexist; engine-level N should be larger than business max (e.g. business 25, compile 100)
- [ ] `CompiledGraph[S]` is a subclass of `Node[S]` (Graph-is-a-Node type wiring); subgraph `execute(ctx)` runs its own engine loop on the shared `ctx`
- [ ] `GraphState(BaseModel)` provides `checkpoint() -> dict[str, JsonValue]` and `from_checkpoint(data) -> Self`; per-field channel declaration via `Annotated[T, ChannelSpec]`
- [ ] `LastValue` and `ReducerChannel` channels ship; `BaseChannel` ABC is the public extension seam
- [ ] Four routing mechanisms coexist with strict priority: `Command(goto=...)` > `transition` > conditional edge > default edge
- [ ] `Command.goto` accepts `str | list[str] | list[Task] | None`; `Task(node, state)` carries independent state for fan-out
- [ ] `add_conditional_edges(src, route_fn, destinations)` supports both direct-node-name and key-mapped modes
- [ ] `GraphRuntime` ABC has 2 engine-auto-invoked methods (`before_node`/`after_node` — node-level universal lifecycle) + 6 node-explicit methods (`dispatch_hook(hook_point, ctx, data: dict | None = None)`/`around(scope, ctx, body)`/`apply_governance(messages, ctx)`/`drain_control(ctx)`/`capture_snapshot(ctx, reason)`/`emit(event_type, data, ctx)`); all default to no-op
- [ ] `GraphRuntime` methods are async-only; `hook_point`/`scope`/`event_type` parameters are `str` (business modules pass `StrEnum` values); `dispatch_hook`'s `data` is generic `dict | None` (NOT `HookPayload` — the engine stays free of `modex_agent` types; `ReactGraphRuntime` wraps `data` into `HookPayload` internally)
- [ ] **`GraphRuntime` does NOT have `before_iteration`/`after_iteration`** — "iteration" is not a universal graph concept (linear/conditional graphs have no iterations). Iteration-level hooks are dispatched explicitly by ReAct nodes via `ctx.runtime.dispatch_hook(ReActHookPoint.BEFORE_ITERATION, ctx)`, preserving current dispatch sites and timing exactly
- [ ] `GraphContext[S]` is a regular class (NOT Pydantic) and is **subclassable** so business modules can add type-safe accessors (e.g. `ReActGraphContext` with `agent_ctx`/`tool_manager`/`context_manager` properties)
- [ ] `GraphContext` provides `state: S`, `runtime: GraphRuntime`, `user_data: Any`, `fork(state=..., parent=...)` for subtask isolation, `emit(event_type, data)` and `interrupt(value)` helpers
- [ ] `fork()` semantics documented: runtime shared (turn-scoped AOP), user_data shared (turn context), state isolated if passed (imperative mutations don't propagate; only `NodeResult.state_update` merges via reducer)
- [ ] `GraphBubbleUp` exception family: `GraphInterrupt` (raised by `ctx.interrupt(value)`), `GraphDrained` (class exists, never raised in Phase a), `ParentCommand` (class exists, never raised in Phase a)
- [ ] Engine never swallows `GraphBubbleUp`; `ctx.interrupt(value)` raises `GraphInterrupt` with suspend-without-re-execution semantics
- [ ] `run_async(ctx)` re-entry semantics: always starts from entry node; resume logic is carried by graph topology (e.g. ReAct's StartNode detects suspended state and routes to TOOL). The engine itself is stateless across `run_async` calls — no internal "resume context"
- [ ] `GraphEngine.run_async(ctx)` returns `ctx.state` (terminal node writes `ctx.state.result`)
- [ ] Channel codec: `register_codec(type, codec)` API; Pydantic `model_dump()`/`model_validate()` is the universal codec for `BaseModel` subclasses
- [ ] Architecture guard test in `tests/architecture/` enforces: (a) no file under `src/modex_graph/` imports `modex_agent` or `examples/` (grep-based); (b) `conftest.py` blocks `modex_agent` in `sys.modules` before importing any `modex_graph` submodule (import-time)
- [ ] Unit tests in `tests/unit/modex_graph/` cover: linear chain topology; conditional branch; loop with cycle guard; HITL interrupt + resume; sync-only node; async-only node; mixed sync+async nodes in one graph; `LastValue` checkpoint round-trip; `ReducerChannel` checkpoint round-trip; `compile()` validation (dangling edge / missing entry / duplicate name / cycle warn); `Command(goto=str)` dynamic routing; `Command(goto=list[str])` sequential multi-target; `Command(goto=list[Task])` sequential fan-out with independent state; `GraphRuntime` no-op default; `GraphBubbleUp` propagation (engine does not swallow); subgraph-as-node execution
- [ ] `modex_agent` is untouched — old `src/modex_agent/core/graph/` remains in place, ReAct still uses it
