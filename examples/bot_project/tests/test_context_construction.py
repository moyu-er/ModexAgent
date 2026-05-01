"""Tests for context construction — system prompt, multi-agent context, routing."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from framework.core.agent import AgentContext
from framework.core.context import InMemoryContextManager, EphemeralContextManager
from framework.core.emitter import AgentResult
from framework.memory.history import ListMessageHistory


class TestContextManagerConstruction:
    """验证不同 context_manager 类型的构造。"""

    async def test_inmemory_context_manager_save_load(self):
        cm = InMemoryContextManager(base_system_prompt="test prompt")
        state = await cm.load("s1")
        assert state.system_prompt == "test prompt"

    async def test_inmemory_context_system_prompt_construction(self):
        cm = InMemoryContextManager(base_system_prompt="You are helpful")
        prompt = await cm.build_system_prompt(tool_manager=MagicMock())
        assert "You are helpful" in (prompt or "")

    async def test_ephemeral_context_is_stateless(self):
        cm = EphemeralContextManager(base_system_prompt="ephemeral")
        state1 = await cm.load("any_id")
        state2 = await cm.load("any_id")
        assert state1.system_prompt == "ephemeral"
        assert state2.system_prompt == "ephemeral"


class TestAgentContextConstruction:
    """验证 AgentContext 构造和字段。"""

    def test_minimal_agent_context(self):
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            session_id="s1",
        )
        assert ctx.session_id == "s1"
        assert ctx.system_prompt == "test"
        assert ctx.max_iterations == 10
        assert ctx.extensions.get("injection_queue") is None

    def test_agent_context_with_injection_queue(self):
        import asyncio
        from framework.core.context_extensions import ExtensionKey
        q = asyncio.Queue()
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            session_id="s1",
            extensions={ExtensionKey.INJECTION_QUEUE: q},
        )
        assert ctx.extensions.get(ExtensionKey.INJECTION_QUEUE) is q

    def test_agent_context_metadata(self):
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            metadata={"user_id": "u1", "chat_id": "c1"},
        )
        assert ctx.metadata["user_id"] == "u1"

    def test_agent_context_with_hooks(self):
        from framework.core.context_extensions import ExtensionKey
        hook1 = MagicMock()
        hook2 = MagicMock()
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            extensions={ExtensionKey.HOOKS: [hook1, hook2]},
        )
        assert len(ctx.extensions.get(ExtensionKey.HOOKS, [])) == 2


class TestAgentContextIsolation:
    """验证不同 session 之间 AgentContext 的隔离性。"""

    def test_different_sessions_have_independent_contexts(self):
        ctx1 = AgentContext(
            system_prompt="prompt1",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            session_id="s1",
            metadata={"key": "val1"},
        )
        ctx2 = AgentContext(
            system_prompt="prompt2",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            session_id="s2",
            metadata={"key": "val2"},
        )
        assert ctx1.session_id != ctx2.session_id
        assert ctx1.system_prompt != ctx2.system_prompt
        assert ctx1.metadata["key"] != ctx2.metadata["key"]
