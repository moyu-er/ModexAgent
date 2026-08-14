"""StartNode — entry point for ReAct graph."""

from __future__ import annotations

from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent
from modex_agent.agents.react.constants import ReActHookPoint, ReActNode
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.runtime.enums import TurnPhase
from modex_graph.context import GraphContext
from modex_graph.integration import IntegratedInput
from modex_graph.node import Node


class StartNode(Node[ReActTurnState]):
    """Routes to BEFORE normally, or to ``state.resume_target`` when resuming."""

    def __init__(self) -> None:
        self.name = ReActNode.START

    async def execute(
        self,
        ctx: GraphContext[ReActTurnState],
        integrated_input: IntegratedInput,
    ) -> None:
        state = ctx.state

        if state.resume_target is not None:
            target = state.resume_target
            state.resume_target = None
            self.deliver(None, target, ctx)
            return None

        state.phase = TurnPhase.RUNNING
        state.current_node = ReActNode.START
        state.iteration = 0

        await ctx.runtime.emit(GraphReActEvent.START, None, ctx)
        await ctx.runtime.dispatch_hook(ReActHookPoint.START_NODE_TURN, ctx)
        self.deliver(None, ReActNode.BEFORE, ctx)
        return None


__all__ = ["StartNode"]
