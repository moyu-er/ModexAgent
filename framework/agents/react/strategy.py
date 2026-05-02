"""SuspendStrategy ABC — pluggable approval behavior for ToolNode."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.core.graph.interrupt import interrupt

from .constants import ReActMetaKey
from .state import TurnResumeState

if TYPE_CHECKING:
    from framework.core.agent import AgentContext


class SuspendStrategy(ABC):
    """Pluggable approval strategy."""

    @abstractmethod
    async def solicit_approval(
        self,
        requests: list[ApprovalRequest],
        ctx: AgentContext,
        all_tool_calls: list[dict[str, Any]] | None = None,
        llm_content: str = "",
        llm_reasoning: str | None = None,
    ) -> list[str]:
        """Request approval and return final decisions."""
        ...


class InlineWaitStrategy(SuspendStrategy):
    """Blocking approval — polls a channel per-tool, no persistence."""

    def __init__(self, channel: Any) -> None:
        self._channel = channel

    async def solicit_approval(
        self,
        requests: list[ApprovalRequest],
        ctx: AgentContext,
        all_tool_calls: list[dict[str, Any]] | None = None,
        llm_content: str = "",
        llm_reasoning: str | None = None,
    ) -> list[str]:
        state = ApprovalState(session_id=ctx.session_id, requests=list(requests))
        for req in requests:
            await ctx.emitter.emit("approval_required", req)
            decision = await self._channel.wait_for_decision(req.tool_call_id)
            state.apply(req.tool_call_id, decision)
            if decision == ApprovalDecision.DENIED:
                break
        return state.final_decisions()


class SuspendResumeStrategy(SuspendStrategy):
    """Interrupt-resume approval — persists state, raises GraphInterrupt."""

    def __init__(self, approval_store: Any, resume_store: Any) -> None:
        self._approval_store = approval_store
        self._resume_store = resume_store

    async def solicit_approval(
        self,
        requests: list[ApprovalRequest],
        ctx: AgentContext,
        all_tool_calls: list[dict[str, Any]] | None = None,
        llm_content: str = "",
        llm_reasoning: str | None = None,
    ) -> list[str]:
        from framework.core.graph.interrupt import _current_resume

        # On resume: consume decisions once, then clear to prevent cross-batch leaks
        resume_val = _current_resume.get(None)
        if resume_val is not None:
            _current_resume.set(None)
            return resume_val

        # First pass: persist state before interrupt
        approval_state = ApprovalState(session_id=ctx.session_id, requests=list(requests))
        await self._approval_store.save(approval_state)

        resume_state = TurnResumeState(
            iteration=ctx.metadata[ReActMetaKey.ITERATION],
            tool_calls=all_tool_calls or [],
            tool_decisions=[ApprovalDecision.PENDING] * len(requests),
            all_new_messages=list(ctx.metadata.get(ReActMetaKey.ITERATION_MSGS, [])),
            llm_content=llm_content,
            llm_reasoning=llm_reasoning,
        )
        await self._resume_store.save(ctx.session_id, resume_state)

        return interrupt(requests)
