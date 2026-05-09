"""StartNode — entry point for ReAct graph."""
from __future__ import annotations

from typing import Any

from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActMetaKey, ReActNode, ReActReason
from framework.core.agent import AgentContext
from framework.core.graph.node import Node, NodeTransition
from framework.core.types import LLMResponse, ToolCall
from framework.runtime.enums import TurnPhase


class StartNode(Node):
    """Routes to LLM normally, or to a resume target captured by runtime state."""

    def __init__(self) -> None:
        super().__init__(ReActNode.START)

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        # ---- typed ReActTurnState path (new) ----
        if ctx.identity is not None and ctx.runtime is not None and hasattr(ctx.runtime, "state"):
            from framework.agents.react.state import ReActTurnState
            state = ctx.runtime.state
            if isinstance(state, ReActTurnState):
                state.phase = TurnPhase.RUNNING
                state.current_node = ReActNode.START
                state.iteration = 0
                if ctx.emitter is not None:
                    await ctx.emitter.emit(ReActEvent.START)
                return NodeTransition(ReActNode.LLM, ReActReason.NORMAL_START)

        # ---- legacy metadata path (backward compat) ----
        resume_state = ctx.metadata.get(ReActMetaKey.RESUME_STATE)
        if resume_state is not None:
            resume_node = getattr(resume_state, "resume_node", ReActNode.TOOL)
            resume_reason = getattr(resume_state, "resume_reason", ReActReason.RESUME_TOOLS)
            if str(resume_node) == ReActNode.TOOL.value:
                tool_calls = [
                    ToolCall(
                        tool_name=tc["function"]["name"],
                        call_id=tc.get("id", ""),
                        arguments=tc["function"].get("arguments", {}),
                    )
                    for tc in resume_state.tool_calls
                ]
                llm_resp = LLMResponse(
                    content=resume_state.llm_content or None,
                    reasoning_content=resume_state.llm_reasoning,
                    tool_calls=tool_calls,
                    finish_reason="tool_calls",
                )
                ctx.metadata[ReActMetaKey.LLM_RESPONSE] = llm_resp
            ctx.metadata[ReActMetaKey.ITERATION] = resume_state.iteration
            ctx.metadata[ReActMetaKey.TOOL_DECISIONS] = resume_state.tool_decisions
            ctx.metadata[ReActMetaKey.ITERATION_MSGS] = list(resume_state.all_new_messages)
            return NodeTransition(str(resume_node), str(resume_reason))

        ctx.metadata[ReActMetaKey.ITERATION] = 0
        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.START)
        return NodeTransition(ReActNode.LLM, ReActReason.NORMAL_START)
