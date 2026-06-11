"""StartNode — entry point for ReAct graph."""

from __future__ import annotations

from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActNode, ReActReason
from framework.agents.react.state import get_react_state
from framework.core.agent import AgentContext
from framework.core.graph.node import Node, NodeTransition
from framework.runtime.enums import TurnPhase


class StartNode(Node):
    """Routes to LLM normally, or to resume target when turn is suspended."""

    def __init__(self) -> None:
        super().__init__(ReActNode.START)

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        state = get_react_state(ctx)

        if state is not None and state.phase == TurnPhase.SUSPENDED:
            # Resume from suspended turn — route to the saved node
            resume_node = ReActNode(state.current_node.value)
            return NodeTransition(resume_node, ReActReason.RESUME_TOOLS)

        # Fresh turn — initialize typed state
        if state is not None:
            state.phase = TurnPhase.RUNNING
            state.current_node = ReActNode.START
            state.iteration = 0

        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.START)
        return NodeTransition(ReActNode.LLM, ReActReason.NORMAL_START)
