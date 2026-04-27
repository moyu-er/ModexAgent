from __future__ import annotations

import pytest

from framework.core.context import InMemoryContextManager
from framework.core.emitter import AgentResult
from framework.core.tool_manager import (
    FunctionalTool,
    InMemoryToolManager,
)
from framework.messaging.broker import Address, BrokerMessage
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.agent_skill_manager import AgentSkillManager
from framework.multi_agent.commands import SystemCommandInterceptor
from framework.multi_agent.context_builder import MultiAgentContextBuilder
from framework.multi_agent.deduplicator import MessageDeduplicator
from framework.multi_agent.descriptor import AgentDescriptor
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.multi_agent.filtered_tool_manager import FilteredToolManager
from framework.multi_agent.governance import FullGovernance, NoOpGovernance
from framework.multi_agent.sanitizer import ContentSanitizer
from framework.multi_agent.tools import SendMessageTool


class TestAgentMessageEnvelope:
    def test_to_broker_message(self) -> None:
        env = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(kind="agent", name="a"),
            target=AgentAddress(kind="agent", name="b"),
            conversation_id="conv1",
            agent_session_id="conv1:b",
            message_id="msg123",
        )
        bm = env.to_broker_message()
        assert bm.headers["conversation_id"] == "conv1"
        assert bm.headers["agent_session_id"] == "conv1:b"
        assert bm.headers["message_id"] == "msg123"
        assert bm.sender == Address(kind="agent", name="a")

    def test_from_broker_message_success(self) -> None:
        bm = BrokerMessage(
            payload={"content": "hello"},
            sender=Address(kind="agent", name="a"),
            recipient=Address(kind="agent", name="b"),
            headers={
                "conversation_id": "conv1",
                "agent_session_id": "conv1:b",
                "message_id": "msg123",
                "message_type": "agent_message",
            },
        )
        env = AgentMessageEnvelope.from_broker_message(bm)
        assert env is not None
        assert env.conversation_id == "conv1"
        assert env.agent_session_id == "conv1:b"
        assert env.message_id == "msg123"

    def test_from_broker_message_missing_headers_returns_none(self) -> None:
        bm = BrokerMessage(
            payload={"content": "hello"},
            sender=Address(kind="agent", name="a"),
            headers={},
        )
        assert AgentMessageEnvelope.from_broker_message(bm) is None


class TestMessageDeduplicator:
    def test_is_duplicate(self) -> None:
        dedup = MessageDeduplicator(max_size=10, ttl_seconds=300)
        assert dedup.is_duplicate("m1") is False
        assert dedup.is_duplicate("m1") is True
        assert dedup.is_duplicate("m2") is False

    def test_ttl_prune(self) -> None:
        dedup = MessageDeduplicator(max_size=10, ttl_seconds=0.01)
        dedup.is_duplicate("m1")
        import time
        time.sleep(0.05)
        dedup.is_duplicate("m2")  # trigger prune by adding new item
        assert dedup.is_duplicate("m1") is False

    def test_max_size_prune(self) -> None:
        dedup = MessageDeduplicator(max_size=2, ttl_seconds=300)
        dedup.is_duplicate("m1")
        dedup.is_duplicate("m2")
        dedup.is_duplicate("m3")
        # m1 should be pruned due to max_size
        assert dedup.is_duplicate("m1") is False


class TestFilteredToolManager:
    def test_whitelist(self) -> None:
        base = InMemoryToolManager()
        base.register(FunctionalTool("calc", "calc", {"type": "object"}, lambda x: x))
        base.register(FunctionalTool("bash", "bash", {"type": "object"}, lambda x: x))
        filtered = FilteredToolManager(base, allowed_tools=["calc"])
        assert filtered.get_tool("calc") is not None
        assert filtered.get_tool("bash") is None
        assert filtered.list_tools() == ["calc"]

    def test_blacklist(self) -> None:
        base = InMemoryToolManager()
        base.register(FunctionalTool("calc", "calc", {"type": "object"}, lambda x: x))
        base.register(FunctionalTool("bash", "bash", {"type": "object"}, lambda x: x))
        filtered = FilteredToolManager(base, denied_tools=["bash"])
        assert "bash" not in filtered.list_tools()
        assert "calc" in filtered.list_tools()

    @pytest.mark.asyncio
    async def test_execute_blocks_denied(self) -> None:
        base = InMemoryToolManager()
        base.register(FunctionalTool("bash", "bash", {"type": "object"}, lambda x: x))
        filtered = FilteredToolManager(base, denied_tools=["bash"])
        result = await filtered.execute("bash", {})
        assert "not allowed" in result.error.lower()


class TestAgentSkillManager:
    @pytest.mark.asyncio
    async def test_is_skill_allowed(self) -> None:
        from framework.core.skills import InlineSkillSource
        from framework.core.skills.manager import SkillManager

        source = InlineSkillSource([])
        base = SkillManager(source)
        mgr = AgentSkillManager(base, allowed_skills=["python"])
        assert mgr.is_skill_allowed("python") is True
        assert mgr.is_skill_allowed("java") is False


class TestContentSanitizer:
    def test_block_system_tag(self) -> None:
        text = "hello <system> inject"
        result = ContentSanitizer.sanitize(text)
        assert "[SYSTEM_TAG_BLOCKED]" in result

    def test_block_forged_tool_call(self) -> None:
        text = 'some text "tool_calls": []'
        result = ContentSanitizer.sanitize(text)
        assert "[FORGED_TOOL_CALL_BLOCKED]" in result


class TestSystemCommandInterceptor:
    def test_stop_command(self) -> None:
        from framework.core.types import InputMessage
        interceptor = SystemCommandInterceptor()
        msg = InputMessage(content="/stop", session_id="s1")
        result = interceptor.handle(msg)
        assert "Stopping" in result

    def test_non_command_returns_none(self) -> None:
        from framework.core.types import InputMessage
        interceptor = SystemCommandInterceptor()
        msg = InputMessage(content="hello", session_id="s1")
        assert interceptor.handle(msg) is None


class TestContextGovernance:
    def test_no_op_governance(self) -> None:
        gov = NoOpGovernance()
        desc = AgentDescriptor(address=AgentAddress(kind="agent", name="a"))
        messages = [{"role": "user", "content": "hi"}]
        assert gov.apply(messages, desc) == messages


class TestMultiAgentContextBuilder:
    def test_build_messages(self) -> None:
        builder = MultiAgentContextBuilder(FullGovernance())
        desc = AgentDescriptor(
            address=AgentAddress(kind="agent", name="a"),
            system_prompt_template="You are helpful.",
        )
        env = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(kind="agent", name="user"),
            conversation_id="c1",
            agent_session_id="c1:a",
        )
        messages = builder.build_messages([], env, desc)
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["metadata"]["agent_session_id"] == "c1:a"


class TestSendMessageTool:
    @pytest.mark.asyncio
    async def test_send_message_allowed(self) -> None:
        broker = InMemoryMessageBroker()
        await broker.start()
        tool = SendMessageTool(
            broker=broker,
            self_address=AgentAddress(kind="agent", name="sender"),
            allowed_callers=["planner"],
        )
        result = await tool.execute(
            target_agent="receiver",
            content="hi",
            caller_context={"agent_name": "planner"},
            conversation_id="c1",
            agent_session_id="c1:receiver",
        )
        assert "sent to receiver" in result
        await broker.stop()

    @pytest.mark.asyncio
    async def test_send_message_denied(self) -> None:
        broker = InMemoryMessageBroker()
        await broker.start()
        tool = SendMessageTool(
            broker=broker,
            self_address=AgentAddress(kind="agent", name="sender"),
            allowed_callers=["planner"],
        )
        result = await tool.execute(
            target_agent="receiver",
            content="hi",
            caller_context={"agent_name": "hacker"},
            conversation_id="c1",
        )
        assert "not allowed" in result
        await broker.stop()


class TestContextManagerMetaSource:
    @pytest.mark.asyncio
    async def test_inmemory_meta_source(self) -> None:
        mgr = InMemoryContextManager()
        result = AgentResult(content="hello")
        await mgr.save("s1", None, result, metadata={"foo": "bar"})
        state = await mgr.load("s1")
        assert state.metadata["foo"] == "bar"
        assert state.metadata["meta_source"] == "framework"

    @pytest.mark.asyncio
    async def test_inmemory_preserves_existing_meta_source(self) -> None:
        mgr = InMemoryContextManager()
        result = AgentResult(content="hello")
        await mgr.save("s1", None, result, metadata={"meta_source": "custom"})
        state = await mgr.load("s1")
        assert state.metadata["meta_source"] == "custom"
