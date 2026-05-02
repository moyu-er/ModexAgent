"""Shell 执行工具.

提供简洁的命令执行功能，支持动态描述生成和可配置的安全校验。
"""

import asyncio
import os
import platform
import re
from typing import Any

from ...core.tool_manager import Tool


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
        timeout: int = 60,
        enable_safety_guard: bool = True,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
    ):
        """初始化 Shell 工具.

        Args:
            timeout: 命令超时时间（秒）
            enable_safety_guard: 是否启用安全校验（默认 True）
            deny_patterns: 自定义禁止命令模式列表
            allow_patterns: 允许命令模式列表（如设置则只允许这些命令）
        """
        super().__init__()
        self.timeout = timeout
        self.enable_safety_guard = enable_safety_guard
        self._platform = platform.system().lower()

        # 根据平台设置默认危险模式，或接受自定义模式
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
        """动态生成描述，包含操作系统特定的提示."""
        parts = ["Execute a shell command and return its output."]

        # 操作系统特定提示
        if self._platform == "windows":
            parts.append(
                "On Windows: Use backslash (\\) as path separator. "
                "Commands run in cmd.exe."
            )
        else:
            parts.append(
                "On Unix/Linux/macOS: Use forward slash (/) as path separator. "
                "Commands run in sh/bash."
            )

        # 安全限制提示
        if self.enable_safety_guard:
            parts.append("Safety guard is enabled.")
        else:
            parts.append("WARNING: Safety guard is disabled.")

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
        cwd = working_dir or os.getcwd()

        # 安全校验（如果启用）
        if self.enable_safety_guard:
            guard_error = self._guard_command(command)
            if guard_error:
                return guard_error

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
            except TimeoutError:
                process.kill()
                return f"Error: Command timed out after {self.timeout} seconds"

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # 截断过长输出
            max_len = 10000
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"

            return result

        except Exception as e:
            return f"Error executing command: {str(e)}"

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
