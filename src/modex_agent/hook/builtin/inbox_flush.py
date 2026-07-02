"""InboxFlushHook — Inbox 消费 Hook。

在 turn 开始和每次迭代前 flush pending 消息到 history。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from modex_agent.core.types import MessageRole
from modex_agent.hook.abc import BeforeIterationHook, BeforeTurnHook
from modex_agent.multi_agent.message_type import AgentMessageType

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.history import MessageHistory
    from modex_agent.multi_agent.inbox.consumer import InboxConsumer


class InboxFlushHook(BeforeTurnHook, BeforeIterationHook):
    """Inbox 消费 Hook：在 turn 开始和每次迭代前 flush pending 消息到 history。

    幂等性由 InboxServer.consume() 的原子性 + InboxConsumer 本地缓存共同保证。
    """

    @property
    def name(self) -> str:
        return "inbox_flush_hook"

    def __init__(
        self,
        consumer: InboxConsumer,
        agent_name: str,
        max_messages_per_flush: int = 10,
    ) -> None:
        self._consumer = consumer
        self._agent_name = agent_name
        self._max_messages = max_messages_per_flush

    async def before_turn(self, ctx: AgentContext) -> None:
        await self._flush(ctx.history, str(ctx.session))

    async def before_iteration(self, ctx: AgentContext) -> None:
        await self._flush(ctx.history, str(ctx.session))

    @staticmethod
    def _sanitize_content(content: str) -> str:
        """对 inbox 消息内容进行基本安全过滤，防止 prompt injection。"""
        if not content:
            return content
        content = re.sub(
            r"<\s*system\b[^>]*>.*?<\s*/\s*system\s*>",
            "",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content.strip()

    async def _flush(self, history: MessageHistory, session_id: str | None) -> bool:
        if not session_id:
            return False
        messages = await self._consumer.consume(
            session_id,
            limit=self._max_messages,
            only_types=AgentMessageType.fold_eligible(),
        )
        if not messages:
            return False

        for msg in messages:
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", msg.source)[:64] or "agent"
            sanitized = self._sanitize_content(msg.content)
            await history.append(
                {
                    "role": MessageRole.AGENT,
                    "source_agent": safe_name,
                    "content": sanitized,
                    "meta_inbox": True,
                    "meta_source": msg.source,
                    "meta_target_agent": self._agent_name,
                }
            )
        return True
