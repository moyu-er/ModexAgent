"""BaseTerminalManager — lightweight multi-session orchestration with fake backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.backends.factory import create_pty_backend as _create_pty_backend
from modex_agent.tools.terminal.backends.visible_windows import VisibleWindowsPtyBackend
from modex_agent.tools.terminal.backends.windows_hidden import WindowsHiddenPtyBackend
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.session import TerminalInfo, TerminalSession
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    TerminalVisibility,
    detect_platform_shell,
)


class TerminalManagerBase(ABC):
    """Abstract base for terminal managers.

    Both TerminalManager (manager.py) and BaseTerminalManager implement this
    interface. Tools use get_default() for command execution and the full
    interface for session management.
    """

    @abstractmethod
    async def get_default(self) -> TerminalSession: ...

    @abstractmethod
    async def get_or_create(self, name: str | None, cwd: str | None = None) -> TerminalSession: ...

    @abstractmethod
    def get(self, name: str) -> TerminalSession | None: ...

    @abstractmethod
    async def get_default_session(self) -> TerminalSession | None: ...

    @abstractmethod
    async def close(self, name: str) -> bool: ...

    @abstractmethod
    async def list_sessions(self) -> list[TerminalInfo]: ...

    @abstractmethod
    def list_names(self) -> list[str]: ...

    @abstractmethod
    async def select_default(self, name: str) -> None: ...


class BaseTerminalManager(TerminalManagerBase):
    """Manages named terminal sessions with a pluggable backend factory.

    Unlike the full TerminalManager (manager.py), this class does not
    handle LRU eviction or persistence.  It is designed for composition:
    real backends in production, fake backends in tests.
    """

    def __init__(
        self,
        *,
        shell_info: ShellInfo,
        visibility: TerminalVisibility,
        backend_factory: Callable[[], Any],
        config: TerminalRuntimeConfig | None = None,
        default_cwd: str | None = None,
    ) -> None:
        self.platform = shell_info.platform
        self.shell_info = shell_info
        self.visibility = visibility
        self.config = config or TerminalRuntimeConfig()
        self._backend_factory = backend_factory
        self._default_cwd: str | None = default_cwd
        self._sessions: dict[str, TerminalSession] = {}
        self._default_name: str | None = None

    async def get_or_create(self, name: str | None, cwd: str | None = None) -> TerminalSession:
        session_name = name or "default"
        session = self._sessions.get(session_name)
        if session is not None:
            return session
        # Fall back to the workspace default cwd (e.g. the workspace target)
        # so a terminal-enabled pool opens in the workspace, not process CWD.
        effective_cwd = cwd if cwd is not None else self._default_cwd
        backend = self._backend_factory()
        session = TerminalSession(
            name=session_name,
            backend=backend,
            shell_info=self.shell_info,
            cwd=effective_cwd,
        )
        self._sessions[session_name] = session
        # A newly created tab becomes the default, matching TerminalManager
        # and the TerminalTool 'open' contract ("create a new tab AND
        # auto-select it"). Without this, opening a second tab leaves the
        # default on the old tab and subsequent commands run there.
        self._default_name = session_name
        return session

    def get(self, name: str) -> TerminalSession | None:
        """Get session by name without creating."""
        return self._sessions.get(name)

    async def get_default_session(self) -> TerminalSession | None:
        """Return the default session, or None if none is set or it has been closed."""
        if self._default_name is None:
            return None
        session = self._sessions.get(self._default_name)
        if session is None:
            self._default_name = None
            return None
        # Only check alive if the backend was actually started, so unstarted
        # sessions (created but never used) are not incorrectly evicted.
        if session.backend_started and not await session.is_alive():
            self._default_name = None
            return None
        return session

    async def get_default(self) -> TerminalSession:
        if self._default_name is None:
            return await self.get_or_create("default")
        return self._sessions[self._default_name]

    def list_names(self) -> list[str]:
        """Return just the session names."""
        return list(self._sessions.keys())

    async def select_default(self, name: str) -> None:
        if name not in self._sessions:
            raise ValueError(f"Terminal '{name}' does not exist")
        self._default_name = name

    async def list_sessions(self) -> list[TerminalInfo]:
        result: list[TerminalInfo] = []
        for name, session in self._sessions.items():
            result.append(await session.to_info(is_default=name == self._default_name))
        return result

    async def close(self, name: str) -> bool:
        session = self._sessions.pop(name, None)
        if session is None:
            return False
        await session.terminate()
        if self._default_name == name:
            self._default_name = next(iter(self._sessions), None)
        return True


class WindowsHiddenTerminalManager(BaseTerminalManager):
    """Terminal manager for hidden Windows PTY sessions."""

    def __init__(
        self,
        config: TerminalRuntimeConfig | None = None,
        default_cwd: str | None = None,
    ) -> None:
        shell_info = _require_windows_shell()
        super().__init__(
            shell_info=shell_info,
            visibility=TerminalVisibility.HIDDEN,
            backend_factory=WindowsHiddenPtyBackend,
            config=config,
            default_cwd=default_cwd,
        )


class WindowsVisibleTerminalManager(BaseTerminalManager):
    """Terminal manager for visible Windows PTY sessions.

    Uses WSL bash > Git bash.  Raises RuntimeError if no supported shell is
    available; the application layer falls back to SubprocessTool in that case.
    PowerShell is not supported.
    """

    def __init__(
        self,
        config: TerminalRuntimeConfig | None = None,
        default_cwd: str | None = None,
    ) -> None:
        shell_info = _require_windows_shell()
        super().__init__(
            shell_info=shell_info,
            visibility=TerminalVisibility.VISIBLE,
            backend_factory=VisibleWindowsPtyBackend,
            config=config,
            default_cwd=default_cwd,
        )


def _create_linux_backend() -> TerminalBackend:
    """Create a Linux PTY backend (pexpect preferred, tmux fallback).

    Delegates to ``create_pty_backend()`` for the actual selection logic.
    Called eagerly by LinuxTerminalManager.__init__ to validate that at
    least one backend is available at pool startup.  Also used as the
    lazy backend_factory for new sessions.

    Raises:
        RuntimeError: If neither pexpect nor tmux+libtmux is available.
    """
    try:
        return _create_pty_backend()
    except ImportError as e:
        raise RuntimeError(
            "No Linux terminal backend available. "
            "Install pexpect (`pip install pexpect`) or tmux+libtmux (`pip install libtmux`)."
        ) from e


class LinuxTerminalManager(BaseTerminalManager):
    """Terminal manager for Linux/macOS headless PTY sessions.

    Eagerly validates backend availability during __init__.  If neither
    pexpect nor tmux+libtmux is importable, raises RuntimeError so the
    caller can degrade to SubprocessTool.

    Degradation chain (per-session): pexpect → tmux.
    """

    def __init__(
        self,
        config: TerminalRuntimeConfig | None = None,
        default_cwd: str | None = None,
    ) -> None:
        shell_info = detect_platform_shell()
        super().__init__(
            shell_info=shell_info
            or ShellInfo(
                family=ShellFamily.BASH,
                path="/bin/sh",
                platform=Platform.LINUX,
            ),
            visibility=TerminalVisibility.HIDDEN,
            backend_factory=_create_linux_backend,
            config=config,
            default_cwd=default_cwd,
        )
        # Eager validation: fail now (at pool startup) rather than at
        # first command if no backend is available.
        _create_linux_backend()


def _require_windows_shell() -> ShellInfo:
    """Detect shell: WSL bash > Git bash.

    Raises RuntimeError if no supported shell is found. Callers that need a
    fallback should degrade to SubprocessTool.
    """
    info = detect_platform_shell()
    if info is not None:
        return info
    raise RuntimeError("No supported shell found on Windows")


def _require_bash_shell() -> ShellInfo | None:
    """Detect a bash shell (WSL or Git).  Returns None if unavailable."""
    info = detect_platform_shell()
    if info is not None and info.family == ShellFamily.BASH:
        return info
    return None


def create_terminal_manager(
    *,
    manager_kind: str,
    config: TerminalRuntimeConfig | None = None,
    default_cwd: str | None = None,
) -> TerminalManagerBase:
    """Create a terminal manager by kind string.

    Args:
        manager_kind: "windows_hidden", "windows_visible", or "linux".
        config: Optional runtime configuration.
        default_cwd: Optional directory new sessions open in when no explicit
            ``cwd`` is given (e.g. the workspace target).

    Returns:
        A TerminalManagerBase instance.

    Raises:
        ValueError: If manager_kind is not recognized.
        RuntimeError: If the selected manager cannot find an available backend.
    """
    if manager_kind == "windows_hidden":
        return WindowsHiddenTerminalManager(config=config, default_cwd=default_cwd)
    if manager_kind == "windows_visible":
        return WindowsVisibleTerminalManager(config=config, default_cwd=default_cwd)
    if manager_kind == "linux":
        return LinuxTerminalManager(config=config, default_cwd=default_cwd)
    raise ValueError(f"Unsupported terminal manager kind: {manager_kind}")
