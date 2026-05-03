"""Approval state data models."""
from dataclasses import dataclass, field

from .constants import ApprovalDecision, ApprovalStatus


@dataclass
class ApprovalRequest:
    """A single tool's approval request."""
    tool_name: str
    tool_call_id: str
    arguments: dict
    tier: str
    iteration: int


@dataclass
class ApprovalState:
    """Approval state for one ReAct turn. May contain multiple tools to approve."""

    session_id: str
    requests: list[ApprovalRequest]
    decisions: dict[str, str] = field(default_factory=dict)
    status: str = ApprovalStatus.PENDING
    deny_reason: str | None = None

    @property
    def every_tool_decided(self) -> bool:
        return all(
            tc_id in self.decisions
            and self.decisions[tc_id] != ApprovalDecision.PENDING
            for tc_id in (r.tool_call_id for r in self.requests)
        )

    @property
    def unresolved_count(self) -> int:
        return sum(
            1 for r in self.requests
            if r.tool_call_id not in self.decisions
            or self.decisions[r.tool_call_id] == ApprovalDecision.PENDING
        )

    def apply(self, tool_call_id: str, decision: str) -> None:
        """Apply a decision. DENIED cascades: ALL PENDING and previously
        ALLOWED tools become PREEMPTED (batch atomicity — deny cancels all)."""
        self.decisions[tool_call_id] = decision
        if decision == ApprovalDecision.DENIED:
            for r in self.requests:
                if r.tool_call_id not in self.decisions or self.decisions[r.tool_call_id] in (
                    ApprovalDecision.PENDING, ApprovalDecision.ALLOWED,
                ):
                    self.decisions[r.tool_call_id] = ApprovalDecision.PREEMPTED
            self.status = ApprovalStatus.DENIED
        elif self.every_tool_decided:
            self.status = ApprovalStatus.APPROVED
        else:
            self.status = ApprovalStatus.PARTIAL

    def final_decisions(self) -> list[str]:
        """Return decisions in request order. Unresolved PREEMPTED."""
        return [
            self.decisions.get(r.tool_call_id, ApprovalDecision.PREEMPTED)
            for r in self.requests
        ]
