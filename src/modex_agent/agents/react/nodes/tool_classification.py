"""ToolNode classification helpers — one classify per call, everything derived.

ToolNode classifies each ToolCall exactly once and derives approval
decisions, denial copy, and guard audit rows from the stored
:class:`~modex_agent.approval.classification.ToolClassification` values —
no re-classification, no side channels.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from modex_agent.agents.react.context import get_agent_ctx
from modex_agent.approval.classification import ToolClassification
from modex_agent.approval.constants import (
    ApprovalAuditDecision,
    ApprovalAuditSource,
    ApprovalDecision,
    ApprovalTier,
)
from modex_agent.runtime.approval_decision import ApprovalAuditEntry

if TYPE_CHECKING:
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.message import ToolCall
    from modex_graph.context import GraphContext

logger = logging.getLogger(__name__)


def decision_of(classification: ToolClassification) -> ApprovalDecision:
    """Map one classification outcome to its pre-execution approval decision."""
    match classification.tier:
        case ApprovalTier.NORMAL:
            return ApprovalDecision.ALLOWED
        case ApprovalTier.HARDLINE:
            return ApprovalDecision.DENIED
        case ApprovalTier.DANGEROUS | ApprovalTier.SENSITIVE:
            return ApprovalDecision.PENDING


def decisions_of(classifications: list[ToolClassification]) -> list[ApprovalDecision]:
    return [decision_of(classification) for classification in classifications]


async def record_guard_audit(
    classifications: list[ToolClassification],
    tool_calls: list[ToolCall],
    ctx: GraphContext[ReActTurnState],
) -> None:
    """Append guard-decision rows to the audit sink.

    Only classifications whose guard made a decision (``audit`` fact
    present) produce rows. The sink is absent in non-persisted
    deployments — recording is then skipped, decisions still happen. A
    failing sink must not break the turn: the audit is observability,
    not a gate.
    """
    facts = [
        (tc, classification)
        for tc, classification in zip(tool_calls, classifications, strict=False)
        if classification.audit is not None
    ]
    if not facts:
        return
    agent_ctx = get_agent_ctx(ctx)
    runtime = agent_ctx.runtime
    if runtime is None:
        return
    audit = runtime.services.approval_audit
    turn_uuid = runtime.turn_uuid
    if audit is None or turn_uuid is None:
        return
    identity = ctx.state.identity
    decided_at = datetime.now(UTC).isoformat()
    for tc, classification in facts:
        fact = classification.audit
        if fact is None:
            continue
        try:
            await audit.record(
                ApprovalAuditEntry(
                    turn_uuid=turn_uuid,
                    session_id=str(identity.session),
                    agent_id=identity.agent_id,
                    turn_id=identity.turn_id,
                    tool_name=tc.tool_name,
                    tool_call_id=tc.call_id or "",
                    decision=fact.decision,
                    deny_reason=classification.reason
                    if fact.decision is ApprovalAuditDecision.DENIED
                    else None,
                    decided_at=decided_at,
                    decided_by=fact.decided_by,
                    source=(
                        runtime.services.delegation.source
                        if runtime.services.delegation is not None
                        else ApprovalAuditSource.RUNTIME
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Guard audit append failed for %s (call %s)",
                tc.tool_name,
                tc.call_id,
            )
