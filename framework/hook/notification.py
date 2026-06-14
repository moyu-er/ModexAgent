"""Agent notification hooks — max-iteration and missed-communication alerts."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from framework.multi_agent.comm_kind import AgentCommKind

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult
    from framework.multi_agent.bus import AgentMessageBus
    from framework.pipeline.adapters import OutputAdapter

logger = logging.getLogger(__name__)


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
        xml_content: str,
    ) -> None:
        if ctx.comm_kind == AgentCommKind.SUBAGENT:
            await self._notify_parent(ctx, xml_content)
        else:
            await self._notify_user(ctx, xml_content)

    async def _notify_user(self, ctx: AgentContext, xml: str) -> None:
        from framework.core.types import OutputMessage

        await self._output_adapter.send(
            OutputMessage(content=xml),
            str(ctx.session),
        )

    async def _notify_parent(self, ctx: AgentContext, xml: str) -> None:
        parent_name = self._parent_agent_name

        parent_session_id = ctx.session.parent_session_id
        if parent_session_id is None:
            logger.warning(
                "AgentNotificationService: no parent_session_id for session %s",
                str(ctx.session),
            )
            return
        inbox_key = parent_session_id

        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope

        envelope = AgentMessageEnvelope(
            payload={"content": xml, "message_type": "agent_result"},
            source=AgentAddress(name=ctx.session.agent_name),
            target=AgentAddress(name=parent_name),
            message_type="agent_result",
            conversation_id=str(ctx.session),
            agent_session_id=inbox_key,
        )
        await self._agent_bus.send(inbox_key, envelope)


class MaxIterationNotifyHook:
    """Sends XML notification when agent hits max_iterations.

    Agent-agnostic: same instance works for NORMAL and SUBAGENT agents.
    Routing is handled internally by AgentNotificationService.
    """

    def __init__(self, notification_service: AgentNotificationService | None = None) -> None:
        self._svc = notification_service

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if self._svc is None:
            return
        if getattr(result, "stop_reason", None) != "max_iterations":
            return

        agent_name = ctx.session.agent_name if ctx.session else "unknown"
        invocation_id = ctx.session.snowflake if ctx.session else None

        content = result.content or ""
        truncated = content[:2000]
        if len(content) > 2000:
            truncated += "\n... (truncated)"

        from framework.multi_agent.message_xml import build_agent_result

        xml = build_agent_result(
            source=agent_name,
            invocation_id=invocation_id,
            status="max_iterations",
            stop_reason="max_iterations",
            content=truncated,
        )
        await self._svc.notify(ctx=ctx, xml_content=xml)
