"""EndNode — reads state.result, emits the terminal event, delivers to END.

The ``AgentResult`` is constructed by ``AfterTurnNode`` (the 4-branch
CANCELLED / ERROR / normal / max-iter logic) and written to ``state.result``
(ADR-0033 D9.3, rule 15 convergence — one result construction path).
``EndNode`` reads it, asserts it is not ``None``, emits the exactly-once
terminal ``turn_finished`` event (the retired ``final_output`` / ``error``
distinction rides the event's ``stop_reason`` / ``error`` fields; the
mid-flight error message was already delivered by ``turn_errored`` when one
existed), marks the turn completed, and delivers to ``GraphNode.END``.
"""

from __future__ import annotations

from modex_agent.agents.react.constants import ReActHookPoint, ReActNode
from modex_agent.agents.react.context import get_agent_ctx
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.emitter import turn_finished_event
from modex_agent.core.turn.enums import TurnPhase
from modex_graph.constants import GraphNode
from modex_graph.context import GraphContext
from modex_graph.integration import IntegratedInput
from modex_graph.node import Node


class EndNode(Node[ReActTurnState]):
    """Reads ``state.result``, emits the terminal event, delivers to ``GraphNode.END``."""

    def __init__(self) -> None:
        self.name = ReActNode.END

    async def execute(
        self,
        ctx: GraphContext[ReActTurnState],
        integrated_input: IntegratedInput,
    ) -> None:
        state = ctx.state
        agent_ctx = get_agent_ctx(ctx)
        state.current_node = ReActNode.END

        result = state.result
        if result is None:
            raise RuntimeError(
                "AfterTurnNode must set state.result before EndNode executes"
            )

        state.phase = TurnPhase.COMPLETING

        # Exactly-once terminal signal — nothing is emitted after it from
        # this turn.
        if agent_ctx.emitter is not None:
            await agent_ctx.emitter.emit(turn_finished_event(result))

        state.mark_completed()

        await ctx.runtime.dispatch_hook(ReActHookPoint.END_NODE_TURN, ctx)
        self.deliver(None, GraphNode.END, ctx)
        return None


__all__ = ["EndNode"]
