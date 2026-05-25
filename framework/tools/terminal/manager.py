"""TerminalManager — multi-session management with LRU eviction and persistence."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from framework.tools.terminal.backends.factory import create_pty_backend
from framework.tools.terminal.session import CommandRecord, TerminalInfo, TerminalSession
from framework.tools.terminal.state_store import JsonTerminalStateStore
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo, detect_platform_shell

logger = logging.getLogger(__name__)


def _family_from_name(name: str) -> ShellFamily:
    """Map a shell name string to ShellFamily enum."""
    mapping: dict[str, ShellFamily] = {
        "bash": ShellFamily.BASH,
        "zsh": ShellFamily.ZSH,
        "sh": ShellFamily.SH,
        "cmd": ShellFamily.CMD,
    }
    return mapping.get(name.lower(), ShellFamily.SH)


from framework.tools.terminal.managers import TerminalManagerBase

class TerminalManager(TerminalManagerBase):
    """Manages named terminal sessions with LRU eviction and JSON persistence.

    Responsibilities:
    - Named session collection (name -> TerminalSession)
    - Default terminal for CommandTool
    - LRU eviction when max_terminals exceeded
    - Lazy alive detection (check only on use)
    - Persist/restore session metadata and history
    """

    def __init__(
        self,
        storage_dir: Path | str = "data/terminals",
        max_terminals: int = 5,
        history_count: int = 5,
        history_truncate: int = 200,
        default_timeout: float = 60.0,
        backend_factory: Callable[[], Any] | None = None,
        shell_info: ShellInfo | None = None,
    ):
        self._storage_dir = Path(storage_dir)
        self._max_terminals = max_terminals
        self._history_count = history_count
        self._history_truncate = history_truncate
        self._default_timeout = default_timeout
        self._sessions: dict[str, TerminalSession] = {}
        self._default_terminal: str | None = None
        self._store = JsonTerminalStateStore(self._storage_dir)
        self._shell_info = shell_info or detect_platform_shell() or ShellInfo(
            family=ShellFamily.BASH,
            path="bash",
            platform=Platform.LINUX,
        )
        self._backend_factory: Callable[..., Any] = backend_factory or create_pty_backend

    async def get_or_create(
        self, name: str, cwd: str | None = None
    ) -> TerminalSession:
        """Get existing session or create a new one. Evicts LRU if at capacity."""
        if name in self._sessions:
            session = self._sessions[name]
            session.last_active = time.time()
            return session

        # Evict oldest if at capacity
        if len(self._sessions) >= self._max_terminals:
            await self._evict_oldest()

        backend = self._backend_factory()
        session = TerminalSession(
            name=name,
            backend=backend,
            shell_info=self._shell_info,
            cwd=cwd,
            max_history=self._history_count,
            history_truncate=self._history_truncate,
        )
        self._sessions[name] = session
        if self._default_terminal is None:
            self._default_terminal = name
        logger.info("Created terminal session: %s", name)
        return session

    def get(self, name: str) -> TerminalSession | None:
        """Get session by name without creating."""
        return self._sessions.get(name)

    async def close(self, name: str) -> bool:
        """Close a session. Returns True if existed."""
        session = self._sessions.pop(name, None)
        if session is None:
            return False
        await session.close()
        if self._default_terminal == name:
            self._default_terminal = next(iter(self._sessions), None)
        logger.info("Closed terminal session: %s", name)
        return True

    async def list_sessions(self) -> list[TerminalInfo]:
        """List all sessions with metadata.

        Only purges sessions whose backend was explicitly started and then
        died. A session whose backend has never been started is kept alive
        so that OPEN → LIST shows the tab before the first command runs.
        """
        alive_names: list[str] = []
        for name, session in list(self._sessions.items()):
            if await session._backend.is_alive():
                alive_names.append(name)
            elif getattr(session, "_backend_started", False):
                # Backend was started but has since died — purge it.
                self._sessions.pop(name, None)
                if self._default_terminal == name:
                    self._default_terminal = None
            else:
                # Backend never started — keep the session.
                alive_names.append(name)

        result: list[TerminalInfo] = []
        for name in alive_names:
            session = self._sessions[name]
            info = await session.to_info(is_default=(name == self._default_terminal))
            result.append(info)
        return result

    def list_names(self) -> list[str]:
        """Return just the session names."""
        return list(self._sessions.keys())

    async def select_default(self, name: str) -> None:
        """Select the default terminal for CommandTool."""
        session = self._sessions.get(name)
        if session is None:
            raise ValueError(f"Terminal '{name}' does not exist")
        if (
            getattr(session, "_backend_started", False)
            and not await session._backend.is_alive()
        ):
            raise ValueError(f"Terminal '{name}' has been closed")
        self._default_terminal = name

    async def get_default_session(self) -> TerminalSession | None:
        """Get the explicitly-set default session.

        Returns None if no default is set or the default has been explicitly
        closed (backend started then died).  An unstarted session is kept so
        that callers can lazily start it on first execute().
        """
        if not self._default_terminal:
            return None
        session = self._sessions.get(self._default_terminal)
        if session is None:
            self._default_terminal = None
            return None
        if (
            getattr(session, "_backend_started", False)
            and not await session._backend.is_alive()
        ):
            self._default_terminal = None
            return None
        return session

    async def get_default(self) -> TerminalSession:
        """Return the default session, creating one if needed.

        Compatible with TerminalManagerProtocol for CommandTool/ProcessTool.
        """
        session = await self.get_default_session()
        if session is None:
            return await self.get_or_create("default")
        return session

    def get_history(self, name: str) -> list[CommandRecord]:
        """Get command history for a session."""
        session = self._sessions.get(name)
        if session is None:
            return []
        return session.get_history()

    async def save_state(self) -> None:
        """Persist session metadata and history to JSON."""
        sessions_data = [session.get_state() for session in self._sessions.values()]
        state = {
            "version": 1,
            "default_terminal": self._default_terminal,
            "sessions": sessions_data,
        }
        self._store.save(state)

    async def load_state(self) -> None:
        """Restore session metadata from JSON. Sessions are lazily restarted on use."""
        data = self._store.load()
        if not data:
            return

        for sess_data in data.get("sessions", []):
            name = sess_data["name"]
            shell_type = sess_data.get("shell_type", self._shell_info.name)
            shell_path = sess_data.get("shell_path", self._shell_info.path)
            backend = self._backend_factory()
            family = _family_from_name(shell_type)
            session = TerminalSession(
                name=name,
                backend=backend,
                shell_info=ShellInfo(
                    family=family,
                    path=shell_path,
                    platform=self._shell_info.platform,
                ),
                cwd=sess_data.get("cwd"),
                env=sess_data.get("env"),
                max_history=self._history_count,
                history_truncate=self._history_truncate,
            )
            session.restore_state(sess_data)
            self._sessions[name] = session

        self._default_terminal = data.get("default_terminal")
        if self._default_terminal not in self._sessions:
            self._default_terminal = next(iter(self._sessions), None)
        logger.info("Loaded %d terminal sessions from state", len(self._sessions))

    async def _evict_oldest(self) -> None:
        """Close the least recently used session."""
        if not self._sessions:
            return
        oldest_name = min(self._sessions, key=lambda n: self._sessions[n].last_active)
        logger.info("LRU evicting terminal: %s", oldest_name)
        await self.close(oldest_name)

    async def close_all(self) -> None:
        """Close all sessions."""
        for name in list(self._sessions.keys()):
            await self.close(name)
