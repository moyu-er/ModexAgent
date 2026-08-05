"""Agent notification hooks — turn-outcome, max-iteration and missed-communication alerts."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.core import AgentCommKind
from modex_agent.core.constants import StopReason
from modex_agent.core.types import ReminderKind
from modex_agent.hook.abc import AfterTurnHook, FinallyTurnHook

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.multi_agent.bus import AgentMessageBus
    from modex_agent.pipeline.adapters import OutputAdapter

logger = logging.getLogger(__name__)

#: OutputMessage.message_type value marking a transient user notice (turn-outcome
#: or compaction). A ChannelRouter fan-outs notices to the originating channel
#: AND the WebUI observer; other adapters render it as plain text. Notices are
#: never persisted to session memory/history.
NOTICE_MESSAGE_TYPE = "notice"


class AgentNotificationService:
    """Unified notification routing by comm_kind.

    NORMAL → output_adapter (user)
    SUBAGENT → agent_bus inbox (parent)
    """

    def __init__(
        self,
        output_adapter: OutputAdapter,
        agent_bus: AgentMessageBus,
        parent_agent_name: str = "main",
    ) -> None:
        self._output_adapter = output_adapter
        self._agent_bus = agent_bus
        self._parent_agent_name = parent_agent_name

    async def notify(
        self,
        ctx: AgentContext,
        content: str,
    ) -> None:
        if ctx.comm_kind == AgentCommKind.SUBAGENT:
            await self._notify_parent(ctx, content)
        else:
            await self._notify_user(ctx, content)

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

    async def _notify_user(self, ctx: AgentContext, content: str) -> None:
        from modex_agent.core.types import OutputMessage

        await self._output_adapter.send(
            OutputMessage(content=content),
            str(ctx.session),
        )

    async def _notify_parent(self, ctx: AgentContext, content: str) -> None:
        parent_name = self._parent_agent_name

        parent_session_id = ctx.session.parent_session_id
        if parent_session_id is None:
            logger.warning(
                "AgentNotificationService: no parent_session_id for session %s",
                str(ctx.session),
            )
            return
        inbox_key = parent_session_id

        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.envelope import AgentMessageEnvelope
        from modex_agent.multi_agent.message_type import AgentMessageType

        envelope = AgentMessageEnvelope(
            payload={"content": content, "message_type": AgentMessageType.AGENT_RESULT},
            source=AgentAddress(name=ctx.session.agent_name),
            target=AgentAddress(name=parent_name),
            message_type=AgentMessageType.AGENT_RESULT,
            session_id=str(ctx.session),
            agent_session_id=inbox_key,
            metadata={"reminder_kind": ReminderKind.SUBAGENT_MAX_ITERATIONS},
        )
        await self._agent_bus.send(inbox_key, envelope)


class MaxIterationNotifyHook(AfterTurnHook):
    """Sends markdown notification to the PARENT when a SUBAGENT hits max_iterations.

    Subagent-only: the user-facing max-iteration notice for NORMAL main agents is
    owned by :class:`TurnOutcomeNotifyHook` (plain text). This hook retains the
    structured markdown result for the subagent→parent result channel.
    """

    @property
    def name(self) -> str:
        return "max_iteration_notify"

    def __init__(self, notification_service: AgentNotificationService | None = None) -> None:
        self._svc = notification_service

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if self._svc is None:
            return
        if ctx.comm_kind != AgentCommKind.SUBAGENT:
            return
        if result.stop_reason != "max_iterations":
            return

        agent_name = ctx.session.agent_name if ctx.session else "unknown"
        invocation_id = ctx.session.session_id_prefix if ctx.session else None

        content = result.content or ""
        truncated = content[:2000]
        if len(content) > 2000:
            truncated += "\n... (truncated)"

        from modex_agent.multi_agent.message_xml import build_agent_result

        markdown_content = build_agent_result(
            source=agent_name,
            invocation_id=invocation_id,
            status="failed",
            stop_reason="max_iterations",
            content=truncated,
        )
        await self._svc.notify(ctx=ctx, content=markdown_content)


class TurnOutcomeNotifyHook(FinallyTurnHook):
    """Notifies the user on the two silent abnormal-end cases for main agents:
    a real exception, or hitting the iteration cap.

    Scope is intentionally narrow — many other abnormal ends ALREADY notify the
    user through their own path, so this hook must NOT duplicate them:

    - Pause / cancel (CANCELLED, TURN_CANCELLED): the control channel already
      acks the user and ``emit_complete`` fires.
    - Approval suspension (GraphInterrupt): ``turn_runner`` already renders the
      approval prompt. GraphInterrupt is re-raised out of ``ReActAgent.run``,
      so FINALLY_TURN sees the *initial* result ``stop_reason=ERROR`` with
      ``error=None``; requiring ``result.error`` to be truthy excludes it.
    - Normal completion.

    Only fires for NORMAL main agents; subagent outcomes go through
    :class:`MaxIterationNotifyHook` / SubagentAutoSendHook.
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

    async def finally_turn(self, ctx: AgentContext, result: AgentResult | None) -> None:
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
