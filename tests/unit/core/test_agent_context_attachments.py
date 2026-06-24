"""Tests for AgentContext.attachments and current_agent_context contextvar."""

from __future__ import annotations

import pytest

from modex_agent.core.agent import AgentContext, current_agent_context
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory


from modex_agent.core.tool_manager import ToolManager, ToolManagerConfig


class FakeToolManager(ToolManager):
    def __init__(self):
        super().__init__(ToolManagerConfig())

    def register(self, tool, config=None):
        pass

    def unregister(self, tool_name):
        return False

    def get_tool(self, tool_name):
        return None

    def list_tools(self):
        return []

    def is_registered(self, tool_name):
        return False


@pytest.fixture
def agent_context():
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=FakeToolManager(),
        session=SessionInfo.from_str("test.agent"),
    )


class TestAgentContextAttachments:
    def test_default_attachments_empty(self, agent_context):
        """attachments 默认应为空列表。"""
        assert agent_context.attachments == []

    def test_add_attachment_appends_path(self, agent_context):
        """add_attachment 应将路径追加到列表。"""
        agent_context.add_attachment("/tmp/file1.pdf")
        agent_context.add_attachment("/tmp/file2.png")
        assert agent_context.attachments == ["/tmp/file1.pdf", "/tmp/file2.png"]

    def test_attachments_field_mutable(self, agent_context):
        """attachments 字段可直接赋值。"""
        agent_context.attachments = ["/tmp/a.txt"]
        assert agent_context.attachments == ["/tmp/a.txt"]


class TestCurrentAgentContextContextVar:
    def test_get_without_set_returns_none(self):
        """未设置时 get(None) 应返回 None。"""
        assert current_agent_context.get(None) is None

    def test_set_and_get(self, agent_context):
        """设置后应能获取到同一对象。"""
        token = current_agent_context.set(agent_context)
        try:
            ctx = current_agent_context.get(None)
            assert ctx is agent_context
        finally:
            current_agent_context.reset(token)

    def test_reset_restores_state(self, agent_context):
        """reset 后应恢复到之前的状态。"""
        token = current_agent_context.set(agent_context)
        try:
            pass
        finally:
            current_agent_context.reset(token)
        assert current_agent_context.get(None) is None

    def test_nested_set(self, agent_context):
        """嵌套 set/reset 应正确工作。"""
        ctx2 = AgentContext(
            system_prompt="test2",
            history=ListMessageHistory(),
            tool_manager=FakeToolManager(),
            session=SessionInfo.from_str("test.agent"),
        )
        token1 = current_agent_context.set(agent_context)
        try:
            token2 = current_agent_context.set(ctx2)
            try:
                assert current_agent_context.get(None) is ctx2
            finally:
                current_agent_context.reset(token2)
            assert current_agent_context.get(None) is agent_context
        finally:
            current_agent_context.reset(token1)


class TestAgentResultAttachments:
    def test_default_attachments_empty(self):
        """AgentResult 默认 attachments 应为空列表。"""
        result = AgentResult(content="hello")
        assert result.attachments == []

    def test_attachments_in_constructor(self):
        """AgentResult 构造时应支持 attachments 参数。"""
        result = AgentResult(
            content="hello",
            attachments=["/tmp/file.pdf"],
        )
        assert result.attachments == ["/tmp/file.pdf"]
