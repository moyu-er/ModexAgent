"""ApprovalResumer — pure approval state machine.

Owns the approval decision/save/prompt/restore half of the resume flow. The
turn-execution half (run the resumed turn, delete the snapshot, drain buffered
messages) is driven by the caller, so this module has NO dependency on turn
execution — a single-direction edge.

Extracted from the pipeline's approval-resume methods. Behaviour identical.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from modex_agent.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from modex_agent.approval.constants import ApprovalDecision
from modex_agent.approval.views import view_from_request
from modex_agent.core.agent import AgentContext
from modex_agent.hook.abc import HookPayload, HookPoint
from modex_agent.messaging.models import ApprovalAction
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.runtime.approval_decision import (
    ApprovalAuditDecision,
    ApprovalAuditEntry,
    DecisionActor,
)
from modex_agent.runtime.enums import SnapshotReason, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import (
    ApprovalRequestState,
    StateQueryScope,
    TurnSnapshot,
)
from modex_agent.sandbox.decision import approval_anchor
from modex_agent.workspace.runtime import resolve_workspace_root

if TYPE_CHECKING:
    from modex_agent.approval.ui import ApprovalUserInterface
    from modex_agent.core.agent import Agent
    from modex_agent.core.events import AgentEvent
    from modex_agent.runtime.store import TurnStateStore

logger = logging.getLogger(__name__)


class MissingApprovalTurnUuidError(ValueError):
    pass


def _mark_human_approved(snapshot: TurnSnapshot, req: ApprovalRequestState) -> None:
    """Bind an allowed call ID to its target when approval is applied.

    Resolve the displayed arguments using the active workspace provider.
    The marker survives partial saves and final state restoration. Before
    execution the interceptor recomputes the target and waives BOUNDARY
    only on an exact match; hard findings are never waived. This does not
    capture the filesystem state at the earlier card-rendering time.
    """
    anchor = approval_anchor(
        req.tool_name,
        dict(req.arguments.values),
        resolve_workspace_root(),
    )
    if anchor is None:
        return
    custom = snapshot.state_payload.get("custom")
    if not isinstance(custom, dict):
        return
    marked = custom.get(TurnCustomKey.HUMAN_APPROVED_CALLS.value)
    marked = dict(marked) if isinstance(marked, dict) else {}
    marked[req.tool_call_id] = anchor
    custom[TurnCustomKey.HUMAN_APPROVED_CALLS.value] = marked


class ApprovalResumer:
    """Approval state machine: load pending snapshot, apply a decision, restore."""

    def __init__(
        self,
        *,
        agent: Agent[AgentEvent],
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
        # Approval turns are partitioned by workspace (turn_store path) + pool
        # + session_id. The session_id already identifies the conversation
        # uniquely, so agent_id is NOT a query dimension — using self._agent.name
        # here (a class-name constant like "ReActAgent") mismatches the snapshot's
        # stored agent_id (the pool registration name) and silently finds nothing.
        snapshots = await turn_store.list_active_turns(
            StateQueryScope(
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
        tool_call_id: str | None = None,
    ) -> TurnStateStore | None:
        """Apply a resume decision and restore state if every tool is decided.

        Returns the ``TurnStateStore`` the caller should use for cleanup
        (``delete_turn`` on the snapshot, then ``drain``) when the resume
        succeeds — i.e. all approval requests are decided and the snapshot
        state has been restored into ``agent_context.runtime.state``. Returns
        ``None`` on every non-resuming path: no approval payload, no
        resolvable turn_store, requests still pending (after a partial save +
        prompt render), or ``agent_context.runtime`` is None.

        On a successful resume the caller runs ``execute_turn`` and, when it
        yields a result, ``turn_store.delete_turn(snapshot.identity)`` and
        ``drain(session_id)`` using the returned store.

        When ``tool_call_id`` is given, only that request is decided (webui
        precision); ``None`` keeps the legacy decide-next-PENDING behaviour
        for IM ``/approve``.
        """
        approval = ReActTurnState.from_checkpoint(dict(snapshot.state_payload)).approval
        if approval is None:
            return None

        decided_request = None
        if action is not None:
            decision = (
                ApprovalDecision.ALLOWED
                if action == ApprovalAction.ALLOW
                else ApprovalDecision.DENIED
            )
            for req in approval.requests:
                current = approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
                if current != ApprovalDecision.PENDING:
                    continue
                if tool_call_id is not None and req.tool_call_id != tool_call_id:
                    continue  # leave non-target requests pending
                approval.apply_decision(req.tool_call_id, decision)
                decided_request = req
                if decision is ApprovalDecision.ALLOWED:
                    _mark_human_approved(snapshot, req)
                break

        snapshot = ReActSnapshotPolicy.replace_approval(snapshot, approval)
        turn_store = self._resolve_turn_store(pool_data)
        if turn_store is None:
            logger.error("Approval resume requested but no TurnStateStore is configured")
            return None

        coordinator = (
            pool_data.decision_coordinator if pool_data is not None else None
        )
        if decided_request is not None and coordinator is not None:
            # In the NEW checkpoint format (Pydantic model_dump), turn_uuid
            # lives inside the ``custom`` dict, not at the top level.
            custom = snapshot.state_payload.get("custom", {})
            if not isinstance(custom, dict):
                custom = {}
            turn_uuid = custom.get(TurnCustomKey.TURN_UUID.value)
            if not isinstance(turn_uuid, str):
                raise MissingApprovalTurnUuidError(
                    "Persisted approval snapshot has no turn UUID"
                )
            audit_decision = (
                ApprovalAuditDecision.APPROVED
                if action is ApprovalAction.ALLOW
                else ApprovalAuditDecision.DENIED
            )
            await coordinator.apply_decision(
                snapshot,
                ApprovalAuditEntry(
                    turn_uuid=turn_uuid,
                    session_id=str(snapshot.identity.session),
                    agent_id=snapshot.identity.agent_id,
                    turn_id=snapshot.identity.turn_id,
                    tool_name=decided_request.tool_name,
                    tool_call_id=decided_request.tool_call_id,
                    decision=audit_decision,
                    deny_reason=approval.deny_reason
                    if audit_decision is ApprovalAuditDecision.DENIED
                    else None,
                    decided_at=datetime.now(UTC).isoformat(),
                    decided_by=DecisionActor.USER,
                ),
            )

        if not approval.every_tool_decided:
            if decided_request is None or coordinator is None:
                await turn_store.save_turn(snapshot)
            if self._user_interface is not None:
                for req in approval.requests:
                    current = approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
                    if current == ApprovalDecision.PENDING:
                        await self._user_interface.render_approval_prompt(
                            session_id,
                            view_from_request(req),
                        )
                        break
            return None

        if agent_context.runtime is None:
            return None
        state = ReActSnapshotPolicy.state_from_snapshot(snapshot)
        agent_context.identity = snapshot.identity
        agent_context.runtime.state = state

        # AFTER_APPROVAL dispatched directly via runtime.hooks (NOT through
        # ReActHookPoint) — approval resume is a pipeline-layer concern.
        hooks = agent_context.runtime.hooks
        if hooks is not None:
            await hooks.dispatch(
                HookPoint.AFTER_APPROVAL,
                agent_context,
                HookPayload(data={"transaction": approval}),
            )

        return turn_store
