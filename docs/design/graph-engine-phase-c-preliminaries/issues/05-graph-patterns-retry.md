# 05 — graph_patterns: retry

**What to build:** A reusable retry graph pattern module under `examples/graph_patterns/`, proving `modex_graph`'s retained API (self-loop edges + `transition`-based exit) composes into a realistic retry-with-backoff workflow.

The module provides two forms of retry, because they exercise different parts of the retained API:

1. **`RetryNode[S](body: Node[S], max_retries: int, is_failure: Callable[[NodeResult], bool])`** — synchronous retry within a single `execute` call. The node calls `body.execute(ctx)` up to `max_retries + 1` times. If `is_failure(result)` returns `False`, the node returns the successful `NodeResult` immediately. If all attempts fail, the node returns the last `NodeResult` with `transition="failed"`. This form exercises `Node` composition (a `Node` wrapping another `Node`) without involving graph topology.

2. **`build_retry_graph(body_node: Node[S], max_retries: int, is_failure: Callable[[NodeResult], bool], counter_state_field: str)`** — topology retry via self-loop. The body node is wired to itself with `transition="retry"` (self-loop edge), and to `GraphNode.END` with `transition="success"` and `transition="failed"`. A counter field in state tracks attempt count; when `counter >= max_retries`, the body node returns `transition="failed"` instead of `transition="retry"`. This form exercises self-loop edges, `transition`-based routing, and `max_iterations` as a panic safety net (set to `max_retries + 5` to allow the loop plus margin).

`tests/unit/examples/graph_patterns/test_retry.py` verifies:

- `RetryNode` succeeds on first attempt when `is_failure` returns `False` (no retry).
- `RetryNode` retries up to `max_retries` times, then succeeds on a later attempt.
- `RetryNode` exhausts `max_retries` and returns `transition="failed"`.
- `build_retry_graph` self-loop topology exits correctly on success (`transition="success"` → END).
- `build_retry_graph` self-loop topology exits correctly on failure after exhausting retries (`transition="failed"` → END).
- `build_retry_graph` does not raise `GraphRecursionError` when `max_iterations` is set appropriately above `max_retries`.

This ticket uses only `modex_graph` API that is already unit-tested (`Node` composition, self-loop edges, `transition` routing, `max_iterations`) — it does not depend on tickets 01/02/03/04. The pattern is an example, not framework code.

**Blocked by:** None — can start immediately. Uses existing retained API.

**Status:** ready-for-agent

- [ ] `examples/graph_patterns/retry.py` provides `RetryNode[S](body, max_retries, is_failure)` synchronous retry within one `execute` call
- [ ] `examples/graph_patterns/retry.py` provides `build_retry_graph(body_node, max_retries, is_failure, counter_state_field)` topology retry via self-loop
- [ ] `examples/graph_patterns/__init__.py` extended to export `RetryNode` and `build_retry_graph`
- [ ] `tests/unit/examples/graph_patterns/test_retry.py` verifies `RetryNode` succeeds on first attempt (no retry)
- [ ] `tests/unit/examples/graph_patterns/test_retry.py` verifies `RetryNode` retries then succeeds on a later attempt
- [ ] `tests/unit/examples/graph_patterns/test_retry.py` verifies `RetryNode` exhausts `max_retries` and returns `transition="failed"`
- [ ] `tests/unit/examples/graph_patterns/test_retry.py` verifies `build_retry_graph` exits correctly on success
- [ ] `tests/unit/examples/graph_patterns/test_retry.py` verifies `build_retry_graph` exits correctly on failure after exhausting retries
- [ ] `tests/unit/examples/graph_patterns/test_retry.py` verifies `build_retry_graph` does not raise `GraphRecursionError` when `max_iterations` is set appropriately
