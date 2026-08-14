"""BeforeTurnNode — brackets the LLM<->TOOL loop at the start of each turn attempt."""

from __future__ import annotations

from modex_agent.agents.react.constants import ReActHookPoint, ReActNode
from modex_agent.agents.react.state import ReActTurnState
from modex_graph.context import GraphContext
from modex_graph.integration import IntegratedInput
from modex_graph.node import Node


class BeforeTurnNode(Node[ReActTurnState]):
    """Turn-attempt lifecycle node: increments turn_attempt, resets iteration, routes to LLM."""

    def __init__(self) -> None:
        self.name = ReActNode.BEFORE

    async def execute(
        self,
        ctx: GraphContext[ReActTurnState],
        integrated_input: IntegratedInput,
    ) -> None:
        state = ctx.state
        state.turn_attempt += 1
        state.iteration = 0
        state.current_node = ReActNode.BEFORE
        await ctx.runtime.dispatch_hook(ReActHookPoint.BEFORE_TURN, ctx)
        self.deliver(None, ReActNode.LLM, ctx)
        return None


__all__ = ["BeforeTurnNode"]
