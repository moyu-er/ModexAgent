# ruff: noqa: ANN401

"""Graph engine exception family.

Two layers:

- `GraphBubbleUp` — the cooperative-control exception family. The engine
  NEVER swallows these; they propagate to the caller. Subclasses:
    - `GraphInterrupt` — HITL suspend. Raised by `ctx.interrupt(value)`.
      Suspend-without-re-execution semantics: already-applied state updates
      persist; resume re-enters from the entry node, NOT by re-running the
      interrupted node body.
    - `GraphDrained` — cooperative pause/stop signal raised by scheduler
      safe points when `GraphRunControl` receives an external request.
    - `ParentCommand` — subgraph→parent routing. Class exists but is never
      raised; wiring is deferred (ADR-0033 D12 Phase c item 2).

- Routing / recursion errors:
    - `RoutingError` — raised when the engine cannot resolve a next node
      (node did not deliver and no default edge exists).
    - `GraphRecursionError` — raised when the engine-level `max_iterations`
      safety net is exceeded (abnormal exit, prevents infinite loops).
"""

from __future__ import annotations

from typing import Any


class GraphBubbleUp(Exception):  # noqa: N818
    """Base class for cooperative-control exceptions the engine never swallows.

    Subclasses: `GraphInterrupt`, `GraphDrained`, `ParentCommand`. The engine
    propagates these to the caller verbatim
    — never caught and silenced. See ADR-0033 D7.
    """


class GraphInterrupt(GraphBubbleUp):
    """HITL suspend. Raised by `ctx.interrupt(value)`.

    Suspend-without-re-execution model: already-applied state updates and
    side effects persist across the interrupt boundary. Resume re-enters
    the graph at the entry node; the interrupted node body is NOT re-run.
    Re-entry routing is driven by `state.resume_target`: the entry node
    reads it and routes via `deliver(content, target, ctx)`.

    The `value` carries the interrupt payload (e.g. an `ApprovalTransaction`
    awaiting human decision). The caller (e.g. `ReActAgent.run()`) inspects
    `value` to determine resume semantics.
    """

    def __init__(self, value: Any = None, *, node_name: str = "") -> None:
        super().__init__(str(value) if value is not None else "interrupt")
        self.value = value
        self.node_name = node_name


class GraphDrained(GraphBubbleUp):
    """Cooperative pause/stop signal raised at scheduler safe points."""


class ParentCommand(GraphBubbleUp):
    """Subgraph→parent routing.

    The class exists but is never raised. Wiring is deferred to the
    graph-of-graphs / subroutine exercise (ADR-0033 D12 Phase c item 2,
    ADR-0034 Out of Scope). When exercised, a subgraph's `execute` raises
    this to redirect its parent graph. See ADR-0033 D7 + D1.
    """


class RoutingError(Exception):
    """Raised when the engine cannot resolve the next node.

    Deliver-only routing: nodes must call ``deliver(content, next_node, ctx)``
    during ``execute()``. If a node produces no delivers and has no downstream
    edges, ``RoutingError`` is raised.

    Subclass ``UndeliveredError`` is raised specifically when a node exhausts
    its ``max_retry`` without delivering — schedulers catch this to produce a
    FAILED outcome rather than CRASHED. Topology ``RoutingError``\\s (ambiguous
    routing, missing topology, invalid dispatch target) are NOT subclasses and
    propagate as CRASHED.
    """


class UndeliveredError(RoutingError):
    """Raised when a node exhausts ``max_retry`` without delivering.

    Subclass of ``RoutingError`` so existing ``isinstance(e, RoutingError)``
    checks still match. The ``LinearScheduler`` and ``ParallelScheduler``
    catch this specifically (not bare ``RoutingError``) to convert a dead-end
    graph into a normal return with ``ctx.reached_end = False``, which the
    orchestrator maps to ``GraphInstanceStatus.FAILED``.

    Topology errors (``_resolve_default_target`` ambiguous routing,
    ``validate_dispatch_target`` invalid edge, missing topology) raise plain
    ``RoutingError`` and propagate as CRASHED — they are NOT caught by the
    schedulers.
    """


class GraphRecursionError(Exception):
    """Raised when the engine-level `max_iterations` safety net is exceeded.

    This is an ABNORMAL exit — it prevents infinite loops. Distinct from
    the node-level graceful exit (nodes check business iteration count and
    deliver to END, producing a normal result). Both coexist; engine-level
    N should be larger than business max (e.g. business 25, compile 100).
    See ADR-0033 D9.3.
    """


class InvocationStateError(Exception):
    """Raised when a CAS (compare-and-swap) transition on a node invocation fails.

    The strict lifecycle methods (``complete_invocation``,
    ``suspend_invocation``, ``cancel_invocation``) update the
    ``node_states`` row only if it is in the ``running`` state with
    ``suspended=0``. If the row is already terminal (``completed`` /
    ``canceled`` / ``crashed``) or suspended, the UPDATE affects 0 rows
    and this exception is raised — indicating a lost race or a duplicate
    transition attempt.
    """
