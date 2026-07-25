# Parallel Scheduling Engine

Status: ready-for-agent

> **Implementation note (post-ADR-0034 revision):** This PRD was written before the continuous scheduling redesign (ADR-0034 D2-D20). The execution model has been updated from batch-barrier (`asyncio.gather`) to continuous scheduling (`asyncio.create_task` + `asyncio.wait(FIRST_COMPLETED)`). See ADR-0034 D2/D7/D8/D17-D20 for the authoritative current design. The user stories and routing model in this PRD remain accurate; only the execution loop and merge mechanism have changed.

## Problem Statement

The Graph Engine (`modex_graph`) currently uses a sequential scheduler: a single `current` node pointer walks the graph one node at a time. When a node fans out to multiple targets (`Command(goto=list[Task])`), the engine executes them sequentially via `for task: await _execute_task(...)`. When a node has multiple incoming edges, there is no join semantics — the engine cannot wait for all predecessors. Conditional branches that skip one arm can leave downstream joins deadlocked or prematurely fired.

Users building complex agent workflows (map-reduce, parallel sub-tasks, fan-out/fan-in pipelines) cannot express parallel execution. They also cannot decouple task dispatch from node return — an agent running inside a node (via modexctl CLI) cannot trigger downstream work mid-execution. The engine's five-level routing priority chain (`Command.goto` → `pending` queue → `transition` → `route_fn` → default edge) is confusing and conflates conditional routing (`route_fn`) with static-edge routing (`transition`), where one silently shadows the other.

## Solution

Introduce a `Scheduler` ABC with two implementations:

- **`LinearScheduler`** (default) — the current Phase a sequential behavior, extracted as-is. Zero behavior change. ReAct and all existing graph patterns use this with zero code changes.
- **`ParallelScheduler`** (opt-in via `Graph.compile(scheduler="parallel")`) — a continuous scheduling, multi-instance, fork-based parallel scheduling engine with:
  - Per-node configurable trigger modes (`ON_ALL_PREDS` / `ON_RECEIVE`)
  - Reachability-based readiness judgment (prevents premature joins, handles skipped branches)
  - `ctx.dispatch(target, payload)` as the framework-level dispatch primitive, with `NodeResult.transition` / `Command.goto` as declarative syntax sugar
  - Multi-instance model: each node execution creates an independent instance (`node#seq`) with a forked state snapshot; loops and multi-triggers produce new instances, not state resets
  - Channel-semantics state merge (`LastValue` multi-write raises `InvalidUpdateError`; `ReducerChannel` folds)
  - A dispatch interface designed for remote invocation (future modexctl IPC adapter)

Simplify the routing model to two layers (`Command.goto` → `transition` + static edges), removing the `route_fn` conditional-edge mechanism and the `list[str]` form of `Command.goto`.

## User Stories

### Scheduler selection

1. As a graph builder, I want to compile a graph with the default sequential scheduler, so that my existing ReAct / linear / conditional / loop graphs work with zero changes.
2. As a graph builder, I want to compile a graph with `scheduler="parallel"`, so that I can use parallel fan-out, fan-in, and dispatch.
3. As a graph builder, I want the scheduler choice to be a compile-time decision on `CompiledGraph`, so that `GraphEngine` delegates transparently and I don't change my engine construction code.

### Parallel fan-out

4. As a graph builder, I want a node to fan out to multiple targets simultaneously via static edges with the same `reason`, so that all matching targets execute in parallel.
5. As a graph builder, I want a node to fan out via `Command(goto=[Task(node="A"), Task(node="B")])`, so that A and B execute in parallel.
6. As a graph builder, I want `Task(node="B", state=None)` to mean "share the main state snapshot", so that simple fan-out doesn't require constructing independent states.
7. As a graph builder, I want `Task(node="B", state=<independent>)` to mean "isolated state", so that parallel workers don't interfere via imperative mutations.
8. As a graph builder, I want `Command(goto="single_node")` (single str) to work as before, so that simple dynamic routing is unchanged.

### Fan-in / join

9. As a graph builder, I want a node with multiple incoming edges to wait for all *activated* predecessors before executing (default `ON_ALL_PREDS`), so that a join node sees all upstream contributions.
10. As a graph builder, I want "activated predecessor" to mean "a predecessor that actually executed and dispatched to this node", so that a conditional branch skipping one arm does not deadlock the join.
11. As a graph builder, I want a node to fire once per dispatch received (`ON_RECEIVE`), so that N predecessors each triggering it produce N independent executions.
12. As a graph builder, I want to set a graph-level default trigger mode, so that I don't have to annotate every node individually.
13. As a graph builder, I want to override the trigger mode on individual nodes, so that one join node can differ from the graph default.

### Reachability-based readiness

14. As a graph builder, I want the scheduler to prevent a join node from firing while any active instance (PENDING, READY, or RUNNING) can still reach it via outgoing edges, so that a long chain (A→E→F→D) that is still running does not cause D to fire prematurely after only its direct predecessor (B) completed.
15. As a graph builder, I want the reachability check to be conservative (BFS over all outgoing edges, regardless of actual routing), so that correctness is never violated for the sake of performance.
16. As a graph builder, I want the reachability check to re-evaluate after every instance completes or becomes ready, so that a previously blocked node becomes eligible as soon as all blocking paths clear.

### Dispatch interface

17. As a node author, I want to call `ctx.dispatch(target, state_update)` inside `execute`, so that I can send work to a downstream node with a payload.
18. As a node author, I want `ctx.dispatch` to validate that `target` is reachable via the current node's outgoing edges, so that I cannot accidentally dispatch to a node with no declared edge.
19. As a node author, I want `NodeResult.transition` to be compiled into dispatch calls automatically, so that I can use the declarative style for simple routing without calling `ctx.dispatch` manually.
20. As a node author, I want `NodeResult.command=Command(goto=...)` to be compiled into dispatch calls automatically, so that dynamic routing works declaratively.
21. As a node author, I want to mix `ctx.dispatch` and `NodeResult.transition` in the same execution, so that I can dispatch to some targets manually and let the framework route others.
22. As a node author, I want dispatch to take effect immediately (not deferred to `execute` return), so that downstream work can start while my node is still running.
23. As a node author, I want to not call `ctx.dispatch` or return a transition and have that be legal (silent skip), so that a node that delegates dispatch to an external agent (via modexctl) is not penalized.

### Payload and state visibility

24. As a node author, I want the dispatch payload (`state_update`) to be merged into `ctx.state` via channel semantics, so that my node reads a single unified state without a separate payload API.
25. As a node author, I want an empty payload (no `state_update`) to naturally fall back to reading the shared main state, so that simple nodes that don't use `state_update` work without changes.
26. As a node author, I want `ON_ALL_PREDS` to fold all activated predecessors' payloads through channel semantics (`LastValue` multi-write raises `InvalidUpdateError`; `ReducerChannel` folds), so that join nodes get merged upstream data.
27. As a node author, I want `ON_RECEIVE` to merge the triggering predecessor's payload into state before each execution, so that each execution sees the cumulative state plus the new input.

### Multi-instance model

28. As a graph builder, I want each node execution to create an independent instance with a unique ID (`node#seq`), so that loops produce fresh state rather than resetting.
29. As a graph builder, I want `ON_RECEIVE` multi-trigger to produce multiple concurrent instances of the same node, so that N dispatches yield N independent executions.
30. As a graph builder, I want each instance to receive a forked (deep-copied) state snapshot at the moment it becomes READY, so that concurrent instances do not interfere via imperative mutations.
31. As a graph builder, I want only `NodeResult.state_update` to merge back to the main state after an instance completes, so that imperative mutations on forks are isolated.
32. As a graph builder, I want the fork to be skipped when only one instance is ready and no other instance is running (fast path), so that simple sequential graphs pay zero copy overhead.

### State merge

33. As a graph builder, I want the main state to be the single accumulation point for all completed instances' `state_update`, so that new instances fork from the latest state.
34. As a graph builder, I want `LastValue` channels to raise `InvalidUpdateError` when ≥2 concurrent instances write the same field, so that silent data loss is prevented.
35. As a graph builder, I want `ReducerChannel` to fold all concurrent writes via its reducer, so that fan-in produces aggregated results.
36. As a graph builder, I want reducers to not be required to be commutative (order-dependent results are my responsibility), so that I can use order-sensitive reducers when needed.

### Routing simplification

37. As a graph builder, I want routing to be two layers (`Command.goto` override → `transition` + static edges), so that I don't deal with a five-level priority chain.
38. As a graph builder, I want `route_fn` conditional edges removed, so that all conditional routing is expressed via `transition` + static edges or `Command.goto`.
39. As a graph builder, I want multiple static edges with the same `reason` to all fire (multi-target fan-out), so that I can declare parallel branches declaratively.
40. As a graph builder, I want `transition=None` to fall back to default edges (`reason=None`), so that simple nodes without explicit transitions work.
41. As a graph builder, I want `Command.goto` to accept `str | list[Task] | None` (not `list[str]`), so that the multi-target form always carries explicit state semantics.
42. As a graph builder, I want `Task(node="B", state=None)` to replace the old `list[str]` form, so that shared-state fan-out is expressed uniformly.

### Termination

43. As a graph builder, I want the graph to terminate when the ready set is empty and no instances are active, so that completion is natural and doesn't require every branch to reach an END sentinel.
44. As a graph builder, I want `GraphNode.END` to have implicit `ON_ALL_PREDS` semantics (all activated END sources must complete), so that a fan-out where one branch reaches END does not terminate the graph prematurely.
45. As a graph builder, I want nodes with outgoing edges that choose not to dispatch to be legal (silent skip), so that conditional skipping doesn't block termination.

### Compile validation

46. As a graph builder, I want compile to validate that every node is reachable from START, so that dangling nodes are caught early.
47. As a graph builder, I want compile to validate that every node has a path to END, so that orphan branches are caught early.
48. As a graph builder, I want compile to reject `add_conditional_edges` calls, so that the removed `route_fn` mechanism is caught at graph construction time.

### Iteration and errors

49. As a graph builder, I want `max_iterations` to count every node instance execution, so that the safety net scales with actual work.
50. As a graph builder, I want a node exception (non-`GraphBubbleUp`) to cancel all concurrent instances and propagate, so that a fatal error stops the entire graph.
51. As a graph builder, I want a `GraphInterrupt` to propagate immediately and cancel other concurrent instances, so that HITL suspension works under parallelism.
52. As a graph builder, I want `InvalidUpdateError` (from `LastValue` multi-write) to be a `GraphBubbleUp` subclass, so that it propagates through the same exception family.

### Concurrency safety

53. As a graph builder, I want `before_node` / `after_node` hooks to be called concurrently from parallel instances, so that AOP works under parallelism.
54. As a graph builder, I want `ctx.emit` to support concurrent calls, so that streaming events from parallel nodes don't block each other.
55. As a runtime implementor, I want to audit and fix `ReactGraphRuntime` hook implementations for concurrency safety, so that parallel execution doesn't cause races in the AOP layer.

### modexctl readiness

56. As an agent author, I want `ctx.dispatch(target, state_update)` to have a stable, serializable interface, so that a future IPC adapter can expose the same contract to the modexctl CLI.
57. As an agent author, I want dispatch events to be persisted by the default implementation, so that crash recovery and modexctl-driven workflows have a durable record.

### Backward compatibility

58. As a ReAct user, I want my existing 4-node ReAct graph to work with zero code changes, so that the parallel scheduling engine doesn't break production.
59. As a graph pattern user, I want my existing conditional / retry / map-reduce patterns to work with `LinearScheduler` (default), so that I'm not forced to migrate.
60. As a graph pattern user, I want my map-reduce pattern to produce correct results under `ParallelScheduler`, so that I can opt into parallelism when ready.

## Implementation Decisions

### Type safety: enums and structured types (no hardcoded strings)

All string literals used in the scheduling design are replaced by enums or frozen Pydantic models:

- **`SchedulerKind`** (`StrEnum`): `LINEAR` / `PARALLEL`. Used in `Graph.compile(scheduler=...)` and stored on `CompiledGraph`.
- **`NodeTrigger`** (`StrEnum`): `ON_ALL_PREDS` / `ON_RECEIVE`. Used as the `trigger` attribute on `Node` and as the `default_trigger` on `CompiledGraph`.
- **`NodeInstanceStatus`** (`StrEnum`): `DORMANT` / `PENDING` / `READY` / `RUNNING` / `COMPLETED`. Used in the instance state machine.
- **`DispatchEvent`** (`BaseModel`, frozen): `source_instance: str`, `target: str`, `payload: dict[str, Any] | None`. The recorded unit of a dispatch.
- **`NodeInstance`** (regular class, not Pydantic — holds runtime state per rule 12): `instance_id: str`, `node_name: str`, `seq: int`, `status: NodeInstanceStatus`, `forked_state: GraphState | None`. Identified by `{node_name}#{seq}`.
- **`InvalidUpdateError`** (`GraphBubbleUp` subclass): raised by `LastValue.update` when `len(values) > 1`.

### Modules to build/modify

**`src/modex_graph/` (new and modified files):**

- `scheduler/` package (new, split into `base.py`, `linear.py`, `instance.py`, `parallel.py`) — `Scheduler` ABC + `LinearScheduler` (extracted from current `engine.py`) + `ParallelScheduler`.
- `engine.py` (modified) — `GraphEngine` delegates to `Scheduler`; the current `run_async` / `run` / `_resolve_next` / `_execute_task` logic moves into `LinearScheduler`.
- `node.py` (modified) — `Node` gains `trigger: NodeTrigger = NodeTrigger.ON_ALL_PREDS` attribute.
- `result.py` (modified) — `Command.goto` type changes from `str | list[str] | list[Task] | None` to `str | list[Task] | None`. `NodeResult` docstring updated (two fields, not three — `transition` and `state_update`; `command` is the routing override).
- `graph.py` (modified) — `add_conditional_edges` removed. `ConditionalEdge` dataclass removed. `compile()` gains `scheduler: SchedulerKind = SchedulerKind.LINEAR` and `default_trigger: NodeTrigger = NodeTrigger.ON_ALL_PREDS` parameters. Compile validation adds START/END reachability checks (for `PARALLEL` mode) and rejects `add_conditional_edges` calls.
- `compiled_graph.py` (modified) — gains `scheduler: SchedulerKind` and `default_trigger: NodeTrigger` fields. Edge lookup helpers updated: `next_nodes_by_transition` returns `list[str]` (all matches, not first). `default_edge_targets` returns `list[str]` (all). `conditional_for` removed.
- `context.py` (modified) — `GraphContext` gains `dispatch(target: str, state_update: dict[str, Any] | None = None)` method. The method validates target against the current node's outgoing edges and records a `DispatchEvent`.
- `channel.py` (modified) — `LastValue.update` raises `InvalidUpdateError` when `len(values) > 1`.
- `exceptions.py` (modified) — `InvalidUpdateError(GraphBubbleUp)` added.
- `constants.py` (modified) — `SchedulerKind` and `NodeTrigger` enums (or in a new `enums.py` if constants.py is for sentinels only).
- `__init__.py` (modified) — export new public types: `Scheduler`, `LinearScheduler`, `ParallelScheduler`, `SchedulerKind`, `NodeTrigger`, `NodeInstanceStatus`, `DispatchEvent`, `InvalidUpdateError`.

**`src/modex_agent/agents/react/` (no changes):**

- ReAct uses `LinearScheduler` (default). `build_react_graph()` compiles without `scheduler=` parameter. Zero code changes.

**`examples/graph_patterns/` (no changes for LinearScheduler):**

- Existing patterns compile with default `scheduler=LINEAR`. Zero code changes.
- `map_reduce.py` can be tested under `PARALLEL` mode in tests without modifying the pattern itself (the `Command(goto=list[Task])` declaration shape upgrades automatically).

**`tests/unit/modex_graph/` (new and modified):**

- `helpers.py` (extended) — new state types and node helpers for parallel testing.
- `test_parallel_topologies.py` (new) — fan-out, fan-in, trigger modes, multi-instance, dispatch.
- `test_parallel_routing.py` (new) — transition multi-target, `Command.goto=list[Task]`, `list[str]` rejection.
- `test_parallel_errors.py` (new) — `InvalidUpdateError`, `max_iterations`, node exception cancellation, `GraphInterrupt` cancellation.
- `test_compile_validation.py` (extended) — START/END reachability, `route_fn` removal.
- `test_routing.py` (modified) — remove `route_fn` tests, update `Command.goto=list[str]` tests to expect rejection.

**`tests/architecture/` (extended):**

- Architecture guard test verifying `add_conditional_edges` / `ConditionalEdge` are deleted.
- Architecture guard test verifying `Command.goto` does not accept `list[str]`.

### ParallelScheduler internal architecture

The `ParallelScheduler` maintains:

- `main_state: GraphState` — the single graph-level state. Instances fork from it; completed instances merge `state_update` into it.
- `instances: dict[str, NodeInstance]` — all instances by ID, across the graph's lifetime.
- `instance_seq: int` — global counter for instance IDs.
- `dispatch_log: list[DispatchEvent]` — all dispatch events recorded (source, target, payload).
- `end_sources: set[str]` — instance IDs that dispatched to `GraphNode.END`. Graph terminates when all activated END sources are COMPLETED.
- `active: set[str]` — instance IDs in PENDING / READY / RUNNING status. Used as BFS starting set for reachability.

**Execution loop:**

1. Entry node becomes instance `entry#0`, status READY.
2. While `ready` is non-empty or `active` is non-empty:
   a. Pop all READY instances.
   b. If only one READY and no RUNNING → fast path: execute directly on `main_state`, no fork.
   c. Otherwise → each READY instance forks `main_state` and starts as an independent `asyncio.create_task`. The scheduler waits for any to complete via `asyncio.wait(FIRST_COMPLETED)`, merges `state_update` immediately (atomic segment: commit + apply_state_update + advance + complete), then launches any newly-READY instances.
   d. For each completed instance: merge `state_update` into `main_state` via `apply_state_update`. Compile `NodeResult.transition` / `Command.goto` into dispatch events. Mark COMPLETED.
   e. For each dispatch event: validate target is in source's outgoing edges. Update target's instance state (create new instance if needed, record dispatch, check trigger mode, check reachability).
   f. Re-evaluate readiness for all PENDING instances.
3. When `ready` is empty and `active` is empty: terminate.

**Reachability check (`can_reach(start_instances, target) -> bool`):**

BFS over outgoing static edges starting from all active instances (PENDING ∪ READY ∪ RUNNING). If any path reaches the target node, the target is not ready (something might still dispatch to it). This is conservative — it checks declared edges, not actual routing decisions.

**Trigger mode evaluation:**

- `ON_ALL_PREDS`: For target node N, collect all dispatch events targeting N. Group by source node. If every activated source node (a node that has at least one dispatch to N) has at least one dispatch in the current group, and no active instance can reach N → N is ready. Create one instance consuming this group.
- `ON_RECEIVE`: Each dispatch event targeting N immediately creates a new instance (if no active instance can reach N). Multiple dispatches produce multiple instances.

### `ON_ALL_PREDS` grouping semantics

When a node N has two incoming edges (`A→N`, `B→N`) and both A and B execute multiple times (A#0, A#1, B#0, B#1), dispatches are grouped:

- Group 1: A#0's dispatch + B#0's dispatch → N#0 executes.
- Group 2: A#1's dispatch + B#1's dispatch → N#1 executes.

If A#1 arrives but B#1 hasn't, N#1 waits. This is "per-group pairing" — each source contributes one dispatch per group, and a group is complete when all activated sources have contributed.

Implementation: maintain a per-target pending-dispatch queue. Each source's dispatch is appended. When the queue has at least one dispatch from every activated source, consume one from each source to form a group and trigger an instance.

### `LinearScheduler` — no dispatch, no fork, no instances

`LinearScheduler` preserves the exact Phase a behavior:

- Single `current: str` pointer.
- `_resolve_next` returns a single next node (first matching edge).
- `Command.goto=list[Task]` executes tasks sequentially via `for task: await _execute_task(...)`.
- `pending: list[str]` queue for `Command.goto=list[str]` — **removed**. `list[str]` is no longer a valid `Command.goto` type. If `list[str]` is encountered, raise `RoutingError`.
- No `ctx.dispatch` method on `GraphContext` (the method exists but is a no-op / raises `NotImplementedError` when `LinearScheduler` is active — or better, `GraphContext.dispatch` checks the scheduler kind and raises a clear error).
- `route_fn` / `add_conditional_edges` — removed from `Graph`. Calling `add_conditional_edges` raises `AttributeError` or a clear `RuntimeError`.

Wait — `route_fn` removal affects `LinearScheduler` too. Existing tests that use `add_conditional_edges` (in `test_engine_topologies.py`) will break. These tests must be migrated to use `transition` + static edges (which is how ReAct already works). This is a breaking change documented in ADR-0034.

### `Command.goto` type change

The `Command.goto` field type changes from `str | list[str] | list[Task] | None` to `str | list[Task] | None`. Pydantic validation rejects `list[str]` at construction time. Existing code that returns `Command(goto=["A", "B"])` must change to `Command(goto=[Task(node="A", state=None), Task(node="B", state=None)])`.

Verified: no internal code uses `Command(goto=list[str])` — the only `list` form in the codebase is `list[Task]` in `map_reduce.py`.

### `ctx.dispatch` interface

```python
class GraphContext[S: GraphState]:
    def dispatch(
        self,
        target: str,
        state_update: dict[str, Any] | None = None,
    ) -> None:
        """Dispatch work to a downstream node.

        Validates target is reachable via the current node's outgoing
        edges. Records a DispatchEvent. Updates target readiness.

        Only available under ParallelScheduler. Raises RuntimeError
        under LinearScheduler.
        """
```

The `target` parameter is a node name (string). It is not an enum because node names are user-defined. The validation (target must be in the current node's outgoing edge targets) prevents arbitrary dispatch.

### `Graph.compile` signature

```python
def compile(
    self,
    max_iterations: int = 100,
    *,
    cycle_detection: str = "warn",
    scheduler: SchedulerKind = SchedulerKind.LINEAR,
    default_trigger: NodeTrigger = NodeTrigger.ON_ALL_PREDS,
) -> CompiledGraph:
```

`default_trigger` is only used when `scheduler=PARALLEL`. It is ignored (but not rejected) when `scheduler=LINEAR`.

## Testing Decisions

### What makes a good test

Tests exercise the **public graph API** (`Graph` → `compile` → `GraphEngine.run_async` → assert `ctx.state`) and assert **external behavior**, not implementation details (instance IDs, BFS internals, dispatch log contents). The only exception is `InvalidUpdateError` which is an observable exception type.

### Test modules

1. **`tests/unit/modex_graph/test_parallel_topologies.py`** (new) — parallel fan-out, fan-in, trigger modes, multi-instance loops, dispatch-driven routing. Prior art: `test_engine_topologies.py` (same structure, different scheduler).

2. **`tests/unit/modex_graph/test_parallel_routing.py`** (new) — transition multi-target, `Command.goto=list[Task]` parallel, `list[str]` rejection, `route_fn` removal. Prior art: `test_routing.py`.

3. **`tests/unit/modex_graph/test_parallel_errors.py`** (new) — `InvalidUpdateError` from concurrent `LastValue` writes, `max_iterations` under parallelism, node exception cancels concurrent instances, `GraphInterrupt` cancels concurrent instances. Prior art: `test_exceptions.py`, `test_engine_topologies.py::TestLoopGuard`.

4. **`tests/unit/modex_graph/test_compile_validation.py`** (extended) — START reachability, END reachability, `add_conditional_edges` rejection. Prior art: existing `test_compile_validation.py`.

5. **`tests/unit/modex_graph/test_routing.py`** (modified) — remove `route_fn` tests, update `Command.goto=list[str]` tests to expect `ValidationError`.

6. **`tests/unit/modex_graph/test_engine_topologies.py`** (modified) — migrate `route_fn`-based conditional tests to `transition` + static edges.

7. **`tests/unit/examples/graph_patterns/test_map_reduce.py`** (extended) — add a `scheduler="parallel"` variant that verifies the same map-reduce produces correct results under parallel execution.

8. **`tests/architecture/test_no_route_fn.py`** (new) — AST guard verifying `add_conditional_edges` and `ConditionalEdge` are absent from `src/modex_graph/`.

9. **Existing ReAct tests** — unmodified, run under `LinearScheduler` (default).

### Test helper extensions

`tests/unit/modex_graph/helpers.py` gains:

- `DispatchNode` — a node that calls `ctx.dispatch(target, state_update)` inside `execute`.
- `PayloadState` — a state with `ReducerChannel` fields for fan-in testing.
- `TrackingRuntime` — a `GraphRuntime` subclass that records `before_node` / `after_node` calls for concurrency-safety testing.
- `make_parallel_ctx` — builds a `GraphContext` with a state and no-op runtime, ready for `ParallelScheduler`.

## Out of Scope

- **modexctl IPC adapter** — the dispatch interface is designed to be remotely callable, but the actual IPC transport (HTTP / Unix socket / stdin) is future work. This spec only ensures the `ctx.dispatch` contract is stable and serializable.
- **Dispatch persistence (remote store)** — `InMemoryDispatchStore` and `SqliteDispatchStore` are implemented (Task 09). Remote / distributed dispatch stores (Redis, HTTP-backed) are future work.
- **`GraphDrained` wiring** — the exception class exists; wiring it for cooperative shutdown (SIGTERM-style graceful drain) is deferred. ADR-0034 uses continuous scheduling (no superstep boundaries), so termination is driven by the ready/active sets being empty (D10).
- **Subroutine / Graph-of-graphs exercising** — the Graph-is-a-Node type wiring exists; exercising it with real subgraph patterns is deferred.
- **`ReactGraphRuntime` concurrency audit** — flagged as needed (D14), but the actual audit and fixes happen during implementation, not in this spec.
- **`Topic` / `EphemeralValue` / `NamedBarrierValue` channels** — not needed; reachability-based readiness replaces `NamedBarrierValue`, and `ReducerChannel` covers fan-in.
- **BSP superstep model** — explicitly rejected (ADR-0034 Rejected alternatives). The ready-set + fast-path approach is not BSP. See ADR-0034 Rejected alternatives for the BSP vs continuous scheduling comparison.
- **Copy-on-write state optimization** — fork deep-copies `GraphState`. COW is a future optimization if profiling shows it's needed.

## Further Notes

### Relationship to ADR-0033 and ADR-0034

This spec implements ADR-0034 (Parallel Scheduling Engine), which supplements ADR-0033 (Generalized Graph Engine) as the Phase c realization. ADR-0033 Phase a (`LinearScheduler`) remains the default and is unchanged. ADR-0034 D12 (routing simplification) applies to both schedulers — `route_fn` removal and `Command.goto` type change are global.

### Breaking changes

Two breaking changes affect any external code (none found internally):

1. `add_conditional_edges` / `ConditionalEdge` removed — use `transition` + static edges instead.
2. `Command(goto=list[str])` rejected — use `Command(goto=[Task(node="X", state=None)])` instead.

Internal code is verified clean: ReAct uses `transition` + static edges only; `map_reduce.py` uses `list[Task]` only; `conditional.py` / `retry.py` use `transition` + static edges only.

### Migration path for existing tests

`test_engine_topologies.py` has 4 tests using `add_conditional_edges`. They migrate to `transition` + static edges:

- `test_conditional_direct_mode` → node returns `NodeResult(transition="high")`, edges `add_edge("decide", "high", reason="high")` + `add_edge("decide", "low", reason="low")`.
- `test_conditional_key_mapped_mode` → same pattern, the "key mapping" is inlined into the node's transition logic.
- `test_conditional_routes_to_end` → `add_edge("decide", GraphNode.END, reason="done")`.

### Fork performance

`GraphState` is a Pydantic `BaseModel`. Deep-copying it via `model_copy(deep=True)` is O(field count × field size). For `ReActTurnState` (messages list + a dozen scalars), this is negligible. The fast path (single instance, no concurrency) skips the copy entirely. If profiling later shows fork cost is significant for large states, copy-on-write or field-level diffing can be added without changing the interface.
