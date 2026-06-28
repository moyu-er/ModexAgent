"""Memory-pressure buffer clearing — flag-guarded; lean form never invokes it."""

from __future__ import annotations

import pytest

from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.managers import BaseTerminalManager
from modex_agent.tools.terminal.results import SlidingOutputBuffer
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility

_TEST_SHELL_INFO = ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX)


class _FakeBackendHasBuffer:
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        self._output_buffer = SlidingOutputBuffer(max_chars=10_000)
        self._output_buffer.append("x" * 50_000)

    async def is_alive(self) -> bool:
        # TerminalSession.to_info() (invoked by list_sessions) requires this.
        return True


def _make_mgr(*, enable_memory_pressure: bool) -> BaseTerminalManager:
    # Tiny threshold so the ~10k-char buffers actually exceed it and trigger clearing.
    config = TerminalRuntimeConfig(max_total_buffer_chars=100)
    return BaseTerminalManager(
        shell_info=_TEST_SHELL_INFO,
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_FakeBackendHasBuffer,
        enable_memory_pressure=enable_memory_pressure,
        config=config,
    )


@pytest.mark.asyncio
async def test_lean_form_does_not_clear_buffers() -> None:
    mgr = _make_mgr(enable_memory_pressure=False)
    session = await mgr.get_or_create(name="s1")
    before = session._backend._output_buffer.total_chars
    await mgr.list_sessions()
    after = session._backend._output_buffer.total_chars
    assert before == after, "Buffer should not be touched when enable_memory_pressure=False"


@pytest.mark.asyncio
async def test_memory_pressure_clears_non_default_buffers() -> None:
    mgr = _make_mgr(enable_memory_pressure=True)
    await mgr.get_or_create(name="default")
    other = await mgr.get_or_create(name="other")
    # get_or_create auto-selects the newest tab as default; explicitly restore
    # 'default' so 'other' is unambiguously the non-default session whose
    # buffer should be cleared under pressure.
    await mgr.select_default("default")
    before = other._backend._output_buffer.total_chars
    assert before > 0
    await mgr.list_sessions()
    after = other._backend._output_buffer.total_chars
    assert after == 0, "Non-default buffer should be cleared under memory pressure"
