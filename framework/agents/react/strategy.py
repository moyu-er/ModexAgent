"""SuspendStrategy ABC — pluggable approval behavior for ToolNode."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.core.graph.interrupt import interrupt

from .constants import ReActMetaKey

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.runtime.store import TurnStateStore
    from framework.runtime.policy import SnapshotPolicy


@dataclass
class TurnResumeState:
    """Legacy resume state used by ``SuspendResumeStrategy``."""
    iteration: int
    tool_calls: list[dict[str, Any]]
    tool_decisions: list[str]
    all_new_messages: list[dict[str, Any]]
    llm_content: str = ""
    llm_reasoning: str | None = None
    resume_node: str = "tool"
    resume_reason: str = "resume_tools"


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

    # ── Public storage API for Pipeline ──

    async def load_approval_state(self, session_id: str) -> Any | None:
        """Load pending approval state for a session, or None."""
        return None

    async def save_approval_state(self, state: Any) -> None:
        """Save approval state (e.g. partial decisions)."""
        pass

    async def delete_approval_state(self, session_id: str) -> None:
        """Delete approval state after completion."""
        pass

    async def load_resume_state(self, session_id: str) -> Any | None:
        """Load TurnResumeState for a session, or None."""
        return None

    async def delete_resume_state(self, session_id: str) -> None:
        """Delete resume state after completion."""
        pass


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


# ── Legacy store types (used by old SuspendResumeStrategy, exported for test compat) ──


class InMemoryTurnResumeStateStore:
    """In-memory TurnResumeState store for testing."""
    def __init__(self) -> None:
        self._store: dict[str, TurnResumeState] = {}
    async def save(self, session_id: str, state: TurnResumeState) -> None:
        self._store[session_id] = state
    async def load(self, session_id: str) -> TurnResumeState | None:
        return self._store.get(session_id)
    async def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)


class StateStoreTurnResumeStateStore:
    """Wraps a checkpoint store for TurnResumeState persistence."""
    def __init__(self, checkpoint_store: Any) -> None:
        self._store = checkpoint_store
    @staticmethod
    def _key(session_id: str) -> str:
        return f"{session_id}:turn_resume"
    async def save(self, session_id: str, state: TurnResumeState) -> None:
        await self._store.save(self._key(session_id), {
            "iteration": state.iteration, "tool_calls": state.tool_calls,
            "tool_decisions": state.tool_decisions, "all_new_messages": state.all_new_messages,
            "llm_content": state.llm_content, "llm_reasoning": state.llm_reasoning,
            "resume_node": state.resume_node, "resume_reason": state.resume_reason,
        })
    async def load(self, session_id: str) -> TurnResumeState | None:
        data = await self._store.load(self._key(session_id))
        if data is None:
            return None
        return TurnResumeState(
            iteration=data["iteration"], tool_calls=data["tool_calls"],
            tool_decisions=data["tool_decisions"], all_new_messages=data["all_new_messages"],
            llm_content=data.get("llm_content", ""), llm_reasoning=data.get("llm_reasoning"),
            resume_node=data.get("resume_node", "tool"), resume_reason=data.get("resume_reason", "resume_tools"),
        )
    async def delete(self, session_id: str) -> None:
        await self._store.clear(self._key(session_id))


class SuspendResumeStrategy(SuspendStrategy):
    """Interrupt-resume approval — persists state, raises GraphInterrupt."""

    def __init__(self, approval_store: Any, resume_store: Any) -> None:
        self._approval_store = approval_store
        self._resume_store = resume_store

    async def load_approval_state(self, session_id: str) -> Any | None:
        return await self._approval_store.load(session_id)

    async def save_approval_state(self, state: Any) -> None:
        await self._approval_store.save(state)

    async def delete_approval_state(self, session_id: str) -> None:
        await self._approval_store.delete(session_id)

    async def load_resume_state(self, session_id: str) -> Any | None:
        return await self._resume_store.load(session_id)

    async def delete_resume_state(self, session_id: str) -> None:
        await self._resume_store.delete(session_id)

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


class TurnStateSuspendStrategy(SuspendStrategy):
    """Typed interrupt-resume approval — uses ``TurnStateStore`` + ``TurnSnapshot``.

    Replaces ``SuspendResumeStrategy`` by persisting ``ApprovalTransaction``
    inside ``ReActTurnState`` as a single ``TurnSnapshot``.
    """

    def __init__(self, turn_store: TurnStateStore, snapshot_policy: SnapshotPolicy) -> None:
        self._turn_store = turn_store
        self._snapshot_policy = snapshot_policy

    async def solicit_approval(
        self,
        requests: list[ApprovalRequest],
        ctx: AgentContext,
        all_tool_calls: list[dict[str, Any]] | None = None,
        llm_content: str = "",
        llm_reasoning: str | None = None,
    ) -> list[str]:
        from framework.core.graph.interrupt import _current_resume

        # On resume: consume decisions once
        resume_val = _current_resume.get(None)
        if resume_val is not None:
            _current_resume.set(None)
            return resume_val

        # Build ApprovalTransaction and save TurnSnapshot
        react_state = self._get_react_state(ctx)
        if react_state is not None:
            from uuid import uuid4
            from framework.runtime.enums import ApprovalSubjectType, SnapshotReason, TurnPhase
            from framework.runtime.models import ApprovalRequestState, ApprovalTransaction, ToolArguments
            from framework.approval.constants import ApprovalStatus

            approval_id = uuid4().hex
            req_states: list[ApprovalRequestState] = [
                ApprovalRequestState(
                    request_id=uuid4().hex,
                    approval_id=approval_id,
                    tool_call_id=r.tool_call_id,
                    tool_name=r.tool_name,
                    arguments=ToolArguments(values=r.arguments),
                    tier=r.tier,
                    iteration=r.iteration,
                )
                for r in requests
            ]
            tx = ApprovalTransaction(
                approval_id=approval_id,
                turn_id=react_state.identity.turn_id,
                subject_type=ApprovalSubjectType.TOOL_BATCH,
                subject_ids=[r.tool_call_id for r in requests],
                requests=req_states,
                status=ApprovalStatus.PENDING,
            )
            react_state.approval = tx
            react_state.phase = TurnPhase.SUSPENDED

            snapshot = self._snapshot_policy.capture(
                react_state, SnapshotReason.TOOL_APPROVAL_REQUIRED,
            )
            await self._turn_store.save_turn(snapshot)

        return interrupt(requests)

    async def load_approval_state(self, session_id: str) -> Any | None:
        from framework.runtime.models import StateQueryScope
        from framework.runtime.enums import TurnPhase, SnapshotReason
        results = await self._turn_store.list_active_turns(
            StateQueryScope(session_id=session_id, phase=TurnPhase.SUSPENDED, reason=SnapshotReason.TOOL_APPROVAL_REQUIRED),
        )
        return results[0] if results else None

    async def delete_approval_state(self, session_id: str) -> None:
        snapshot = await self.load_approval_state(session_id)
        if snapshot is not None:
            await self._turn_store.delete_turn(snapshot.identity)

    @staticmethod
    def _get_react_state(ctx: AgentContext) -> Any:
        if getattr(ctx, "identity", None) is None or ctx.runtime is None:
            return None
        if not hasattr(ctx.runtime, "state"):
            return None
        from .state import ReActTurnState
        state = ctx.runtime.state
        if isinstance(state, ReActTurnState):
            return state
        return None
