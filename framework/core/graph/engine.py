"""GraphEngine — drives node execution and edge routing."""
from __future__ import annotations

from typing import Any, Generic
from typing_extensions import TypeVar

from framework.core.agent import AgentContext

from .constants import GraphMetaKey, GraphNode
from .graph import Graph
from .interrupt import GraphInterrupt

R = TypeVar("R", default=Any)


class GraphEngine(Generic[R]):
    """Executes a Graph by iterating nodes and following edges.
    Agnostic to ReAct / Hook / Interceptor / Approval.
    """

    def __init__(self, graph: Graph[R]) -> None:
        self.graph = graph

    async def run(self, ctx: AgentContext[R]) -> Any:
        """Single entry point. Runs from entry_node until GraphNode.END."""
        current: str = self.graph.entry_node
        while current != GraphNode.END:
            node = self.graph._nodes[current]
            try:
                transition = await node.execute(ctx)
            except GraphInterrupt:
                raise
            if transition.target == GraphNode.END:
                current = GraphNode.END
            else:
                current = self.graph.next_node(current, transition.reason)
        return self.build_result(ctx)

    def build_result(self, ctx: AgentContext[R]) -> Any:
        """Extract final result from ctx. Override for typed returns."""
        return ctx.metadata.get(GraphMetaKey.GRAPH_RESULT)
