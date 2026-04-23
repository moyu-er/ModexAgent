"""Tests for AgentMessageBus and LocalAgentMessageBus."""

import asyncio

import pytest

from framework.multi_agent.address import AgentAddress
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_memory import InMemoryInboxServer
from framework.messaging.broker_memory import InMemoryMessageBroker


class TestLocalAgentMessageBus:
    async def test_send_persists_message(self):
        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        consumer = InboxConsumer(server=server)
        bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(kind="agent", name="a1"),
            target=AgentAddress(kind="agent", name="a2"),
            message_type="agent_message",
        )
        await bus.send("s1", envelope)
        assert await server.count("s1") == 1

    async def test_poll_returns_immediately(self):
        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        consumer = InboxConsumer(server=server)
        bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(kind="agent", name="a1"),
            target=AgentAddress(kind="agent", name="a2"),
            message_type="agent_message",
        )
        await bus.send("s1", envelope)

        results = await bus.poll("s1", limit=10)
        assert len(results) == 1
        assert results[0].payload["content"] == "hello"

    async def test_consume_blocks_and_wakes(self):
        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        consumer = InboxConsumer(server=server)
        bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

        envelope = AgentMessageEnvelope(
            payload={"content": "delayed"},
            source=AgentAddress(kind="agent", name="a1"),
            target=AgentAddress(kind="agent", name="a2"),
            message_type="agent_message",
        )

        consumed = []

        async def _consumer():
            msgs = await bus.consume("s1", limit=10)
            consumed.extend(msgs)

        task = asyncio.create_task(_consumer())
        await asyncio.sleep(0.05)
        assert len(consumed) == 0

        await bus.send("s1", envelope)
        await asyncio.wait_for(task, timeout=1.0)

        assert len(consumed) == 1
        assert consumed[0].payload["content"] == "delayed"

    async def test_close_wakes_blocked_consumer(self):
        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        consumer = InboxConsumer(server=server)
        bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

        consumed = []

        async def _consumer():
            msgs = await bus.consume("s1", limit=10)
            consumed.extend(msgs)

        task = asyncio.create_task(_consumer())
        await asyncio.sleep(0.05)
        await bus.close()
        await asyncio.wait_for(task, timeout=1.0)

        assert consumed == []

    async def test_send_signals_broker_wakeup(self):
        broker = InMemoryMessageBroker()
        await broker.start()
        await broker.register_consumer(AgentAddress(kind="agent", name="a2"))

        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        consumer = InboxConsumer(server=server)
        bus = LocalAgentMessageBus(producer=producer, consumer=consumer, broker=broker)

        envelope = AgentMessageEnvelope(
            payload={"content": "wake up"},
            source=AgentAddress(kind="agent", name="a1"),
            target=AgentAddress(kind="agent", name="a2"),
            message_type="agent_message",
        )

        received = []

        async def _collect():
            msg = await broker.consume(AgentAddress(kind="agent", name="a2"))
            received.append(msg)

        collect_task = asyncio.create_task(_collect())
        await asyncio.sleep(0.05)

        await bus.send("s1", envelope)
        await asyncio.wait_for(collect_task, timeout=1.0)

        assert len(received) == 1
        assert received[0].payload.get("_inbox_wakeup") is True
        assert received[0].payload.get("session_id") == "s1"

        await broker.stop()

    async def test_wraps_inbox_message_with_defaults(self):
        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        consumer = InboxConsumer(server=server)
        bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

        envelope = AgentMessageEnvelope(
            payload={"content": "wrapped"},
            source=AgentAddress(kind="agent", name="src"),
            message_type="subagent_result",
        )
        await bus.send("sess_1", envelope)

        results = await bus.consume("sess_1", limit=10)
        assert len(results) == 1
        result = results[0]
        assert result.message_type == "subagent_result"
        assert result.agent_session_id == "sess_1"
        assert result.source.name == "src"
