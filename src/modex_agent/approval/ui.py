"""Approval system user interface — IM interaction for approval prompts."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import TYPE_CHECKING
from uuid import uuid4

from modex_agent.core.types import OutputMessage

if TYPE_CHECKING:
    from modex_agent.approval.views import ApprovalRequestView
    from modex_agent.adapters.output import OutputAdapter

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
    async def render_approval_prompt(
        self,
        session_id: str,
        view: ApprovalRequestView,
    ) -> None:
        """Push a structured approval request (IM text + webui view)."""
        ...


class IMUserInterface(ApprovalUserInterface):
    """IM interaction (QQ/Discord/Telegram etc). Uses OutputAdapter for sending."""

    def __init__(
        self,
        *,
        output_adapter: OutputAdapter,
    ) -> None:
        self._output = output_adapter

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

    async def render_approval_prompt(self, session_id: str, view: ApprovalRequestView) -> None:
        from modex_agent.pipeline.approval_renderer import approval_output_message

        try:
            await self._output.send(approval_output_message(view), session_id)
        except Exception:
            logger.exception("IMUserInterface.render_approval_prompt failed: session=%s", session_id)
