"""`CompiledGraph[S]` — validated graph; subclass of `Node[S]`.

Per ADR-0033 D8: `Graph` is-a `Node`. `Graph.compile()` returns a
`CompiledGraph`, which is a `Node[S]` subclass. This enables:

- Subgraph patterns (outer turn graph embeds inner agent graph).
- Reusable graph fragments as nodes.
- The "graph-of-graphs" / "图套图" target.

`CompiledGraph.execute(ctx)` runs its own `GraphEngine` loop on `ctx`,
sharing the parent context's state, runtime, and user_data. The subgraph
writes its result to `ctx.state` (a field on the state, per D9.3).

`CompiledGraph` is a regular class (NOT a frozen dataclass). Per rule 12:
runtime objects holding state and connections are NOT covered by the
frozen-Pydantic rule — they remain regular classes with mutable
attributes. The graph topology (nodes/edges/entry_node) is set at
construction by `Graph.compile()` and not mutated afterward, but the
class itself is not frozen because `Node` subclass instances need
mutable per-execution attributes (deliver/submit state on `ctx`, not
on the instance). A frozen dataclass would raise `FrozenInstanceError`
on legitimate `Node` attribute access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from typing_extensions import TypeVar

from .constants import NodeTrigger, SchedulerKind
from .integration import IntegratedInput
from .node import Node

if TYPE_CHECKING:
    from .context import GraphContext
    from .graph import Edge
    from .state import GraphState

S = TypeVar("S", bound="GraphState")


class CompiledGraph(Node[S]):
    """Validated graph. Subclass of `Node[S]` (Graph-is-a-Node, D8).

    Constructed by `Graph.compile()`. Holds the validated node registry,
    edges, entry node, and `max_iterations` safety net.

    As a `Node`: `execute(ctx)` runs its own `GraphEngine` loop on `ctx`.
    The subgraph writes its result to `ctx.state` (a field on the state) —
    the engine returns `ctx.state` after the inner loop reaches END.

    `default_trigger` (Task 06) is the graph-level default trigger mode for
    `ParallelScheduler`. A per-node `Node.trigger` (when not `None`) overrides
    it for that node.
    """

    def __init__(
        self,
        *,
        name: str,
        nodes: dict[str, Node[Any]],
        edges: list[Edge],
        entry_node: str,
        max_iterations: int = 1000,
        scheduler: SchedulerKind = SchedulerKind.LINEAR,
        default_trigger: NodeTrigger = NodeTrigger.ON_ALL_PREDS,
    ) -> None:
        self.name = name
        self.nodes = nodes
        self.edges = edges
        self.entry_node = entry_node
        self.max_iterations = max_iterations
        self.scheduler = scheduler
        self.default_trigger = default_trigger

    async def execute(
        self,
        ctx: GraphContext[S],
        integrated_input: IntegratedInput,
    ) -> None:
        """Run this graph as a node. Delegates to `GraphEngine.run_async`.

        ``integrated_input`` is accepted to satisfy the ``Node.execute``
        contract but is ignored — the subgraph runs its own engine loop
        which manages its own input integration internally.

        The subgraph shares `ctx.state` / `ctx.runtime` / `ctx.user_data`
        with the parent. The subgraph's terminal node writes its result to
        a state field; the parent reads it after this `execute` returns.

        The dispatch handler is saved and restored so the inner scheduler
        does not clobber the outer scheduler's routing. Invocation identity
        (instance_id, invocation) is handled automatically by the
        ContextVar-based execution context — token-based reset in
        ``Node.run()`` restores the parent's value when the subgraph
        finishes.
        """
        from .engine import GraphEngine

        saved_dispatch_handler = ctx._dispatch_handler

        engine: GraphEngine[S] = GraphEngine(self)
        try:
            await engine.run_async(ctx)
        finally:
            ctx.set_dispatch_handler(saved_dispatch_handler)
        return None

    # ── Edge lookup helpers (used by GraphEngine) ──────────────────────

    def edges_from(self, source: str) -> list[Edge]:
        """Return all edges originating from `source`, in declaration order."""
        return [e for e in self.edges if e.source == source]


__all__ = ["CompiledGraph", "S"]
