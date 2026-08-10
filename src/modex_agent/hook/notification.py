"""User-facing turn-outcome notification hooks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from modex_agent.core import AgentCommKind
from modex_agent.core.constants import StopReason
from modex_agent.core.types import OutputMessageType
from modex_agent.hook.abc import FinallyGraphHook

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.pipeline.adapters import OutputAdapter

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
        from modex_agent.core.types import OutputMessage

        try:
            await self._output_adapter.send(
                OutputMessage(content=text, message_type=NOTICE_MESSAGE_TYPE),
                session_id,
            )
        except Exception:
            logger.exception("send_notice failed: session=%s", session_id)


class TurnOutcomeNotifyHook(FinallyGraphHook):
    """Notifies the user on the two silent abnormal-end cases for main agents:
    a real exception, or hitting the iteration cap.

    Scope is intentionally narrow — many other abnormal ends ALREADY notify the
    user through their own path, so this hook must NOT duplicate them:

    - Pause / cancel (CANCELLED, TURN_CANCELLED): the control channel already
      acks the user and ``emit_complete`` fires.
    - Approval suspension (GraphInterrupt): ``turn_runner`` already renders the
      approval prompt. GraphInterrupt is re-raised out of ``ReActAgent.run``,
      so FINALLY_GRAPH sees the *initial* result ``stop_reason=ERROR`` with
      ``error=None``; requiring ``result.error`` to be truthy excludes it.
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

    async def finally_graph(self, ctx: AgentContext, result: AgentResult | None) -> None:
        if self._svc is None or result is None:
            return
        if ctx.comm_kind == AgentCommKind.SUBAGENT:
            return
        reason = result.stop_reason
        if reason == StopReason.MAX_ITERATIONS:
            text = self._MAX_ITERATIONS_NOTICE
        elif reason == StopReason.ERROR and result.error:
            # A real error (exception / LLM error). The ``result.error`` check
            # excludes the GraphInterrupt false-positive (initial result has
            # stop_reason=ERROR but error=None).
            text = self._ERROR_NOTICE
        else:
            # COMPLETED / CANCELLED / TURN_CANCELLED / TIMEOUT / approval
            # suspension — all either normal or already user-notified.
            return
        await self._svc.send_notice(str(ctx.session), text)
