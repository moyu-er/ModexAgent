"""BaseTerminalManager LRU — flag-guarded; lean form does not evict."""

from __future__ import annotations

import pytest

from modex_agent.tools.terminal.managers import BaseTerminalManager
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility

_TEST_SHELL_INFO = ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX)


class _FakeBackend:
    """Stand-in backend; only identity / generic attrs used by LRU path."""
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        self.last_active = 0.0

    async def terminate(self) -> None:
        """Minimal close hook — the LRU eviction path calls session.terminate()."""
        return None


def _make_mgr(max_terminals: int | None) -> BaseTerminalManager:
    return BaseTerminalManager(
        shell_info=_TEST_SHELL_INFO,
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_FakeBackend,
        max_terminals=max_terminals,
    )


@pytest.mark.asyncio
async def test_lean_form_does_not_evict_when_capacity_unset() -> None:
    mgr = _make_mgr(max_terminals=None)
    for i in range(20):
        await mgr.get_or_create(name=f"s{i}")
    assert len(mgr.list_names()) == 20


@pytest.mark.asyncio
async def test_lru_evicts_oldest_when_at_capacity() -> None:
    mgr = _make_mgr(max_terminals=3)
    await mgr.get_or_create(name="a")
    await mgr.get_or_create(name="b")
    await mgr.get_or_create(name="c")  # max=3 now full
    await mgr.get_or_create(name="d")  # 4th evicts the least-recently-used ('a')
    names = sorted(mgr.list_names())
    assert "a" not in names, f"'a' should have been evicted; got {names}"
    assert names == ["b", "c", "d"]
