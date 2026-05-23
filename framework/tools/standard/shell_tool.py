"""Shell 执行工具.

提供简洁的命令执行功能，支持动态描述生成和可配置的安全校验。
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...core.tool_manager import Tool

if TYPE_CHECKING:
    from framework.tools.terminal.manager import TerminalManager


@dataclass(frozen=True)
class ShellInfo:
    """Information about the detected shell.

    Used to generate dynamic tool descriptions so the LLM knows
    which shell syntax to use.
    """

    name: str
    path: str
    platform: str
    is_stateful: bool


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

    def __init__(self, shell_info: ShellInfo | None = None):
        self._shell_info = shell_info or detect_platform_shell()

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
    ):
        self._tm = terminal_manager
        self._default_terminal = default_terminal

    async def execute(self, command: str, working_dir: str | None = None, timeout: int = 60) -> str:
        session = self._tm.get_default_session()
        if session is None:
            name = self._default_terminal or "default"
            session = await self._tm.get_or_create(name, cwd=working_dir)
        elif working_dir is not None:
            await session.execute(f"cd {working_dir}", timeout=10)
        return await session.execute(command, timeout=timeout)

    def shell_info(self) -> ShellInfo:
        session = self._tm.get_default_session()
        if session is not None:
            info = session.shell_info
            return ShellInfo(
                name=info.name,
                path=info.path,
                platform=info.platform,
                is_stateful=True,
            )
        info = detect_platform_shell()
        return ShellInfo(
            name=info.name,
            path=info.path,
            platform=info.platform,
            is_stateful=True,
        )


def detect_platform_shell() -> ShellInfo:
    """Detect the best available shell for the current platform.

    Windows priority: bash > powershell > cmd
    Linux priority: bash > sh
    macOS priority: bash > zsh > sh
    """
    plat = platform.system().lower()

    if plat == "windows":
        bash_path = shutil.which("bash")
        if bash_path:
            try:
                result = subprocess.run(
                    [bash_path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and "bash" in result.stdout.lower():
                    return ShellInfo(name="bash", path=bash_path, platform="windows", is_stateful=False)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

        ps_path = shutil.which("powershell") or shutil.which("pwsh")
        if ps_path:
            return ShellInfo(name="powershell", path=ps_path, platform="windows", is_stateful=False)

        cmd_path = shutil.which("cmd") or shutil.which("cmd.exe")
        if cmd_path:
            return ShellInfo(name="cmd", path=cmd_path, platform="windows", is_stateful=False)

        return ShellInfo(name="cmd", path="cmd.exe", platform="windows", is_stateful=False)

    env_shell = os.environ.get("SHELL", "")
    if env_shell and shutil.which(env_shell):
        shell_name = Path(env_shell).name
        return ShellInfo(name=shell_name, path=env_shell, platform=plat, is_stateful=False)

    bash_path = shutil.which("bash")
    if bash_path:
        return ShellInfo(name="bash", path=bash_path, platform=plat, is_stateful=False)

    if plat == "darwin":
        zsh_path = shutil.which("zsh")
        if zsh_path:
            return ShellInfo(name="zsh", path=zsh_path, platform="darwin", is_stateful=False)

    sh_path = shutil.which("sh") or "/bin/sh"
    return ShellInfo(name="sh", path=sh_path, platform=plat, is_stateful=False)


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
    ):
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

    @property
    def description(self) -> str:
        """Dynamically generate description based on actual shell type."""
        shell_info = self._executor.shell_info()
        parts = [
            f"Execute a shell command using {shell_info.name} and return its output."
        ]

        if shell_info.name == "bash":
            parts.append(
                "Commands run in bash. Use POSIX syntax: forward slashes for paths, "
                "single quotes for strings, && for chaining."
            )
        elif shell_info.name == "powershell":
            parts.append(
                "Commands run in PowerShell. Use PowerShell syntax: "
                "Get-ChildItem instead of ls, semicolons for chaining, "
                "backtick for line continuation."
            )
        elif shell_info.name == "cmd":
            parts.append(
                "Commands run in Windows CMD. Use CMD syntax: backslashes for paths, "
                "&& for chaining, %VAR% for environment variables."
            )
        elif shell_info.name == "zsh":
            parts.append(
                "Commands run in zsh. Compatible with bash syntax."
            )
        else:
            parts.append(
                "Commands run in sh. Use basic POSIX syntax."
            )

        if shell_info.is_stateful:
            parts.append(
                "This is a stateful session: cd, environment variables, "
                "and aliases persist across commands."
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

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
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
