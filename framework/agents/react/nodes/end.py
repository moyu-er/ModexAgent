"""EndNode — builds AgentResult and marks completion."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActMetaKey, ReActNode, ReActReason
from framework.core.agent import AgentContext
from framework.core.constants import FinishReason
from framework.core.emitter import AgentResult
from framework.core.graph.constants import GraphMetaKey, GraphNode
from framework.core.graph.node import Node, NodeTransition
from framework.runtime.enums import TurnPhase

if TYPE_CHECKING:
    from framework.agents.react.state import ReActTurnState
    from framework.agents.react.agent import ReActAgent


class EndNode(Node):
    """Constructs final result and emits completion events."""

    def __init__(self, agent: ReActAgent) -> None:
        super().__init__(ReActNode.END)
        self._agent = agent

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        response = ctx.metadata.pop(ReActMetaKey.LLM_RESPONSE, None)
        messages = ctx.metadata.pop(ReActMetaKey.ITERATION_MSGS, [])
        end_reason = ctx.metadata.pop(ReActMetaKey.END_REASON, None)
        cancel_reason = ctx.metadata.pop(ReActMetaKey.CANCEL_REASON, None)

        # ---- typed ReActTurnState path (new) ----
        react_state = self._get_react_state(ctx)
        if react_state is not None:
            react_state.current_node = ReActNode.END

        if response is not None and response.finish_reason == FinishReason.ERROR.value:
            error_text = response.error or response.content or "LLM request failed"
            result = AgentResult(
                error=error_text,
                stop_reason="error",
                messages=messages,
                attachments=ctx.attachments,
            )
            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.ERROR, error_text)
        elif response is not None and not response.tool_calls:
            result = AgentResult(
                content=response.content or "",
                reasoning=response.reasoning_content,
                messages=messages,
                attachments=ctx.attachments,
            )
            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.FINAL_OUTPUT, result)
        elif end_reason == ReActReason.TURN_CANCELLED:
            result = AgentResult(
                content="turn cancelled",
                stop_reason=ReActReason.TURN_CANCELLED.value,
                metadata={"cancel_reason": cancel_reason} if cancel_reason else {},
                messages=messages,
                attachments=ctx.attachments,
            )
        else:
            result = AgentResult(
                content="max iterations reached",
                stop_reason="max_iterations",
                messages=messages,
                attachments=ctx.attachments,
            )

        if react_state is not None:
            react_state.phase = TurnPhase.COMPLETING

        await self._agent._clear_checkpoint(ctx)
        if ctx.emitter is not None:
            await ctx.emitter.emit_complete(result)
        ctx.metadata[GraphMetaKey.GRAPH_RESULT] = result

        if react_state is not None:
            react_state.mark_completed()

        return NodeTransition(GraphNode.END, ReActReason.DONE)

    @staticmethod
    def _get_react_state(ctx: AgentContext) -> ReActTurnState | None:
        if getattr(ctx, "identity", None) is None or ctx.runtime is None:
            return None
        if not hasattr(ctx.runtime, "state"):
            return None
        from framework.agents.react.state import ReActTurnState
        state = ctx.runtime.state
        if isinstance(state, ReActTurnState):
            return state
        return None
