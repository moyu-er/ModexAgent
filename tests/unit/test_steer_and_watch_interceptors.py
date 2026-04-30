"""Tests for SteerInjectInterceptor and ToolWatchInterceptor."""

import asyncio
import pytest

from framework.control.channel import InMemoryControlChannel
from framework.control.types import (
    ControlCommand, ControlCommandType, ControlScope,
)
from framework.interceptor.builtin.steer_inject import SteerInjectInterceptor
from framework.interceptor.builtin.tool_watch import ToolWatchInterceptor, ToolCancelPolicy
from framework.interceptor.abc import ToolCallContext
from framework.core.tool_manager import ToolResult
from framework.core.types import ToolCall


def _tool_call(name="test_tool", call_id="tc1") -> ToolCall:
    return ToolCall(tool_name=name, arguments={}, call_id=call_id)


def _ctx(sid="s1"):
    from unittest.mock import MagicMock
    ctx = MagicMock()
    ctx.session_id = sid
    ctx.metadata = {}
    return ctx


class TestSteerInject:
    async def test_injects_into_result(self):
        ch = InMemoryControlChannel()
        await ch.send(ControlCommand(
            command_id="steer1",
            type=ControlCommandType.INJECT_STEER,
            scope=ControlScope(session_id="s1"),
            payload={"text": "try a different approach"},
        ))
        interceptor = SteerInjectInterceptor(channel=ch)
        call = ToolCallContext(
            tool_call=_tool_call("shell", "tc1"),
            tool_name="shell",
            arguments={"cmd": "ls"},
            session_id="s1",
        )

        async def _next():
            return ToolResult(tool_name="shell", result="file1\nfile2")

        result = await interceptor.around_tool_call(_ctx(), call, _next)
        assert "[User guidance" in result.result

    async def test_injects_into_error_result(self):
        ch = InMemoryControlChannel()
        await ch.send(ControlCommand(
            command_id="steer1",
            type=ControlCommandType.INJECT_STEER,
            scope=ControlScope(session_id="s1"),
            payload={"text": "check permissions"},
        ))
        interceptor = SteerInjectInterceptor(channel=ch)
        call = ToolCallContext(
            tool_call=_tool_call("shell", "tc1"),
            tool_name="shell",
            arguments={"cmd": "bad"},
            session_id="s1",
        )

        async def _next():
            return ToolResult(tool_name="shell", result=None, error="command not found")

        result = await interceptor.around_tool_call(_ctx(), call, _next)
        assert "[User guidance" in result.error

    async def test_no_steer_when_channel_empty(self):
        ch = InMemoryControlChannel()
        interceptor = SteerInjectInterceptor(channel=ch)
        call = ToolCallContext(
            tool_call=_tool_call("echo", "tc1"),
            tool_name="echo",
            arguments={"text": "hi"},
            session_id="s1",
        )

        async def _next():
            return ToolResult(tool_name="echo", result="hi")

        result = await interceptor.around_tool_call(_ctx(), call, _next)
        assert result.result == "hi"
        assert "[User guidance" not in str(result.result)


class TestToolWatch:
    """ToolWatchInterceptor 竞争条件测试。

    在单线程 async 环境中，监控异步延迟工具（sleep 模拟），验证取消逻辑。
    """

    async def test_tool_completes_before_cancel(self):
        """工具在取消到达前完成 — 返回正常结果。"""
        ch = InMemoryControlChannel()
        interceptor = ToolWatchInterceptor(channel=ch, poll_interval=0.01)
        call = ToolCallContext(
            tool_call=_tool_call("fast", "tc1"),
            tool_name="fast",
            arguments={},
            session_id="s1",
        )

        async def _next():
            return ToolResult(tool_name="fast", result="done")

        result = await interceptor.around_tool_call(_ctx(), call, _next)
        assert result.result == "done"

    async def test_cancel_tool_arrives_during_execution(self):
        """取消命令在工具执行期间到达 — 应触发 AgentCancelled。"""
        ch = InMemoryControlChannel()
        interceptor = ToolWatchInterceptor(
            channel=ch, poll_interval=0.01,
            cancel_policy=ToolCancelPolicy.DISCARD_RESULT,
        )
        call = ToolCallContext(
            tool_call=_tool_call("slow", "tc1"),
            tool_name="slow",
            arguments={},
            session_id="s1",
        )

        async def _slow():
            await asyncio.sleep(0.1)
            return ToolResult(tool_name="slow", result="done")

        # Schedule cancel after a short delay
        async def _send_cancel():
            await asyncio.sleep(0.01)
            await ch.send(ControlCommand(
                command_id="cancel_tc1",
                type=ControlCommandType.CANCEL_TOOL,
                scope=ControlScope(session_id="s1"),
                payload={"tool_call_id": "tc1"},
            ))

        from framework.control.exceptions import AgentCancelled

        with pytest.raises(AgentCancelled):
            task_tool = asyncio.create_task(
                interceptor.around_tool_call(_ctx(), call, _slow)
            )
            task_cancel = asyncio.create_task(_send_cancel())
            done, _ = await asyncio.wait(
                [task_tool, task_cancel], return_when=asyncio.FIRST_COMPLETED,
            )
            await task_tool  # Should raise AgentCancelled
