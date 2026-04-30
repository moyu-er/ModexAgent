"""Extended tests for multi-agent communication — message routing, inbox flow."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from framework.multi_agent.inbox.server_memory import InMemoryInboxServer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.tools import SendMessageTool
from framework.multi_agent.session_id import DefaultSessionIdStrategy
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.envelope import AgentMessageEnvelope


class TestMessageRouting:
    """消息路由边界情况。"""

    async def test_send_message_tool_publishes_to_target(self):
        from framework.messaging.broker_memory import InMemoryMessageBroker

        broker = InMemoryMessageBroker()
        await broker.start()
        try:
            tool = SendMessageTool(
                broker=broker,
                self_address=AgentAddress(kind="agent", name="main"),
                allowed_targets=["peer1"],
            )
            result = await tool.execute(
                target_agent="peer1",
                content="hello peer",
                conversation_id="conv1",
            )
            assert isinstance(result, str)
            assert "peer1" in result or "Sent" in result or "sent" in result.lower()
        finally:
            await broker.stop()

    async def test_send_message_rejected_for_unauthorized_target(self):
        from framework.messaging.broker_memory import InMemoryMessageBroker

        broker = InMemoryMessageBroker()
        await broker.start()
        try:
            tool = SendMessageTool(
                broker=broker,
                self_address=AgentAddress(kind="agent", name="main"),
                allowed_targets=["peer1"],
            )
            result = await tool.execute(
                target_agent="peer2",
                content="hello",
                conversation_id="conv1",
            )
            # Should indicate failure via str or raise
            assert isinstance(result, str)
            assert "not allowed" in result.lower() or "rejected" in result.lower()
        finally:
            await broker.stop()

    async def test_message_envelope_construction(self):
        envelope = AgentMessageEnvelope(
            payload={"content": "hello peer1"},
            source=AgentAddress(kind="agent", name="main"),
            target=AgentAddress(kind="agent", name="peer1"),
            conversation_id="conv1",
            agent_session_id="conv1:main",
        )
        assert envelope.source.name == "main"
        assert envelope.target.name == "peer1"
        assert envelope.conversation_id == "conv1"


class TestAsyncMessageRouting:
    """异步消息路由 — inbox 存储和消费者读取。"""

    async def test_async_send_stores_in_target_inbox(self):
        inbox = InMemoryInboxServer()
        producer = InboxProducer(inbox)
        consumer = InboxConsumer(inbox)

        envelope = AgentMessageEnvelope(
            payload={"content": "hello from peer"},
            source=AgentAddress(kind="agent", name="peer1"),
            target=AgentAddress(kind="agent", name="main"),
            conversation_id="conv1",
            agent_session_id="conv1:main",
        )
        await producer.send("conv1:main", envelope)
        messages = await consumer.consume("conv1:main")
        assert isinstance(messages, list)
        assert len(messages) >= 1

    async def test_empty_inbox_returns_empty_list(self):
        inbox = InMemoryInboxServer()
        consumer = InboxConsumer(inbox)
        messages = await consumer.consume("unknown:agent")
        assert isinstance(messages, list)
        assert len(messages) == 0


class TestSessionIdStrategy:
    """Session ID 策略格式。"""

    def test_main_session_format(self):
        strategy = DefaultSessionIdStrategy()
        sid = strategy.main_session("conv1")
        assert "conv1" in sid

    def test_target_session_format(self):
        strategy = DefaultSessionIdStrategy()
        sid = strategy.target_session("conv1", "peer1")
        assert "conv1" in sid
        assert "peer1" in sid

    def test_parse_returns_tuple(self):
        strategy = DefaultSessionIdStrategy()
        result = strategy.parse("conv1:main")
        assert isinstance(result, tuple)
        assert result[0] == "conv1"
        assert result[1] == "main"
