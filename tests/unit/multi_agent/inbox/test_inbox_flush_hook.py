"""Tests for InboxFlushHook."""

from unittest.mock import MagicMock

from framework.core.agent import AgentContext
from framework.core.tool_manager import ToolManager
from framework.memory.history import ListMessageHistory
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.hook.builtin import InboxFlushHook
from framework.multi_agent.inbox.server_memory import InMemoryInboxServer
from framework.multi_agent.inbox.types import InboxMessage


class TestInboxFlushHook:
    async def test_before_turn_injects_messages(self):
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        hook = InboxFlushHook(consumer=consumer, agent_name="main")

        await server.receive(
            "s1",
            InboxMessage(session_id="s1", source="helper", content="done", message_type="test"),
        )

        history = ListMessageHistory([])
        ctx = AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
            session_id="s1",
        )
        await hook.before_turn(ctx)

        msgs = await history.to_list()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "agent"
        assert msgs[0]["source_agent"] == "helper"
        assert msgs[0]["content"] == "[From Agent helper]\ndone"
        assert msgs[0].get("meta_inbox") is True

    async def test_before_turn_no_session_id(self):
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        hook = InboxFlushHook(consumer=consumer, agent_name="main")

        history = ListMessageHistory([])
        ctx = AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
            session_id="",
        )
        await hook.before_turn(ctx)
        assert await history.to_list() == []

    async def test_before_turn_empty_inbox(self):
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        hook = InboxFlushHook(consumer=consumer, agent_name="main")

        history = ListMessageHistory([])
        ctx = AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
            session_id="s1",
        )
        await hook.before_turn(ctx)
        assert await history.to_list() == []

    async def test_before_iteration_also_flushes(self):
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        hook = InboxFlushHook(consumer=consumer, agent_name="main")

        await server.receive(
            "s1",
            InboxMessage(session_id="s1", source="helper", content="iter", message_type="test"),
        )

        history = ListMessageHistory([])
        ctx = AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
            session_id="s1",
        )
        await hook.before_iteration(ctx)

        msgs = await history.to_list()
        assert len(msgs) == 1
        assert "iter" in msgs[0]["content"]

    async def test_idempotent_across_hook_recreation(self):
        server = InMemoryInboxServer()
        await server.receive(
            "s1",
            InboxMessage(
                session_id="s1", source="helper", content="once", message_type="test", message_id="m1"
            ),
        )

        history = ListMessageHistory([])
        ctx = AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
        )

        # First hook instance flushes
        hook1 = InboxFlushHook(consumer=InboxConsumer(server=server), agent_name="main")
        await hook1.before_turn(ctx)

        # Recreate hook and context (simulating framework behavior)
        history2 = ListMessageHistory([])
        ctx2 = AgentContext(
            system_prompt="",
            history=history2,
            tool_manager=MagicMock(spec=ToolManager),
        )
        hook2 = InboxFlushHook(consumer=InboxConsumer(server=server), agent_name="main")
        await hook2.before_turn(ctx2)

        # Should not inject duplicate
        assert await history2.to_list() == []
