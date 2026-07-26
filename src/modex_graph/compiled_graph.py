"""`CompiledGraph[S]` — frozen, validated graph; subclass of `Node[S]`.

Per ADR-0033 D8: `Graph` is-a `Node`. `Graph.compile()` returns a
`CompiledGraph`, which is a `Node[S]` subclass. This enables:

- Subgraph patterns (outer turn graph embeds inner agent graph).
- Reusable graph fragments as nodes.
- The "graph-of-graphs" / "图套图" target.

The type relationship is wired (ADR-0033 D8) but no consumer exercises
it yet. Nested subgraph patterns (outer turn graph embeds inner agent
graph) are deferred to ADR-0033 D12 Phase c item 5.

`CompiledGraph.execute(ctx)` runs its own `GraphEngine` loop on `ctx`,
sharing the parent context's state, runtime, and user_data. The subgraph
writes its result to `ctx.state` (a field on the state, per D9.3) and
returns a `NodeResult(transition=...)` for the parent graph to route on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from typing_extensions import TypeVar

from .constants import NodeTrigger, SchedulerKind
from .node import Node

if TYPE_CHECKING:
    from .context import GraphContext
    from .graph import Edge
    from .result import NodeResult
    from .state import GraphState

# CompiledGraph is generic over the same state type as Node. Defined locally
# (mirroring graph.py / engine.py) because TypeVars cannot be aliased across
# modules — `S = NodeS` is not a valid type alias target per mypy.
S = TypeVar("S", bound="GraphState")


@dataclass(frozen=True, kw_only=True)
class CompiledGraph(Node[S]):
    """Frozen, validated graph. Subclass of `Node[S]` (Graph-is-a-Node, D8).

    Constructed by `Graph.compile()`. Holds the validated node registry,
    edges, entry node, and `max_iterations` safety net.

    As a `Node`: `execute(ctx)` runs its own `GraphEngine` loop on `ctx`,
    returning a `NodeResult` for the parent graph to route on. The subgraph
    writes its result to `ctx.state` (a field on the state) — the engine
    returns `ctx.state` after the inner loop reaches END.

    Note: `@dataclass(frozen=True)` on a `Generic[S]` subclass is supported
    in Python 3.12+. The frozen dataclass enforces immutability of the
    graph topology after `compile()`.

    `default_trigger` (Task 06) is the graph-level default trigger mode for
    `ParallelScheduler`. A per-node `Node.trigger` (when not `None`) overrides
    it for that node.
    """

    name: str
    nodes: dict[str, Node[Any]]
    edges: list[Edge]
    entry_node: str
    max_iterations: int = 1000
    scheduler: SchedulerKind = SchedulerKind.LINEAR
    default_trigger: NodeTrigger = NodeTrigger.ON_ALL_PREDS

    async def execute(self, ctx: GraphContext[Any]) -> NodeResult:
        """Run this graph as a node. Delegates to `GraphEngine.run_async`.

        This is an `async def` override of `Node.execute` (which is declared
        `def`). The parent engine's `inspect.isawaitable` detects and awaits
        the returned coroutine. This is the dual-mode design from D2/D3.

        The subgraph shares `ctx.state` / `ctx.runtime` / `ctx.user_data`
        with the parent. The subgraph's terminal node writes its result to
        a state field; the parent reads it after this `execute` returns.
        """
        # Imported here to avoid a circular import at module load.
        from .engine import GraphEngine
        from .result import NodeResult as _NodeResult

        engine: GraphEngine[Any] = GraphEngine(self)
        await engine.run_async(ctx)
        # The subgraph writes its result to ctx.state.<some_field>.
        # The parent graph reads it. We return an empty NodeResult —
        # the parent routes via its own edges, not via
        # the subgraph's return value.
        return _NodeResult()

    # ── Edge lookup helpers (used by GraphEngine) ──────────────────────

    def edges_from(self, source: str) -> list[Edge]:
        """Return all edges originating from `source`, in declaration order."""
        return [e for e in self.edges if e.source == source]

    # ── Plural forms (fan-out: all matching targets) ───────────────────

    def next_nodes_by_transition(self, source: str, transition: str) -> list[str]:
        """Return ALL targets whose edge `reason` matches `transition`.

        Fan-out: multiple edges with the same `reason` all fire under
        `ParallelScheduler`. Returns targets in declaration order.
        """
        return [e.target for e in self.edges_from(source) if e.reason == transition]

    def default_edge_targets(self, source: str) -> list[str]:
        """Return ALL default (reason=None) edge targets from `source`.

        Fan-out: multiple default edges all fire under `ParallelScheduler`.
        Returns targets in declaration order.
        """
        return [e.target for e in self.edges_from(source) if e.reason is None]

    # ── Singular forms (first match; used by LinearScheduler) ──────────

    def next_node_by_transition(self, source: str, transition: str) -> str | None:
        """Return the FIRST target matching `transition`, or None.

        Used by `LinearScheduler` (sequential — one target per transition).
        For fan-out, use `next_nodes_by_transition`.
        """
        targets = self.next_nodes_by_transition(source, transition)
        return targets[0] if targets else None

    def default_edge_target(self, source: str) -> str | None:
        """Return the FIRST default (reason=None) edge target, or None.

        Used by `LinearScheduler` (sequential — one default target).
        For fan-out, use `default_edge_targets`.
        """
        targets = self.default_edge_targets(source)
        return targets[0] if targets else None


__all__ = ["CompiledGraph", "S"]
