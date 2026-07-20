"""EndNode — builds AgentResult and marks completion."""

from __future__ import annotations

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ReActNode, ReActReason
from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason, StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.graph.constants import GraphNode
from modex_agent.core.graph.node import Node, NodeTransition
from modex_agent.runtime.enums import TurnCustomKey, TurnPhase


class EndNode(Node):
    """Constructs final result and emits completion events."""

    def __init__(self) -> None:
        super().__init__(ReActNode.END)

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        state = get_react_state(ctx)
        response = state.llm_response if state else None
        if state is not None:
            state.current_node = ReActNode.END

        messages = [md.message for md in state.message_delta] if state else []

        if state is not None and state.phase == TurnPhase.CANCELLED:
            result = AgentResult(
                content="turn cancelled",
                stop_reason=StopReason.TURN_CANCELLED,
                messages=messages,
                attachments=ctx.attachments,
            )
        elif response is not None and response.finish_reason == FinishReason.ERROR.value:
            error_text = response.error or response.content or "LLM request failed"
            result = AgentResult(
                error=error_text,
                stop_reason=StopReason.ERROR,
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
        else:
            result = AgentResult(
                content="max iterations reached",
                stop_reason=StopReason.MAX_ITERATIONS,
                messages=messages,
                attachments=ctx.attachments,
            )

        if state is not None:
            state.phase = TurnPhase.COMPLETING

        if ctx.emitter is not None:
            await ctx.emitter.emit_complete(result)
        # ADR-0033 D9.3: write to the explicit ``state.result`` field instead
        # of the old ``custom[TurnCustomKey.GRAPH_RESULT]`` dict escape hatch.
        # ``ReActAgent.run()`` reads ``state.result`` after the engine returns.
        # Still uses old engine (``ctx.runtime.state``) — ticket 04 changes to
        # ``ctx.state.result``.
        # Backward compat: also write to ``custom[GRAPH_RESULT]`` so the old
        # ``result_extractor`` in graph.py and existing tests that read
        # ``custom[GRAPH_RESULT]`` continue to work. Ticket 04 removes this.
        if state is not None:
            state.result = result
            state.custom[TurnCustomKey.GRAPH_RESULT] = result

        if state is not None:
            state.mark_completed()

        return NodeTransition(GraphNode.END, ReActReason.DONE)
