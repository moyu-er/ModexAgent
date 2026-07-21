# 06 — graph_patterns: map_reduce

**What to build:** A reusable MapReduce graph pattern module under `examples/graph_patterns/`, proving `modex_graph`'s retained API (`Command(goto=list[Task])` fan-out + `ReducerChannel` fan-in + `ctx.fork` state isolation) composes into a realistic split → fan-out → reduce workflow. This is the most complex of the three patterns because it exercises the API surface that Phase c's parallel fan-out would build on (today the fan-out is sequential, but the declaration shape is identical).

The module provides:

1. **`MapNode[S](items_fn: Callable[[S], list], worker_node: str, state_fn: Callable[[Any], S])`** — a `Node[S]` whose `execute` reads `items_fn(ctx.state)` to get a list of items, then returns `NodeResult(command=Command(goto=[Task(node=worker_node, state=state_fn(item)) for item in items]))`. Each `Task` carries an independent state (constructed by `state_fn`) so imperative mutations in one worker do not propagate to siblings — only `NodeResult.state_update` merges back to the parent via `ReducerChannel`.

2. **`ReduceNode[S](reducer: Callable[[list], Any], source_field: str, result_field: str)`** — a `Node[S]` whose `execute` reads the `ReducerChannel`-backed `source_field` (which has accumulated all workers' `state_update` contributions), applies `reducer(values)`, and writes the result to `result_field` via imperative mutation (`setattr(ctx.state, result_field, reduced_value)`).

3. **An example graph builder `build_map_reduce_graph(...)`** that wires: `START → map → worker (self-loop via Command.goto list[Task]) → reduce → END`. The worker node is a simple `Node` that processes one item and returns `NodeResult(state_update={source_field: [processed_result]})`. The `ReducerChannel` on `source_field` folds all workers' contributions into a single list that `ReduceNode` reads.

`tests/unit/examples/graph_patterns/test_map_reduce.py` verifies:

- `MapNode` emits `Command(goto=list[Task])` with the correct number of tasks (one per item from `items_fn`).
- Each `Task` carries an independent state (constructed by `state_fn`) — imperative mutations in one worker do not appear in another worker's state.
- `ReducerChannel` on the source field accumulates all workers' `state_update` contributions in order.
- `ReduceNode` reads the accumulated list, applies `reducer`, and writes the result to `result_field`.
- A complete split → fan-out → reduce graph produces the expected final aggregated state (e.g. map a list of numbers → each worker squares its number → reduce sums the squares).

This ticket uses `modex_graph` API that is already unit-tested (`Command.goto=list[Task]`, `Task(state=...)`, `ctx.fork(state=...)`, `ReducerChannel`, `NodeResult.state_update`) — see `tests/unit/modex_graph/test_routing.py::TestCommandGotoListTask` for prior art. It does not depend on tickets 01/02/03/04/05. The pattern is an example, not framework code.

**Note on parallelism:** Phase a executes `list[Task]` sequentially. This pattern demonstrates the *declaration* shape of fan-out/fan-in; the *execution* shape (parallel via `asyncio.gather`) is a Phase c item (ADR-0033 D12) and is out of scope. The pattern works correctly today because `ReducerChannel` folds contributions regardless of execution order (for order-sensitive reducers, the test uses order-preserving operations).

**Blocked by:** None — can start immediately. Uses existing retained API.

**Status:** ready-for-agent

- [ ] `examples/graph_patterns/map_reduce.py` provides `MapNode[S](items_fn, worker_node, state_fn)` emitting `Command(goto=list[Task])` for fan-out
- [ ] `examples/graph_patterns/map_reduce.py` provides `ReduceNode[S](reducer, source_field, result_field)` aggregating via `ReducerChannel`
- [ ] `examples/graph_patterns/map_reduce.py` provides `build_map_reduce_graph(...)` example split → fan-out → reduce topology
- [ ] `examples/graph_patterns/__init__.py` extended to export `MapNode`, `ReduceNode`, and `build_map_reduce_graph`
- [ ] `tests/unit/examples/graph_patterns/test_map_reduce.py` verifies `MapNode` emits correct task count
- [ ] `tests/unit/examples/graph_patterns/test_map_reduce.py` verifies each `Task` carries independent state (imperative mutations don't cross workers)
- [ ] `tests/unit/examples/graph_patterns/test_map_reduce.py` verifies `ReducerChannel` accumulates all workers' `state_update` contributions in order
- [ ] `tests/unit/examples/graph_patterns/test_map_reduce.py` verifies `ReduceNode` reads accumulated list, applies reducer, writes to `result_field`
- [ ] `tests/unit/examples/graph_patterns/test_map_reduce.py` verifies complete split → fan-out → reduce graph produces expected final aggregated state
