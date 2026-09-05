"""Tests for InboxConsumer."""

from unittest.mock import AsyncMock

from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox.types import InboxMessage


class TestInboxConsumer:
    async def test_consume_messages(self) -> None:
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        await server.receive(
            "s1",
            InboxMessage(session_id="s1", source="a", content="hello", message_type="test"),
        )
        msgs = await consumer.consume("s1")
        assert len(msgs) == 1
        assert msgs[0].content == "hello"

    async def test_second_consume_empty(self) -> None:
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

    async def test_local_cache_safety_net(self) -> None:
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

    async def test_set_on_consumed_fires_for_each_message(self) -> None:
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        callback = AsyncMock()
        consumer.set_on_consumed(callback)
        for index in range(3):
            await server.receive(
                "s1",
                InboxMessage(
                    session_id="s1",
                    source="a",
                    content=f"message {index}",
                    message_type="test",
                ),
            )

        messages = await consumer.consume("s1")

        assert len(messages) == 3
        assert callback.await_count == 0
        for message in messages:
            await consumer.acknowledge("s1", message.message_id)
        assert callback.await_count == 3

    async def test_set_on_consumed_none_no_crash(self) -> None:
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        consumer.set_on_consumed(None)
        await server.receive(
            "s1",
            InboxMessage(
                session_id="s1",
                source="a",
                content="hello",
                message_type="test",
            ),
        )

        messages = await consumer.consume("s1")

        assert len(messages) == 1
