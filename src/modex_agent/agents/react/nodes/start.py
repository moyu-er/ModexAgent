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
from modex_graph.result import Command, NodeResult


class StartNode(Node[ReActTurnState]):
    """Routes to LLM normally, or to ``state.resume_target`` when resuming."""

    def __init__(self) -> None:
        self.name = ReActNode.START

    async def execute(self, ctx: GraphContext[ReActTurnState]) -> NodeResult:
        state = ctx.state

        if state.resume_target is not None:
            target = state.resume_target
            state.resume_target = None
            return NodeResult(command=Command(goto=target))

        state.phase = TurnPhase.RUNNING
        state.current_node = ReActNode.START
        state.iteration = 0

        await ctx.runtime.emit(GraphReActEvent.START, None, ctx)
        return NodeResult(transition=ReActReason.NORMAL_START)


__all__ = ["StartNode"]
