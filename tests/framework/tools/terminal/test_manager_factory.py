"""Tests for create_terminal_manager factory and Windows manager subclasses."""

from __future__ import annotations

import pytest

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import (
    BaseTerminalManager,
    WindowsHiddenTerminalManager,
    WindowsVisibleTerminalManager,
    create_terminal_manager,
)
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility


# ---------------------------------------------------------------------------
# Fake backend for unit-testing BaseTerminalManager methods
# ---------------------------------------------------------------------------


class FakeBackend:
    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        self.started = False
        self.alive = False

    async def start(self, shell=None, cwd=None, env=None) -> None:
        self.started = True
        self.alive = True

    async def write(self, data: str) -> None:
        pass

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        return ""

    async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
        return TerminalRead()

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="")

    async def interrupt(self) -> None:
        pass

    async def is_alive(self) -> bool:
        return self.alive

    async def terminate(self) -> None:
        self.alive = False

    async def kill(self) -> None:
        self.alive = False

    async def drain_startup(self) -> None:
        pass

    async def clear_input_line(self) -> None:
        pass

    def stdin_writable(self) -> bool:
        return self.alive


def _make_fake_manager() -> BaseTerminalManager:
    return BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
    )


# ---------------------------------------------------------------------------
# Factory: valid kinds
# ---------------------------------------------------------------------------


def test_factory_returns_hidden_manager() -> None:
    manager = create_terminal_manager(manager_kind="windows_hidden")
    assert isinstance(manager, WindowsHiddenTerminalManager)
    assert isinstance(manager, BaseTerminalManager)
    assert manager.visibility is TerminalVisibility.HIDDEN


def test_factory_returns_visible_manager() -> None:
    manager = create_terminal_manager(manager_kind="windows_visible")
    assert isinstance(manager, WindowsVisibleTerminalManager)
    assert isinstance(manager, BaseTerminalManager)
    assert manager.visibility is TerminalVisibility.VISIBLE


# ---------------------------------------------------------------------------
# Factory: invalid kind
# ---------------------------------------------------------------------------


def test_factory_raises_on_unknown_kind() -> None:
    with pytest.raises(ValueError, match="Unsupported terminal manager kind"):
        create_terminal_manager(manager_kind="linux_bash")


# ---------------------------------------------------------------------------
# Factory: config passthrough
# ---------------------------------------------------------------------------


def test_factory_passes_custom_config() -> None:
    config = TerminalRuntimeConfig(default_yield_ms=999)
    manager = create_terminal_manager(manager_kind="windows_hidden", config=config)
    assert manager.config.default_yield_ms == 999


def test_factory_uses_default_config_when_none() -> None:
    manager = create_terminal_manager(manager_kind="windows_hidden", config=None)
    assert manager.config == TerminalRuntimeConfig()


# ---------------------------------------------------------------------------
# Factory: shell auto-detection
# ---------------------------------------------------------------------------


def test_factory_auto_detects_shell() -> None:
    manager = create_terminal_manager(manager_kind="windows_hidden")
    assert isinstance(manager.shell_info, ShellInfo)
    # Shell detection returns something on every platform
    assert manager.shell_info.family in list(ShellFamily)


# ---------------------------------------------------------------------------
# Subclass: WindowsHiddenTerminalManager
# ---------------------------------------------------------------------------


def test_hidden_manager_visibility() -> None:
    manager = WindowsHiddenTerminalManager()
    assert manager.visibility is TerminalVisibility.HIDDEN


def test_hidden_manager_platform_is_windows() -> None:
    manager = WindowsHiddenTerminalManager()
    assert manager.platform == Platform.WINDOWS


# ---------------------------------------------------------------------------
# Subclass: WindowsVisibleTerminalManager
# ---------------------------------------------------------------------------


def test_visible_manager_visibility() -> None:
    manager = WindowsVisibleTerminalManager()
    assert manager.visibility is TerminalVisibility.VISIBLE


def test_visible_manager_platform_is_windows() -> None:
    manager = WindowsVisibleTerminalManager()
    assert manager.platform == Platform.WINDOWS


# ---------------------------------------------------------------------------
# Subclass: custom config propagation
# ---------------------------------------------------------------------------


def test_hidden_manager_with_custom_config() -> None:
    config = TerminalRuntimeConfig(default_command_timeout_seconds=30)
    manager = WindowsHiddenTerminalManager(config=config)
    assert manager.config.default_command_timeout_seconds == 30


def test_visible_manager_with_custom_config() -> None:
    config = TerminalRuntimeConfig(max_output_chars=5000)
    manager = WindowsVisibleTerminalManager(config=config)
    assert manager.config.max_output_chars == 5000


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_factory_result_satisfies_protocol() -> None:
    """Manager returned by factory has all TerminalManagerProtocol attributes."""
    manager = create_terminal_manager(manager_kind="windows_hidden")
    assert hasattr(manager, "platform")
    assert hasattr(manager, "shell_info")
    assert hasattr(manager, "visibility")
    assert hasattr(manager, "get_or_create")
    assert hasattr(manager, "get_default")
    assert hasattr(manager, "select_default")
    assert hasattr(manager, "list_sessions")
    assert hasattr(manager, "close")


# ---------------------------------------------------------------------------
# BaseTerminalManager: get / get_default_session / list_names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_manager_get_returns_session() -> None:
    manager = _make_fake_manager()
    session = await manager.get_or_create("test-tab")
    assert manager.get("test-tab") is session
    assert manager.get("nonexistent") is None


@pytest.mark.asyncio
async def test_base_manager_get_default_session_returns_none_initially() -> None:
    manager = _make_fake_manager()
    assert await manager.get_default_session() is None


@pytest.mark.asyncio
async def test_base_manager_get_default_session_after_get_default() -> None:
    manager = _make_fake_manager()
    session = await manager.get_default()
    # Session backend is not started, so get_default_session should still
    # return it (only started+dead sessions are evicted).
    result = await manager.get_default_session()
    assert result is session


@pytest.mark.asyncio
async def test_base_manager_get_default_session_evicts_dead() -> None:
    """Started-then-died session should be evicted from default."""
    manager = _make_fake_manager()
    session = await manager.get_default()
    await session.ensure_started()
    backend: FakeBackend = session._backend  # type: ignore[assignment]
    backend.alive = False
    result = await manager.get_default_session()
    assert result is None


@pytest.mark.asyncio
async def test_base_manager_list_names() -> None:
    manager = _make_fake_manager()
    assert manager.list_names() == []
    await manager.get_or_create("tab-1")
    await manager.get_or_create("tab-2")
    assert set(manager.list_names()) == {"tab-1", "tab-2"}


@pytest.mark.asyncio
async def test_base_manager_get_or_create_uses_cwd_parameter() -> None:
    """get_or_create accepts cwd (not workdir) keyword argument."""
    manager = _make_fake_manager()
    session = await manager.get_or_create("test", cwd="/tmp")
    assert session._cwd == "/tmp"
