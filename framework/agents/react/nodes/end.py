"""EndNode — builds AgentResult and marks completion."""
from __future__ import annotations

from typing import TYPE_CHECKING

from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActNode, ReActReason
from framework.agents.react.state import get_react_state
from framework.core.agent import AgentContext
from framework.core.constants import FinishReason
from framework.core.emitter import AgentResult
from framework.core.graph.constants import GraphMetaKey, GraphNode
from framework.core.graph.node import Node, NodeTransition
from framework.runtime.enums import TurnPhase

if TYPE_CHECKING:
    from framework.agents.react.agent import ReActAgent


class EndNode(Node):
    """Constructs final result and emits completion events."""

    def __init__(self, agent: ReActAgent) -> None:
        super().__init__(ReActNode.END)
        self._agent = agent

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        state = get_react_state(ctx)
        response = state.llm_response if state else None
        state.current_node = ReActNode.END if state else None

        messages = [md.message for md in state.message_delta] if state else []

        if response is not None and response.finish_reason == FinishReason.ERROR.value:
            error_text = response.error or response.content or "LLM request failed"
            result = AgentResult(
                error=error_text, stop_reason="error",
                messages=messages, attachments=ctx.attachments,
            )
            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.ERROR, error_text)
        elif response is not None and not response.tool_calls:
            result = AgentResult(
                content=response.content or "",
                reasoning=response.reasoning_content,
                messages=messages, attachments=ctx.attachments,
            )
            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.FINAL_OUTPUT, result)
        else:
            result = AgentResult(
                content="max iterations reached", stop_reason="max_iterations",
                messages=messages, attachments=ctx.attachments,
            )

        if state is not None:
            state.phase = TurnPhase.COMPLETING

        await self._agent._clear_checkpoint(ctx)
        if ctx.emitter is not None:
            await ctx.emitter.emit_complete(result)
        ctx.metadata[GraphMetaKey.GRAPH_RESULT] = result

        if state is not None:
            state.mark_completed()

        return NodeTransition(GraphNode.END, ReActReason.DONE)
