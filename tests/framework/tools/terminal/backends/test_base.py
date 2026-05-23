"""Tests for TerminalBackend ABC."""

import pytest

from framework.tools.terminal.backends.base import TerminalBackend


class DummyBackend(TerminalBackend):
    async def start(self, shell: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        pass

    async def write(self, data: str) -> None:
        pass

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        return ""

    async def is_alive(self) -> bool:
        return True

    async def terminate(self) -> None:
        pass

    async def kill(self) -> None:
        pass


class TestTerminalBackend:
    def test_can_instantiate_concrete_subclass(self) -> None:
        backend = DummyBackend()
        assert isinstance(backend, TerminalBackend)
