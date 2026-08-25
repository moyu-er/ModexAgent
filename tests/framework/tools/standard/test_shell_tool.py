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
    def test_default_timeout_is_none(self) -> None:
        """Default timeout is None (no tool-level deadline) — the persistent
        bash default contract; callers opt into per-command deadlines."""
        tool = SubprocessTool(executor=create_subprocess_executor())
        assert tool.timeout is None

    def test_explicit_timeout_respected(self) -> None:
        """Explicit timeout override is honoured."""
        tool = SubprocessTool(executor=create_subprocess_executor(), timeout=60)
        assert tool.timeout == 60


class TestTimeoutInvariant:
    """Interceptor deadline vs the persistent-bash timeout ladder.

    Default: ``tool_timeout_seconds`` (540s) > PersistentBashTool default
    (480s), so the shell's own kill-and-reset timeout fires first and
    returns partial output. ``SubprocessTool`` defaults to None (no
    tool-level deadline) and is exempt from the ladder.
    """

    def test_interceptor_exceeds_persistent_bash_default(self) -> None:
        """``TurnTimeoutPolicy.tool_timeout_seconds`` (540s) >
        ``PersistentBashTool`` default (480s) so the shell's own timeout
        fires first and returns partial output."""
        from modex_agent.tools.terminal.persistent_bash import PersistentBashTool

        safety = RuntimeSafetyPolicy()
        safety = RuntimeSafetyPolicy()
        bash = PersistentBashTool()
        bash_timeout = bash.session.timeout_seconds
        assert bash_timeout is not None, "persistent bash default must be a number (480s)"
        assert safety.turn.tool_timeout_seconds > bash_timeout, (
            f"TurnTimeoutPolicy.tool_timeout_seconds ({safety.turn.tool_timeout_seconds}s) "
            f"must be greater than the persistent bash timeout "
            f"({bash_timeout}s) or the interceptor cancels the tool "
            f"before its own kill-and-reset contract can fire."
        )

    def test_default_tool_timeout_matches_constants(self) -> None:
        """``TurnTimeoutPolicy.tool_timeout_seconds`` default equals
        ``DefaultValues.TOOL_TIMEOUT_SECONDS``."""
        policy = TurnTimeoutPolicy()
        assert policy.tool_timeout_seconds == DefaultValues.TOOL_TIMEOUT_SECONDS
