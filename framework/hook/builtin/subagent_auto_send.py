"""SubagentAutoSendHook — agent 自动转发 Hook。

确保 agent 内容总是转发给父 agent（main），即使 LLM 忘记调用 send_to_agent_async。
这是一个安全网，不替代系统提示词指引。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.multi_agent.bus import AgentMessageBus

logger = logging.getLogger(__name__)


class SubagentAutoSendHook:
    """Peer agent auto-send hook。

    在 agent turn 结束后自动将内容转发给父 agent。
    """

    _THINK_PAIRED_RE = re.compile(
        r"<\s*(?:think|reasoning|reflection)\b[^>]*(?:>|\n)"
        r"(.*?)</\s*(?:think|reasoning|reflection)\b[^>]*(?:>|\n)",
        re.IGNORECASE | re.DOTALL,
    )
    _THINK_TAG_RE = re.compile(
        r"<\s*/?\s*(?:think|reasoning|reflection)\b[^>]*>?",
        re.IGNORECASE,
    )

    def __init__(
        self,
        agent_bus: AgentMessageBus,
        self_name: str,
        parent_name: str = "main",
        notification_service: Any | None = None,
    ) -> None:
        self._agent_bus = agent_bus
        self._self_name = self_name
        self._parent_name = parent_name
        self._svc = notification_service
        # Track sessions where the subagent has already sent a message
        # via send_to_agent / send_to_agent_async.  Once communication has
        # happened in a session, subsequent turns should not auto-forward.
        self._communicated: set[str] = set()

    async def before_turn(self, ctx: AgentContext) -> None:
        pass

    async def after_turn(self, ctx: AgentContext, result: Any = None) -> None:
        if not result or not getattr(result, "content", None):
            return

        rt = ctx.runtime
        if rt is None:
            return
        rc = rt._runtime_context
        if rc is None:
            rt_mgr = rt.services.runtime_context_manager
            if rt_mgr is not None:
                rc = await rt_mgr.get_context(
                    ctx.session_id, None
                )
                rt._runtime_context = rc
        if rc is not None:
            calls = await rc.get_tool_calls()
            sent_tools = {"send_to_agent", "send_to_agent_async"}
            if any(c.tool_name in sent_tools for c in calls):
                self._communicated.add(ctx.session_id)
                logger.debug(
                    "SubagentAutoSendHook: skipped, message already sent via tool (agent=%s)",
                    self._self_name,
                )
                return

        # Already communicated in this session — skip auto-forward
        if ctx.session_id in self._communicated:
            logger.debug(
                "SubagentAutoSendHook: skipped, already communicated (agent=%s)",
                self._self_name,
            )
            return

        logger.info(
            "SubagentAutoSendHook: auto-forwarding subagent %s content to %s (len=%d)",
            self._self_name,
            self._parent_name,
            len(result.content),
        )

        session_id = ctx.session_id or ""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.session_id import DefaultSessionIdStrategy

        strategy = DefaultSessionIdStrategy(main_agent_name=self._parent_name)
        parts = strategy.parse(session_id)
        conversation_id = parts.conversation_id
        inbox_key = strategy.format(conversation_id=conversation_id, agent_name=self._parent_name)

        sanitized = self._sanitize_forward_content(result.content)

        envelope = AgentMessageEnvelope(
            payload={"content": sanitized, "message_type": "agent_message"},
            source=AgentAddress(name=self._self_name),
            target=AgentAddress(name=self._parent_name),
            message_type="agent_message",
            conversation_id=conversation_id,
            agent_session_id=inbox_key,
            invocation_id=parts.invocation_id,
        )

        forwarded = False
        try:
            await self._agent_bus.send(inbox_key, envelope)
            forwarded = True
            logger.info(
                "Auto-forwarded subagent %s content to %s (session=%s)",
                self._self_name,
                self._parent_name,
                session_id,
            )
        except Exception:
            logger.exception(
                "Failed to auto-forward subagent %s content to %s",
                self._self_name,
                self._parent_name,
            )

        # Send XML notification if notification_service is configured
        if self._svc is not None and forwarded:
            try:
                await self._svc.notify(
                    ctx=ctx,
                    notification_type="missed_communication",
                    reason="subagent 未通过通信工具发送消息",
                    details=(
                        f"agent '{self._self_name}' 已完成但未调用 "
                        f"send_to_agent_async，内容已自动转发给 "
                        f"'{self._parent_name}'"
                    ),
                    content=sanitized[:2000] if sanitized else None,
                )
            except Exception:
                logger.exception(
                    "Failed to send missed_communication notification for %s",
                    self._self_name,
                )

    @classmethod
    def _sanitize_forward_content(cls, content: str) -> str:
        """Strip LLM reasoning tags and apply inbox sanitization."""
        from framework.hook.builtin.inbox_flush import InboxFlushHook

        content = cls._THINK_PAIRED_RE.sub("", content)
        content = cls._THINK_TAG_RE.sub("", content)
        content = InboxFlushHook._sanitize_content(content)
        return content
