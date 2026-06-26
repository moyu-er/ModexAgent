"""ApprovalResumer — pure approval state machine.

Owns the approval decision/save/prompt/restore half of the resume flow. The
turn-execution half (run the resumed turn, delete the snapshot, drain buffered
messages) is driven by the caller, so this module has NO dependency on turn
execution — a single-direction edge.

Extracted from the pipeline's approval-resume methods. Behaviour identical.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.agents.react.state import ReActSnapshotPolicy
from modex_agent.approval.constants import ApprovalDecision
from modex_agent.approval.types import ApprovalAction
from modex_agent.core.agent import AgentContext
from modex_agent.pipeline.approval_renderer import format_approval_prompt
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.runtime.enums import SnapshotReason, TurnPhase
from modex_agent.runtime.models import StateQueryScope, TurnSnapshot

if TYPE_CHECKING:
    from modex_agent.approval.ui import ApprovalUserInterface
    from modex_agent.core.agent import Agent
    from modex_agent.runtime.store import TurnStateStore

logger = logging.getLogger(__name__)


class ApprovalResumer:
    """Approval state machine: load pending snapshot, apply a decision, restore."""

    def __init__(
        self,
        *,
        agent: Agent,
        turn_store: TurnStateStore | None,
        user_interface: ApprovalUserInterface | None,
    ) -> None:
        self._agent = agent
        self._turn_store = turn_store
        self._user_interface = user_interface

    def _resolve_turn_store(
        self, pool_data: PoolDataSnapshot | None,
    ) -> TurnStateStore | None:
        return pool_data.turn_store if pool_data is not None else self._turn_store

    async def load_pending(
        self,
        session_id: str,
        *,
        pool_data: PoolDataSnapshot | None = None,
    ) -> TurnSnapshot | None:
        turn_store = self._resolve_turn_store(pool_data)
        if turn_store is None:
            return None
        agent_id = self._agent.name
        snapshots = await turn_store.list_active_turns(
            StateQueryScope(
                agent_id=agent_id,
                session_id=session_id,
                phase=TurnPhase.SUSPENDED,
                reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
            )
        )
        if not snapshots:
            return None
        snapshots.sort(key=lambda snapshot: snapshot.created_at)
        return snapshots[-1]

    async def apply_resume(
        self,
        snapshot: TurnSnapshot,
        *,
        action: ApprovalAction | None,
        session_id: str,
        pool_data: PoolDataSnapshot | None,
        agent_context: AgentContext,
    ) -> bool:
        approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
        if approval is None:
            return False

        if action is not None:
            decision = (
                ApprovalDecision.ALLOWED
                if action == ApprovalAction.ALLOW
                else ApprovalDecision.DENIED
            )
            for req in approval.requests:
                current = approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
                if current == ApprovalDecision.PENDING:
                    approval.apply_decision(req.tool_call_id, decision)
                    break

        snapshot = ReActSnapshotPolicy.replace_approval(snapshot, approval)
        turn_store = self._resolve_turn_store(pool_data)
        if turn_store is None:
            logger.error("Approval resume requested but no TurnStateStore is configured")
            return False

        if not approval.every_tool_decided:
            await turn_store.save_turn(snapshot)
            if self._user_interface is not None:
                for req in approval.requests:
                    current = approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
                    if current == ApprovalDecision.PENDING:
                        await self._user_interface.render_message(
                            session_id,
                            format_approval_prompt(req),
                        )
                        break
            return False

        state = ReActSnapshotPolicy.state_from_snapshot(snapshot)
        if agent_context.runtime is None:
            return False
        agent_context.identity = snapshot.identity
        agent_context.runtime.state = state
        return True
