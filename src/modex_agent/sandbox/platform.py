"""Cross-platform utilities for sandbox execution."""

import os
import sys
from enum import Enum


class Platform(Enum):
    """Supported platforms."""

    WINDOWS = "windows"
    MACOS = "darwin"
    LINUX = "linux"
    UNKNOWN = "unknown"


def get_platform() -> Platform:
    """Detect the current operating system platform.

    Returns:
        Platform enum value for the current OS.
    """
    if sys.platform == "win32":
        return Platform.WINDOWS
    elif sys.platform == "darwin":
        return Platform.MACOS
    elif sys.platform.startswith("linux"):
        return Platform.LINUX
    else:
        return Platform.UNKNOWN


def get_default_shell() -> str:
    """Get the default shell for the current platform.

    Returns:
        Shell command string (e.g., 'bash', 'cmd.exe', 'powershell.exe').
    """
    platform = get_platform()

    if platform == Platform.WINDOWS:
        # Prefer PowerShell on Windows, fallback to cmd
        return "powershell.exe"
    elif platform == Platform.MACOS:
        return "bash"
    else:  # Linux and others
        return "bash"


def get_shell_executable() -> str | None:
    """Get the full path to the default shell executable.

    Returns:
        Path to shell executable or None if not found.
    """
    import shutil

    shell = get_default_shell()
    return shutil.which(shell)


def convert_path_for_platform(path: str, target_platform: Platform | None = None) -> str:
    """Convert a path to use the correct separators for the target platform.

    Args:
        path: The path to convert.
        target_platform: Target platform. If None, uses current platform.

    Returns:
        Path with correct separators for the target platform.
    """
    if target_platform is None:
        target_platform = get_platform()

    # Normalize to forward slashes first
    normalized = path.replace("\\", "/")

    if target_platform == Platform.WINDOWS:
        # Convert to backslashes for Windows
        return normalized.replace("/", "\\")
    else:
        # Keep forward slashes for Unix-like systems
        return normalized


def is_unix_like() -> bool:
    """Check if the current platform is Unix-like (macOS or Linux).

    Returns:
        True if platform is macOS or Linux, False otherwise.
    """
    platform = get_platform()
    return platform in (Platform.MACOS, Platform.LINUX)


def get_command_separator() -> str:
    """Get the command separator for the current platform.

    Returns:
        '&&' for Unix-like, '&' for Windows.
    """
    return "&&" if is_unix_like() else "&"


def get_shell_command_args(command: str) -> list[str]:
    """Build platform-appropriate shell invocation args for a command string.

    Args:
        command: The shell command string to execute.

    Returns:
        List of args for subprocess.Popen, e.g. ["bash", "-c", cmd] or ["cmd.exe", "/c", cmd].
    """
    if get_platform() == Platform.WINDOWS:
        return ["cmd.exe", "/c", command]
    else:
        return ["bash", "-c", command]


def join_commands(*commands: str) -> str:
    """Join multiple commands with the appropriate separator for the platform.

    Args:
        *commands: Commands to join.

    Returns:
        Joined command string.
    """
    separator = f" {get_command_separator()} "
    return separator.join(commands)


_SHELL_WHITELIST: frozenset[str] = frozenset({"sh", "bash", "zsh"})


def resolve_shell(shell: str | None = None) -> str | None:
    """Validate a shell program and return its executable path.

    Only sh, bash, and zsh are allowed. The shell must be found via
    shutil.which() and be a bare name or absolute path (no relative paths
    with separators).

    Args:
        shell: Shell name (e.g. "bash"), absolute path, or None for default.

    Returns:
        Absolute path to the shell executable, or None if not allowed/found.
    """
    import shutil

    if shell is None:
        # Default: find bash or sh
        for name in ("bash", "sh"):
            resolved = shutil.which(name)
            if resolved:
                return resolved
        return None

    # Reject null bytes and control characters
    if any(c in shell for c in ("\x00", "\n", "\r")):
        return None

    # Reject relative paths with separators
    if (os.sep in shell or (os.altsep and os.altsep in shell)) and not os.path.isabs(shell):
        # Allow if it's an absolute path
        return None

    # Extract basename for whitelist check
    basename = os.path.basename(shell)
    if basename not in _SHELL_WHITELIST:
        return None

    # Resolve via which
    resolved = shutil.which(shell)
    return resolved
