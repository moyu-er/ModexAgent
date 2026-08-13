"""Tests for SubprocessTool timeout invariants.

Verifies the timeout layering invariant: the outer ToolTimeoutInterceptor
deadline (``TurnTimeoutPolicy.tool_timeout_seconds``, default 400s) must
exceed SubprocessTool's own timeout (default 90s) so that SubprocessTool's
internal timeout fires first — returning partial output — rather than
being cancelled by the interceptor (which would lose all output).

The invariant lives across two layers:
- Inner: ``SubprocessTool.timeout`` — asyncio.wait_for inside execute()
- Outer: ``ToolTimeoutInterceptor`` — ``asyncio.timeout(safety.turn.tool_timeout_seconds)``
"""

from __future__ import annotations

from modex_agent.core.constants import DefaultValues
from modex_agent.core.llm_struct import RuntimeSafetyPolicy, TurnTimeoutPolicy
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool, create_subprocess_executor


class TestSubprocessToolTimeoutDefaults:
    def test_default_timeout_is_90(self) -> None:
        """Default timeout is 90s — short enough for interactive use,
        leaving ample margin under the 400s interceptor deadline."""
        tool = SubprocessTool(executor=create_subprocess_executor())
        assert tool.timeout == 90

    def test_explicit_timeout_respected(self) -> None:
        """Explicit timeout override is honoured."""
        tool = SubprocessTool(executor=create_subprocess_executor(), timeout=60)
        assert tool.timeout == 60


class TestTimeoutInvariant:
    """The outer interceptor deadline must exceed the inner tool timeout."""

    def test_interceptor_exceeds_subprocess_default(self) -> None:
        """``TurnTimeoutPolicy.tool_timeout_seconds`` (400s) >
        ``SubprocessTool.timeout`` (90s) so the tool's own timeout fires
        first and returns partial output."""
        safety = RuntimeSafetyPolicy()
        shell = SubprocessTool(executor=create_subprocess_executor())
        assert safety.turn.tool_timeout_seconds > shell.timeout, (
            f"TurnTimeoutPolicy.tool_timeout_seconds ({safety.turn.tool_timeout_seconds}s) "
            f"must be greater than SubprocessTool.timeout ({shell.timeout}s) or the "
            f"interceptor cancels the tool before it can return partial output."
        )

    def test_interceptor_margin(self) -> None:
        """At least 30s margin between interceptor and tool timeout to
        account for scheduling jitter."""
        safety = RuntimeSafetyPolicy()
        shell = SubprocessTool(executor=create_subprocess_executor())
        margin = safety.turn.tool_timeout_seconds - shell.timeout
        assert margin >= 30, (
            f"Expected at least 30s margin, got {margin}s — "
            f"interceptor ({safety.turn.tool_timeout_seconds}s) too close to "
            f"tool ({shell.timeout}s)."
        )

    def test_default_tool_timeout_matches_constants(self) -> None:
        """``TurnTimeoutPolicy.tool_timeout_seconds`` default equals
        ``DefaultValues.TOOL_TIMEOUT_SECONDS``."""
        policy = TurnTimeoutPolicy()
        assert policy.tool_timeout_seconds == DefaultValues.TOOL_TIMEOUT_SECONDS
