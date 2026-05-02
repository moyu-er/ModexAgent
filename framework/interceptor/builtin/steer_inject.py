"""SteerInjectInterceptor — 引导注入拦截器。

将用户 steer 文本追加到 tool result（非中断式方向引导）。
必须在审批外层注册，确保审批拒绝时 steer 也能注入到伪错误结果。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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


class SteerInjectInterceptor:
    """引导注入拦截器。将用户 steer 文本追加到 tool result。

    必须在审批外层注册（洋葱序上排在 TieredToolApprovalInterceptor 之前），
    确保审批拒绝产生伪 ToolResult 时 steer 也能注入。
    """

    scopes = frozenset([InterceptorScope.TOOL_CALL])

    def __init__(self, channel: ControlChannel):
        self._channel = channel

    async def around_tool_call(
        self,
        ctx: AgentContext[Any],
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        result = await next_call()

        scope = ControlScope(session_id=ctx.session_id)
        cmds = await self._channel.drain(
            scope, limit=1,
            command_types={ControlCommandType.INJECT_STEER},
        )
        for cmd in cmds:
            if cmd.type == ControlCommandType.INJECT_STEER:
                text = str(cmd.payload.get("text", ""))
                if text:
                    if result.result:
                        result.result = str(result.result) + (
                            f"\n\n[User guidance for next step]: {text}"
                        )
                    elif result.error:
                        result.error = str(result.error) + (
                            f"\n\n[User guidance for next step]: {text}"
                        )
                    else:
                        result.result = (
                            f"[User guidance for next step]: {text}"
                        )
        return result
