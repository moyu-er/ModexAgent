"""Subprocess-based shell execution tool.

Provides stateless shell command execution via asyncio subprocess.
Each command runs in a fresh process -- no terminal state is preserved.
Used by subagents for simple command execution.

Architecture (family split):

``ShellExecutor`` (ABC) → ``SubprocessExecutor`` (template, shared
env/sanitize/timeout/output logic) → ``PosixSubprocessExecutor`` /
``CmdSubprocessExecutor`` (``_spawn`` hook per shell family).

The split exists because CPython ``shell=True`` on Windows injects
``/c`` (a cmd.exe flag) into ``args`` unconditionally — see
``Lib/subprocess.py:_execute_child`` Windows branch.  POSIX shells
(bash/zsh/sh) need ``-c`` instead, so they use
``create_subprocess_exec(path, "-c", command)``.  cmd.exe/powershell
use ``create_subprocess_shell`` (``shell=True``) so CPython adds the
correct ``/c`` via ``COMSPEC``.  Routing by shell family (not OS)
keeps WSL bash-on-Windows correct too.
"""

from __future__ import annotations

import asyncio
import os
import platform
from abc import ABC, abstractmethod
from typing import Any

from modex_agent.core.tool_manager import Tool
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    _parse_platform,
    detect_platform_shell,
)


class ShellExecutor(ABC):
    """Abstract strategy for executing shell commands."""

    @abstractmethod
    async def execute(
        self, command: str, working_dir: str | None = None, timeout: int = 300
    ) -> str:
        """Execute a shell command and return its output."""

    @abstractmethod
    def shell_info(self) -> ShellInfo:
        """Return information about the shell for dynamic description generation."""


def _default_fallback_shell() -> ShellInfo:
    """Last-resort shell when ``detect_platform_shell()`` returns None.

    Windows defaults to ``cmd.exe``; POSIX (macOS/Linux) defaults to
    ``/bin/sh``.  This prevents the Windows-biased ``cmd.exe`` from
    leaking onto macOS/Linux when detection unexpectedly fails.
    """
    plat = _parse_platform(platform.system().lower())
    if plat is Platform.WINDOWS:
        return ShellInfo(family=ShellFamily.CMD, path="cmd.exe", platform=plat)
    return ShellInfo(family=ShellFamily.SH, path="/bin/sh", platform=plat)


class SubprocessExecutor(ShellExecutor):
    """Template: shared env/NO_COLOR/sanitize/timeout/output logic.

    Subclasses override ``_spawn`` to choose exec-vs-shell per shell
    family.  See module docstring for the CPython ``/c`` rationale.
    """

    def __init__(self, shell_info: ShellInfo | None = None) -> None:
        detected = detect_platform_shell()
        self._shell_info = shell_info or detected or _default_fallback_shell()

    @abstractmethod
    async def _spawn(
        self, command: str, cwd: str, env: dict[str, str]
    ) -> asyncio.subprocess.Process:
        """Create the subprocess — family-specific exec-vs-shell choice."""

    async def execute(
        self, command: str, working_dir: str | None = None, timeout: int | None = None
    ) -> str:
        from modex_agent.runtime.env_context import _modex_env
        from modex_agent.tools.terminal.env import build_full_env
        from modex_agent.tools.terminal.prompt import sanitize_terminal_output

        overrides = _modex_env.get()
        cwd = working_dir or os.getcwd()
        env = build_full_env(overrides=overrides)
        # Non-tty pipe: colour codes are noise.  NO_COLOR prevents them at
        # source (https://no-color.org/); sanitize_terminal_output is the
        # fallback for tools that ignore NO_COLOR.
        env["NO_COLOR"] = "1"

        process = await self._spawn(command, cwd, env)

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            return f"Error: Command timed out after {timeout} seconds"

        output_parts: list[str] = []
        if stdout:
            output_parts.append(sanitize_terminal_output(stdout.decode("utf-8", errors="replace")))
        if stderr:
            stderr_text = sanitize_terminal_output(stderr.decode("utf-8", errors="replace"))
            if stderr_text.strip():
                output_parts.append(f"STDERR:\n{stderr_text}")
        if process.returncode != 0:
            output_parts.append(f"\nExit code: {process.returncode}")

        result = "\n".join(output_parts) if output_parts else "(no output)"
        return result

    def shell_info(self) -> ShellInfo:
        return self._shell_info


class PosixSubprocessExecutor(SubprocessExecutor):
    """bash / zsh / sh: ``create_subprocess_exec(path, "-c", command)``.

    ``create_subprocess_exec`` bypasses ``shell=True``, which on Windows
    injects ``/c`` (a cmd.exe flag bash does not understand).  Works
    cross-platform: ``/bin/bash -c`` on POSIX, ``bash.exe -c`` on WSL.
    """

    async def _spawn(
        self, command: str, cwd: str, env: dict[str, str]
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            self._shell_info.path,
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )


class CmdSubprocessExecutor(SubprocessExecutor):
    """cmd.exe: ``create_subprocess_shell`` (shell=True).

    ``shell=True`` lets CPython add the correct ``/c`` flag via
    ``COMSPEC``.  ``executable`` is left unset to avoid ``/c``
    injection onto a non-cmd shell.
    """

    async def _spawn(
        self, command: str, cwd: str, env: dict[str, str]
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )


class PowerShellSubprocessExecutor(SubprocessExecutor):
    """pwsh / powershell: ``create_subprocess_exec(path, "-NoProfile", "-Command", command)``.

    Explicitly invokes the PowerShell executable with ``-NoProfile`` to
    skip profile loading (deterministic, fast) and ``-Command`` to pass
    the command string.  This avoids ``shell=True`` which would route
    through ``COMSPEC`` (cmd.exe) and misinterpret PowerShell syntax.
    """

    async def _spawn(
        self, command: str, cwd: str, env: dict[str, str]
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            self._shell_info.path,
            "-NoProfile",
            "-Command",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )


def create_subprocess_executor(shell_info: ShellInfo | None = None) -> SubprocessExecutor:
    """Factory: pick the executor subclass by shell family.

    bash/zsh/sh → ``PosixSubprocessExecutor``;
    powershell → ``PowerShellSubprocessExecutor``;
    cmd → ``CmdSubprocessExecutor``.
    Callers should use this instead of instantiating ``SubprocessExecutor``
    subclasses directly.
    """
    resolved = shell_info or detect_platform_shell() or _default_fallback_shell()
    if resolved.family in (ShellFamily.BASH, ShellFamily.ZSH, ShellFamily.SH):
        return PosixSubprocessExecutor(shell_info=resolved)
    if resolved.family is ShellFamily.POWERSHELL:
        return PowerShellSubprocessExecutor(shell_info=resolved)
    return CmdSubprocessExecutor(shell_info=resolved)


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
        timeout: int | None = None,
    ) -> None:
        """Initialize SubprocessTool.

        Args:
            executor: Shell execution strategy (defaults to
                ``create_subprocess_executor()`` which picks the right
                family subclass).
            timeout: Command timeout in seconds.
        """
        super().__init__()
        self._executor = executor or create_subprocess_executor()
        self.timeout = timeout

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

        parts.append(
            "A trailing `Exit code:` line with a non-zero code means the "
            "command failed — investigate the cause before continuing."
        )

        parts.append(
            "Very long output is returned as head + an "
            "`[... OUTPUT ELIDED ...]` marker + tail, ending with a "
            "`[Full output ... saved to: <path>/full.txt]` notice pointing "
            "to the saved full output; read that file in segments "
            "when you need the elided middle or the complete content."
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

    async def execute(
        self, command: str = "", working_dir: str | None = None, **kwargs: object
    ) -> str:
        return await self._executor.execute(command, working_dir, timeout=self.timeout)
