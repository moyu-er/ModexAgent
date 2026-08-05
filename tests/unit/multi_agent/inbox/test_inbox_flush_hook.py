"""Tests for InboxFlushHook."""

from unittest.mock import MagicMock

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolManager
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.hook.builtin import InboxFlushHook
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox.types import InboxMessage
from modex_agent.multi_agent.message_type import AgentMessageType


class TestInboxFlushHook:
    async def test_before_turn_injects_messages(self):
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        hook = InboxFlushHook(consumer=consumer, agent_name="main")

        await server.receive(
            "s1",
            InboxMessage(
                session_id="s1", source="helper", content="done", message_type="agent_message"
            ),
        )

        history = ListMessageHistory([])
        ctx = AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
            session=SessionInfo.from_str("s1"),
        )
        await hook.before_turn(ctx)

        msgs = await history.to_list()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "system_reminder"
        assert msgs[0]["source_agent"] == "helper"
        assert "done" in msgs[0]["content"]
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
            session=SessionInfo.from_str("s1"),
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
            session=SessionInfo.from_str("s1"),
        )
        await hook.before_turn(ctx)
        assert await history.to_list() == []

    async def test_before_iteration_also_flushes(self):
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        hook = InboxFlushHook(consumer=consumer, agent_name="main")

        await server.receive(
            "s1",
            InboxMessage(
                session_id="s1", source="helper", content="iter", message_type="agent_message"
            ),
        )

        history = ListMessageHistory([])
        ctx = AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
            session=SessionInfo.from_str("s1"),
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
                session_id="s1",
                source="helper",
                content="once",
                message_type="agent_message",
                message_id="m1",
            ),
        )

        history = ListMessageHistory([])
        ctx = AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
            session=SessionInfo.from_str("s1"),
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
            session=SessionInfo.from_str("s1"),
        )
        hook2 = InboxFlushHook(consumer=InboxConsumer(server=server), agent_name="main")
        await hook2.before_turn(ctx2)

        # Should not inject duplicate
        assert await history2.to_list() == []

    async def test_before_iteration_pulls_subagent_result_mid_turn(self):
        """A subagent reply (AGENT_RESULT) arriving while the parent is mid-turn
        MUST be folded into history — that is the "active pull" a busy parent
        agent relies on to see the result promptly instead of only after its
        turn ends (which would leave it blind to the deliverable for the whole
        turn). Regression: AGENT_RESULT was excluded from fold_eligible."""
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        hook = InboxFlushHook(consumer=consumer, agent_name="coding")

        await server.receive(
            "pfx.coding",
            InboxMessage(
                session_id="pfx.coding",
                source="scout",
                content="<subagent_result>done</subagent_result>",
                message_type=AgentMessageType.AGENT_RESULT,
            ),
        )

        history = ListMessageHistory([])
        ctx = AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
            session=SessionInfo.from_str("pfx.coding"),
        )
        await hook.before_iteration(ctx)

        msgs = await history.to_list()
        assert len(msgs) == 1, "AGENT_RESULT must be pulled mid-turn (active fold-in)"
        assert msgs[0]["role"] == "system_reminder"
        assert msgs[0]["source_agent"] == "scout"

    async def test_external_input_never_folded(self):
        """A human DM (EXTERNAL_INPUT) must NOT fold mid-turn — it is a new
        user input and starts its own between-turn (spec P6). Only the
        between-turn poller consumes it."""
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        hook = InboxFlushHook(consumer=consumer, agent_name="coding")

        await server.receive(
            "pfx.coding",
            InboxMessage(
                session_id="pfx.coding",
                source="user",
                content="a human DM",
                message_type=AgentMessageType.EXTERNAL_INPUT,
            ),
        )

        history = ListMessageHistory([])
        ctx = AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
            session=SessionInfo.from_str("pfx.coding"),
        )
        await hook.before_iteration(ctx)
        assert await history.to_list() == [], "EXTERNAL_INPUT must stay for the next between-turn"
