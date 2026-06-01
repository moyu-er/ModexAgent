"""Tests for ToolPolicyInterceptor — actual veto of denied tools."""

import asyncio
from unittest.mock import MagicMock
from framework.interceptor.builtin.tool_policy_interceptor import ToolPolicyInterceptor
from framework.interceptor.abc import ToolCallContext
from framework.core.tool_manager import ToolResult
from framework.core.types import ToolCall
from framework.core.agent import AgentContext
from framework.memory.history import ListMessageHistory
from framework.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from framework.runtime.models import TurnIdentity, TurnStateBase
from framework.runtime.services import AgentRuntime, AgentRuntimeServices


def _ctx(meta: dict | None = None) -> AgentContext:
    state = TurnStateBase(
        identity=TurnIdentity(agent_id="test", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING,
    )
    if meta:
        state.custom[TurnCustomKey.POLICY_DENIED_TOOLS] = meta.get("_policy_denied_tools")
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=MagicMock(),
        session_id="s1",
        runtime=runtime,
    )


def _call(name: str = "bash") -> ToolCallContext:
    return ToolCallContext(
        tool_call=ToolCall(tool_name=name, arguments={}),
        tool_name=name,
        arguments={},
        session_id="s1",
    )


class TestToolPolicyInterceptor:
    async def test_vetoes_denied_tool(self):
        interceptor = ToolPolicyInterceptor()
        ctx = _ctx({"_policy_denied_tools": {"bash": "security policy"}})

        async def _next():
            return ToolResult(tool_name="bash", result="should not execute")

        result = await interceptor.around_tool_call(ctx, _call("bash"), _next)
        assert result.error is not None
        assert "blocked by policy" in result.error
        assert result.result is None

    async def test_passes_non_denied_tool(self):
        interceptor = ToolPolicyInterceptor()
        ctx = _ctx({"_policy_denied_tools": {"bash": "security policy"}})

        async def _next():
            return ToolResult(tool_name="read_file", result="content")

        result = await interceptor.around_tool_call(ctx, _call("read_file"), _next)
        assert result.result == "content"
        assert result.error is None

    async def test_passes_when_no_denial_metadata(self):
        interceptor = ToolPolicyInterceptor()
        ctx = _ctx()

        async def _next():
            return ToolResult(tool_name="bash", result="ok")

        result = await interceptor.around_tool_call(ctx, _call("bash"), _next)
        assert result.result == "ok"
