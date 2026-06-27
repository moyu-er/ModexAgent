"""Cross-channel approval DTOs.

``ApprovalRequestView`` is the single payload shape shared by push (suspend-time
prompt) and pull (GET query for restart recovery). ``ApprovalDecisionInput``
carries a webui approve/deny decision on ``InputMessage``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modex_agent.approval.types import ApprovalAction

if TYPE_CHECKING:
    from modex_agent.runtime.models import ApprovalRequestState


@dataclass(frozen=True)
class ApprovalRequestView:
    """Serializable view of one approval request — the push/pull contract."""
    tool_call_id: str
    tool_name: str
    tier: str
    arguments: dict[str, Any]
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tier": self.tier,
            "arguments": dict(self.arguments),
            "status": self.status,
        }


@dataclass(frozen=True)
class ApprovalDecisionInput:
    """A webui approve/deny decision carried on ``InputMessage`` (not a slash command)."""
    tool_call_id: str
    action: ApprovalAction

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a broker-safe plain dict (crosses the message broker)."""
        return {"tool_call_id": self.tool_call_id, "action": self.action.value}

    @classmethod
    def from_dict(cls, data: Any) -> ApprovalDecisionInput | None:
        """Reconstruct from ``to_dict`` output; None when *data* is falsy."""
        if not data:
            return None
        return cls(tool_call_id=str(data["tool_call_id"]), action=ApprovalAction(str(data["action"])))


def view_from_request(req: ApprovalRequestState, *, status: str = "pending") -> ApprovalRequestView:
    """Serialize an ``ApprovalRequestState`` snapshot into the wire DTO."""
    return ApprovalRequestView(
        tool_call_id=req.tool_call_id,
        tool_name=req.tool_name,
        tier=str(req.tier),
        arguments=dict(req.arguments.values),
        status=status,
    )
