"""Inbox 消费 Hook。"""

import re

from framework.core.agent import AgentContext
from framework.core.types import MessageRole
from framework.memory.history import MessageHistory
from framework.core.hooks import AgentRunHook

from .consumer import InboxConsumer


class InboxFlushHook(AgentRunHook):
    """Inbox 消费 Hook：在 turn 开始和每次迭代前 flush pending 消息到 history。

    重要：本 Hook 不维护任何持久化去重状态。
    幂等性由 InboxServer.consume() 的原子性 + InboxConsumer 本地缓存共同保证。
    """

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
        await self._flush(ctx.history, ctx.metadata.get("session_id"))

    async def before_iteration(self, ctx: AgentContext) -> None:
        await self._flush(ctx.history, ctx.metadata.get("session_id"))

    @staticmethod
    def _sanitize_content(content: str) -> str:
        """对 inbox 消息内容进行基本安全过滤，防止 prompt injection。

        注意：当前过滤规则是防御性启发式策略，可能存在误报（例如合法地
        包含 tool_calls 示例代码块时会被规则 2 误移除）。后续可根据业务
        场景引入 `strict_mode` 开关进行更精细的控制。
        """
        if not content:
            return content
        # 1. 移除 <system> 标签及其内容
        content = re.sub(
            r"<\s*system\b[^>]*>.*?<\s*/\s*system\s*>", "", content, flags=re.IGNORECASE | re.DOTALL
        )
        # 2. 移除伪装的 tool_calls JSON 块（防御性启发式，存在误报风险）
        content = re.sub(
            r"```\s*json\s*\{\s*[\"']tool_calls[\"'].*?```",
            "",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        # 3. 清理连续空行
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content.strip()

    async def _flush(self, history: MessageHistory, session_id: str | None) -> bool:
        if not session_id:
            return False
        messages = await self._consumer.consume(session_id, limit=self._max_messages)
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
