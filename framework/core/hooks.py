"""Agent 运行 Hook 基类与组合。

这些类属于通用 Agent 执行基础设施，不依赖 multi_agent 包。
被 core.agent.AgentContext 引用作为 hooks 字段的类型。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import AgentContext
    from .emitter import AgentResult
    from .types import LLMResponse

logger = logging.getLogger(__name__)


class AgentRunHook:
    """Agent 运行 Hook 基类。

    提供默认空实现，子类可选择性覆盖。
    """

    async def before_turn(self, ctx: AgentContext) -> None:
        """在 Agent.run() 开始时、while 循环之前调用，且只调用一次。"""
        pass

    async def before_iteration(self, ctx: AgentContext) -> None:
        """每次迭代开始前调用。"""
        pass

    async def before_tool_execution(self, ctx: AgentContext, tool_calls: list[Any]) -> None:
        """工具执行前调用。"""
        pass

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        """Called after a full LLM response is available."""
        pass

    async def after_tool_execution(self, ctx: AgentContext, results: list[Any]) -> None:
        """工具执行后调用。"""
        pass

    async def after_iteration(self, ctx: AgentContext) -> None:
        """每次迭代结束后调用。"""
        pass

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        """在 Agent.run() 结束后调用（无论成功、失败或达到最大迭代次数），且只调用一次。"""
        pass

    def finalize_content(self, ctx: AgentContext, content: str | None) -> str | None:
        """最终内容调整。"""
        return content


class RuntimeContextHook(AgentRunHook):
    """Manage per-turn RuntimeContext lifecycle via the hook interface.

    - ``before_turn`` resolves and caches the session's :class:`RuntimeContext`
      on :attr:`AgentContext.runtime_context`, then clears it for the new turn.
    - ``before_tool_execution`` stashes the raw *tool_calls* so that
      ``after_tool_execution`` can match them to their results and record a
      complete :class:`ToolCallRecord`.

    This hook is **automatically injected** by :class:`AgentPipeline` and
    :class:`AgentSession` when a ``runtime_context_manager`` is provided.
    """

    _PENDING_KEY = "_pending_tool_calls"

    async def before_turn(self, ctx: AgentContext) -> None:
        if ctx.runtime_context is None and ctx.runtime_context_manager is not None:
            ctx.runtime_context = await ctx.runtime_context_manager.get_context(
                ctx.session_id, ctx.metadata
            )
        if ctx.runtime_context is not None:
            await ctx.runtime_context.clear()

    async def before_tool_execution(self, ctx: AgentContext, tool_calls: list[Any]) -> None:
        if ctx.runtime_context is not None:
            await ctx.runtime_context.set(self._PENDING_KEY, list(tool_calls))

    async def after_tool_execution(self, ctx: AgentContext, results: list[Any]) -> None:
        if ctx.runtime_context is None:
            return
        pending = await ctx.runtime_context.get(self._PENDING_KEY, [])
        if not pending:
            return

        # Build a lookup from tool_call_id → result content
        result_map: dict[str | None, str] = {
            msg.get("tool_call_id"): msg.get("content", "")
            for msg in results
            if isinstance(msg, dict) and msg.get("role") == "tool"
        }

        for tool_call in pending:
            call_id = getattr(tool_call, "call_id", None)
            tool_name = getattr(tool_call, "tool_name", None)
            arguments = getattr(tool_call, "arguments", None) or {}
            if tool_name:
                await ctx.runtime_context.record_tool_call(
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    result=result_map.get(call_id, ""),
                )

        await ctx.runtime_context.set(self._PENDING_KEY, [])


class CompositeRunHook(AgentRunHook):
    """组合 Agent Run Hook。"""

    def __init__(self, hooks: list[AgentRunHook] | None = None):
        self._hooks = list(hooks or [])

    async def before_turn(self, ctx: AgentContext) -> None:
        for h in self._hooks:
            try:
                await h.before_turn(ctx)
            except Exception:
                logger.debug("Hook %s.before_turn failed", type(h).__name__, exc_info=True)

    async def before_iteration(self, ctx: AgentContext) -> None:
        for h in self._hooks:
            try:
                await h.before_iteration(ctx)
            except Exception:
                logger.debug("Hook %s.before_iteration failed", type(h).__name__, exc_info=True)

    async def before_tool_execution(self, ctx: AgentContext, tool_calls: list[Any]) -> None:
        for h in self._hooks:
            try:
                await h.before_tool_execution(ctx, tool_calls)
            except Exception:
                logger.debug("Hook %s.before_tool_execution failed", type(h).__name__, exc_info=True)

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        for h in self._hooks:
            try:
                await h.after_llm_response(ctx, response)
            except Exception:
                logger.debug("Hook %s.after_llm_response failed", type(h).__name__, exc_info=True)

    async def after_tool_execution(self, ctx: AgentContext, results: list[Any]) -> None:
        for h in self._hooks:
            try:
                await h.after_tool_execution(ctx, results)
            except Exception:
                logger.debug("Hook %s.after_tool_execution failed", type(h).__name__, exc_info=True)

    async def after_iteration(self, ctx: AgentContext) -> None:
        for h in self._hooks:
            try:
                await h.after_iteration(ctx)
            except Exception:
                logger.debug("Hook %s.after_iteration failed", type(h).__name__, exc_info=True)

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        for h in self._hooks:
            try:
                await h.after_turn(ctx, result)
            except Exception:
                logger.debug("Hook %s.after_turn failed", type(h).__name__, exc_info=True)

    def finalize_content(self, ctx: AgentContext, content: str | None) -> str | None:
        for h in self._hooks:
            try:
                content = h.finalize_content(ctx, content)
            except Exception:
                logger.debug("Hook %s.finalize_content failed", type(h).__name__, exc_info=True)
        return content


class RunLoggingHook(AgentRunHook):
    """Detailed per-session logging for LLM responses and tool execution."""

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

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        tool_names = [call.tool_name for call in response.tool_calls]
        self._logger.log(
            self._level,
            "LLM response session_id=%s finish_reason=%s tool_calls=%s usage=%s "
            "reasoning=%s content=%s",
            ctx.session_id,
            response.finish_reason,
            self._format_value(tool_names, self._max_content_chars),
            self._format_value(response.usage, self._max_content_chars),
            self._format_text(response.reasoning_content, self._max_content_chars),
            self._format_text(response.content, self._max_content_chars),
        )

    async def before_tool_execution(self, ctx: AgentContext, tool_calls: list[Any]) -> None:
        self._pending_tool_calls[ctx.session_id] = list(tool_calls)
        for tool_call in tool_calls:
            tool_name = getattr(tool_call, "tool_name", "<unknown>")
            call_id = getattr(tool_call, "call_id", None)
            arguments = getattr(tool_call, "arguments", {}) or {}
            self._logger.log(
                self._level,
                "Tool call start session_id=%s tool=%s call_id=%s arguments=%s",
                ctx.session_id,
                tool_name,
                call_id,
                self._format_value(arguments, self._max_content_chars),
            )

    async def after_tool_execution(self, ctx: AgentContext, results: list[Any]) -> None:
        pending = self._pending_tool_calls.get(ctx.session_id, [])
        pending_by_call_id = {getattr(call, "call_id", None): call for call in pending}
        pending_by_name = {getattr(call, "tool_name", None): call for call in pending}

        for result in results:
            tool_name = self._result_tool_name(result)
            call_id = self._result_call_id(result)
            tool_call = pending_by_call_id.get(call_id) or pending_by_name.get(tool_name)
            arguments = getattr(tool_call, "arguments", {}) if tool_call is not None else {}
            error = self._result_error(result)
            output = self._result_output(result)
            self._logger.log(
                self._level,
                "Tool call end session_id=%s tool=%s call_id=%s success=%s arguments=%s result=%s",
                ctx.session_id,
                tool_name,
                call_id,
                error is None,
                self._format_value(arguments or {}, self._max_content_chars),
                self._format_value(output if error is None else {"error": error}, self._max_result_chars),
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
