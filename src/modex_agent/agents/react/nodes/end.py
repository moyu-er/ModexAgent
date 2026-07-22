"""EndNode — builds AgentResult and marks completion."""

from __future__ import annotations

from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent
from modex_agent.agents.react.constants import (
    ReActNode,
)
from modex_agent.agents.react.context import get_agent_ctx
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.constants import FinishReason, StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.runtime.enums import TurnPhase
from modex_graph.context import GraphContext
from modex_graph.node import Node
from modex_graph.result import NodeResult


class EndNode(Node[ReActTurnState]):
    """Constructs final result and emits completion events."""

    def __init__(self) -> None:
        self.name = ReActNode.END

    async def execute(self, ctx: GraphContext[ReActTurnState]) -> NodeResult:
        state = ctx.state
        agent_ctx = get_agent_ctx(ctx)
        response = state.llm_response
        state.current_node = ReActNode.END

        messages = [md.message for md in state.message_delta]

        if state.phase == TurnPhase.CANCELLED:
            result = AgentResult(
                content="turn cancelled",
                stop_reason=StopReason.TURN_CANCELLED,
                messages=messages,
                attachments=agent_ctx.attachments,
            )
        elif response is not None and response.finish_reason == FinishReason.ERROR.value:
            error_text = response.error or response.content or "LLM request failed"
            result = AgentResult(
                error=error_text,
                stop_reason=StopReason.ERROR,
                messages=messages,
                attachments=agent_ctx.attachments,
            )
            await ctx.runtime.emit(GraphReActEvent.ERROR, error_text, ctx)
        elif response is not None and not response.tool_calls:
            result = AgentResult(
                content=response.content or "",
                reasoning=response.reasoning_content,
                messages=messages,
                attachments=agent_ctx.attachments,
            )
            await ctx.runtime.emit(GraphReActEvent.FINAL_OUTPUT, result, ctx)
        else:
            result = AgentResult(
                content="max iterations reached",
                stop_reason=StopReason.MAX_ITERATIONS,
                messages=messages,
                attachments=agent_ctx.attachments,
            )

        state.phase = TurnPhase.COMPLETING

        # ``emit_complete`` signals end-of-stream — a different method on
        # ``ContentEmitter`` than ``emit``. ``ctx.runtime.emit`` only wraps
        # ``emitter.emit``, so this call stays direct.
        if agent_ctx.emitter is not None:
            await agent_ctx.emitter.emit_complete(result)
        # ADR-0033 D9.3: write to the explicit ``state.result`` field.
        # The caller's ``run()`` reads ``state.result`` after the engine returns.
        # The old ``custom[TurnCustomKey.GRAPH_RESULT]`` dual-write is removed
        # (ticket 05) — the typed ``state.result`` field is the single source.
        state.result = result

        state.mark_completed()

        # ``transition=None`` falls through to the default edge
        # ``add_edge(ReActNode.END, GraphNode.END)`` declared in
        # ``build_react_graph()``, routing the engine to ``GraphNode.END`` and
        # terminating the loop.
        return NodeResult(transition=None)


__all__ = ["EndNode"]
