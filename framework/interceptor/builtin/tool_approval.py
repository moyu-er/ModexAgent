"""ToolApprovalInterceptor — 工具调用审批拦截器。

TieredToolApprovalInterceptor 提供三级审批 (hardline/dangerous/sensitive)。
ToolApprovalInterceptor 保留为简化的单层审批实现（向后兼容）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping
import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from framework.runtime.models import ApprovalDenialContext
from framework.control.types import (
    ControlCommand,
    ControlCommandType,
    ControlEvent,
    ControlEventType,
    ControlScope,
)
from framework.core.tool_manager import ToolResult
from framework.interceptor.abc import (
    InterceptorScope,
    ToolCallContext,
    ToolCallNext,
)

if TYPE_CHECKING:
    from framework.control.channel import ControlChannel
    from framework.control.event_bus import ControlEventBus
    from framework.core.agent import AgentContext

logger = logging.getLogger(__name__)


class ApprovalTier(str, Enum):
    """审批层级。"""

    HARDLINE = "hardline"    # 无条件拒绝，永不执行
    DANGEROUS = "dangerous"  # 必须审批，YOLO 不可跳过
    SENSITIVE = "sensitive"  # 需要审批，YOLO 可跳过
    NORMAL = "normal"        # 直接放行


class DenyAction(str, Enum):
    """审批拒绝时的行为。"""

    TOOL_ERROR = "deny_as_tool_error"    # 返回伪 ToolResult，agent 继续
    CANCEL_TURN = "deny_as_cancel"       # 终止，但补齐所有未执行 tool 的伪结果


class TimeoutAction(str, Enum):
    """审批超时时的行为。"""

    TOOL_ERROR = "timeout_as_tool_error"
    CANCEL_TURN = "timeout_as_cancel"


def _looks_like_path(val: str) -> bool:
    """Heuristic: does the value look like a filesystem path?"""
    if "/" in val or "\\" in val:
        return True
    if len(val) >= 2 and val[1] == ":" and val[0].isalpha():
        return True  # Windows drive letter: C:...
    return val.endswith((".txt", ".py", ".json", ".yml", ".yaml", ".md", ".csv"))


class ArgumentMatcher:
    """Match tool arguments against allowed path patterns for approval.

    Uses fnmatch for cross-platform wildcard support (*, ?, []).
    Resolves . to project_root, ~ to user home.
    """

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = project_root

    def matches(self, arguments: dict[str, Any], allowed_paths: list[str]) -> bool:
        """Returns True if all path arguments match at least one allowed pattern."""
        paths = self._extract_paths(arguments)
        if not paths:
            return True  # No path arguments — nothing to check
        for path in paths:
            resolved = self._resolve_path(path)
            if not self._match_any(resolved, allowed_paths):
                return False
        return True

    def _resolve_path(self, raw: str) -> Path:
        """Resolve special path prefixes.

        - . / ./xxx  → project_root (or cwd if project_root not set)
        - ~ / ~/xxx  → Path.home()
        - absolute   → as-is
        """
        if raw.startswith("~/"):
            return Path.home() / raw[2:]
        if raw == ".":
            return self.project_root if self.project_root is not None else Path(".").resolve()
        if raw.startswith("./"):
            root = self.project_root if self.project_root is not None else Path(".").resolve()
            return root / raw[2:]
        return Path(raw).expanduser()

    def _match_any(self, path: Path, patterns: list[str]) -> bool:
        """Check if path matches any pattern using fnmatch.

        Normalizes Windows backslashes to forward slashes for fnmatch.
        """
        path_str = str(path).replace("\\", "/")
        for pattern in patterns:
            resolved_pattern = self._resolve_path(pattern)
            pattern_str = str(resolved_pattern).replace("\\", "/")
            if fnmatch.fnmatch(path_str, pattern_str):
                return True
        return False

    def _extract_paths(self, arguments: dict[str, Any]) -> list[str]:
        """Extract path-like values from tool arguments."""
        path_keys = {
            "path", "file_path", "target", "dest",
            "directory", "dir", "working_dir",
        }
        paths: list[str] = []
        for key, value in arguments.items():
            if key in path_keys and isinstance(value, str):
                paths.append(value)
        return paths

    # ---- Legacy API (retained for backward compat during migration) ----

    def is_allowed(self, tool_call) -> bool:
        """Legacy API — delegates to matches() with empty allowed_paths.

        This returns False (not allowed) when no paths match, which in the
        old API meant "needs approval" (dangerous). Kept for interceptor use.
        """
        args = tool_call.arguments or {}
        return self.matches(args, [])  # type: ignore[return-value]


class ToolNameMatcher:
    """工具名称匹配器，支持精确匹配。"""

    def __init__(self, patterns: set[str]) -> None:
        self._patterns = patterns

    def matches(self, tool_name: str) -> bool:
        """检查工具名称是否匹配。"""
        return tool_name in self._patterns


# ── Simple single-tier (backward compatible) ──


class ApprovalDeniedAction(str, Enum):
    """审批拒绝时的行为（兼容旧名）。"""
    TOOL_ERROR = "deny_as_tool_error"
    CANCEL_TURN = "deny_as_cancel"


class ApprovalTimeoutAction(str, Enum):
    """审批超时时的行为（兼容旧名）。"""
    TOOL_ERROR = "timeout_as_tool_error"
    CANCEL_TURN = "timeout_as_cancel"


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
    """简化的单层审批拦截器（向后兼容）。

    推荐新代码使用 TieredToolApprovalInterceptor。
    """

    scopes = frozenset([InterceptorScope.TOOL_CALL])

    @staticmethod
    def _redact_args(arguments: Mapping[str, object]) -> dict[str, object]:
        return _redact_args(arguments)

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
        ctx: AgentContext[Any],
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        if self._matcher is not None and not self._matcher.matches(call.tool_name):
            return await next_call()

        approval_req = ToolApprovalRequest(
            agent_id=ctx.metadata.get("agent_id", "unknown"),
            session_id=ctx.session_id,
            turn_id=call.turn_id,
            tool_call_id=call.tool_call.call_id or "",
            tool_name=call.tool_name,
            redacted_arguments=_redact_args(call.arguments),
        )

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

        scope = ControlScope(session_id=ctx.session_id, agent_id=approval_req.agent_id)
        try:
            commands = await asyncio.wait_for(
                _drain_approval(self._channel, scope, approval_req.correlation_id),
                timeout=self._approval_timeout,
            )
        except TimeoutError:
            return _handle_timeout(call, self._on_timeout)

        if not commands:
            return _handle_timeout(call, self._on_timeout)

        cmd = commands[0]
        action = str(cmd.payload.get("action", ""))

        if action == "allow":
            return await next_call()
        elif action == "deny":
            return _handle_denied(call, self._on_denied)
        else:
            return await next_call()


# ── Tiered approval ──


class TieredToolApprovalInterceptor:
    """三级审批拦截器。

    关键原则：永远返回合法 ToolResult。deny_as_cancel 时设置终止标记，
    由 ReActAgent 在循环中检测并补齐剩余 tool call 的结果。
    """

    scopes = frozenset([InterceptorScope.TOOL_CALL])

    def __init__(
        self,
        channel: ControlChannel,
        event_bus: ControlEventBus | None = None,
        hardline_matcher: ToolNameMatcher | None = None,
        dangerous_matcher: ToolNameMatcher | None = None,
        sensitive_matcher: ToolNameMatcher | None = None,
        argument_matcher: ArgumentMatcher | None = None,
        approval_timeout_seconds: float = 60.0,
        on_denied: DenyAction = DenyAction.TOOL_ERROR,
        on_timeout: TimeoutAction = TimeoutAction.TOOL_ERROR,
    ):
        self._channel = channel
        self._event_bus = event_bus
        self._hardline = hardline_matcher
        self._dangerous = dangerous_matcher
        self._sensitive = sensitive_matcher
        self._argument_matcher = argument_matcher
        self._approval_timeout = approval_timeout_seconds
        self._on_denied = on_denied
        self._on_timeout = on_timeout

    async def around_tool_call(
        self,
        ctx: AgentContext[Any],
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        tool_name = call.tool_name
        call_id = call.tool_call.call_id or "" if call.tool_call else ""

        # If the ToolNode already resolved approval (Phase 2), skip redundant
        # control-channel approval here.  Pre-approved ids are written by
        # ToolNode._execute_batch before Phase 3.
        pre_approved: set[str] = ctx.metadata.get("_pre_approved_tool_ids", set())  # type: ignore[assignment]
        if call_id in pre_approved:
            return await next_call()

        # 1) Hardline: 无条件拒绝
        if self._hardline and self._hardline.matches(tool_name):
            return ToolResult(
                tool_name=tool_name,
                call_id=call.tool_call.call_id or "",
                error=f"Error: '{tool_name}' is blocked by safety policy (hardline).",
            )

        # 2) Dangerous: 必须审批
        if self._dangerous and self._dangerous.matches(tool_name):
            return await self._request_approval(ctx, call, next_call, ApprovalTier.DANGEROUS)

        # 3) Sensitive: YOLO 可跳过
        if self._sensitive and self._sensitive.matches(tool_name):
            yolo = ctx.metadata.get("approval_yolo", False)
            if not yolo:
                return await self._request_approval(ctx, call, next_call, ApprovalTier.SENSITIVE)

        # 4) Normal: 直接放行
        return await next_call()

    async def _request_approval(
        self,
        ctx: AgentContext[Any],
        call: ToolCallContext,
        next_call: ToolCallNext,
        tier: ApprovalTier,
    ) -> ToolResult:
        correlation_id = uuid.uuid4().hex
        scope = ControlScope(session_id=ctx.session_id)

        if self._event_bus:
            await self._event_bus.emit(ControlEvent(
                event_id=uuid.uuid4().hex,
                type=ControlEventType.TOOL_APPROVAL_REQUESTED,
                scope=scope,
                correlation_id=correlation_id,
                payload={
                    "tool_name": call.tool_name,
                    "tier": tier.value,
                    "args": _redact_args(call.arguments),
                },
            ))

        try:
            response = await asyncio.wait_for(
                self._wait_response(scope, correlation_id),
                timeout=self._approval_timeout,
            )
        except TimeoutError:
            return self._handle_timeout(ctx, call)

        if response.get("action") == "allow":
            return await next_call()

        return self._handle_denied(ctx, call, tier)

    async def _wait_response(
        self, scope: ControlScope, correlation_id: str,
    ) -> dict[str, object]:
        deadline = time.monotonic() + self._approval_timeout
        pending: list[ControlCommand] = []

        while time.monotonic() < deadline:
            for i, cmd in enumerate(pending):
                if cmd.correlation_id == correlation_id:
                    pending.pop(i)
                    return dict(cmd.payload)

            cmds = await self._channel.drain(
                scope, limit=1,
                command_types={ControlCommandType.APPROVAL_RESPONSE},
            )
            if cmds:
                cmd = cmds[0]
                if cmd.correlation_id == correlation_id:
                    return dict(cmd.payload)
                pending.append(cmd)
            else:
                await asyncio.sleep(0.3)

        raise TimeoutError()

    def _handle_denied(
        self, ctx: AgentContext[Any], call: ToolCallContext, tier: ApprovalTier,
    ) -> ToolResult:
        call_id = call.tool_call.call_id or ""

        if self._on_denied == DenyAction.CANCEL_TURN:
            return ToolResult(
                tool_name=call.tool_name, call_id=call_id,
                error=(
                    f"Tool '{call.tool_name}' was not approved by the user. "
                    f"The tool was not executed. You should inform the user "
                    f"that this operation was denied and suggest alternatives."
                ),
            )

        # deny_as_tool_error: 返回伪错误，agent 继续处理后续 tool
        return ToolResult(
            tool_name=call.tool_name, call_id=call_id,
            error=f"Tool '{call.tool_name}' was not approved by the user. "
                  f"The tool was not executed.",
        )

    def _handle_timeout(
        self, ctx: AgentContext[Any], call: ToolCallContext,
    ) -> ToolResult:
        call_id = call.tool_call.call_id or ""
        if self._on_timeout == TimeoutAction.CANCEL_TURN:
            return ToolResult(
                tool_name=call.tool_name, call_id=call_id,
                error="Error: Tool approval timed out (cancel_turn).",
            )
        return ToolResult(
            tool_name=call.tool_name, call_id=call_id,
            error="Error: Tool approval timed out. The tool was not run.",
        )


# ── Shared helpers ──


async def _drain_approval(
    channel: ControlChannel,
    scope: ControlScope,
    correlation_id: str,
) -> list[ControlCommand]:
    cmds = await channel.drain(scope, limit=1)
    return [c for c in cmds if c.correlation_id == correlation_id]


def _handle_denied(
    call: ToolCallContext,
    on_denied: ApprovalDeniedAction,
) -> ToolResult:
    from framework.control.exceptions import ApprovalDenied

    if on_denied == ApprovalDeniedAction.CANCEL_TURN:
        raise ApprovalDenied(f"Tool '{call.tool_name}' was not approved")
    call_id = call.tool_call.call_id or "" if call.tool_call else ""
    return ToolResult(
        tool_name=call.tool_name, call_id=call_id, result=None,
        error="Error: Tool execution was not approved. The tool was not run.",
    )


def _handle_timeout(
    call: ToolCallContext,
    on_timeout: ApprovalTimeoutAction,
) -> ToolResult:
    from framework.control.exceptions import AgentCancelled

    if on_timeout == ApprovalTimeoutAction.CANCEL_TURN:
        raise AgentCancelled(f"Tool approval timed out for '{call.tool_name}'")
    call_id = call.tool_call.call_id or "" if call.tool_call else ""
    return ToolResult(
        tool_name=call.tool_name, call_id=call_id, result=None,
        error="Error: Tool approval timed out. The tool was not run.",
    )


def _redact_args(arguments: Mapping[str, object]) -> dict[str, object]:
    sensitive_keys = {
        "api_key", "secret", "token", "password", "credential",
        "access_key", "private_key",
    }
    return {
        k: ("***" if k.lower() in sensitive_keys else v)
        for k, v in arguments.items()
    }
