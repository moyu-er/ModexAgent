"""Subprocess-based shell execution tool.

Provides stateless shell command execution via asyncio subprocess.
Each command runs in a fresh process -- no terminal state is preserved.
Used by subagents for simple command execution.
"""

from __future__ import annotations

import asyncio
import os
import platform
from abc import ABC, abstractmethod
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.terminal.types import (
    ShellFamily,
    ShellInfo,
    _parse_platform,
    detect_platform_shell,
)


class ShellExecutor(ABC):
    """Abstract strategy for executing shell commands."""

    @abstractmethod
    async def execute(self, command: str, working_dir: str | None = None, timeout: int = 300) -> str:
        """Execute a shell command and return its output."""

    @abstractmethod
    def shell_info(self) -> ShellInfo:
        """Return information about the shell for dynamic description generation."""


class SubprocessExecutor(ShellExecutor):
    """Stateless executor: each command runs in a fresh subprocess.

    This is the fallback when TerminalManager is unavailable.
    """

    def __init__(self, shell_info: ShellInfo | None = None) -> None:
        detected = detect_platform_shell()
        self._shell_info = (
            shell_info
            or detected
            or ShellInfo(
                family=ShellFamily.CMD,
                path="cmd.exe",
                platform=_parse_platform(platform.system().lower()),
            )
        )

    async def execute(self, command: str, working_dir: str | None = None, timeout: int = 300) -> str:
        from framework.tools.terminal.env import build_full_env

        cwd = working_dir or os.getcwd()
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=build_full_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            return f"Error: Command timed out after {timeout} seconds"

        output_parts: list[str] = []
        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace")
            if stderr_text.strip():
                output_parts.append(f"STDERR:\n{stderr_text}")
        if process.returncode != 0:
            output_parts.append(f"\nExit code: {process.returncode}")

        result = "\n".join(output_parts) if output_parts else "(no output)"
        return result

    def shell_info(self) -> ShellInfo:
        return self._shell_info


class SubprocessTool(Tool):
    """Execute shell commands in a fresh subprocess (stateless).

    For subagent use -- each command runs in a new process, no terminal state.

    Safety checks (dangerous command blocking) are NOT in the tool layer.
    They are handled at the ToolNode level via the approval system.
    See ``framework/agents/react/nodes/tool.py`` and the "Approval & Security"
    section in ``framework/AGENTS.md`` for the full architecture.
    """

    def __init__(
        self,
        executor: ShellExecutor | None = None,
        timeout: int = 300,
    ) -> None:
        """Initialize SubprocessTool.

        Args:
            executor: Shell execution strategy (defaults to SubprocessExecutor).
            timeout: Command timeout in seconds.
        """
        super().__init__()
        self._executor = executor or SubprocessExecutor()
        self.timeout = timeout
        # Ensure ToolManager's outer asyncio.wait_for never preempts our own
        # timeout handling (which returns partial output + timeout marker).
        self.config.timeout = timeout + 10

    @property
    def name(self) -> str:
        return "bash"

    _FAMILY_DESCRIPTIONS: dict[ShellFamily, str] = {
        ShellFamily.BASH: (
            "Commands run in bash. Use POSIX syntax: forward slashes for paths, "
            "single quotes for strings, && for chaining."
        ),
        ShellFamily.CMD: (
            "Commands run in Windows CMD. Use CMD syntax: backslashes for paths, "
            "&& for chaining, %VAR% for environment variables."
        ),
        ShellFamily.ZSH: ("Commands run in zsh. Compatible with bash syntax."),
        ShellFamily.SH: ("Commands run in sh. Use basic POSIX syntax."),
    }

    @property
    def description(self) -> str:
        """Dynamically generate description based on actual shell type."""
        shell_info = self._executor.shell_info()
        parts = [f"Execute a shell command using {shell_info.name} and return its output."]

        family_desc = self._FAMILY_DESCRIPTIONS.get(shell_info.family)
        if family_desc:
            parts.append(family_desc)

        parts.append(
            "Each invocation runs independently in a fresh process. "
            "Working directory, environment variables, and background "
            "processes do NOT persist between calls."
        )

        return " ".join(parts)

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
            },
            "required": ["command"],
        }

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: object) -> str:
        return await self._executor.execute(command, working_dir, timeout=self.timeout)
