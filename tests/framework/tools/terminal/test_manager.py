"""Tests for TerminalManager."""

import tempfile
from pathlib import Path

import pytest

from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo


class MockBackend:
    """Mock TerminalBackend for testing TerminalManager."""

    def __init__(self) -> None:
        self.alive = True
        self.buffer = ""
        self._started = False
        self._backend_started = True

    async def start(self, shell=None, cwd=None, env=None) -> None:
        self._started = True

    async def write(self, data) -> None:
        self.buffer += data

    async def read(self, timeout=5.0, max_size=65536) -> str:
        return "mock-output\n$ "

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


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as td:
        tm = TerminalManager(
            storage_dir=Path(td),
            max_terminals=3,
            history_count=2,
            history_truncate=50,
            backend_factory=MockBackend,
        )
        yield tm


class TestTerminalManager:
    @pytest.mark.asyncio
    async def test_get_or_create_creates_session(self, manager):
        session = await manager.get_or_create("tab-1")
        assert session.name == "tab-1"
        assert "tab-1" in manager.list_names()

    @pytest.mark.asyncio
    async def test_lru_eviction(self, manager):
        """Max 3 terminals; creating 4 evicts the least recently used."""
        s1 = await manager.get_or_create("tab-1")
        s2 = await manager.get_or_create("tab-2")
        s3 = await manager.get_or_create("tab-3")

        # Touch tab-1 to make it recently used
        s1.last_active = 9999999999.0

        # Create tab-4, should evict tab-2 (oldest last_active)
        s4 = await manager.get_or_create("tab-4")
        assert "tab-1" in manager.list_names()
        assert "tab-2" not in manager.list_names()
        assert "tab-3" in manager.list_names()
        assert "tab-4" in manager.list_names()

    @pytest.mark.asyncio
    async def test_select_default(self, manager):
        await manager.get_or_create("tab-1")
        await manager.get_or_create("tab-2")
        await manager.select_default("tab-2")
        default_session = await manager.get_default_session()
        assert default_session is not None
        assert default_session.name == "tab-2"

    @pytest.mark.asyncio
    async def test_close_removes_session(self, manager):
        await manager.get_or_create("tab-1")
        result = await manager.close("tab-1")
        assert result is True
        assert "tab-1" not in manager.list_names()

    @pytest.mark.asyncio
    async def test_persistence(self, manager):
        await manager.get_or_create("tab-1")
        await manager.save_state()

        # Create new manager pointing to same directory
        manager2 = TerminalManager(
            storage_dir=manager._storage_dir,
            max_terminals=3,
            backend_factory=MockBackend,
        )
        await manager2.load_state()
        assert "tab-1" in manager2.list_names()

    @pytest.mark.asyncio
    async def test_persistence_restores_sessions(self):
        """load_state must restore sessions from saved state."""
        with tempfile.TemporaryDirectory() as td:
            tm = TerminalManager(
                storage_dir=Path(td),
                max_terminals=3,
                backend_factory=MockBackend,
            )
            session = await tm.get_or_create("tab-1")
            assert session.name == "tab-1"
            await tm.save_state()

            # Load into new manager
            tm2 = TerminalManager(
                storage_dir=Path(td),
                max_terminals=3,
                backend_factory=MockBackend,
            )
            await tm2.load_state()

            assert "tab-1" in tm2.list_names()

    @pytest.mark.asyncio
    async def test_default_not_overwritten_on_get(self, manager):
        """get_or_create on existing session should NOT change default."""
        await manager.get_or_create("tab-1")
        await manager.select_default("tab-1")
        await manager.get_or_create("tab-2")
        default_session = await manager.get_default_session()
        assert default_session is not None
        assert default_session.name == "tab-1"

    @pytest.mark.asyncio
    async def test_lru_eviction_default_reassigns(self, manager):
        """When default terminal is evicted, default falls back to remaining session."""
        s1 = await manager.get_or_create("tab-1")
        s2 = await manager.get_or_create("tab-2")
        s3 = await manager.get_or_create("tab-3")
        await manager.select_default("tab-1")

        # Make tab-1 the oldest so it gets evicted
        s1.last_active = 0.0
        s4 = await manager.get_or_create("tab-4")

        # Default should have been reassigned since tab-1 was evicted
        assert "tab-1" not in manager.list_names()
        default = await manager.get_default_session()
        assert default is not None
        assert default.name != "tab-1"

    @pytest.mark.asyncio
    async def test_list_sessions_returns_terminal_info(self, manager):
        await manager.get_or_create("tab-1")
        sessions = await manager.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].name == "tab-1"
        assert hasattr(sessions[0], "is_alive")
        assert hasattr(sessions[0], "is_default")

    @pytest.mark.asyncio
    async def test_get_default_returns_none_when_default_exernally_killed(self, manager):
        """When the default session dies externally, get_default_session should
        return None so the executor creates a new default instead of falling
        back to an unrelated tab."""
        s1 = await manager.get_or_create("tab-1")
        await manager.get_or_create("tab-2")
        await manager.select_default("tab-1")

        # Simulate external kill (user closes the window)
        # Backend was started, then died.
        s1._backend_started = True
        s1._backend.alive = False

        # Should return None -- the default is dead, don't silently switch
        default = await manager.get_default_session()
        assert default is None, (
            f"Expected None when default is externally killed, got {default}"
        )

    @pytest.mark.asyncio
    async def test_list_sessions_filters_externally_killed(self, manager):
        """Sessions that died externally should not appear in list results."""
        s1 = await manager.get_or_create("tab-1")
        await manager.get_or_create("tab-2")

        # Simulate external kill (backend started, then died)
        s1._backend_started = True
        s1._backend.alive = False

        sessions = await manager.list_sessions()
        names = {s.name for s in sessions}
        assert "tab-1" not in names, "Dead session should be filtered from list"
        assert "tab-2" in names, "Alive session should still be listed"

    @pytest.mark.asyncio
    async def test_select_default_on_externally_killed_tab_raises(self, manager):
        """Selecting a tab that was killed externally should raise ValueError."""
        s1 = await manager.get_or_create("tab-1")

        # Simulate external kill (backend started, then died)
        s1._backend_started = True
        s1._backend.alive = False

        with pytest.raises(ValueError, match="has been closed"):
            await manager.select_default("tab-1")

    @pytest.mark.asyncio
    async def test_shell_creates_new_default_after_default_killed(self, manager):
        """Full integration: default killed, shell command creates new default
        instead of falling back to another existing tab."""
        from framework.tools.terminal.tool import TerminalTool
        from framework.tools.standard.shell_tool import TerminalSessionExecutor

        tool = TerminalTool(manager)
        executor = TerminalSessionExecutor(terminal_manager=manager)

        # Open tab-1 as default
        await tool.execute(action="open", name="tab-1")
        await manager.select_default("tab-1")

        # Open tab-2 (non-default)
        await tool.execute(action="open", name="tab-2")

        # Simulate external kill of default (backend started, then died)
        tab1 = manager.get("tab-1")
        assert tab1 is not None
        tab1._backend_started = True
        tab1._backend.alive = False

        # Shell execute should create a NEW "default" tab, not reuse tab-2
        result = await executor.execute("echo hello")
        default = await manager.get_default_session()
        assert default is not None
        assert default.name == "default", (
            f"Expected new 'default' tab created, got {default.name}"
        )
