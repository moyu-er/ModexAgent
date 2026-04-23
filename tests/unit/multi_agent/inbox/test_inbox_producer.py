"""Tests for InboxProducer."""

from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_memory import InMemoryInboxServer
from framework.multi_agent.inbox.types import InboxMessage


class TestInboxProducer:
    async def test_send_new_message(self):
        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.address import AgentAddress

        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="agent_a"),
            target=AgentAddress(name="agent_b"),
            message_type="test",
        )
        assert await producer.send("s1", envelope) is True
        assert await server.count("s1") == 1

    async def test_send_duplicate_ignored(self):
        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.address import AgentAddress

        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="agent_a"),
            target=AgentAddress(name="agent_b"),
            message_type="test",
            message_id="m1",
        )
        assert await producer.send("s1", envelope) is True
        assert await producer.send("s1", envelope) is False
        assert await server.count("s1") == 1

    async def test_local_cache_dedup(self):
        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.address import AgentAddress

        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="agent_a"),
            target=AgentAddress(name="agent_b"),
            message_type="test",
            message_id="m1",
        )
        # First send populates cache and server
        assert await producer.send("s1", envelope) is True
        # Second send hits local cache, no server call needed
        assert await producer.send("s1", envelope) is False
        assert await server.count("s1") == 1
