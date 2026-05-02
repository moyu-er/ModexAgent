"""Tests for TieredToolApprovalInterceptor — 3-tier approval + deny_as_cancel."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from framework.control.channel import InMemoryControlChannel
from framework.control.types import (
    ControlCommand, ControlCommandType, ControlEventType, ControlScope,
)
from framework.core.agent import AgentContext
from framework.core.types import ToolCall
from framework.interceptor.abc import ToolCallContext
from framework.interceptor.builtin.tool_approval import (
    ApprovalTier,
    DenyAction,
    TieredToolApprovalInterceptor,
    TimeoutAction,
    ToolNameMatcher,
)
from framework.core.tool_manager import ToolResult
from framework.memory.history import ListMessageHistory


def _make_tool_call(name: str = "shell", call_id: str = "tc1") -> ToolCall:
    return ToolCall(tool_name=name, arguments={"cmd": "ls"}, call_id=call_id)


def _make_ctx() -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=MagicMock(),
        session_id="s1",
        metadata={},
    )


class TestHardline:
    """Hardline 层：无条件拒绝，不通过审批。"""

    async def test_hardline_rejects_directly(self):
        ch = InMemoryControlChannel()
        interceptor = TieredToolApprovalInterceptor(
            channel=ch,
            hardline_matcher=ToolNameMatcher({"rm_rf_root"}),
        )
        ctx = _make_ctx()
        call = ToolCallContext(
            tool_call=_make_tool_call("rm_rf_root"),
            tool_name="rm_rf_root",
            arguments={"target": "/"},
            session_id="s1",
        )

        async def _next() -> ToolResult:
            return ToolResult(tool_name="rm_rf_root", result="ok")

        result = await interceptor.around_tool_call(ctx, call, _next)
        assert result.error is not None
        assert "hardline" in result.error.lower()
        assert result.result is None  # 工具未执行

    async def test_hardline_bypasses_non_matching_tool(self):
        ch = InMemoryControlChannel()
        interceptor = TieredToolApprovalInterceptor(
            channel=ch,
            hardline_matcher=ToolNameMatcher({"rm_rf_root"}),
        )
        ctx = _make_ctx()
        call = ToolCallContext(
            tool_call=_make_tool_call("read_file"),
            tool_name="read_file",
            arguments={"path": "/tmp/x"},
            session_id="s1",
        )

        async def _next() -> ToolResult:
            return ToolResult(tool_name="read_file", result="content")

        result = await interceptor.around_tool_call(ctx, call, _next)
        assert result.result == "content"  # 正常执行


class TestDangerous:
    """Dangerous 层：必须审批，YOLO 不可跳过。

    由于审批响应的 correlation_id 由拦截器内部生成，外部无法预置匹配的响应。
    这些测试验证审批超时的行为（超时是审批流程的合理终止路径）。
    """

    async def test_dangerous_tool_triggers_approval_timeout(self):
        ch = InMemoryControlChannel()
        interceptor = TieredToolApprovalInterceptor(
            channel=ch,
            dangerous_matcher=ToolNameMatcher({"shell"}),
            approval_timeout_seconds=0.001,
            on_timeout=TimeoutAction.TOOL_ERROR,
        )
        ctx = _make_ctx()
        call = ToolCallContext(
            tool_call=_make_tool_call("shell", "tc1"),
            tool_name="shell",
            arguments={"cmd": "rm -rf /"},
            session_id="s1",
        )

        async def _next() -> ToolResult:
            return ToolResult(tool_name="shell", result="ok")

        result = await interceptor.around_tool_call(ctx, call, _next)
        assert result.error is not None
        assert "timed out" in result.error.lower()


class TestDenyAsCancel:
    """deny_as_cancel 设置 _deny_as_cancel 标记，不抛异常，返回合法 ToolResult。"""

    async def test_deny_as_cancel_sets_flag_and_returns_tool_result(self):
        ch = InMemoryControlChannel()
        interceptor = TieredToolApprovalInterceptor(
            channel=ch,
            dangerous_matcher=ToolNameMatcher({"shell"}),
            approval_timeout_seconds=0.001,
            on_timeout=TimeoutAction.CANCEL_TURN,
        )
        ctx = _make_ctx()
        call = ToolCallContext(
            tool_call=_make_tool_call("shell", "tc1"),
            tool_name="shell",
            arguments={"cmd": "rm"},
            session_id="s1",
        )

        async def _next() -> ToolResult:
            return ToolResult(tool_name="shell", result="ok")

        result = await interceptor.around_tool_call(ctx, call, _next)
        # 返回合法 ToolResult（非异常）
        assert isinstance(result, ToolResult)
        assert result.error is not None
        # 设置标记
        assert ctx.metadata.get("_deny_as_cancel") is True


class TestSensitiveYolo:
    """Sensitive 层：YOLO 模式可跳过审批。"""

    async def test_yolo_skips_sensitive_approval(self):
        ch = InMemoryControlChannel()
        interceptor = TieredToolApprovalInterceptor(
            channel=ch,
            sensitive_matcher=ToolNameMatcher({"shell"}),
        )
        ctx = _make_ctx()
        ctx.metadata["approval_yolo"] = True
        call = ToolCallContext(
            tool_call=_make_tool_call("shell", "tc1"),
            tool_name="shell",
            arguments={"cmd": "ls"},
            session_id="s1",
        )

        async def _next() -> ToolResult:
            return ToolResult(tool_name="shell", result="ok")

        result = await interceptor.around_tool_call(ctx, call, _next)
        assert result.result == "ok"  # 直接放行


class TestTimeout:
    """审批超时处理。"""

    async def test_timeout_as_cancel_sets_flag(self):
        ch = InMemoryControlChannel()
        interceptor = TieredToolApprovalInterceptor(
            channel=ch,
            dangerous_matcher=ToolNameMatcher({"shell"}),
            approval_timeout_seconds=0.001,
            on_timeout=TimeoutAction.CANCEL_TURN,
        )
        ctx = _make_ctx()
        call = ToolCallContext(
            tool_call=_make_tool_call("shell", "tc1"),
            tool_name="shell",
            arguments={"cmd": "ls"},
            session_id="s1",
        )

        async def _next() -> ToolResult:
            return ToolResult(tool_name="shell", result="ok")

        result = await interceptor.around_tool_call(ctx, call, _next)
        assert result.error is not None
        assert "timed out" in result.error.lower()
        assert ctx.metadata.get("_deny_as_cancel") is True


class TestPreApprovedSkip:
    """Pre-approved tools (via ToolNode Phase 2) must NOT be re-intercepted.

    This reproduces the regression where TieredToolApprovalInterceptor.around_tool_call
    independently re-requests approval via ControlChannel for tools already resolved
    by the ToolNode's SuspendResumeStrategy, causing the resume path to hang.
    """

    async def test_pre_approved_tool_bypasses_control_channel_approval(self):
        """A dangerous tool marked in _pre_approved_tool_ids executes directly.

        Without the fix, around_tool_call re-matches 'shell' as DANGEROUS and
        calls _request_approval → _wait_response, blocking on the control channel
        until timeout.  With the fix, the pre-approved check at the top of
        around_tool_call returns next_call() immediately.
        """
        ch = InMemoryControlChannel()
        interceptor = TieredToolApprovalInterceptor(
            channel=ch,
            dangerous_matcher=ToolNameMatcher({"shell"}),
            approval_timeout_seconds=0.05,  # short but distinguishable from pre-approved fast path
            on_timeout=TimeoutAction.TOOL_ERROR,
        )
        ctx = _make_ctx()
        # Simulate ToolNode._execute_batch marking this tool as pre-approved
        ctx.metadata["_pre_approved_tool_ids"] = {"tc1"}

        call = ToolCallContext(
            tool_call=_make_tool_call("shell", "tc1"),
            tool_name="shell",
            arguments={"cmd": "dir"},
            session_id="s1",
        )

        async def _next() -> ToolResult:
            return ToolResult(tool_name="shell", result="ok")

        result = await interceptor.around_tool_call(ctx, call, _next)
        # Pre-approved → tool executes immediately, no control-channel wait
        assert result.result == "ok"
        assert result.error is None

    async def test_non_pre_approved_tool_still_goes_through_approval(self):
        """Without the pre-approved marker, the same dangerous tool times out.

        This confirms the pre-approval check is the ONLY reason the test above
        succeeds, not a side effect of the interceptor config.
        """
        ch = InMemoryControlChannel()
        interceptor = TieredToolApprovalInterceptor(
            channel=ch,
            dangerous_matcher=ToolNameMatcher({"shell"}),
            approval_timeout_seconds=0.001,
            on_timeout=TimeoutAction.TOOL_ERROR,
        )
        ctx = _make_ctx()
        # NO _pre_approved_tool_ids — tool must go through control channel

        call = ToolCallContext(
            tool_call=_make_tool_call("shell", "tc2"),
            tool_name="shell",
            arguments={"cmd": "dir"},
            session_id="s1",
        )

        async def _next() -> ToolResult:
            return ToolResult(tool_name="shell", result="ok")

        result = await interceptor.around_tool_call(ctx, call, _next)
        # No pre-approval → control channel wait → timeout
        assert result.error is not None
        assert "timed out" in result.error.lower()
