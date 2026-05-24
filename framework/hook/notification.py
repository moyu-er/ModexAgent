"""Agent notification hooks — max-iteration and missed-communication alerts."""
from __future__ import annotations

import logging
import xml.sax.saxutils as saxutils
from typing import TYPE_CHECKING

from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.session_id import DefaultSessionIdStrategy

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
        session_strategy: DefaultSessionIdStrategy | None = None,
        parent_map: dict[str, str] | None = None,
    ):
        self._output_adapter = output_adapter
        self._agent_bus = agent_bus
        self._session_strategy = session_strategy or DefaultSessionIdStrategy()
        self._parent_map = parent_map or {}

    async def notify(
        self,
        ctx: AgentContext,
        notification_type: str,
        reason: str,
        details: str,
        content: str | None = None,
        content_max_chars: int = 2000,
    ) -> None:
        xml = self._build_xml(notification_type, reason, details, content, content_max_chars)
        if (
            ctx.session_meta is not None
            and ctx.session_meta.comm_kind == AgentCommKind.SUBAGENT
        ):
            parent = self._parent_map.get(ctx.session_meta.agent_name)
            await self._notify_parent(ctx, xml, parent)
        else:
            await self._notify_user(ctx, xml)

    def _build_xml(
        self,
        notification_type: str,
        reason: str,
        details: str,
        content: str | None,
        max_chars: int,
    ) -> str:
        lines = [
            f'<agent_notification type="{saxutils.escape(notification_type)}">',
            f"  <reason>{saxutils.escape(reason)}</reason>",
            f"  <details>{saxutils.escape(details)}</details>",
        ]
        if content:
            truncated = content[:max_chars]
            if len(content) > max_chars:
                truncated += "\n... (truncated)"
            lines.append(f"  <truncated_content>{saxutils.escape(truncated)}</truncated_content>")
        lines.append("</agent_notification>")
        return "\n".join(lines)

    async def _notify_user(self, ctx: AgentContext, xml: str) -> None:
        from framework.core.types import OutputMessage
        await self._output_adapter.send(
            OutputMessage(content=xml), ctx.session_id,
        )

    async def _notify_parent(
        self, ctx: AgentContext, xml: str, parent_name: str | None,
    ) -> None:
        if not parent_name:
            logger.warning(
                "No parent mapped for subagent '%s', dropping notification",
                ctx.session_meta.agent_name if ctx.session_meta else "unknown",
            )
            return

        session_id = ctx.session_id or ""
        parts = self._session_strategy.parse(session_id)
        inbox_key = self._session_strategy.format(
            conversation_id=parts.conversation_id,
            agent_name=parent_name,
        )

        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope

        envelope = AgentMessageEnvelope(
            payload={"content": xml, "message_type": "agent_notification"},
            source=AgentAddress(name=ctx.session_meta.agent_name),
            target=AgentAddress(name=parent_name),
            message_type="agent_notification",
            conversation_id=parts.conversation_id,
            agent_session_id=inbox_key,
        )
        await self._agent_bus.send(inbox_key, envelope)


class MaxIterationNotifyHook:
    """Sends XML notification when agent hits max_iterations.

    Agent-agnostic: same instance works for NORMAL and SUBAGENT agents.
    Routing is handled internally by AgentNotificationService.
    """

    def __init__(self, notification_service: AgentNotificationService):
        self._svc = notification_service

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if getattr(result, "stop_reason", None) != "max_iterations":
            return

        agent_name = (
            ctx.session_meta.agent_name
            if ctx.session_meta
            else "unknown"
        )
        truncated = None
        content = getattr(result, "content", None)
        if content:
            truncated = content[:2000]
            if len(content) > 2000:
                truncated += "\n... (truncated)"

        await self._svc.notify(
            ctx=ctx,
            notification_type="max_iterations_exceeded",
            reason="迭代次数达到上限而退出",
            details=(
                f"agent '{agent_name}' 已达到最大迭代次数 "
                f"(max_iterations={ctx.max_iterations})"
            ),
            content=truncated,
        )
