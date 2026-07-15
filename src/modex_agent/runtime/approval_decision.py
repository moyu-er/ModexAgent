from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from modex_agent.runtime.models import TurnSnapshot


class ApprovalAuditDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


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
    decided_by: str


class ApprovalDecisionCoordinator(ABC):
    @abstractmethod
    async def apply_decision(
        self,
        snapshot: TurnSnapshot,
        entry: ApprovalAuditEntry,
    ) -> None:
        ...
