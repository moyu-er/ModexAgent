"""ToolApprovalInterceptor — 工具调用审批拦截器。

在工具执行前发起审批，支持 den_as_tool_error 与 deny_as_cancel。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from framework.control.exceptions import AgentCancelled, ApprovalDenied
from framework.control.types import (
    ControlCommand,
    ControlEvent,
    ControlEventType,
    ControlScope,
)
from framework.interceptor.abc import (
    InterceptorScope,
    ToolCallContext,
    ToolCallNext,
)
from framework.core.tool_manager import ToolResult

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.control.channel import ControlChannel
    from framework.control.event_bus import ControlEventBus

logger = logging.getLogger(__name__)


class ApprovalDeniedAction(str, Enum):
    """审批拒绝时的行为。"""

    TOOL_ERROR = "deny_as_tool_error"
    CANCEL_TURN = "deny_as_cancel"


class ApprovalTimeoutAction(str, Enum):
    """审批超时时的行为。"""

    TOOL_ERROR = "timeout_as_tool_error"
    CANCEL_TURN = "timeout_as_cancel"


class ToolNameMatcher:
    """工具名称匹配器，支持精确匹配和通配符。"""

    def __init__(self, patterns: set[str]) -> None:
        self._patterns = patterns

    def matches(self, tool_name: str) -> bool:
        """检查工具名称是否匹配。"""
        return tool_name in self._patterns


@dataclass(frozen=True)
class ToolApprovalRequest:
    """工具审批请求（脱敏后）。"""

    agent_id: str
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    redacted_arguments: Mapping[str, object]
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class ToolApprovalInterceptor:
    """工具调用审批拦截器。

    在 around_tool_call 中：
    1. 检查工具名称是否需要审批
    2. 发布 TOOL_APPROVAL_REQUESTED 事件
    3. 等待审批响应（通过 ControlChannel.drain）
    4. 根据审批结果决定：执行、写入伪错误、或取消
    """

    scopes = frozenset([InterceptorScope.TOOL_CALL])

    def __init__(
        self,
        channel: ControlChannel,
        event_bus: ControlEventBus | None = None,
        matcher: ToolNameMatcher | None = None,
        approval_timeout_seconds: float = 60.0,
        on_denied: ApprovalDeniedAction = ApprovalDeniedAction.TOOL_ERROR,
        on_timeout: ApprovalTimeoutAction = ApprovalTimeoutAction.TOOL_ERROR,
    ) -> None:
        self._channel = channel
        self._event_bus = event_bus
        self._matcher = matcher
        self._approval_timeout = approval_timeout_seconds
        self._on_denied = on_denied
        self._on_timeout = on_timeout

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        # 无需审批的工具直接放行
        if self._matcher is not None and not self._matcher.matches(call.tool_name):
            return await next_call()

        # 构建审批请求
        approval_req = ToolApprovalRequest(
            agent_id=ctx.metadata.get("agent_id", "unknown"),
            session_id=ctx.session_id,
            turn_id=call.turn_id,
            tool_call_id=call.tool_call.call_id or "",
            tool_name=call.tool_name,
            redacted_arguments=self._redact_args(call.arguments),
        )

        # 发布审批事件
        if self._event_bus is not None:
            await self._event_bus.emit(
                ControlEvent(
                    event_id=uuid.uuid4().hex,
                    type=ControlEventType.TOOL_APPROVAL_REQUESTED,
                    scope=ControlScope(session_id=ctx.session_id),
                    correlation_id=approval_req.correlation_id,
                    payload={
                        "tool_name": call.tool_name,
                        "tool_call_id": approval_req.tool_call_id,
                    },
                )
            )

        # 等待审批响应
        import asyncio

        scope = ControlScope(session_id=ctx.session_id, agent_id=approval_req.agent_id)
        try:
            commands = await asyncio.wait_for(
                self._drain_approval(scope, approval_req.correlation_id),
                timeout=self._approval_timeout,
            )
        except asyncio.TimeoutError:
            return self._handle_timeout(call)

        if not commands:
            return self._handle_timeout(call)

        cmd = commands[0]
        action = str(cmd.payload.get("action", ""))

        if action == "allow":
            return await next_call()
        elif action == "deny":
            return self._handle_denied(call)
        else:
            return await next_call()

    async def _drain_approval(
        self,
        scope: ControlScope,
        correlation_id: str,
    ) -> list[ControlCommand]:
        """获取与审批请求相关的命令。"""
        cmds = await self._channel.drain(scope, limit=1)
        return [c for c in cmds if c.correlation_id == correlation_id]

    def _handle_denied(self, call: ToolCallContext) -> ToolResult:
        if self._on_denied == ApprovalDeniedAction.CANCEL_TURN:
            raise ApprovalDenied(
                f"Tool '{call.tool_name}' was not approved"
            )

        call_id = call.tool_call.call_id or "" if call.tool_call else ""
        return ToolResult(
            tool_name=call.tool_name,
            call_id=call_id,
            result=None,
            error="Error: Tool execution was not approved. The tool was not run.",
        )

    def _handle_timeout(self, call: ToolCallContext) -> ToolResult:
        if self._on_timeout == ApprovalTimeoutAction.CANCEL_TURN:
            raise AgentCancelled(
                f"Tool approval timed out for '{call.tool_name}'"
            )

        call_id = call.tool_call.call_id or "" if call.tool_call else ""
        return ToolResult(
            tool_name=call.tool_name,
            call_id=call_id,
            result=None,
            error="Error: Tool approval timed out. The tool was not run.",
        )

    @staticmethod
    def _redact_args(arguments: Mapping[str, object]) -> dict[str, object]:
        """脱敏参数，移除敏感字段。"""
        sensitive_keys = {
            "api_key", "secret", "token", "password", "credential",
            "access_key", "private_key",
        }
        return {
            k: ("***" if k.lower() in sensitive_keys else v)
            for k, v in arguments.items()
        }
