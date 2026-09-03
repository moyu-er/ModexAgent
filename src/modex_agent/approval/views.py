"""Cross-channel approval DTOs.

``ApprovalRequestView`` is the single payload shape shared by push (suspend-time
prompt) and pull (GET query for restart recovery).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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


def view_from_request(req: ApprovalRequestState, *, status: str = "pending") -> ApprovalRequestView:
    """Serialize an ``ApprovalRequestState`` snapshot into the wire DTO."""
    return ApprovalRequestView(
        tool_call_id=req.tool_call_id,
        tool_name=req.tool_name,
        tier=str(req.tier),
        arguments=dict(req.arguments.values),
        status=status,
    )
