"""Approval rendering helpers for turn-state based approval flow."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.react.state import ReActSnapshotPolicy
from ..approval.constants import ApprovalDecision
from ..approval.types import ApprovalAction
from ..core.types import InputMessage
from ..runtime.models import ToolArguments, TurnSnapshot

if TYPE_CHECKING:
    from ..control.ui.abc import ControlUserInterface

logger = logging.getLogger(__name__)

_UNRELATED_INPUT_PREVIEW_LIMIT = 50


def _format_arguments(args: object) -> str:
    if isinstance(args, ToolArguments):
        values: Mapping[str, object] = args.values
    elif isinstance(args, Mapping):
        values = args
    else:
        values = {}
    return ", ".join(f"{key}={value}" for key, value in values.items())


def format_approval_prompt(req: object) -> str:
    """Format an approval request for display to the user."""
    tool_name = getattr(req, "tool_name", "unknown")
    call_id = getattr(req, "tool_call_id", "")
    tier = getattr(req, "tier", "unknown")
    args_str = _format_arguments(getattr(req, "arguments", {}))
    return (
        f"Approval Required [{str(tier).upper()}]\n"
        f"Tool: {tool_name}\n"
        f"ID: {call_id}\n"
        f"Args: {args_str}\n"
        f"Reply /approve or /deny"
    )


class ApprovalRenderer:
    """Approval prompt rendering and peer-message buffering.

    Approval state is owned by ``TurnStateStore`` and represented by
    ``ApprovalTransaction`` inside ``TurnSnapshot``. This helper never loads or
    saves approval state directly.
    """

    def __init__(
        self,
        *,
        approval_workspace: Path,
        agent: object | None = None,
        user_interface: ControlUserInterface | None = None,
        on_drain: Callable[[InputMessage], Awaitable[None]] | None = None,
    ) -> None:
        self._approval_workspace = approval_workspace
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
            return False, pending_snapshot

        approval = ReActSnapshotPolicy.approval_from_snapshot(pending_snapshot)
        if approval is None:
            return False, pending_snapshot

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

        return False, ReActSnapshotPolicy.replace_approval(pending_snapshot, approval)

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
