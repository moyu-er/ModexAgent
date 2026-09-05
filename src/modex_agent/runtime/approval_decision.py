from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from modex_agent.approval.constants import (
    ApprovalAuditDecision,
    ApprovalAuditSource,
    DecisionActor,
)
from modex_agent.runtime.models import TurnSnapshot

SANDBOX_GUARD_DECIDED_BY: str = DecisionActor.SANDBOX_GUARD.value
"""``ApprovalAuditEntry.decided_by`` value for guard-made decisions.

Guard denials/escalations land on the same audit
timeline as human decisions (``DecisionActor.USER``), distinguished by this
value."""


class ApprovalAuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_uuid: str
    session_id: str
    agent_id: str
    turn_id: str
    tool_name: str
    tool_call_id: str
    decision: ApprovalAuditDecision
    deny_reason: str | None = None
    decided_at: str
    decided_by: DecisionActor = DecisionActor.USER
    source: ApprovalAuditSource = ApprovalAuditSource.RUNTIME


class ApprovalAuditStore(ABC):
    """Append-only audit log for approval decisions.

    The ABC defines two operations: :meth:`record` (append one entry) and
    :meth:`query` (filter by session and optional timestamp). Implementations
    MUST NOT expose update or delete — the log is append-only.

    ``AgentRuntimeServices`` carries this contract without depending on a
    persistence adapter. Native main agents and subagents share the pool sink;
    source provenance distinguishes delegation from runtime decisions.
    """

    @abstractmethod
    async def record(self, entry: ApprovalAuditEntry) -> None:
        """Append *entry* to the audit log."""
        ...

    @abstractmethod
    async def query(
        self,
        session_id: str,
        since: datetime | None = None,
        limit: int = 100,
        decided_by: DecisionActor | None = None,
        source: ApprovalAuditSource | None = None,
    ) -> list[ApprovalAuditEntry]:
        """Return audit entries for *session_id*, optionally filtered.

        ``since`` keeps entries at or after that moment; ``decided_by`` (e.g.
        ``DecisionActor.SANDBOX_GUARD`` vs
        ``DecisionActor.USER``) narrows to one decision-maker; ``source``
        narrows by provenance (runtime vs delegation boundary); ``limit``
        caps the result. Entries are returned in ascending ``decided_at``
        order.
        """
        ...


class ApprovalDecisionCoordinator(ABC):
    @abstractmethod
    async def apply_decision(
        self,
        snapshot: TurnSnapshot,
        entry: ApprovalAuditEntry,
    ) -> None: ...
