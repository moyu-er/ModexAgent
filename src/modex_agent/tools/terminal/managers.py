"""BaseTerminalManager — lightweight multi-session orchestration with fake backends."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.backends.factory import create_pty_backend as _create_pty_backend
from modex_agent.tools.terminal.backends.visible_windows import WinptyConsoleWindowBackend
from modex_agent.tools.terminal.backends.windows_hidden import WinptyHiddenBackend
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.session import TerminalInfo, TerminalSession
from modex_agent.tools.terminal.state_store import JsonTerminalStateStore
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    TerminalVisibility,
    detect_platform_shell,
)

_DEFAULT_SHELL_INFO = ShellInfo(
    family=ShellFamily.BASH,
    path="/bin/bash",
    platform=Platform.LINUX,
)

logger = logging.getLogger(__name__)


class TerminalManagerBase(ABC):
    """Abstract seam for multi-session terminal managers.

    Two intentional implementations (ADR-0007 — a real adapter seam, not
    duplication):

    - ``BaseTerminalManager`` (this module): lightweight, composition-friendly
      base with a pluggable ``backend_factory``. No LRU eviction, no
      persistence. This is the PRODUCTION path — the example bot uses its
      platform subclasses (``WindowsHiddenTerminalManager`` /
      ``WindowsVisibleTerminalManager`` / ``LinuxTerminalManager``) via
      ``create_terminal_manager``.
    - ``TerminalManager`` (``manager.py``): full-featured manager adding LRU
      eviction, JSON persistence (save/load state), and memory-pressure
      buffer clearing. Currently has ZERO production callers; retained as a
      capability seam per ADR-0007 (do not delete as "unused" — see
      ``tests/architecture/test_terminal_manager_seam_preserved.py``).

    Tools consume the seam via ``get_default()`` (command execution) and the
    session-management methods.
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
    """Production terminal-session manager — lightweight, no eviction/persistence.

    Manages named sessions with a pluggable ``backend_factory`` (real backends
    in production, fakes in tests). Role in the seam (see
    ``TerminalManagerBase``): the lean production implementation. The example
    bot uses the platform subclasses below via ``create_terminal_manager``.
    Unlike ``TerminalManager`` it does NOT do LRU eviction or persistence —
    by design.
    """

    def __init__(
        self,
        *,
        shell_info: ShellInfo,
        visibility: TerminalVisibility,
        backend_factory: Callable[[], Any],
        config: TerminalRuntimeConfig | None = None,
        default_cwd: str | None = None,
        max_terminals: int | None = None,
        storage_dir: Path | None = None,
        enable_memory_pressure: bool = False,
    ) -> None:
        self.platform = shell_info.platform
        self.shell_info = shell_info
        self.visibility = visibility
        self.config = config or TerminalRuntimeConfig()
        self._backend_factory = backend_factory
        self._default_cwd: str | None = default_cwd
        self._sessions: dict[str, TerminalSession] = {}
        self._default_name: str | None = None
        # Capability flags — all default-off → lean form behaviour-equivalent
        self._max_terminals: int | None = max_terminals
        self._storage_dir: Path | None = storage_dir
        self._enable_memory_pressure: bool = enable_memory_pressure
        # Lazily-created state store (only when storage_dir set)
        self._store: JsonTerminalStateStore | None = (
            JsonTerminalStateStore(storage_dir) if storage_dir is not None else None
        )

    async def get_or_create(self, name: str | None, cwd: str | None = None) -> TerminalSession:
        session_name = name or "default"
        session = self._sessions.get(session_name)
        if session is not None:
            session.last_active = time.time()
            return session
        # LRU eviction — only when capacity is configured
        if self._max_terminals is not None and len(self._sessions) >= self._max_terminals:
            await self._evict_oldest()
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
        await self._check_memory_pressure()
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

    async def _evict_oldest(self) -> None:
        """Close the least recently used session (only called when LRU is enabled)."""
        if not self._sessions:
            return
        oldest_name = min(self._sessions, key=lambda n: self._sessions[n].last_active)
        logger.info("LRU evicting terminal: %s", oldest_name)
        await self.close(oldest_name)

    async def _check_memory_pressure(self) -> None:
        """Clear buffers of largest non-default sessions when total exceeds threshold.

        Per ADR-0010 Decision 8: folded inward from the legacy TerminalManager.
        No-op when ``self._enable_memory_pressure`` is False.
        """
        if not self._enable_memory_pressure:
            return

        total_buffer = 0
        session_buffers: list[tuple[str, int]] = []
        for name, session in self._sessions.items():
            buf = session._backend._output_buffer
            if buf is not None:
                size = buf.total_chars
                total_buffer += size
                session_buffers.append((name, size))

        if total_buffer <= self.config.max_total_buffer_chars:
            return

        session_buffers.sort(key=lambda x: x[1], reverse=True)
        for name, size in session_buffers:
            if name == self._default_name:
                continue
            if total_buffer <= self.config.max_total_buffer_chars:
                break
            buf = self._sessions[name]._backend._output_buffer
            if buf is not None:
                buf.clear()
                logger.warning(
                    "Memory pressure: cleared buffer for '%s' (was %d chars)", name, size
                )
                total_buffer -= size

    async def save_state(self) -> None:
        """Persist session metadata and history to JSON.

        No-op when storage_dir is None (lean form, per ADR-0010 Decision 8).
        """
        if self._store is None:
            return
        sessions_data = [session.get_state() for session in self._sessions.values()]
        state = {
            "version": 1,
            "default_terminal": self._default_name,
            "sessions": sessions_data,
        }
        self._store.save(state)

    async def load_state(self) -> None:
        """Restore session metadata from JSON. Sessions are lazily restarted on use.

        No-op when storage_dir is None (lean form, per ADR-0010 Decision 8).
        """
        if self._store is None:
            return
        data = self._store.load()
        if not data:
            return
        for sess_data in data.get("sessions", []):
            name = sess_data["name"]
            backend = self._backend_factory()
            session = TerminalSession(
                name=name,
                backend=backend,
                shell_info=self.shell_info,
                cwd=sess_data.get("cwd"),
                env=sess_data.get("env"),
            )
            session.restore_state(sess_data)
            self._sessions[name] = session
        self._default_name = data.get("default_terminal")
        if self._default_name not in self._sessions:
            self._default_name = next(iter(self._sessions), None)
        logger.info("Loaded %d terminal sessions from state", len(self._sessions))

    async def select_default(self, name: str) -> None:
        if name not in self._sessions:
            raise ValueError(f"Terminal '{name}' does not exist")
        self._default_name = name

    async def list_sessions(self) -> list[TerminalInfo]:
        result: list[TerminalInfo] = []
        for name, session in self._sessions.items():
            result.append(await session.to_info(is_default=name == self._default_name))
        await self._check_memory_pressure()
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
            backend_factory=WinptyHiddenBackend,
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
            backend_factory=WinptyConsoleWindowBackend,
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
