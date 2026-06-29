"""Approval rendering helpers for turn-state based approval flow."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.agents.react.state import ReActSnapshotPolicy
from modex_agent.approval.constants import ApprovalDecision
from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalRequestView
from modex_agent.core.types import InputMessage, OutputMessage
from modex_agent.runtime.models import ToolArguments, TurnSnapshot

if TYPE_CHECKING:
    from modex_agent.approval.ui import ApprovalUserInterface

logger = logging.getLogger(__name__)

_UNRELATED_INPUT_PREVIEW_LIMIT = 50


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

    IM/QQ adapters read ``content`` and are unchanged; ``WebSocketOutputAdapter``
    branches on ``message_type == "approval_request"`` to emit a structured envelope.
    """
    return OutputMessage(
        content=format_approval_prompt(view),
        message_type="approval_request",
        metadata={"approval": view.to_dict()},
    )


class ApprovalRenderer:
    """Approval prompt rendering and agent-message buffering.

    Approval state is owned by ``TurnStateStore`` and represented by
    ``ApprovalTransaction`` inside ``TurnSnapshot``. This helper never loads or
    saves approval state directly.
    """

    def __init__(
        self,
        *,
        agent: ReActAgent | None = None,
        user_interface: ApprovalUserInterface | None = None,
        on_drain: Callable[[InputMessage], Awaitable[None]] | None = None,
    ) -> None:
        self.agent = agent
        self._user_interface = user_interface
        self._on_drain = on_drain
        self._approval_pending: dict[str, list[InputMessage]] = {}

    async def detect(
        self,
        input_msg: InputMessage,
        session_id: str,
        input_metadata: dict[str, object],
        *,
        pending_snapshot: TurnSnapshot | None,
        approval_action: ApprovalAction | None = None,
    ) -> tuple[bool, TurnSnapshot | None]:
        """Detect whether input targets an active approval transaction."""
        if pending_snapshot is None:
            return False, None

        if approval_action is not None:
            return True, pending_snapshot

        if input_metadata.get("source_agent"):
            self._approval_pending.setdefault(session_id, []).append(input_msg)
            return True, pending_snapshot

        approval = ReActSnapshotPolicy.approval_from_snapshot(pending_snapshot)
        if approval is None:
            return True, pending_snapshot

        truncated = (input_msg.content or "")[:_UNRELATED_INPUT_PREVIEW_LIMIT]
        for req in approval.requests:
            current = approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
            if current == ApprovalDecision.PENDING:
                approval.apply_decision(
                    req.tool_call_id,
                    ApprovalDecision.DENIED,
                    reason=f'unrelated input: "{truncated}"',
                )
                break

        return True, ReActSnapshotPolicy.replace_approval(pending_snapshot, approval)

    def cleanup_session(self, session_id: str) -> None:
        """Clean up per-session approval resources."""
        self._approval_pending.pop(session_id, None)

    async def drain(self, session_id: str) -> None:
        """Replay buffered agent messages after approval completes."""
        await self._drain(session_id)

    async def _drain(self, session_id: str) -> None:
        pending = self._approval_pending.pop(session_id, [])
        if pending and self._on_drain is None:
            logger.warning(
                "ApprovalRenderer: _on_drain is None, dropping %d buffered messages for %s",
                len(pending),
                session_id,
            )
            return
        for msg in pending:
            asyncio.create_task(self._on_drain(msg))  # type: ignore[misc]
