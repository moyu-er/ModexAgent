"""Tests for AgentMessageBus and LocalAgentMessageBus."""

import asyncio

import pytest

from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.messaging.broker_memory import InMemoryMessageBroker


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

    async def test_send_works_without_callback_set(self):
        """bus.send without a callback does not crash (persist-only, no broker)."""
        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        consumer = InboxConsumer(server=server)
        bus = LocalAgentMessageBus(producer=producer, consumer=consumer, broker=None)

        envelope = AgentMessageEnvelope(
            payload={"content": "hi", "message_type": "agent_message"},
            source=AgentAddress(kind="agent", name="src"),
            target=AgentAddress(kind="agent", name="main"),
            message_type="agent_message",
            session_id="pfx.main",
            agent_session_id="pfx.main",
        )
        # Must not raise.
        await bus.send("pfx.main", envelope)
        assert "pfx.main" in await bus.sessions_with_pending()


class TestBusPreservesSourceKind:
    """The bus must preserve the original envelope ``source.kind`` across the
    inbox round-trip.

    Role assignment (context_assembler) keys off ``source_agent`` in the
    dispatch metadata, which ``AgentPool._envelope_metadata`` derives from
    ``envelope.source.kind == "agent"``. Human DMs arrive as ``external_input``
    with ``source.kind == "channel"``; if the bus normalizes every source to
    ``kind="agent"``, human input is mis-stored as ``role=agent`` in session
    memory. Only inter-agent messages (and hook notifications) should be
    ``role=agent``.
    """

    async def test_channel_source_kind_preserved_for_external_input(self):
        server = InMemoryInboxServer()
        bus = LocalAgentMessageBus(
            producer=InboxProducer(server=server),
            consumer=InboxConsumer(server=server),
        )
        envelope = AgentMessageEnvelope(
            payload={"content": "hi", "message_type": "external_input"},
            source=AgentAddress(kind="channel", name="user"),
            target=AgentAddress(kind="agent", name="main"),
            message_type="external_input",
            session_id="conv.main",
            agent_session_id="conv.main",
        )
        await bus.send("conv.main", envelope)
        got = await bus.consume("conv.main", limit=10)
        assert len(got) == 1
        # The human-input origin must survive the round-trip.
        assert got[0].source.kind == "channel"
        assert got[0].source.name == "user"

    async def test_agent_source_kind_preserved_for_inter_agent(self):
        server = InMemoryInboxServer()
        bus = LocalAgentMessageBus(
            producer=InboxProducer(server=server),
            consumer=InboxConsumer(server=server),
        )
        envelope = AgentMessageEnvelope(
            payload={"content": "do", "message_type": "task_request"},
            source=AgentAddress(kind="agent", name="scout"),
            target=AgentAddress(kind="agent", name="main"),
            message_type="task_request",
            session_id="inv.scout",
            agent_session_id="inv.scout",
        )
        await bus.send("inv.scout", envelope)
        got = await bus.consume("inv.scout", limit=10)
        assert got[0].source.kind == "agent"
        assert got[0].source.name == "scout"
