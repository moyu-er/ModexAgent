"""Agent 运行 Hook 基类与组合。

这些类属于通用 Agent 执行基础设施，不依赖 multi_agent 包。
被 core.agent.AgentContext 引用作为 hooks 字段的类型。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import AgentContext
    from .emitter import AgentResult

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
