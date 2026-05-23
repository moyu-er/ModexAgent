"""Tests for TerminalManager."""

import tempfile
from pathlib import Path

import pytest

from framework.tools.terminal.manager import TerminalManager


class MockBackend:
    """Mock TerminalBackend for testing TerminalManager."""

    def __init__(self):
        self.alive = True
        self.buffer = ""
        self._started = False

    async def start(self, shell=None, cwd=None, env=None):
        self._started = True

    async def write(self, data):
        self.buffer += data

    async def read(self, timeout=5.0, max_size=65536):
        return "mock-output\n$ "

    async def is_alive(self):
        return self.alive

    async def terminate(self):
        self.alive = False

    async def kill(self):
        self.alive = False


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
        manager.select_default("tab-2")
        assert manager.get_default_session().name == "tab-2"

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
    async def test_default_not_overwritten_on_get(self, manager):
        """get_or_create on existing session should NOT change default."""
        await manager.get_or_create("tab-1")
        manager.select_default("tab-1")
        await manager.get_or_create("tab-2")
        assert manager.get_default_session().name == "tab-1"

    @pytest.mark.asyncio
    async def test_lru_eviction_default_reassigns(self, manager):
        """When default terminal is evicted, default falls back to remaining session."""
        s1 = await manager.get_or_create("tab-1")
        s2 = await manager.get_or_create("tab-2")
        s3 = await manager.get_or_create("tab-3")
        manager.select_default("tab-1")

        # Make tab-1 the oldest so it gets evicted
        s1.last_active = 0.0
        s4 = await manager.get_or_create("tab-4")

        # Default should have been reassigned since tab-1 was evicted
        assert "tab-1" not in manager.list_names()
        default = manager.get_default_session()
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
