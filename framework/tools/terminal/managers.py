"""BaseTerminalManager — lightweight multi-session orchestration with fake backends."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.session import TerminalInfo, TerminalSession
from framework.tools.terminal.types import Platform, ShellInfo, TerminalVisibility


class TerminalManagerProtocol(Protocol):
    platform: Platform
    shell_info: ShellInfo
    visibility: TerminalVisibility

    async def get_or_create(self, name: str | None, workdir: str | None = None) -> TerminalSession: ...
    async def get_default(self) -> TerminalSession: ...
    async def select_default(self, name: str) -> None: ...
    async def list_sessions(self) -> list[TerminalInfo]: ...
    async def close(self, name: str) -> bool: ...


class BaseTerminalManager:
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
        backend_factory: Callable[[], object],
        config: TerminalRuntimeConfig | None = None,
    ) -> None:
        self.platform = shell_info.platform
        self.shell_info = shell_info
        self.visibility = visibility
        self.config = config or TerminalRuntimeConfig()
        self._backend_factory = backend_factory
        self._sessions: dict[str, TerminalSession] = {}
        self._default_name: str | None = None

    async def get_or_create(self, name: str | None, workdir: str | None = None) -> TerminalSession:
        session_name = name or "default"
        session = self._sessions.get(session_name)
        if session is not None:
            return session
        backend = self._backend_factory()
        session = TerminalSession(
            name=session_name,
            backend=backend,
            shell_info=self.shell_info,
            cwd=workdir,
        )
        self._sessions[session_name] = session
        if self._default_name is None:
            self._default_name = session_name
        return session

    async def get_default(self) -> TerminalSession:
        if self._default_name is None:
            return await self.get_or_create("default")
        return self._sessions[self._default_name]

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
