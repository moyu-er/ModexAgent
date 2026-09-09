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
    approval_id: str = ""
    """Owner-minted identity from ``ApprovalRequestState.approval_id``.

    Empty only for views built directly in old tests — real suspensions
    always carry it. Opaque to channels: used for exact resume matching,
    never reconstructed from wire data.
    """
    turn_uuid: str | None = None
    """Turn uuid of the suspended turn, bound by the suspension site."""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tier": self.tier,
            "arguments": dict(self.arguments),
            "status": self.status,
        }
        # Identity fields are conditional: existing UI payloads keep their
        # exact prior shape (zero behavior mutation); real suspensions carry
        # the owner-minted identity.
        if self.approval_id:
            payload["approval_id"] = self.approval_id
        if self.turn_uuid is not None:
            payload["turn_uuid"] = self.turn_uuid
        return payload


def view_from_request(
    req: ApprovalRequestState,
    *,
    status: str = "pending",
    turn_uuid: str | None = None,
) -> ApprovalRequestView:
    """Serialize an ``ApprovalRequestState`` snapshot into the wire DTO."""
    return ApprovalRequestView(
        tool_call_id=req.tool_call_id,
        tool_name=req.tool_name,
        tier=str(req.tier),
        arguments=dict(req.arguments.values),
        status=status,
        approval_id=req.approval_id,
        turn_uuid=turn_uuid,
    )
