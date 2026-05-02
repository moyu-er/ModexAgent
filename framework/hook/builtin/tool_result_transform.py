"""ToolResultTransformHook — 工具结果脱敏/格式化。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from framework.core.agent import AgentContext

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ToolResultTransformHook:
    """在 after_tool_execution 中对 tool 结果进行脱敏或格式化。

    per-session 模式：无实例可变状态，所有数据走 ctx.metadata。
    """

    def __init__(
        self,
        max_result_chars: int = 20000,
        sanitize_credentials: bool = True,
    ) -> None:
        self._max_result_chars = max_result_chars
        self._sanitize_credentials = sanitize_credentials

    async def after_tool_execution(
        self,
        ctx: AgentContext[Any],
        results: list[Any],
    ) -> None:
        if not results:
            return

        transformed_count = 0
        for r in results:
            # 检查 result 属性是否存在
            raw = getattr(r, "result", None)
            if raw is not None and isinstance(raw, str):
                if self._sanitize_credentials:
                    import re
                    new_raw, n = re.subn(
                        r'(?:api[_-]?key|apikey|secret|token|password)\s*[:=]\s*[\S]+',
                        '[REDACTED]', raw, flags=re.IGNORECASE,
                    )
                    if n > 0:
                        r.result = new_raw
                        transformed_count += 1

                # 截断过长结果
                if len(r.result) > self._max_result_chars:
                    original_len = len(r.result)
                    r.result = r.result[:self._max_result_chars] + (
                        f"\n... (truncated, {original_len} chars total)"
                    )
                    transformed_count += 1

        if transformed_count:
            logger.debug(
                "ToolResultTransform: %d results transformed session=%s",
                transformed_count, ctx.session_id,
            )
