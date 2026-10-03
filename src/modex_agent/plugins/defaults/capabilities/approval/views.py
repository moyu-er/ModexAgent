"""Cross-channel approval DTOs.

``ApprovalRequestView`` is the single payload shape shared by push (suspend-time
prompt) and pull (GET query for restart recovery).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modex_agent.core.turn.models import ToolArguments
from modex_agent.messaging.models import OutputMessage, OutputMessageType

if TYPE_CHECKING:
    from modex_agent.core.turn.models import ApprovalRequestState


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


# ── Rendering (relocated into the approval slice, W3b) ──────────────────────
# The IM text/structured-message presentation of a view is approval-channel
# vocabulary: approval/ui.py needs it, and approval must not import the
# pipeline. The renderer SERVICE (ApprovalRenderer) lives in this bundle's
# renderer.py (W1-B2).


def _format_arguments(args: ToolArguments | Mapping[str, object] | None) -> str:
    if args is None:
        return ""
    if isinstance(args, ToolArguments):
        values: Mapping[str, object] = args.values
    else:
        values = args
    return ", ".join(f"{key}={value}" for key, value in values.items())


def format_approval_prompt(view: ApprovalRequestView) -> str:
    """Format an approval request view for display to the user."""
    args_str = _format_arguments(view.arguments)
    return (
        f"Approval Required [{view.tier.upper()}]\n"
        f"Tool: {view.tool_name}\n"
        f"ID: {view.tool_call_id}\n"
        f"Args: {args_str}\n"
        f"Reply /approve or /deny"
    )


def approval_output_message(view: ApprovalRequestView) -> OutputMessage:
    """One message serving both channels: IM text (content) + webui structured (metadata).

    IM/QQ adapters read ``content`` and are unchanged; the WebUI channel drops
    this message — its approval card streams from the turn sink (the
    presentation ApprovalRequested projection), so the prompt never renders
    twice.
    """
    return OutputMessage(
        content=format_approval_prompt(view),
        message_type=OutputMessageType.APPROVAL_REQUEST,
        metadata={"approval": view.to_dict()},
    )
