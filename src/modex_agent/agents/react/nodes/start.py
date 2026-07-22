"""StartNode — entry point for ReAct graph."""

from __future__ import annotations

from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent
from modex_agent.agents.react.constants import (
    ReActNode,
    ReActReason,
)
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.runtime.enums import TurnPhase
from modex_graph.context import GraphContext
from modex_graph.node import Node
from modex_graph.result import NodeResult


class StartNode(Node[ReActTurnState]):
    """Routes to LLM normally, or to resume target when turn is suspended."""

    def __init__(self) -> None:
        # Name is also set by ``Graph.add_node(name, node)``; setting it here
        # keeps the instance usable for direct-invocation tests that bypass
        # the graph builder.
        self.name = ReActNode.START

    async def execute(self, ctx: GraphContext[ReActTurnState]) -> NodeResult:
        state = ctx.state

        if state.phase == TurnPhase.SUSPENDED:
            # Resume from suspended turn — route to the saved node (TOOL for
            # approval resume). The static edge START --RESUME_TOOLS--> TOOL
            # carries this transition; StartNode merely emits the reason.
            return NodeResult(transition=ReActReason.RESUME_TOOLS)

        # Fresh turn — initialize typed state.
        state.phase = TurnPhase.RUNNING
        state.current_node = ReActNode.START
        state.iteration = 0

        await ctx.runtime.emit(GraphReActEvent.START, None, ctx)
        return NodeResult(transition=ReActReason.NORMAL_START)


__all__ = ["StartNode"]
