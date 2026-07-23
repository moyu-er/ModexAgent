"""ToolExecutor — run a tool call through the interceptor chain with its own timeout.

Extracted from ReActAgent._execute_tool / _execute_tool_raw / _resolve_tool_timeout
so ToolNode holds a collaborator instead of a back-reference to the agent.
Behaviour identical.
"""
from __future__ import annotations

import asyncio
import logging

from modex_agent.core.agent import AgentContext
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.interceptor.abc import ToolCallContext

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Execute a single ToolCall via the interceptor chain, with an isolated timeout."""

    def __init__(self, default_tool_timeout: float) -> None:
        self._default_tool_timeout = default_tool_timeout

    async def execute(self, tool_call: ToolCall, ctx: AgentContext) -> ToolResult:
        interceptor_chain = ctx.runtime.interceptors if ctx.runtime else None
        if interceptor_chain is not None:
            call_ctx = ToolCallContext(
                tool_call=tool_call,
                tool_name=tool_call.tool_name,
                arguments=tool_call.arguments or {},
                session_id=str(ctx.session),
            )

            async def _actual() -> ToolResult:
                return await self._execute_raw(tool_call, ctx)

            # Canonical AOP path for TOOL_CALL. `ctx.runtime.around` is for
            # ITERATION only — see ADR-0033 D5.
            return await interceptor_chain.around_tool_call(ctx, call_ctx, _actual)

        return await self._execute_raw(tool_call, ctx)

    async def _execute_raw(self, tool_call: ToolCall, ctx: AgentContext) -> ToolResult:
        tool_timeout = self._resolve_tool_timeout(ctx)
        try:
            result = await asyncio.wait_for(
                ctx.tool_manager.execute(tool_call.tool_name, tool_call.arguments or {}),
                timeout=tool_timeout,
            )
            return result
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning("Tool %s timed out after %.1fs", tool_call.tool_name, tool_timeout)
            return ToolResult(
                tool_name=tool_call.tool_name,
                result=None,
                error=f"Error: Tool execution timeout after {tool_timeout:.0f}s",
            )
        except Exception as e:
            logger.warning("Tool %s execution failed: %s", tool_call.tool_name, e)
            return ToolResult(
                tool_name=tool_call.tool_name,
                result=None,
                error=f"Error: {e}",
            )

    def _resolve_tool_timeout(self, ctx: AgentContext) -> float:
        """读 runtime.safety，fallback ctor 默认（原 ReActAgent._resolve_tool_timeout，agent.py:349-353）。"""
        safety = ctx.runtime.safety if ctx.runtime else None
        if safety is not None:
            return safety.turn.tool_timeout_seconds
        return self._default_tool_timeout
