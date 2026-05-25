"""Tests for TerminalBackend ABC."""

import pytest

from framework.tools.terminal.backends.base import TerminalBackend
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, TerminalVisibility


class DummyBackend(TerminalBackend):
    platform = Platform.LINUX
    visibility = TerminalVisibility.HIDDEN

    async def start(self, shell: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        pass

    async def write(self, data: str) -> None:
        pass

    async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
        return TerminalRead()

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="$ ")

    async def interrupt(self) -> None:
        pass

    async def is_alive(self) -> bool:
        return True

    async def terminate(self) -> None:
        pass

    async def kill(self) -> None:
        pass

    async def drain_startup(self) -> None:
        pass

    async def clear_input_line(self) -> None:
        pass

    def stdin_writable(self) -> bool:
        return True


class TestTerminalBackend:
    def test_can_instantiate_concrete_subclass(self) -> None:
        backend = DummyBackend()
        assert isinstance(backend, TerminalBackend)

    def test_backend_has_window_title_property(self) -> None:
        backend = DummyBackend()
        assert backend.window_title is None
