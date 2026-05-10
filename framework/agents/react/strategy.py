"""SuspendStrategy ABC — pluggable approval behavior for ToolNode."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.core.graph.interrupt import interrupt

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.runtime.store import TurnStateStore
    from framework.runtime.policy import SnapshotPolicy
    from .state import ReActTurnState


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


class TurnStateSuspendStrategy(SuspendStrategy):
    """Typed interrupt-resume approval — uses ``TurnStateStore`` + ``TurnSnapshot``."""

    def __init__(self, turn_store: TurnStateStore, snapshot_policy: SnapshotPolicy) -> None:
        self._turn_store = turn_store
        self._snapshot_policy = snapshot_policy
        self._resume_decisions: dict[str, list[str]] = {}

    async def solicit_approval(
        self,
        requests: list[ApprovalRequest],
        ctx: AgentContext,
        all_tool_calls: list[dict[str, Any]] | None = None,
        llm_content: str = "",
        llm_reasoning: str | None = None,
    ) -> list[str]:
        # On resume: consume decisions once, then clear
        decisions = self._resume_decisions.pop(ctx.session_id, None)
        if decisions is not None:
            return decisions

        # First pass: build ApprovalTransaction and save TurnSnapshot
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

    def save_resume_decisions(self, session_id: str, decisions: list[str]) -> None:
        """Called by pipeline after user approves/denies. Stored in-memory
        so the next ``solicit_approval`` call can consume them."""
        self._resume_decisions[session_id] = list(decisions)

    async def load_approval_state(self, session_id: str) -> Any | None:
        from framework.runtime.models import StateQueryScope
        from framework.runtime.enums import TurnPhase, SnapshotReason
        results = await self._turn_store.list_active_turns(
            StateQueryScope(session_id=session_id, phase=TurnPhase.SUSPENDED, reason=SnapshotReason.TOOL_APPROVAL_REQUIRED),
        )
        return results[0] if results else None

    async def delete_approval_state(self, session_id: str) -> None:
        self._resume_decisions.pop(session_id, None)
        snapshot = await self.load_approval_state(session_id)
        if snapshot is not None:
            await self._turn_store.delete_turn(snapshot.identity)

    @staticmethod
    def _get_react_state(ctx: AgentContext) -> ReActTurnState | None:
        if getattr(ctx, "identity", None) is None or ctx.runtime is None:
            return None
        if not hasattr(ctx.runtime, "state"):
            return None
        state = ctx.runtime.state
        if isinstance(state, ReActTurnState):
            return state
        return None
