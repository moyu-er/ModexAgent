"""InboxFlushHook — Inbox 消费 Hook。

在 turn 开始和每次迭代前 flush pending 消息到 history。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.core.types import ReminderKind
from modex_agent.hook.abc import BeforeIterationHook, StartNodeTurnHook
from modex_agent.multi_agent.message_format import build_agent_reminder_record
from modex_agent.multi_agent.message_type import AgentMessageType

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.history import MessageHistory
    from modex_agent.multi_agent.inbox.consumer import InboxConsumer


class InboxFlushHook(StartNodeTurnHook, BeforeIterationHook):
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

    async def start_node_turn(self, ctx: AgentContext) -> None:
        await self._flush(ctx.history, str(ctx.session))

    async def before_iteration(self, ctx: AgentContext) -> None:
        await self._flush(ctx.history, str(ctx.session))

    async def _flush(self, history: MessageHistory, session_id: str | None) -> bool:
        if not session_id:
            return False
        messages = await self._consumer.consume(
            session_id,
            limit=self._max_messages,
            only_types={message_type.value for message_type in AgentMessageType.fold_eligible()},
        )
        if not messages:
            return False

        for msg in messages:
            msg_meta = msg.metadata or {}
            reminder_kind_raw = msg_meta.get("reminder_kind")
            reminder_kind = ReminderKind(reminder_kind_raw) if reminder_kind_raw else None
            invocation_id_raw = msg_meta.get("invocation_id")
            invocation_id = str(invocation_id_raw) if invocation_id_raw else None
            append_dict = build_agent_reminder_record(
                msg.content,
                source_agent=msg.source,
                reminder_kind=reminder_kind,
                message_type=AgentMessageType(msg.message_type),
                invocation_id=invocation_id,
            )
            append_dict.update(
                {
                    "meta_inbox": True,
                    "meta_source": msg.source,
                    "meta_target_agent": self._agent_name,
                }
            )
            await history.append(append_dict)
        return True
