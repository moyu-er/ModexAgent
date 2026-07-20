# ruff: noqa: ANN401

"""Graph engine exception family.

Two layers:

- `GraphBubbleUp` — the cooperative-control exception family. The engine
  NEVER swallows these; they propagate to the caller. Subclasses:
    - `GraphInterrupt` — HITL suspend. Raised by `ctx.interrupt(value)`.
      Suspend-without-re-execution semantics: already-applied state updates
      persist; resume re-enters from the entry node, NOT by re-running the
      interrupted node body.
    - `GraphDrained` — cooperative shutdown at superstep boundary.
      Phase-a: class exists, never raised.
    - `ParentCommand` — subgraph→parent routing. Phase-a: class exists,
      never raised.

- Routing / recursion errors:
    - `RoutingError` — raised when the engine cannot resolve a next node
      (no matching edge, conditional, or default).
    - `GraphRecursionError` — raised when the engine-level `max_iterations`
      safety net is exceeded (abnormal exit, prevents infinite loops).
"""

from __future__ import annotations

from typing import Any


class GraphBubbleUp(Exception):  # noqa: N818
    """Base class for cooperative-control exceptions the engine never swallows.

    Subclasses: `GraphInterrupt`, `GraphDrained`, `ParentCommand`.
    The engine propagates these to the caller verbatim — never caught and
    silenced. See ADR-0033 D7.
    """


class GraphInterrupt(GraphBubbleUp):
    """HITL suspend. Raised by `ctx.interrupt(value)`.

    Suspend-without-re-execution model: already-applied state updates and
    side effects persist across the interrupt boundary. Resume re-enters
    the graph at the entry node; the interrupted node body is NOT re-run.
    Resume logic is carried by graph topology (e.g. ReAct's StartNode
    detects suspended state and routes to TOOL).

    The `value` carries the interrupt payload (e.g. an `ApprovalTransaction`
    awaiting human decision). The caller (e.g. `ReActAgent.run()`) inspects
    `value` to determine resume semantics.
    """

    def __init__(self, value: Any = None, *, node_name: str = "") -> None:
        super().__init__(str(value) if value is not None else "interrupt")
        self.value = value
        self.node_name = node_name


class GraphDrained(GraphBubbleUp):
    """Cooperative shutdown at superstep boundary.

    Phase-a: the class exists but is never raised. Phase-c wires it at
    superstep boundaries to support SIGTERM-style cooperative shutdown
    with checkpoint preservation. See ADR-0033 D7 + D1 (Phase c deferred).
    """


class ParentCommand(GraphBubbleUp):
    """Subgraph→parent routing.

    Phase-a: the class exists but is never raised. Phase-c wires it for
    cross-graph routing when a subgraph needs to redirect its parent.
    See ADR-0033 D7 + D1 (Phase c deferred).
    """


class RoutingError(Exception):
    """Raised when the engine cannot resolve the next node.

    The four routing mechanisms (Command.goto > transition > conditional
    edge > default edge) are tried in strict priority order. If none
    matches, `RoutingError` is raised. See ADR-0033 D6.
    """


class GraphRecursionError(Exception):
    """Raised when the engine-level `max_iterations` safety net is exceeded.

    This is an ABNORMAL exit — it prevents infinite loops. Distinct from
    the node-level graceful exit (nodes check business iteration count and
    return `transition=...` to route to END via static edge, producing a
    normal result). Both coexist; engine-level N should be larger than
    business max (e.g. business 25, compile 100). See ADR-0033 D9.3.
    """
