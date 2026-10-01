"""Approval rendering helpers for turn-state based approval flow."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from modex_agent.core.turn.approval_types import ApprovalDecision
from modex_agent.core.turn.models import TurnSnapshot
from modex_agent.messaging.models import ApprovalAction, InputMessage

if TYPE_CHECKING:
    from modex_agent.approval.ui import ApprovalUserInterface

logger = logging.getLogger(__name__)

_UNRELATED_INPUT_PREVIEW_LIMIT = 50


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

    @property
    def user_interface(self) -> ApprovalUserInterface | None:
        return self._user_interface

    @user_interface.setter
    def user_interface(self, value: ApprovalUserInterface | None) -> None:
        self._user_interface = value

    @property
    def on_drain(self) -> Callable[[InputMessage], Awaitable[None]] | None:
        return self._on_drain

    @on_drain.setter
    def on_drain(self, value: Callable[[InputMessage], Awaitable[None]] | None) -> None:
        self._on_drain = value

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

        approval = ReActTurnState.from_checkpoint(dict(pending_snapshot.state_payload)).approval
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
        on_drain = self._on_drain
        if pending and on_drain is None:
            logger.warning(
                "ApprovalRenderer: _on_drain is None, dropping %d buffered messages for %s",
                len(pending),
                session_id,
            )
            return
        for msg in pending:
            if on_drain is not None:
                asyncio.ensure_future(on_drain(msg))
