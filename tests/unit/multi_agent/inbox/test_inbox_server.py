"""Tests for InboxServer implementations."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from modex_agent.multi_agent.inbox.server_local import LocalFileInboxServer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox.types import InboxMessage


class TestInMemoryInboxServer:
    async def test_receive_new_message(self):
        server = InMemoryInboxServer()
        msg = InboxMessage(session_id="s1", source="a", content="hello", message_type="test")
        assert await server.receive("s1", msg) is True
        assert await server.count("s1") == 1

    async def test_receive_duplicate_ignored(self):
        server = InMemoryInboxServer()
        msg = InboxMessage(
            session_id="s1", source="a", content="hello", message_type="test", message_id="m1"
        )
        assert await server.receive("s1", msg) is True
        assert await server.receive("s1", msg) is False
        assert await server.count("s1") == 1

    async def test_receive_after_consume_ignored(self):
        server = InMemoryInboxServer()
        msg = InboxMessage(
            session_id="s1", source="a", content="hello", message_type="test", message_id="m1"
        )
        await server.receive("s1", msg)
        consumed = await server.consume("s1")
        assert len(consumed) == 1
        assert await server.receive("s1", msg) is False

    async def test_consume_atomic(self):
        server = InMemoryInboxServer()
        for i in range(3):
            await server.receive(
                "s1",
                InboxMessage(
                    session_id="s1",
                    source="a",
                    content=f"msg{i}",
                    message_type="test",
                    message_id=f"mid{i}",
                ),
            )
        msgs = await server.consume("s1")
        assert len(msgs) == 3
        assert await server.count("s1") == 0

    async def test_consume_limit(self):
        server = InMemoryInboxServer()
        for i in range(5):
            await server.receive(
                "s1",
                InboxMessage(
                    session_id="s1",
                    source="a",
                    content=f"msg{i}",
                    message_type="test",
                    message_id=f"mid{i}",
                ),
            )
        msgs = await server.consume("s1", limit=2)
        assert len(msgs) == 2
        assert await server.count("s1") == 3

    async def test_fifo_order(self):
        server = InMemoryInboxServer()
        for i in range(3):
            await server.receive(
                "s1",
                InboxMessage(
                    session_id="s1",
                    source="a",
                    content=f"msg{i}",
                    message_type="test",
                    message_id=f"mid{i}",
                ),
            )
        msgs = await server.consume("s1")
        assert [m.content for m in msgs] == ["msg0", "msg1", "msg2"]

    async def test_session_isolation(self):
        server = InMemoryInboxServer()
        await server.receive(
            "s1",
            InboxMessage(session_id="s1", source="a", content="s1msg", message_type="test"),
        )
        await server.receive(
            "s2",
            InboxMessage(session_id="s2", source="a", content="s2msg", message_type="test"),
        )
        assert await server.count("s1") == 1
        assert await server.count("s2") == 1
        s1_msgs = await server.consume("s1")
        assert len(s1_msgs) == 1
        assert s1_msgs[0].content == "s1msg"
        assert await server.count("s2") == 1


class TestLocalFileInboxServer:
    async def test_receive_new_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            msg = InboxMessage(
                session_id="s1", source="a", content="hello", message_type="test"
            )
            assert await server.receive("s1", msg) is True
            assert await server.count("s1") == 1

    async def test_receive_duplicate_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            msg = InboxMessage(
                session_id="s1", source="a", content="hello", message_type="test", message_id="m1"
            )
            assert await server.receive("s1", msg) is True
            assert await server.receive("s1", msg) is False
            assert await server.count("s1") == 1

    async def test_receive_after_consume_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            msg = InboxMessage(
                session_id="s1", source="a", content="hello", message_type="test", message_id="m1"
            )
            await server.receive("s1", msg)
            await server.consume("s1")
            assert await server.receive("s1", msg) is False

    async def test_consume_atomic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            for i in range(3):
                await server.receive(
                    "s1",
                    InboxMessage(
                        session_id="s1",
                        source="a",
                        content=f"msg{i}",
                        message_type="test",
                        message_id=f"mid{i}",
                    ),
                )
            msgs = await server.consume("s1")
            assert len(msgs) == 3
            assert await server.count("s1") == 0

    async def test_fifo_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            for i in range(3):
                await server.receive(
                    "s1",
                    InboxMessage(
                        session_id="s1",
                        source="a",
                        content=f"msg{i}",
                        message_type="test",
                        message_id=f"mid{i}",
                    ),
                )
            msgs = await server.consume("s1")
            assert [m.content for m in msgs] == ["msg0", "msg1", "msg2"]

    async def test_session_isolation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            await server.receive(
                "s1",
                InboxMessage(session_id="s1", source="a", content="s1msg", message_type="test"),
            )
            await server.receive(
                "s2",
                InboxMessage(session_id="s2", source="a", content="s2msg", message_type="test"),
            )
            s1_msgs = await server.consume("s1")
            assert len(s1_msgs) == 1
            assert s1_msgs[0].content == "s1msg"
            assert await server.count("s2") == 1

    async def test_list_sessions_returns_original_session_id_from_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            session_id = "30932BC02F825E64D069B1E67347C8FF:office-expert:c3f7b726"
            await server.receive(
                session_id,
                InboxMessage(
                    session_id=session_id,
                    source="main",
                    content="make a report",
                    message_type="agent_message",
                    message_id="m1",
                    metadata={"agent_session_id": session_id},
                ),
            )

            sessions = await server.list_sessions()

            assert session_id in sessions
            assert session_id.replace(":", "_") not in sessions

    async def test_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            server1 = LocalFileInboxServer(workspace=workspace)
            msg = InboxMessage(
                session_id="s1", source="a", content="persist", message_type="test", message_id="m1"
            )
            await server1.receive("s1", msg)

            server2 = LocalFileInboxServer(workspace=workspace)
            msgs = await server2.consume("s1")
            assert len(msgs) == 1
            assert msgs[0].content == "persist"
            assert await server2.receive("s1", msg) is False

    async def test_concurrent_receives_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            msg = InboxMessage(
                session_id="s1", source="a", content="c", message_type="test", message_id="m1"
            )

            async def _recv():
                return await server.receive("s1", msg)

            results = await asyncio.gather(*[_recv() for _ in range(10)])
            assert sum(1 for r in results if r is True) == 1
            assert await server.count("s1") == 1

    async def test_peek_does_not_remove(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            await server.receive(
                "s1",
                InboxMessage(session_id="s1", source="a", content="peek", message_type="test"),
            )
            peeked = await server.peek("s1")
            assert len(peeked) == 1
            assert await server.count("s1") == 1

    async def test_clear(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            await server.receive(
                "s1",
                InboxMessage(session_id="s1", source="a", content="clear", message_type="test"),
            )
            await server.clear("s1")
            assert await server.count("s1") == 0
            assert await server.consume("s1") == []

    async def test_consume_limit_with_remainder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = LocalFileInboxServer(workspace=Path(tmpdir))
            for i in range(5):
                await server.receive(
                    "s1",
                    InboxMessage(
                        session_id="s1",
                        source="a",
                        content=f"msg{i}",
                        message_type="test",
                        message_id=f"mid{i}",
                    ),
                )
            msgs = await server.consume("s1", limit=2)
            assert len(msgs) == 2
            assert await server.count("s1") == 3
            # 验证剩余消息的 FIFO 顺序
            remaining = await server.consume("s1")
            assert [m.content for m in remaining] == ["msg2", "msg3", "msg4"]


class TestInboxMessage:
    async def test_timestamp_is_utc(self):
        from datetime import timezone

        msg = InboxMessage(session_id="s1", source="a", content="hello", message_type="test")
        assert msg.timestamp.tzinfo == timezone.utc
