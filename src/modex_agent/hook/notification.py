"""User-facing turn-outcome notification hooks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from modex_agent.core import AgentCommKind
from modex_agent.core.emitter import StopReason
from modex_agent.hook.abc import OutcomeFinallyHook
from modex_agent.messaging.models import OutputMessageType

if TYPE_CHECKING:
    from modex_agent.adapters.output import OutputAdapter
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult

logger = logging.getLogger(__name__)

#: OutputMessage.message_type value marking a transient user notice (turn-outcome
#: or compaction). A ChannelRouter fan-outs notices to the originating channel
#: AND the WebUI observer; other adapters render it as plain text. Notices are
#: never persisted to session memory/history.
NOTICE_MESSAGE_TYPE: Final[OutputMessageType] = OutputMessageType.NOTICE


class AgentNotificationService:
    """Deliver transient notices through the configured output adapter."""

    def __init__(self, output_adapter: OutputAdapter) -> None:
        self._output_adapter = output_adapter

    async def send_notice(self, session_id: str, text: str) -> None:
        """Deliver a transient plain-text notice to the user.

        Tagged ``message_type=notice`` so a ChannelRouter can fan it out to the
        originating channel AND the WebUI observer; non-routing adapters simply
        render it as text. Notices are never written to session memory/history.
        """
        from modex_agent.messaging.models import OutputMessage

        try:
            await self._output_adapter.send(
                OutputMessage(content=text, message_type=NOTICE_MESSAGE_TYPE),
                session_id,
            )
        except Exception:
            logger.exception("send_notice failed: session=%s", session_id)


class TurnOutcomeNotifyHook(OutcomeFinallyHook):
    """Notifies the user on the two silent abnormal-end cases for main agents:
    a real exception, or hitting the iteration cap.

    Scope is intentionally narrow — many other abnormal ends ALREADY notify
    the user through their own path, so this hook must NOT duplicate them:

    - Pause / cancel (CANCELLED, TURN_CANCELLED): the control channel already
      acks the user and ``emit_complete`` fires.
    - Approval suspension (GraphInterrupt): ``turn_runner`` already renders the
      approval prompt. The suspend leg (``result=None``) never reaches
      ``on_outcome`` — skipped by ``OutcomeFinallyHook``.
    - Normal completion.

    Only fires for NORMAL main agents; subagent outcomes go through
    ``SubagentAutoSendHook``.
    """

    _MAX_ITERATIONS_NOTICE = (
        "Reached the maximum reasoning steps without finishing. "
        "Please rephrase or continue."
    )
    _ERROR_NOTICE = "The turn ended unexpectedly due to an error. Please try again."

    @property
    def name(self) -> str:
        return "turn_outcome_notify"

    def __init__(self, notification_service: AgentNotificationService | None = None) -> None:
        self._svc = notification_service

    async def on_outcome(self, ctx: AgentContext, result: AgentResult) -> None:
        if self._svc is None:
            return
        if ctx.comm_kind == AgentCommKind.SUBAGENT:
            return
        reason = result.stop_reason
        if reason == StopReason.MAX_ITERATIONS:
            text = self._MAX_ITERATIONS_NOTICE
        elif reason == StopReason.ERROR and result.error:
            # A real error (exception / LLM error). The ``result.error`` check
            # excludes the pre-turn default ``AgentResult(stop_reason=ERROR)``
            # with ``error=None`` (e.g. legacy crash paths).
            text = self._ERROR_NOTICE
        else:
            # COMPLETED / CANCELLED / TURN_CANCELLED / TIMEOUT — all either
            # normal or already user-notified.
            return
        await self._svc.send_notice(str(ctx.session), text)
