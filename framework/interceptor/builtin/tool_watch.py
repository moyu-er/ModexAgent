"""ToolWatchInterceptor — 工具执行中并发监听取消命令。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from enum import Enum
from typing import TYPE_CHECKING

from framework.control.exceptions import AgentCancelled
from framework.control.types import ControlCommandType, ControlScope
from framework.interceptor.abc import (
    InterceptorScope,
    ToolCallContext,
    ToolCallNext,
)

if TYPE_CHECKING:
    from framework.control.channel import ControlChannel
    from framework.core.agent import AgentContext
    from framework.core.tool_manager import ToolResult

logger = logging.getLogger(__name__)


class ToolCancelPolicy(str, Enum):
    """取消命令到达时的处理策略。"""

    WAIT_GRACEFUL = "wait_graceful"       # 等 tool 自然结束，保留结果但标记为 cancelled
    DISCARD_RESULT = "discard_result"     # 立即丢弃，返回伪错误


class ToolWatchInterceptor:
    """Tool 执行期间并发监听取消命令。

    温和取消策略：先 set event 通知 → 等 tool 自行结束（5s 兜底）→ 超时强制 cancel task。

    竞争条件处理：如果 tool_task 和 watcher 同时完成，优先返回 tool 的正常结果。
    """

    scopes = frozenset([InterceptorScope.TOOL_CALL])

    def __init__(
        self,
        channel: ControlChannel,
        poll_interval: float = 0.3,
        cancel_policy: ToolCancelPolicy = ToolCancelPolicy.WAIT_GRACEFUL,
    ):
        self._channel = channel
        self._poll_interval = poll_interval
        self._cancel_policy = cancel_policy

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        scope = ControlScope(session_id=ctx.session_id)
        cancel_evt = asyncio.Event()
        cancel_reason: str | None = None

        async def _execute() -> ToolResult:
            return await next_call()

        async def _watch() -> None:
            nonlocal cancel_reason
            while not cancel_evt.is_set():
                cmds = await self._channel.peek(
                    scope,
                    command_types={
                        ControlCommandType.CANCEL_TOOL,
                        ControlCommandType.CANCEL_TURN,
                        ControlCommandType.CANCEL_RUN,
                    },
                )
                for cmd in cmds:
                    if cmd.type == ControlCommandType.CANCEL_TOOL:
                        target_id = cmd.payload.get("tool_call_id")
                        current_id = call.tool_call.call_id or ""
                        if target_id is not None and target_id != current_id:
                            continue
                        await self._channel.drain(
                            scope, limit=1,
                            command_types={ControlCommandType.CANCEL_TOOL},
                        )
                        cancel_reason = f"Tool cancelled: {cmd.type.value}"
                        cancel_evt.set()
                        return
                    elif cmd.type in (ControlCommandType.CANCEL_TURN,
                                      ControlCommandType.CANCEL_RUN):
                        cancel_reason = f"Tool cancelled: {cmd.type.value}"
                        cancel_evt.set()
                        ctx.metadata["_cancel_cmd_type"] = cmd.type.value
                        return
                await asyncio.sleep(self._poll_interval)

        tool_task = asyncio.create_task(_execute())
        watcher = asyncio.create_task(_watch())

        try:
            done, _ = await asyncio.wait(
                [tool_task, watcher], return_when=asyncio.FIRST_COMPLETED,
            )

            # 优先 tool_task 结果
            if tool_task in done:
                result = await tool_task
                if watcher in done:
                    ctx.metadata.setdefault("_cancelled_tool_records", {})[
                        call.tool_call.call_id or ""
                    ] = {"tool_name": call.tool_name, "result_retained": True}
                return result

            # watcher completed first — tool still running
            if self._cancel_policy == ToolCancelPolicy.WAIT_GRACEFUL:
                try:
                    result = await asyncio.wait_for(tool_task, timeout=5.0)
                    # tool completed within grace period — return result with annotation
                    ctx.metadata.setdefault("_cancelled_tool_records", {})[
                        call.tool_call.call_id or ""
                    ] = {"tool_name": call.tool_name, "result_retained": True}
                    cancelled_note = (
                        "\n\n[Note: This tool was requested to cancel but "
                        "completed before the grace timeout.]"
                    )
                    if result.result:
                        result.result = str(result.result) + cancelled_note
                    elif result.error:
                        result.error = str(result.error) + cancelled_note
                    return result
                except TimeoutError:
                    tool_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await tool_task
                    ctx.metadata.setdefault("_cancelled_tool_records", {})[
                        call.tool_call.call_id or ""
                    ] = {"tool_name": call.tool_name}
                    raise AgentCancelled(
                        f"Tool '{call.tool_name}' cancelled (timeout)"
                    ) from None

            # DISCARD_RESULT
            tool_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tool_task
            ctx.metadata.setdefault("_cancelled_tool_records", {})[
                call.tool_call.call_id or ""
            ] = {"tool_name": call.tool_name}
            raise AgentCancelled(f"Tool '{call.tool_name}' cancelled")
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
