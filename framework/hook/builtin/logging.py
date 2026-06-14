"""RunLoggingHook — 详细 per-session 日志记录。

记录 LLM 响应和工具执行的详细信息。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from framework.core.types import LLMResponse

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.tool_manager import ToolResult
    from framework.core.types import ToolCall

from framework.hook.abc import (
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    BeforeToolExecutionHook,
)

logger = logging.getLogger(__name__)


class RunLoggingHook(AfterLLMResponseHook, BeforeToolExecutionHook, AfterToolExecutionHook):
    """详细 per-session 日志：LLM 响应和工具执行。"""

    @property
    def name(self) -> str:
        return "run_logging_hook"

    def __init__(
        self,
        logger_name: str = "framework.agent.run",
        level: int = logging.INFO,
        max_content_chars: int = 4000,
        max_result_chars: int = 4000,
    ) -> None:
        self._logger = logging.getLogger(logger_name)
        self._level = level
        self._max_content_chars = max_content_chars
        self._max_result_chars = max_result_chars

    @staticmethod
    def _get_agent_name(ctx: AgentContext) -> str:
        return ctx.session.agent_name if ctx.session else "<unknown>"

    @staticmethod
    def _get_iteration(ctx: AgentContext) -> int:
        if ctx.runtime is not None:
            return getattr(ctx.runtime.state, "iteration", 0)
        return 0

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        tool_names = [call.tool_name for call in response.tool_calls]
        agent = self._get_agent_name(ctx)
        iteration = self._get_iteration(ctx)
        self._logger.log(
            self._level,
            "[LLM] session_id=%s agent=%s iter=%s finish_reason=%s tools=%s usage=%s\ncontent=%s",
            str(ctx.session),
            agent,
            iteration,
            response.finish_reason,
            self._format_value(tool_names, self._max_content_chars),
            self._format_value(response.usage, self._max_content_chars),
            self._format_text(response.content, self._max_content_chars),
        )

    async def before_tool_execution(
        self,
        ctx: AgentContext,
        tool_calls: Sequence[ToolCall] | None = None,
    ) -> None:
        if tool_calls is None:
            return
        agent = self._get_agent_name(ctx)
        iteration = self._get_iteration(ctx)
        for tool_call in tool_calls:
            self._logger.log(
                self._level,
                "[TOOL_CALL] session_id=%s agent=%s iter=%s tool=%s call_id=%s\narguments=%s",
                str(ctx.session),
                agent,
                iteration,
                tool_call.tool_name,
                tool_call.call_id,
                self._format_value(tool_call.arguments or {}, self._max_content_chars),
            )

    async def after_tool_execution(
        self,
        ctx: AgentContext,
        results: Sequence[ToolResult] | None = None,
    ) -> None:
        if results is None:
            return
        agent = self._get_agent_name(ctx)
        iteration = self._get_iteration(ctx)

        for result in results:
            error = result.error
            self._logger.log(
                self._level,
                "[TOOL_RESULT] session_id=%s agent=%s iter=%s tool=%s call_id=%s success=%s\nresult=%s",
                str(ctx.session),
                agent,
                iteration,
                result.tool_name,
                result.call_id,
                error is None,
                self._format_value(
                    result.result if error is None else {"error": error},
                    self._max_result_chars,
                ),
            )

    @classmethod
    def _format_value(cls, value: Any, max_chars: int) -> str:  # noqa: ANN401
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            text = str(value)
        return cls._format_text(text, max_chars) or ""

    @classmethod
    def _format_text(cls, value: str | None, max_chars: int) -> str | None:
        if value is None:
            return None
        return cls._truncate(cls._collapse_whitespace(value), max_chars)

    @staticmethod
    def _collapse_whitespace(value: str) -> str:
        value = value.replace("\\r", " ").replace("\\n", " ").replace("\\t", " ")
        return " ".join(value.split())

    @staticmethod
    def _truncate(value: str | None, max_chars: int) -> str | None:
        if value is None:
            return None
        if len(value) <= max_chars:
            return value
        return f"{value[:max_chars]}... (truncated, {len(value)} chars total)"
