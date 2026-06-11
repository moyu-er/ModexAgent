"""Approval system user interface — IM interaction for approval prompts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time as _time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING
from uuid import uuid4

from framework.control.channel import InMemoryControlChannel
from framework.control.types import ControlCommandType, ControlScope
from framework.core.types import OutputMessage

if TYPE_CHECKING:
    from framework.pipeline.adapters import OutputAdapter

logger = logging.getLogger(__name__)


class ApprovalUserInterface(ABC):
    """User interface for approval scenarios.

    Renamed from ControlUserInterface — this is approval-specific UI,
    not control-plane UI.
    """

    @abstractmethod
    async def render_message(
        self,
        session_id: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        """Display a message (no reply expected). Returns message_id."""
        ...

    @abstractmethod
    async def render_question(
        self,
        session_id: str,
        question: str,
        options: Sequence[str],
        timeout: float,
        metadata: Mapping[str, object] | None = None,
    ) -> str | None:
        """Display a question, wait for selection. Returns None on timeout."""
        ...

    @abstractmethod
    async def update_message(
        self,
        session_id: str,
        message_id: str,
        content: str,
    ) -> None:
        """Update a previously sent message."""
        ...


class IMUserInterface(ApprovalUserInterface):
    """IM interaction (QQ/Discord/Telegram etc).

    Uses OutputAdapter for sending, InMemoryControlChannel for polling responses.
    """

    def __init__(
        self,
        *,
        output_adapter: OutputAdapter,
        channel: InMemoryControlChannel,
    ) -> None:
        self._output = output_adapter
        self._channel = channel

    async def render_message(
        self,
        session_id: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        msg_id = uuid4().hex[:12]
        try:
            msg = OutputMessage(
                content=content,
                metadata=dict(metadata or {}),
            )
            await self._output.send(msg, session_id)
        except Exception:
            logger.exception("IMUserInterface.render_message failed: session=%s", session_id)
        return msg_id

    async def render_question(
        self,
        session_id: str,
        question: str,
        options: Sequence[str],
        timeout: float,
        metadata: Mapping[str, object] | None = None,
    ) -> str | None:
        await self.render_message(session_id, question, metadata)
        scope = ControlScope(session_id=session_id)
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            cmds = await self._channel.drain(
                scope,
                limit=1,
                command_types={ControlCommandType.APPROVAL_RESPONSE},
            )
            for cmd in cmds:
                action = str(cmd.payload.get("action", ""))
                if action in options:
                    return action
            await asyncio.sleep(0.3)
        return None

    async def update_message(
        self,
        session_id: str,
        message_id: str,
        content: str,
    ) -> None:
        with contextlib.suppress(Exception):
            msg = OutputMessage(
                content=content,
                metadata={"_edit_id": message_id},
            )
            await self._output.send(msg, session_id)
