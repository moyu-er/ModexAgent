from __future__ import annotations

import os
import platform as _platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Platform(StrEnum):
    """Supported operating system platforms."""

    WINDOWS = "windows"
    LINUX = "linux"
    DARWIN = "darwin"


class ShellFamily(StrEnum):
    """Supported shell families with platform-specific behavior."""

    BASH = "bash"
    CMD = "cmd"
    POWERSHELL = "powershell"
    ZSH = "zsh"
    SH = "sh"

    def uses_readline(self) -> bool:
        """Return True if the shell uses readline for line editing."""
        return self in (ShellFamily.BASH, ShellFamily.ZSH, ShellFamily.SH)

    def command_ending(self) -> str:
        """Return the command terminator for this shell family."""
        return "\n" if self.uses_readline() else "\r\n"


@dataclass(frozen=True)
class ShellInfo:
    """Immutable description of a discovered shell."""

    family: ShellFamily
    path: str
    platform: Platform

    @property
    def name(self) -> str:
        """Return the shell family name for backward compatibility."""
        return self.family.value


class TerminalVisibility(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"


class ProcessStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMED_OUT = "timed_out"


class CommandResultStatus(StrEnum):
    """CommandTool return status — used in command_result XML."""

    COMPLETED     = "completed"
    EXECUTING     = "executing"      # was: running
    TIMED_OUT     = "timed_out"
    PAGINATED     = "paginated"
    WAITING_INPUT = "waiting_input"  # was: input_wait
    STUCK         = "stuck"          # new


class TerminalCommandStatus(StrEnum):
    """Unified terminal status — used by terminal current, CommandTool, and session layer."""

    UNKNOWN       = "unknown"
    IDLE          = "idle"
    EXECUTING     = "executing"
    WAITING_INPUT = "waiting_input"
    STUCK         = "stuck"
    COMPLETED     = "completed"
    TIMED_OUT     = "timed_out"
    PAGINATED     = "paginated"


# XML root tag → list of element names whose text content is safe to truncate.
# Defined once here so governance code does not hard-code element names.
_TERMINAL_XML_TRUNCATABLE: dict[str, list[str]] = {
    "command_result": ["output"],
    "process_result": ["output"],
    "terminal_result": ["output", "cursor"],
    "tool_result_overflow": ["chunk", "instruction"],
}


def get_terminal_xml_truncatable_paths(content: str) -> list[str] | None:
    """Return truncatable element names if *content* is a known terminal XML format.

    Returns None for unrecognised / plain-text content so callers can fall
    back to content-format-agnostic truncation.
    """
    for root_tag, paths in _TERMINAL_XML_TRUNCATABLE.items():
        # Match complete tag open: <tag> or <tag attr="..."> but not <tag_extra>
        if re.search(rf"<{re.escape(root_tag)}\b", content):
            return paths
    return None


def _parse_platform(name: str) -> Platform:
    """Map platform.system() string to Platform enum."""
    mapping: dict[str, Platform] = {
        "windows": Platform.WINDOWS,
        "linux": Platform.LINUX,
        "darwin": Platform.DARWIN,
    }
    return mapping.get(name, Platform.LINUX)


def _family_from_path(shell_path: str) -> ShellFamily:
    """Infer ShellFamily from executable path or name."""
    name = Path(shell_path).name.lower()
    mapping: dict[str, ShellFamily] = {
        "bash": ShellFamily.BASH,
        "zsh": ShellFamily.ZSH,
        "sh": ShellFamily.SH,
        "cmd": ShellFamily.CMD,
        "cmd.exe": ShellFamily.CMD,
    }
    return mapping.get(name, ShellFamily.SH)


def _verify_bash(path: str) -> ShellInfo | None:
    """Return ShellInfo if *path* is a working bash, else None."""
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and "bash" in result.stdout.lower():
            return ShellInfo(
                family=ShellFamily.BASH,
                path=path,
                platform=Platform.WINDOWS,
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def detect_platform_shell() -> ShellInfo | None:
    """Detect the best available shell for the current platform.

    Windows priority: WSL bash > Git bash / MSYS2 > PowerShell.
    Linux priority: bash > sh
    macOS priority: bash > zsh > sh
    """
    plat = _parse_platform(_platform.system().lower())

    if plat is Platform.WINDOWS:
        # Priority 1: WSL bash
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        wsl_bash = Path(system_root) / "System32" / "bash.exe"
        if wsl_bash.is_file():
            info = _verify_bash(str(wsl_bash))
            if info is not None:
                return info

        # Priority 2: Git bash / MSYS2 (any bash in PATH)
        bash_path = shutil.which("bash")
        if bash_path:
            info = _verify_bash(bash_path)
            if info is not None:
                return info

        # Priority 3: PowerShell
        ps_path = shutil.which("powershell.exe")
        if ps_path:
            return ShellInfo(
                family=ShellFamily.POWERSHELL,
                path=ps_path,
                platform=plat,
            )

        # Should never happen on Windows
        return None

    env_shell = os.environ.get("SHELL", "")
    if env_shell and shutil.which(env_shell):
        family = _family_from_path(env_shell)
        return ShellInfo(family=family, path=env_shell, platform=plat)

    bash_path = shutil.which("bash")
    if bash_path:
        return ShellInfo(family=ShellFamily.BASH, path=bash_path, platform=plat)

    if plat is Platform.DARWIN:
        zsh_path = shutil.which("zsh")
        if zsh_path:
            return ShellInfo(family=ShellFamily.ZSH, path=zsh_path, platform=plat)

    sh_path = shutil.which("sh") or "/bin/sh"
    return ShellInfo(family=ShellFamily.SH, path=sh_path, platform=plat)
