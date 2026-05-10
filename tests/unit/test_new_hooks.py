"""Tests for new Phase 2 hooks — guard, filter, transform, progress."""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

from framework.core.agent import AgentContext
from framework.core.tool_manager import ToolResult
from framework.core.types import LLMResponse
from framework.hook.builtin.llm_output_guard import LLMOutputGuardHook
from framework.hook.builtin.tool_result_transform import ToolResultTransformHook
from framework.memory.history import ListMessageHistory
from framework.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from framework.runtime.models import TurnIdentity, TurnStateBase
from framework.runtime.services import AgentRuntime, AgentRuntimeServices


def _ctx() -> AgentContext:
    state = TurnStateBase(
        identity=TurnIdentity(agent_id="test", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=MagicMock(),
        session_id="s1",
        runtime=runtime,
    )


class MockToolResult:
    def __init__(self, tool_name="test", result=None, error=None):
        self.tool_name = tool_name
        self.result = result
        self.error = error


class _MutableResponse:
    """Simple mutable response for LLMOutputGuard testing."""

    def __init__(self, content: str | None) -> None:
        self.content = content


class TestLLMOutputGuardHook:
    async def test_redacts_api_key(self):
        hook = LLMOutputGuardHook()
        ctx = _ctx()
        response = _MutableResponse("api_key=sk-abc123secret, continue...")

        await hook.after_llm_response(ctx, response)
        assert "sk-abc123secret" not in (response.content or "")
        assert "REDACTED" in (response.content or "")

    async def test_detects_risk_keywords(self):
        hook = LLMOutputGuardHook()
        ctx = _ctx()
        response = _MutableResponse("Here is an exploit for the vulnerability")

        await hook.after_llm_response(ctx, response)
        risk = ctx.runtime.state.custom.get(TurnCustomKey.LLM_OUTPUT_RISK)
        assert risk is not None
        assert "exploit" in risk
        assert "vulnerability" in risk

    async def test_empty_content_noop(self):
        hook = LLMOutputGuardHook()
        ctx = _ctx()
        response = _MutableResponse(None)

        await hook.after_llm_response(ctx, response)
        assert ctx.runtime.state.custom.get(TurnCustomKey.LLM_OUTPUT_RISK) is None


class TestToolResultTransformHook:
    async def test_sanitize_credentials_in_result(self):
        hook = ToolResultTransformHook(sanitize_credentials=True)
        ctx = _ctx()
        r = MockToolResult(result="key password=hunter2, continue")

        await hook.after_tool_execution(ctx, [r])
        assert "hunter2" not in r.result
        assert "REDACTED" in r.result

    async def test_truncate_long_result(self):
        hook = ToolResultTransformHook(max_result_chars=10, sanitize_credentials=False)
        ctx = _ctx()
        r = MockToolResult(result="A" * 100)

        await hook.after_tool_execution(ctx, [r])
        assert len(r.result) < 100
        assert "truncated" in r.result
        assert "100 chars" in r.result

    async def test_no_truncate_short_result(self):
        hook = ToolResultTransformHook(max_result_chars=100)
        ctx = _ctx()
        r = MockToolResult(result="short")

        await hook.after_tool_execution(ctx, [r])
        assert r.result == "short"

    async def test_empty_results_list(self):
        hook = ToolResultTransformHook()
        ctx = _ctx()
        await hook.after_tool_execution(ctx, [])  # should not raise
