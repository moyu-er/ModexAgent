"""Tests for ShellTool."""

import pytest

from framework.core.tool_manager import ToolConfig
from framework.ioc.configs.safety import SafetyConfig
from framework.tools.standard.shell_tool import ShellTool, SubprocessExecutor


class TestShellToolConfig:
    def test_config_timeout_matches_tool_timeout(self) -> None:
        """ShellTool.config.timeout must be >= self.timeout so that
        InMemoryToolManager's outer asyncio.wait_for never preempts
        ShellTool's own timeout handling (which returns partial output).
        """
        tool = ShellTool(executor=SubprocessExecutor(), timeout=60)
        assert tool.config.timeout >= tool.timeout, (
            f"ToolManager timeout ({tool.config.timeout}s) must not be less than "
            f"ShellTool timeout ({tool.timeout}s) or partial output on timeout is lost"
        )

    def test_config_timeout_has_margin(self) -> None:
        """There should be a safety margin between the two timeouts."""
        tool = ShellTool(executor=SubprocessExecutor(), timeout=60)
        assert tool.config.timeout >= tool.timeout + 10, (
            f"Expected at least 10s margin, got {tool.config.timeout - tool.timeout}s"
        )

    def test_default_timeout_values(self) -> None:
        """Default timeout should be 60s with ToolManager margin applied."""
        tool = ShellTool(executor=SubprocessExecutor())
        assert tool.timeout == 60
        assert tool.config.timeout >= 70


class TestSafetyTimeoutNotTruncatesShell:
    def test_safety_tool_timeout_exceeds_shell_timeout(self) -> None:
        """SafetyConfig.turn.tool_timeout must exceed ShellTool.timeout so that
        outer ReActAgent/Interceptor timeout never preempts ShellTool's own
        timeout handling.  When outer fires first, tool coroutine is cancelled
        and partial output is lost — tool has no chance to return anything.
        """
        safety = SafetyConfig()
        shell = ShellTool(executor=SubprocessExecutor(), timeout=60)
        assert safety.turn.tool_timeout > shell.timeout, (
            f"Safety turn.tool_timeout ({safety.turn.tool_timeout}s) must be "
            f"greater than ShellTool.timeout ({shell.timeout}s) or outer "
            f"interceptor cancels the tool coroutine before it can return "
            f"partial output."
        )

    def test_safety_tool_timeout_has_margin(self) -> None:
        """There should be a comfortable margin between safety timeout and
        shell timeout to account for scheduling jitter.
        """
        safety = SafetyConfig()
        shell = ShellTool(executor=SubprocessExecutor(), timeout=60)
        assert safety.turn.tool_timeout >= shell.timeout + 30, (
            f"Expected at least 30s margin, got "
            f"{safety.turn.tool_timeout - shell.timeout}s"
        )
