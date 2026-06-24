"""Tests for InboxConsumer."""

from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox.types import InboxMessage


class TestInboxConsumer:
    async def test_consume_messages(self):
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        await server.receive(
            "s1",
            InboxMessage(session_id="s1", source="a", content="hello", message_type="test"),
        )
        msgs = await consumer.consume("s1")
        assert len(msgs) == 1
        assert msgs[0].content == "hello"

    async def test_second_consume_empty(self):
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        await server.receive(
            "s1",
            InboxMessage(
                session_id="s1", source="a", content="hello", message_type="test", message_id="m1"
            ),
        )
        await consumer.consume("s1")
        second = await consumer.consume("s1")
        assert second == []

    async def test_local_cache_safety_net(self):
        # Simulate an extreme case where server somehow re-delivers
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        msg = InboxMessage(
            session_id="s1", source="a", content="hello", message_type="test", message_id="m1"
        )
        await server.receive("s1", msg)
        first = await consumer.consume("s1")
        assert len(first) == 1
        # Manually re-inject to pending (simulating file tampering)
        server._pending.setdefault("s1", []).append(msg)
        second = await consumer.consume("s1")
        # Consumer local cache should filter it out
        assert second == []
