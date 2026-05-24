"""Shell 执行工具.

提供简洁的命令执行功能，支持动态描述生成和可配置的安全校验。
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ...core.tool_manager import Tool

from framework.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    _parse_platform,
    detect_platform_shell,
)

if TYPE_CHECKING:
    from framework.tools.terminal.manager import TerminalManager


class ShellExecutor(ABC):
    """Abstract strategy for executing shell commands.

    EXTENSION: Phase 2+ can add:
      - RemoteExecutor (asyncssh/paramiko)
      - DockerExecutor (docker exec)
    """

    @abstractmethod
    async def execute(self, command: str, working_dir: str | None = None, timeout: int = 60) -> str:
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
        self._shell_info = shell_info or detected or ShellInfo(
            family=ShellFamily.CMD,
            path="cmd.exe",
            platform=_parse_platform(platform.system().lower()),
        )

    async def execute(self, command: str, working_dir: str | None = None, timeout: int = 60) -> str:
        cwd = working_dir or os.getcwd()
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
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
        max_len = 10000
        if len(result) > max_len:
            result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
        return result

    def shell_info(self) -> ShellInfo:
        return self._shell_info


class TerminalSessionExecutor(ShellExecutor):
    """Stateful executor: commands run in a persistent terminal session.

    EXTENSION: Phase 2+ can add:
      - RemoteExecutor: asyncssh/paramiko remote execution
      - DockerExecutor: docker exec
    """

    def __init__(
        self,
        terminal_manager: TerminalManager,
        default_terminal: str | None = None,
    ) -> None:
        self._tm = terminal_manager
        self._default_terminal = default_terminal

    _INTERRUPT_COMMANDS: frozenset[str] = frozenset({"^c", "\x03", "ctrl+c", "ctrl-c"})

    async def execute(self, command: str, working_dir: str | None = None, timeout: int = 60) -> str:
        session = await self._tm.get_default_session()
        if session is None:
            # Create a fresh default session rather than silently switching
            # to an unrelated existing tab.
            name = self._default_terminal or "default"
            session = await self._tm.get_or_create(name, cwd=working_dir)
        elif working_dir is not None:
            quoted = f'"{working_dir}"' if " " in working_dir else working_dir
            await session.execute(f"cd {quoted}", timeout=10)

        # Allow the agent to interrupt a timed-out command via the shell tool
        # itself (e.g. shell.execute("^C")) without switching to the terminal tool.
        if command.strip().lower() in self._INTERRUPT_COMMANDS:
            await session.send_interrupt()
            return "Sent Ctrl+C to interrupt the running command."

        return await session.execute(command, timeout=timeout)

    def shell_info(self) -> ShellInfo:
        # shell_info is sync; look up the default tab name directly without
        # awaiting get_default_session().  Metadata is valid even if the
        # session happens to be dead.
        if self._tm._default_terminal and self._tm._default_terminal in self._tm._sessions:
            session = self._tm._sessions[self._tm._default_terminal]
            info = session.shell_info
            return ShellInfo(
                family=info.family,
                path=info.path,
                platform=info.platform,
            )
        info = detect_platform_shell()
        if info is not None:
            return info
        # Fallback — should never happen in practice because
        # TerminalSessionExecutor is only created when a shell is available.
        return ShellInfo(
            family=ShellFamily.BASH, path="bash", platform=Platform.LINUX,
        )


class ShellTool(Tool):
    """执行 shell 命令的工具.

    支持动态描述生成，根据操作系统提供相关提示。
    安全校验可配置，默认启用。
    """

    # Windows 危险命令模式
    WINDOWS_DENY_PATTERNS = [
        r"\bdel\s+/[fq]\b",              # del /f, del /q
        r"\brmdir\s+/s\b",               # rmdir /s
        r"\bformat\b",                   # format
        r"\bdiskpart\b",                 # diskpart
        r"\bshutdown\b",                 # shutdown
        r"\breboot\b",                   # reboot
        r"\bpoweroff\b",                 # poweroff
    ]

    # POSIX (Linux/macOS) 危险命令模式
    POSIX_DENY_PATTERNS = [
        r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
        r"\bmkfs\.",                     # mkfs
        r"\bdd\s+if=",                   # dd
        r">\s*/dev/sd",                  # 写入磁盘
        r"\b(shutdown|reboot|poweroff)\b",  # 系统电源
        r":\(\)\s*\{.*\};\s*:",          # fork bomb
        r"\brm\s+-rf\s+/\b",             # rm -rf /
        r"\bdd\s+if=.*of=/dev/[sh]d",    # dd to disk
    ]

    def __init__(
        self,
        executor: ShellExecutor | None = None,
        timeout: int = 60,
        enable_safety_guard: bool = True,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
    ) -> None:
        """Initialize Shell tool.

        Args:
            executor: Shell execution strategy (defaults to SubprocessExecutor).
            timeout: Command timeout in seconds.
            enable_safety_guard: Whether to enable safety checks.
            deny_patterns: Custom deny patterns.
            allow_patterns: Allowlist patterns.
        """
        super().__init__()
        self._executor = executor or SubprocessExecutor()
        self.timeout = timeout
        # Ensure ToolManager's outer asyncio.wait_for never preempts our own
        # timeout handling (which returns partial output + timeout marker).
        self.config.timeout = timeout + 10
        self.enable_safety_guard = enable_safety_guard
        self._platform = platform.system().lower()

        if deny_patterns is not None:
            self.deny_patterns = deny_patterns
        elif self._platform == "windows":
            self.deny_patterns = self.WINDOWS_DENY_PATTERNS.copy()
        else:
            self.deny_patterns = self.POSIX_DENY_PATTERNS.copy()

        self.allow_patterns = allow_patterns or []

    @property
    def name(self) -> str:
        return "shell"

    _FAMILY_DESCRIPTIONS: dict[ShellFamily, str] = {
        ShellFamily.BASH: (
            "Commands run in bash. Use POSIX syntax: forward slashes for paths, "
            "single quotes for strings, && for chaining."
        ),
        ShellFamily.CMD: (
            "Commands run in Windows CMD. Use CMD syntax: backslashes for paths, "
            "&& for chaining, %VAR% for environment variables."
        ),
        ShellFamily.ZSH: (
            "Commands run in zsh. Compatible with bash syntax."
        ),
        ShellFamily.SH: (
            "Commands run in sh. Use basic POSIX syntax."
        ),
    }

    @property
    def description(self) -> str:
        """Dynamically generate description based on actual shell type."""
        shell_info = self._executor.shell_info()
        parts = [
            f"Execute a shell command using {shell_info.name} and return its output."
        ]

        family_desc = self._FAMILY_DESCRIPTIONS.get(shell_info.family)
        if family_desc:
            parts.append(family_desc)

        is_stateful = isinstance(self._executor, TerminalSessionExecutor)
        if is_stateful:
            parts.append(
                "This is a stateful session: cd, environment variables, "
                "and aliases persist across commands. "
                "The terminal session is created automatically on first use — "
                "do NOT call any other tool to create a terminal before running commands."
            )
            parts.append(
                "Commands run in a visible terminal window — "
                "you can watch execution and the user can intervene directly."
            )
        else:
            parts.append(
                "Each command runs in a fresh process: cd and environment "
                "changes do NOT persist."
            )

        if self.enable_safety_guard:
            parts.append("Safety guard is enabled.")

        return " ".join(parts)

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: object) -> str:
        if self.enable_safety_guard:
            guard_error = self._guard_command(command)
            if guard_error:
                return guard_error
        return await self._executor.execute(command, working_dir, timeout=self.timeout)

    def _guard_command(self, command: str) -> str | None:
        """安全检查，防止危险命令."""
        cmd = command.strip()
        lower = cmd.lower()

        # 检查禁止模式
        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return f"Error: Command blocked by safety guard (dangerous pattern: {pattern})"

        # 检查允许模式
        if self.allow_patterns and not any(re.search(p, lower) for p in self.allow_patterns):
            return "Error: Command blocked by safety guard (not in allowlist)"

        return None
