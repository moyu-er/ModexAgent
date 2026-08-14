"""EndNode — reads state.result, emits completion events, delivers to END.

The ``AgentResult`` is constructed by ``AfterTurnNode`` (the 4-branch
CANCELLED / ERROR / normal / max-iter logic) and written to ``state.result``
(ADR-0033 D9.3, rule 15 convergence — one result construction path).
``EndNode`` reads it, asserts it is not ``None``, emits the matching
completion event (``FINAL_OUTPUT`` / ``ERROR``) based on
``result.stop_reason``, calls ``emit_complete``, marks the turn completed,
and delivers to ``GraphNode.END``.
"""

from __future__ import annotations

from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent
from modex_agent.agents.react.constants import ReActHookPoint, ReActNode
from modex_agent.agents.react.context import get_agent_ctx
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.constants import StopReason
from modex_agent.runtime.enums import TurnPhase
from modex_graph.constants import GraphNode
from modex_graph.context import GraphContext
from modex_graph.integration import IntegratedInput
from modex_graph.node import Node


class EndNode(Node[ReActTurnState]):
    """Reads ``state.result``, emits completion events, delivers to ``GraphNode.END``."""

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

        # Emit the matching completion event based on result.stop_reason.
        # CANCELLED / MAX_ITERATIONS produce no event (same as pre-refactor);
        # ERROR emits an ERROR event; everything else emits FINAL_OUTPUT.
        if result.stop_reason == StopReason.ERROR:
            error_text = result.error or result.content or "LLM request failed"
            await ctx.runtime.emit(GraphReActEvent.ERROR, error_text, ctx)
        elif result.stop_reason not in (
            StopReason.TURN_CANCELLED,
            StopReason.MAX_ITERATIONS,
        ):
            await ctx.runtime.emit(GraphReActEvent.FINAL_OUTPUT, result, ctx)

        # ``emit_complete`` signals end-of-stream — a different method on
        # ``ContentEmitter`` than ``emit``. ``ctx.runtime.emit`` only wraps
        # ``emitter.emit``, so this call stays direct.
        if agent_ctx.emitter is not None:
            await agent_ctx.emitter.emit_complete(result)

        state.mark_completed()

        await ctx.runtime.dispatch_hook(ReActHookPoint.END_NODE_TURN, ctx)
        self.deliver(result, GraphNode.END, ctx)
        return None


__all__ = ["EndNode"]
