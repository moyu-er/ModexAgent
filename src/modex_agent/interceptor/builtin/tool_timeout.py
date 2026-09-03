"""ToolTimeoutInterceptor — framework-mandatory per-invocation tool deadline.

Wraps ``next_call()`` in ``asyncio.timeout()``. On expiry the tool coroutine
is cancelled and a structured ``<tool_timeout>`` XML ``ToolResult`` is returned
instead of raising an exception — the ReAct loop continues with the timeout
result as a normal tool failure.

External cancellation (``/stop``, ``task.cancel()``) propagates naturally:
``asyncio.timeout()`` only converts its own deadline expiry to
``TimeoutError``; external ``CancelledError`` is re-raised.

Watchdog protocol: at entry the interceptor declares the tool's full budget
into the dispatch deadline (``tool_timeout + phase_margin``) so the outer
pool watchdog never fires first — the inner graceful path (XML result) is
always reachable. The margin (2 × watchdog poll interval) covers poll-granularity
wake latency; no-ops when no deadline is set (clean mode, dispatch_timeout=0).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from modex_agent.core.llm_struct import DEFAULT_TOOL_TIMEOUT_SECONDS
from modex_agent.core.message import ContentFormat, TextPart
from modex_agent.core.tool_manager import ToolResult
from modex_agent.interceptor.abc import (
    ToolCallContext,
    ToolCallInterceptor,
    ToolCallNext,
)
from modex_agent.runtime.dispatch import renew_dispatch_deadline
from modex_agent.utils.xml import xml_text

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext

logger = logging.getLogger(__name__)

# Fallback margin when no safety policy is reachable (clean mode): matches
# DeadlinePolicy default (2 × default 5s watchdog poll).
_NO_POLICY_MARGIN_SECONDS: float = 10.0


class ToolTimeoutInterceptor(ToolCallInterceptor):
    """Enforce a per-invocation tool execution deadline.

    Composed by ``ToolExecutor`` as the innermost interceptor so that the
    deadline measures only ``ToolManager.execute()`` time, excluding other
    interceptors' pre/post-processing.
    """

    @property
    def name(self) -> str:
        return "tool_timeout"

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        timeout = self._resolve_timeout(ctx)
        renew_dispatch_deadline(timeout + self._resolve_margin(ctx))
        started_at = time.monotonic()

        try:
            async with asyncio.timeout(timeout):
                return await next_call()  # type: ignore[misc]
        except TimeoutError:
            elapsed = time.monotonic() - started_at
            logger.warning(
                "Tool %s timed out after %.1fs (elapsed=%.2fs)",
                call.tool_name,
                timeout,
                elapsed,
            )
            return _build_tool_timeout_result(
                tool_name=call.tool_name,
                call_id=call.tool_call.call_id if call.tool_call else None,
                timeout_seconds=timeout,
                elapsed_seconds=elapsed,
            )

    @staticmethod
    def _resolve_timeout(ctx: AgentContext) -> float:
        safety = ctx.runtime.safety if ctx.runtime else None
        if safety is not None:
            return safety.turn.tool_timeout_seconds
        return DEFAULT_TOOL_TIMEOUT_SECONDS

    @staticmethod
    def _resolve_margin(ctx: AgentContext) -> float:
        safety = ctx.runtime.safety if ctx.runtime else None
        if safety is not None:
            return safety.deadline.phase_margin_seconds
        return _NO_POLICY_MARGIN_SECONDS


def _build_tool_timeout_result(
    *,
    tool_name: str,
    call_id: str | None,
    timeout_seconds: float,
    elapsed_seconds: float,
) -> ToolResult:
    xml = _build_tool_timeout_xml(
        tool_name=tool_name,
        timeout_seconds=timeout_seconds,
        elapsed_seconds=elapsed_seconds,
    )
    return ToolResult(
        tool_name=tool_name,
        content=[TextPart(text=xml)],
        error=f"Tool execution timed out after {timeout_seconds:.0f} seconds",
        execution_time=elapsed_seconds,
        call_id=call_id,
        content_format=ContentFormat.XML,
        truncatable_paths=[],
    )


def _build_tool_timeout_xml(
    *,
    tool_name: str,
    timeout_seconds: float,
    elapsed_seconds: float,
) -> str:
    message = (
        "This tool invocation exceeded its execution deadline and was cancelled. "
        "Side effects may be partial. Inspect the current state before retrying."
    )
    return "\n".join(
        [
            "<tool_timeout>",
            f"  <tool_name>{xml_text(tool_name)}</tool_name>",
            "  <status>timed_out</status>",
            f"  <timeout_seconds>{timeout_seconds:.0f}</timeout_seconds>",
            f"  <elapsed_seconds>{elapsed_seconds:.2f}</elapsed_seconds>",
            f"  <message>{xml_text(message)}</message>",
            "</tool_timeout>",
        ]
    )
