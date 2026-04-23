"""Shell 执行工具.

提供简洁的命令执行功能，支持动态描述生成和可配置的安全校验。
"""

import asyncio
import os
import platform
import re
from pathlib import Path
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
        working_dir: str | None = None,
        enable_safety_guard: bool = True,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
    ):
        """初始化 Shell 工具.

        Args:
            timeout: 命令超时时间（秒）
            working_dir: 默认工作目录
            enable_safety_guard: 是否启用安全校验（默认 True）
            deny_patterns: 自定义禁止命令模式列表
            allow_patterns: 允许命令模式列表（如设置则只允许这些命令）
            restrict_to_workspace: 是否限制在工作目录内
        """
        super().__init__()
        self.timeout = timeout
        self.working_dir = working_dir
        self.enable_safety_guard = enable_safety_guard
        self.restrict_to_workspace = restrict_to_workspace
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

        # 工作目录提示
        if self.working_dir:
            parts.append(f"Default working directory: {self.working_dir}")

        # 安全限制提示
        if self.enable_safety_guard:
            parts.append("Safety guard is enabled.")
            if self.restrict_to_workspace:
                parts.append("Path traversal (../) is blocked for security.")
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
        cwd = working_dir or self.working_dir or os.getcwd()

        # 安全校验（如果启用）
        if self.enable_safety_guard:
            guard_error = self._guard_command(command, cwd)
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
            except asyncio.TimeoutError:
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

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """安全检查，防止危险命令."""
        cmd = command.strip()
        lower = cmd.lower()

        # 检查禁止模式
        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return f"Error: Command blocked by safety guard (dangerous pattern: {pattern})"

        # 检查允许模式
        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        # 检查工作目录限制
        if self.restrict_to_workspace:
            cwd_path = Path(cwd).resolve()

            # 根据操作系统选择路径匹配模式
            if self._platform == "windows":
                # Windows: 匹配 C:\path 或 C:/path，以及相对路径
                path_patterns = [
                    r"[A-Za-z]:[/\\][^\s\"'|<>]+",  # C:\path 或 C:/path
                    r"\.\.?[/\\][^\s\"'|<>]+",      # .\path 或 ..\path
                ]
            else:
                # POSIX: 匹配 /path 以及相对路径
                path_patterns = [
                    r"(?<![A-Za-z0-9_-])(?<![A-Za-z0-9_-]-)(?<!-)(?<!/)(/[^\s\"'|<>]+)",  # /path
                    r"\.\.?/[^\s\"'|<>]+",  # ./path 或 ../path
                ]

            found_paths = []
            for pattern in path_patterns:
                found_paths.extend(re.findall(pattern, cmd))

            for raw_path in found_paths:
                try:
                    # 清理路径（去除可能的尾随标点）
                    clean_path = raw_path.rstrip('.,;:!?)')
                    resolved_path = Path(clean_path)

                    # 如果是相对路径，基于工作目录解析
                    if not resolved_path.is_absolute():
                        resolved_path = (cwd_path / resolved_path).resolve()
                    else:
                        resolved_path = resolved_path.resolve()

                    # 检查路径是否在工作目录内
                    try:
                        resolved_path.relative_to(cwd_path)
                    except ValueError:
                        # 路径在工作目录外
                        if resolved_path != cwd_path:
                            return f"Error: Command blocked by safety guard (path outside working dir: {clean_path})"

                except Exception:
                    # 路径解析失败，跳过
                    continue

        return None
