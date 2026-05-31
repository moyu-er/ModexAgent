"""RunLoggingHook — 详细 per-session 日志记录。

记录 LLM 响应和工具执行的详细信息。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.types import LLMResponse

logger = logging.getLogger(__name__)


class RunLoggingHook:
    """详细 per-session 日志：LLM 响应和工具执行。"""

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
        self._pending_tool_calls: dict[str, list[Any]] = {}

    @staticmethod
    def _get_agent_name(ctx: AgentContext[Any]) -> str:
        if ctx.session_meta is not None:
            return ctx.session_meta.agent_name
        if ctx.identity is not None:
            return ctx.identity.agent_id
        return "<unknown>"

    @staticmethod
    def _get_iteration(ctx: AgentContext[Any]) -> int:
        if ctx.runtime is not None:
            return getattr(ctx.runtime.state, "iteration", 0)
        return 0

    async def after_llm_response(self, ctx: AgentContext[Any], response: LLMResponse) -> None:
        tool_names = [call.tool_name for call in response.tool_calls]
        agent = self._get_agent_name(ctx)
        iteration = self._get_iteration(ctx)
        self._logger.log(
            self._level,
            "[LLM] session_id=%s agent=%s iter=%s finish_reason=%s "
            "tools=%s usage=%s\ncontent=%s",
            ctx.session_id,
            agent,
            iteration,
            response.finish_reason,
            self._format_value(tool_names, self._max_content_chars),
            self._format_value(response.usage, self._max_content_chars),
            self._format_text(response.content, self._max_content_chars),
        )

    async def before_tool_execution(
        self,
        ctx: AgentContext[Any],
        tool_calls: list[Any] | None = None,
    ) -> None:
        if tool_calls is None:
            return
        self._pending_tool_calls[ctx.session_id] = list(tool_calls)
        agent = self._get_agent_name(ctx)
        iteration = self._get_iteration(ctx)
        for tool_call in tool_calls:
            tool_name = getattr(tool_call, "tool_name", "<unknown>")
            call_id = getattr(tool_call, "call_id", None)
            arguments = getattr(tool_call, "arguments", {}) or {}
            self._logger.log(
                self._level,
                "[TOOL_CALL] session_id=%s agent=%s iter=%s tool=%s call_id=%s\narguments=%s",
                ctx.session_id,
                agent,
                iteration,
                tool_name,
                call_id,
                self._format_value(arguments, self._max_content_chars),
            )

    async def after_tool_execution(
        self,
        ctx: AgentContext[Any],
        results: list[Any] | None = None,
    ) -> None:
        if results is None:
            return
        pending = self._pending_tool_calls.get(ctx.session_id, [])
        pending_by_call_id = {getattr(call, "call_id", None): call for call in pending}
        pending_by_name = {getattr(call, "tool_name", None): call for call in pending}
        agent = self._get_agent_name(ctx)
        iteration = self._get_iteration(ctx)

        for result in results:
            tool_name = self._result_tool_name(result)
            call_id = self._result_call_id(result)
            tool_call = pending_by_call_id.get(call_id) or pending_by_name.get(tool_name)
            arguments = getattr(tool_call, "arguments", {}) if tool_call is not None else {}
            error = self._result_error(result)
            output = self._result_output(result)
            self._logger.log(
                self._level,
                "[TOOL_RESULT] session_id=%s agent=%s iter=%s tool=%s call_id=%s success=%s\nresult=%s",
                ctx.session_id,
                agent,
                iteration,
                tool_name,
                call_id,
                error is None,
                self._format_value(
                    output if error is None else {"error": error},
                    self._max_result_chars,
                ),
            )

        self._pending_tool_calls.pop(ctx.session_id, None)

    @staticmethod
    def _result_tool_name(result: Any) -> str:
        if isinstance(result, dict):
            return str(result.get("name") or result.get("tool_name") or "<unknown>")
        return str(getattr(result, "tool_name", "<unknown>"))

    @staticmethod
    def _result_call_id(result: Any) -> str | None:
        if isinstance(result, dict):
            value = result.get("tool_call_id") or result.get("call_id")
            return str(value) if value is not None else None
        value = getattr(result, "call_id", None)
        return str(value) if value is not None else None

    @staticmethod
    def _result_error(result: Any) -> str | None:
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, str) and content.startswith("Error: "):
                return content
            return None
        error = getattr(result, "error", None)
        return str(error) if error is not None else None

    @staticmethod
    def _result_output(result: Any) -> Any:
        if isinstance(result, dict):
            return result.get("content")
        return getattr(result, "result", None)

    @classmethod
    def _format_value(cls, value: Any, max_chars: int) -> str:
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
