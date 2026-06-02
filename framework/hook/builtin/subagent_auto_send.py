"""SubagentAutoSendHook — agent 自动转发 Hook。

确保 agent 内容总是转发给父 agent（main），即使 LLM 忘记调用 send_to_agent。
这是一个安全网，不替代系统提示词指引。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.multi_agent.bus import AgentMessageBus

from framework.hook.abc import AfterTurnHook

logger = logging.getLogger(__name__)


class SubagentAutoSendHook(AfterTurnHook):
    """Peer agent auto-send hook。

    在 agent turn 结束后自动将内容转发给父 agent。
    """

    @property
    def name(self) -> str:
        return "subagent_auto_send_hook"

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
        agent_bus: AgentMessageBus | None = None,
        self_name: str = "",
        parent_name: str = "main",
        notification_service: Any | None = None,  # noqa: ANN401
    ) -> None:
        self._agent_bus = agent_bus
        self._self_name = self_name
        self._parent_name = parent_name
        self._svc = notification_service
        # Track sessions where the subagent has already sent a message
        # via send_to_agent (send_to_agent_async kept for transition compat).
        # happened in a session, subsequent turns should not auto-forward.
        self._communicated: set[str] = set()

    async def before_turn(self, ctx: AgentContext) -> None:
        """No-op kept for backward compatibility with existing callers."""
        pass

    async def after_turn(self, ctx: AgentContext, result: Any = None) -> None:  # noqa: ANN401
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
            sent_tools = {"send_to_agent"}
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

        # Also forward on max_iterations — the subagent may have produced
        # output that the parent needs to see, even if it hit its step limit.
        # MaxIterationNotifyHook handles notification separately.

        # No agent_bus wired yet — no-op (wired later by pool/subagent service)
        if self._agent_bus is None:
            return

        agent_bus = self._agent_bus  # narrow None

        # Derive reply target from session_meta, fallback to parent_name
        reply_target = self._parent_name
        invocation_id: str | None = None

        if ctx.session_meta is not None:
            invocation_id = ctx.session_meta.invocation_id if hasattr(ctx.session_meta, 'invocation_id') else None

        session_id = ctx.session_id or ""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.message_xml import build_agent_result
        from framework.multi_agent.session_id import DefaultSessionIdStrategy

        strategy = DefaultSessionIdStrategy(main_agent_name=self._parent_name)
        parts = strategy.parse(session_id)
        conversation_id = parts.conversation_id
        invocation_id = parts.invocation_id
        inbox_key = strategy.format(conversation_id=conversation_id, agent_name=reply_target)

        logger.info(
            "SubagentAutoSendHook: auto-forwarding subagent %s content to %s (len=%d)",
            self._self_name,
            reply_target,
            len(result.content),
        )

        sanitized = self._sanitize_forward_content(result.content)

        xml_content = build_agent_result(
            source=self._self_name,
            invocation_id=invocation_id,
            status="completed",
            stop_reason="missed_communication",
            content=sanitized,
        )

        envelope = AgentMessageEnvelope(
            payload={
                "content": xml_content,
                "message_type": "agent_result",
                "metadata": {"agent_type": self._self_name},
            },
            source=AgentAddress(name=self._self_name),
            target=AgentAddress(name=reply_target),
            message_type="agent_result",
            conversation_id=conversation_id,
            agent_session_id=inbox_key,
            invocation_id=parts.invocation_id,
        )

        forwarded = False
        try:
            await agent_bus.send(inbox_key, envelope)
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
                notification_xml = build_agent_result(
                    source=self._self_name,
                    invocation_id=invocation_id,
                    status="missed_communication",
                    stop_reason="missed_communication",
                    content=sanitized[:2000] if sanitized else "",
                )
                await self._svc.notify(ctx=ctx, xml_content=notification_xml)
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
