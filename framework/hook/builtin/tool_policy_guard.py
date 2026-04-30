"""ToolPolicyGuardHook — 策略静默否决不合规 tool_call。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from framework.core.agent import AgentContext

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ToolPolicyGuardHook:
    """在 before_tool_execution 中静默过滤不合规的 tool_call。

    通过 ctx.metadata["_denied_tool_calls"] 标记被否决的 tool，
    供后续拦截器层处理（如 TieredToolApprovalInterceptor 在 TOOL_CALL scope 中检测）。
    """

    def __init__(
        self,
        deny_patterns: dict[str, str] | None = None,
    ) -> None:
        """Args:
            deny_patterns: tool_name → reason 映射，匹配的工具被静默否决。
        """
        self._deny_patterns: dict[str, str] = dict(deny_patterns) if deny_patterns else {}

    async def before_tool_execution(
        self,
        ctx: AgentContext,
        tool_calls: list[Any],
    ) -> None:
        """检查 tool_calls 列表，标记需要否决的。"""
        denied: dict[str, str] = {}
        for tc in tool_calls:
            tool_name = getattr(tc, "tool_name", "")
            if tool_name in self._deny_patterns:
                denied[tool_name] = self._deny_patterns[tool_name]
        if denied:
            ctx.metadata["_policy_denied_tools"] = denied
            logger.info(
                "ToolPolicyGuard: denied tools=%s session=%s",
                list(denied.keys()), ctx.session_id,
            )
