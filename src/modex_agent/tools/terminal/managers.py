"""Terminal-manager seam — BaseTerminalManager + two-axis factory (ADR-0010)."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.backends.factory import (
    UnsupportedVisibilityForTransport,
    create_pty_backend,
)
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.session import TerminalInfo, TerminalSession
from modex_agent.tools.terminal.types import (
    ShellInfo,
    TerminalVisibility,
    detect_platform_shell,
)

logger = logging.getLogger(__name__)


class TerminalManagerBase(ABC):
    """Abstract seam for multi-session terminal managers.

    Under ADR-0010 there is a single production implementation,
    ``BaseTerminalManager``, parameterised by the two contract axes
    ``shell_info`` (carrying Shell Family + platform) x ``visibility`` plus
    optional capability flags (``max_terminals`` / ``enable_memory_pressure``).
    The legacy OS-named subclasses and the second
    ``TerminalManager`` class have been folded inward (ADR-0010 Decision 8,
    closing the ADR-0007 fork); the capability helpers are retained as
    flag-guarded private methods here.

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
    """Production terminal-session manager parameterised by two ADR-0010 axes.

    Manages named sessions with a pluggable ``backend_factory`` (real backends
    in production, fakes in tests). Role in the seam (see
    ``TerminalManagerBase``): the single production implementation.
    Capability behaviours — LRU eviction and memory-pressure buffer clearing —
    are flag-guarded (``max_terminals`` / ``enable_memory_pressure``), both
    default-off, folded inward from the legacy ``TerminalManager`` per ADR-0010
    Decision 8.
    """

    def __init__(
        self,
        *,
        shell_info: ShellInfo,
        visibility: TerminalVisibility,
        backend_factory: Callable[[], TerminalBackend],
        config: TerminalRuntimeConfig | None = None,
        default_cwd: str | None = None,
        max_terminals: int | None = None,
        enable_memory_pressure: bool = True,
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
        self._enable_memory_pressure: bool = enable_memory_pressure

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
        """Return the default session, or None if none is set or it has been closed.

        A dead session is dropped from ``_sessions`` so that ``get_default``
        can create a fresh one instead of repeatedly trying to resurrect a
        backend the user manually killed (e.g. closing a visible window).
        """
        if self._default_name is None:
            return None
        session = self._sessions.get(self._default_name)
        if session is None:
            self._default_name = None
            return None
        # Only check alive if the backend was actually started, so unstarted
        # sessions (created but never used) are not incorrectly evicted.
        if session.backend_started and not await session.is_alive():
            self._sessions.pop(self._default_name, None)
            self._default_name = None
            return None
        return session

    async def get_default(self) -> TerminalSession:
        """Return the default session, creating a fresh one if the current is dead."""
        session = await self.get_default_session()
        if session is not None:
            return session
        return await self.get_or_create("default")

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
            size = session._backend.buffer_size()
            if size > 0:
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
            self._sessions[name]._backend.clear_buffer()
            logger.warning(
                "Memory pressure: cleared buffer for '%s' (was %d chars)", name, size
            )
            total_buffer -= size

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


def create_terminal_manager(
    *,
    shell_info: ShellInfo,
    visibility: TerminalVisibility,
    config: TerminalRuntimeConfig | None = None,
    default_cwd: str | None = None,
    max_terminals: int | None = None,
    enable_memory_pressure: bool = True,
) -> TerminalManagerBase:
    """Construct a ``BaseTerminalManager`` parameterised by the two ADR-0010 axes.

    The two upstream contract axes (ADR-0010 Decision 1) are:

    - ``shell_info`` (carrying Shell Family + platform) — in production the
      caller derives it from ``detect_platform_shell()``.
    - ``visibility`` — ``VISIBLE`` or ``HIDDEN``.

    Eagerly validates the (transport, visibility) combo by probing
    ``create_pty_backend(visibility=visibility)`` BEFORE constructing the
    manager, so an unsupported combination surfaces here as
    ``UnsupportedVisibilityForTransport`` (ADR-0010 Consequences) rather than
    on the first command.

    Args:
        shell_info: detected platform shell (Shell Family + platform + path).
        visibility: ``VISIBLE`` or ``HIDDEN``.
        config/default_cwd/max_terminals/enable_memory_pressure:
            forwarded to ``BaseTerminalManager``; capability flags default-off.

    Returns:
        A ``TerminalManagerBase`` instance.
    """
    logger.info(
        "Creating terminal manager (family=%s, visibility=%s)",
        shell_info.family.value,
        visibility.value,
    )
    # Eager probe — surfaces UnsupportedVisibilityForTransport here (ADR-0010),
    # before the LAZY backend factory is ever invoked inside BaseTerminalManager.
    create_pty_backend(visibility=visibility)
    return BaseTerminalManager(
        shell_info=shell_info,
        visibility=visibility,
        backend_factory=_make_backend_factory(visibility),
        config=config,
        default_cwd=default_cwd,
        max_terminals=max_terminals,
        enable_memory_pressure=enable_memory_pressure,
    )


def create_terminal_manager_or_none(
    *,
    use_terminal: bool,
    terminal_visibility: bool,
    pool_name: str,
    default_cwd: str | None = None,
) -> TerminalManagerBase | None:
    """Pool terminal manager with the platform/backend fallback ladder.

    ``use_terminal`` governs exactly one thing (SPEC scope-assembly §5.1):
    whether the terminal manager — infrastructure — is built. The tool
    roster (``bash``/``process``/``terminal``) is assembled independently
    through the TOOL-slot factories; bash degrades to SubprocessTool when
    no manager exists, process/terminal raise.

    Ladder (real platform logic, not glue — not config-ized): shell
    detection -> requested-visibility attempt -> fallback visibility ->
    None (SubprocessTool-only) so the agent still works. Migrated from the
    BIZ ``builders._build_terminal_manager`` (ADR-0010 two-axis
    construction; ``terminal_visibility`` True -> VISIBLE, False ->
    HIDDEN).

    Args:
        use_terminal: ``False`` -> no manager (logged, info).
        terminal_visibility: requested visibility axis.
        pool_name: log-context label only.
        default_cwd: workspace default cwd for new sessions.

    Returns:
        A ``TerminalManagerBase``, or ``None`` when terminal support is
        disabled or every backend is unavailable on this platform.
    """
    if not use_terminal:
        logger.info("Pool '%s': use_terminal=false, skipping terminal tools", pool_name)
        return None

    shell_info = detect_platform_shell()
    if shell_info is None:
        logger.warning(
            "Pool '%s': no supported shell detected; falling back to SubprocessTool.",
            pool_name,
        )
        return None

    attempts: list[TerminalVisibility] = (
        [TerminalVisibility.VISIBLE, TerminalVisibility.HIDDEN]
        if terminal_visibility
        else [TerminalVisibility.HIDDEN]
    )

    last_err: Exception | None = None
    for vis in attempts:
        try:
            mgr = create_terminal_manager(
                shell_info=shell_info,
                visibility=vis,
                default_cwd=default_cwd,
            )
            logger.info(
                "Pool '%s': terminal manager created (family=%s, visibility=%s)",
                pool_name,
                shell_info.family.value,
                vis.value,
            )
            return mgr
        except UnsupportedVisibilityForTransport as exc:
            last_err = exc
            logger.warning(
                "Pool '%s': terminal backend (family=%s, visibility=%s) unavailable: %s",
                pool_name,
                shell_info.family.value,
                vis.value,
                exc,
            )
        except Exception as exc:
            last_err = exc
            logger.warning(
                "Pool '%s': terminal backend (family=%s, visibility=%s) failed: %s",
                pool_name,
                shell_info.family.value,
                vis.value,
                exc,
            )

    logger.error(
        "Pool '%s': ALL terminal backends failed (tried %s). Last error: %s. "
        "Falling back to SubprocessTool only.",
        pool_name,
        attempts,
        last_err,
    )
    return None


def _make_backend_factory(visibility: TerminalVisibility) -> Callable[[], TerminalBackend]:
    """Return a 0-arg callable producing a fresh backend for ``visibility``.

    The factory's capability table (ADR-0010) is the single source of truth;
    ``create_pty_backend`` selects the transport from platform + visibility.
    """

    def _factory() -> TerminalBackend:
        return create_pty_backend(visibility=visibility)

    return _factory

