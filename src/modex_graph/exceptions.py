# ruff: noqa: ANN401

"""Graph engine exception family.

Two layers:

- `GraphBubbleUp` — the cooperative-control exception family. The engine
  NEVER swallows these; they propagate to the caller. Subclasses:
    - `GraphInterrupt` — HITL suspend. Raised by `ctx.interrupt(value)`.
      Suspend-without-re-execution semantics: already-applied state updates
      persist; resume re-enters from the entry node, NOT by re-running the
      interrupted node body.
    - `GraphDrained` — cooperative shutdown. Class exists but is never
      raised; wiring is deferred (ADR-0034 D10 termination does not use it).
    - `ParentCommand` — subgraph→parent routing. Class exists but is never
      raised; wiring is deferred (ADR-0033 D12 Phase c item 2).
    - `InvalidUpdateError` — multiple concurrent writes to the same
      `LastValue` channel in one generation. Raised by `LastValue.update`
      when `len(values) > 1`. See ADR-0033 D4.

- Routing / recursion errors:
    - `RoutingError` — raised when the engine cannot resolve a next node
      (no matching `Command.goto`, transition, or default edge).
    - `GraphRecursionError` — raised when the engine-level `max_iterations`
      safety net is exceeded (abnormal exit, prevents infinite loops).
"""

from __future__ import annotations

from typing import Any


class GraphBubbleUp(Exception):  # noqa: N818
    """Base class for cooperative-control exceptions the engine never swallows.

    Subclasses: `GraphInterrupt`, `GraphDrained`, `ParentCommand`,
    `InvalidUpdateError`. The engine propagates these to the caller verbatim
    — never caught and silenced. See ADR-0033 D7.
    """


class GraphInterrupt(GraphBubbleUp):
    """HITL suspend. Raised by `ctx.interrupt(value)`.

    Suspend-without-re-execution model: already-applied state updates and
    side effects persist across the interrupt boundary. Resume re-enters
    the graph at the entry node; the interrupted node body is NOT re-run.
    Re-entry routing is driven by `state.resume_target`: the entry node
    reads it and routes via `Command(goto=...)`.

    The `value` carries the interrupt payload (e.g. an `ApprovalTransaction`
    awaiting human decision). The caller (e.g. `ReActAgent.run()`) inspects
    `value` to determine resume semantics.
    """

    def __init__(self, value: Any = None, *, node_name: str = "") -> None:
        super().__init__(str(value) if value is not None else "interrupt")
        self.value = value
        self.node_name = node_name


class GraphDrained(GraphBubbleUp):
    """Cooperative shutdown signal.

    The class exists but is never raised. ADR-0034 realized Phase c via
    continuous scheduling (not BSP supersteps), so there are no superstep
    boundaries to wire this at. Termination is driven by the ready/active
    sets being empty (ADR-0034 D10). Cooperative shutdown wiring (e.g.
    SIGTERM-style graceful drain with checkpoint preservation) remains
    deferred. See ADR-0033 D7 + D1.
    """


class ParentCommand(GraphBubbleUp):
    """Subgraph→parent routing.

    The class exists but is never raised. Wiring is deferred to the
    graph-of-graphs / subroutine exercise (ADR-0033 D12 Phase c item 2,
    ADR-0034 Out of Scope). When exercised, a subgraph's `execute` raises
    this to redirect its parent graph. See ADR-0033 D7 + D1.
    """


class InvalidUpdateError(GraphBubbleUp):
    """Raised when multiple concurrent writes target the same `LastValue` channel.

    Per ADR-0033 D4: `LastValue` enforces single-writer semantics. When ≥2
    concurrent instances write the same `LastValue` field in one generation,
    `LastValue.update(values)` with `len(values) > 1` raises this error.
    Callers should use `ReducerChannel` for fan-in, or restructure the graph
    so only one instance writes each `LastValue` field per generation.

    This is a `GraphBubbleUp` subclass: the engine propagates it to the
    caller verbatim (never caught and silenced). The caller can catch it as
    `InvalidUpdateError`, `GraphBubbleUp`, or `Exception`.

    Raised by:
    - `WriteConflictDetector.commit()` when two same-generation instances
      write the same field.
    - `LastValue.update(values)` when `len(values) > 1` (the batch merge
      path, still used by `GraphState.apply_concurrent_updates`).
    """


class RoutingError(Exception):
    """Raised when the engine cannot resolve the next node.

    Two-layer routing model (ADR-0034 D12): `Command.goto` (dynamic layer)
    is tried first, then `transition` matched against static edges, then the
    default edge (`reason=None`). If none matches, `RoutingError` is raised.
    """


class GraphRecursionError(Exception):
    """Raised when the engine-level `max_iterations` safety net is exceeded.

    This is an ABNORMAL exit — it prevents infinite loops. Distinct from
    the node-level graceful exit (nodes check business iteration count and
    return `transition=...` to route to END via static edge, producing a
    normal result). Both coexist; engine-level N should be larger than
    business max (e.g. business 25, compile 100). See ADR-0033 D9.3.
    """
