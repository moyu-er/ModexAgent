"""Tests for ToolApprovalInterceptor — tool_error vs cancel_turn."""

from __future__ import annotations

import pytest

from framework.control.channel import InMemoryControlChannel
from framework.control.exceptions import ApprovalDenied
from framework.control.types import (
    ControlCommand,
    ControlCommandType,
    ControlScope,
)
from framework.core.agent import AgentContext
from framework.core.tool_manager import ToolResult
from framework.core.types import ToolCall
from framework.interceptor.abc import InterceptorScope, ToolCallContext
from framework.interceptor.builtin.tool_approval import (
    ApprovalDeniedAction,
    ApprovalTimeoutAction,
    ToolApprovalInterceptor,
    ToolNameMatcher,
)


class FakeCtx:
    def __init__(self) -> None:
        self.session_id = "s1"
        self.metadata = {"agent_id": "agent-1"}


@pytest.fixture
def fake_ctx():
    return FakeCtx()


@pytest.fixture
def channel():
    return InMemoryControlChannel()


@pytest.fixture
def matched_tool_call():
    return ToolCallContext(
        tool_call=ToolCall(tool_name="shell", arguments={"cmd": "ls"}, call_id="call-1"),
        tool_name="shell",
        arguments={"cmd": "ls"},
        session_id="s1",
        turn_id="t1",
    )


@pytest.fixture
def unmatched_tool_call():
    return ToolCallContext(
        tool_call=ToolCall(tool_name="read_file", arguments={"path": "/tmp/a"}, call_id="call-2"),
        tool_name="read_file",
        arguments={"path": "/tmp/a"},
        session_id="s1",
        turn_id="t1",
    )


class TestToolApprovalDenyAsToolError:
    """deny_as_tool_error must produce a valid ToolResult (is_error=True)."""

    @pytest.mark.asyncio
    async def test_unmatched_tool_bypasses_approval(self, fake_ctx, channel, unmatched_tool_call):
        interceptor = ToolApprovalInterceptor(
            channel=channel,
            matcher=ToolNameMatcher({"shell"}),
        )

        async def next_call() -> ToolResult:
            return ToolResult(tool_name="read_file", result="content")

        result = await interceptor.around_tool_call(fake_ctx, unmatched_tool_call, next_call)
        assert result.result == "content"

    @pytest.mark.asyncio
    async def test_deny_produces_tool_error_result(self, fake_ctx, channel, matched_tool_call):
        interceptor = ToolApprovalInterceptor(
            channel=channel,
            matcher=ToolNameMatcher({"shell"}),
            on_denied=ApprovalDeniedAction.TOOL_ERROR,
            approval_timeout_seconds=0.1,
        )

        async def next_call() -> ToolResult:
            return ToolResult(tool_name="shell", result="should_not_run")

        # Do NOT send approval response → triggers timeout → timeout_as_tool_error
        result = await interceptor.around_tool_call(fake_ctx, matched_tool_call, next_call)

        assert isinstance(result, ToolResult)
        assert result.error is not None
        assert "timed out" in result.error or "not approved" in result.error
        assert result.call_id == "call-1"

    @pytest.mark.asyncio
    async def test_timeout_produces_tool_error_result(self, fake_ctx, channel, matched_tool_call):
        """Timeout with timeout_as_tool_error produces valid ToolResult."""
        interceptor = ToolApprovalInterceptor(
            channel=channel,
            matcher=ToolNameMatcher({"shell"}),
            on_denied=ApprovalDeniedAction.TOOL_ERROR,
            approval_timeout_seconds=0.05,
            on_timeout=ApprovalTimeoutAction.TOOL_ERROR,
        )

        async def next_call() -> ToolResult:
            return ToolResult(tool_name="shell", result="should_not_run")

        result = await interceptor.around_tool_call(fake_ctx, matched_tool_call, next_call)

        assert isinstance(result, ToolResult)
        assert result.error is not None
        assert "timed out" in result.error
        assert result.call_id == "call-1"


class TestToolApprovalDenyAsCancel:
    """cancel_turn / timeout cancel_turn must raise AgentControlError."""

    @pytest.mark.asyncio
    async def test_timeout_cancel_turn_raises_agent_cancelled(self, fake_ctx, channel, matched_tool_call):
        """Timeout with CANCEL_TURN raises AgentCancelled (AgentControlError)."""
        from framework.control.exceptions import AgentControlError

        interceptor = ToolApprovalInterceptor(
            channel=channel,
            matcher=ToolNameMatcher({"shell"}),
            on_denied=ApprovalDeniedAction.CANCEL_TURN,
            approval_timeout_seconds=0.05,
            on_timeout=ApprovalTimeoutAction.CANCEL_TURN,
        )

        async def next_call() -> ToolResult:
            return ToolResult(tool_name="shell", result="should_not_run")

        with pytest.raises(AgentControlError) as exc_info:
            await interceptor.around_tool_call(fake_ctx, matched_tool_call, next_call)

        # Timeout path raises AgentCancelled, which is an AgentControlError
        assert exc_info.value.termination.value == "cancelled"


class TestToolApprovalRedaction:
    """Approval request parameters must be redacted."""

    def test_redact_args_removes_sensitive_keys(self):
        args = {
            "api_key": "secret123",
            "token": "tok456",
            "password": "pwd789",
            "normal_field": "visible",
            "ACCESS_KEY": "AKIA...",
        }
        result = ToolApprovalInterceptor._redact_args(args)
        assert result["api_key"] == "***"
        assert result["token"] == "***"
        assert result["password"] == "***"
        assert result["ACCESS_KEY"] == "***"
        assert result["normal_field"] == "visible"
