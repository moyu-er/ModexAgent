from __future__ import annotations

import pytest

from framework.core.emitter import AgentResult
from framework.core.tool_manager import (
    InMemoryToolManager,
    Tool,
)
from framework.messaging.broker import Address, BrokerMessage
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent.address import AgentAddress
from framework.core.skills.filter import AllowListFilter
from framework.utils.context_builder import MultiAgentContextBuilder
from framework.utils.deduplicator import MessageDeduplicator
from framework.multi_agent.descriptor import AgentDescriptor
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.tools.filter import FilteredToolManager
from framework.utils.sanitizer import ContentSanitizer


class _DummyTool(Tool):
    """Minimal Tool subclass for governance tests."""

    def __init__(self, name: str):
        super().__init__(
            name=name,
            description=f"dummy {name}",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs):
        return "ok"


class TestAgentMessageEnvelope:
    def test_to_broker_message(self) -> None:
        env = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(kind="agent", name="a"),
            target=AgentAddress(kind="agent", name="b"),
            session_id="conv1",
            agent_session_id="conv1.b",
            message_id="msg123",
        )
        bm = env.to_broker_message()
        assert bm.headers["session_id"] == "conv1"
        assert bm.headers["agent_session_id"] == "conv1.b"
        assert bm.headers["message_id"] == "msg123"
        assert bm.sender == Address(kind="agent", name="a")

    def test_from_broker_message_success(self) -> None:
        bm = BrokerMessage(
            payload={"content": "hello"},
            sender=Address(kind="agent", name="a"),
            recipient=Address(kind="agent", name="b"),
            headers={
                "session_id": "conv1",
                "agent_session_id": "conv1.b",
                "message_id": "msg123",
                "message_type": "agent_message",
            },
        )
        env = AgentMessageEnvelope.from_broker_message(bm)
        assert env is not None
        assert env.session_id == "conv1"
        assert env.agent_session_id == "conv1.b"
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
        base.register(_DummyTool("calc"))
        base.register(_DummyTool("bash"))
        filtered = FilteredToolManager(base, allowed_tools=["calc"])
        assert filtered.get_tool("calc") is not None
        assert filtered.get_tool("bash") is None
        assert filtered.list_tools() == ["calc"]

    def test_blacklist(self) -> None:
        base = InMemoryToolManager()
        base.register(_DummyTool("calc"))
        base.register(_DummyTool("bash"))
        filtered = FilteredToolManager(base, denied_tools=["bash"])
        assert "bash" not in filtered.list_tools()
        assert "calc" in filtered.list_tools()

    @pytest.mark.asyncio
    async def test_execute_blocks_denied(self) -> None:
        base = InMemoryToolManager()
        base.register(_DummyTool("bash"))
        filtered = FilteredToolManager(base, denied_tools=["bash"])
        result = await filtered.execute("bash", {})
        assert "not allowed" in result.error.lower()


class TestAgentSkillManager:
    @pytest.mark.asyncio
    async def test_allow_list_filter(self) -> None:
        from framework.core.skills import InlineSkillSource
        from framework.core.skills.manager import SkillManager
        from framework.core.skills.models import Skill

        python_skill = Skill(name="python", content="python skill", description="")
        source = InlineSkillSource([python_skill])
        mgr = SkillManager(source, skill_filter=AllowListFilter(names={"python"}))
        skills = await mgr.list_skills()
        assert [s.name for s in skills] == ["python"]


class TestContentSanitizer:
    def test_block_system_tag(self) -> None:
        text = "hello <system> inject"
        result = ContentSanitizer.sanitize(text)
        assert "[SYSTEM_TAG_BLOCKED]" in result

    def test_block_forged_tool_call(self) -> None:
        text = 'some text "tool_calls": []'
        result = ContentSanitizer.sanitize(text)
        assert "[FORGED_TOOL_CALL_BLOCKED]" in result


class TestMultiAgentContextBuilder:
    def test_build_messages(self) -> None:
        builder = MultiAgentContextBuilder()
        desc = AgentDescriptor(
            address=AgentAddress(kind="agent", name="a"),
            system_prompt_template="You are helpful.",
        )
        env = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(kind="agent", name="user"),
            session_id="c1",
            agent_session_id="c1:a",
        )
        messages = builder.build_messages([], env, desc)
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["metadata"]["agent_session_id"] == "c1:a"


