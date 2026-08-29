"""Lock the empty-evidence gate against false interactive-hint regressions.

The user-reported defect was a normally finishing zero-output raw-mode command
being returned early as an interactive session. These tests pin both the pure
command-window evidence rule and its real PTY behavior.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from time import monotonic

import pytest

import modex_agent.tools.terminal._persistent_session as session_mod
from modex_agent.tools.terminal._foreground_probe import stdin_probe_available
from modex_agent.tools.terminal._persistent_session import (
    _PendingWait,
    _Phase,
    _RollingBuffer,
)
from modex_agent.tools.terminal.persistent_bash import BashInputTool, PersistentBashTool

_HAS_PERSISTENT_BASH = (
    sys.platform != "win32"
    and shutil.which("bash") is not None
    and importlib.util.find_spec("pexpect") is not None
)
_PTY_SKIP = pytest.mark.skipif(
    not _HAS_PERSISTENT_BASH,
    reason="persistent bash requires POSIX pexpect + /bin/bash",
)
_START_TOKEN = "__MODEX_START_deadbeef__"


def _pending() -> _PendingWait:
    return _PendingWait(
        end_re=re.compile(r"__MODEX_END_deadbeef:(\d+)"),
        start_token=_START_TOKEN,
        deadline=None,
    )


def test_has_real_output_empty_buffer() -> None:
    accum = _RollingBuffer()

    assert session_mod._has_real_output(accum, _pending()) is False


def test_has_real_output_start_token_only() -> None:
    accum = _RollingBuffer()
    accum.append(_START_TOKEN)

    assert session_mod._has_real_output(accum, _pending()) is False


def test_has_real_output_start_token_with_body_text() -> None:
    accum = _RollingBuffer()
    accum.append(f"{_START_TOKEN}\nbody")

    assert session_mod._has_real_output(accum, _pending()) is True


def test_has_real_output_start_token_with_whitespace_only_body() -> None:
    accum = _RollingBuffer()
    accum.append(f"{_START_TOKEN}\n \t\r\n")

    assert session_mod._has_real_output(accum, _pending()) is False


def test_has_real_output_dropped_head_with_residue() -> None:
    accum = _RollingBuffer()
    accum.append("residue")
    accum._dropped = True  # noqa: SLF001 - simulate a lost START boundary

    assert session_mod._has_real_output(accum, _pending()) is True


def test_has_real_output_pre_start_noise_only() -> None:
    accum = _RollingBuffer()
    accum.append("earlier transaction noise")

    assert session_mod._has_real_output(accum, _pending()) is False


def test_has_real_output_pre_start_noise_and_start_only() -> None:
    accum = _RollingBuffer()
    accum.append(f"earlier transaction noise\n{_START_TOKEN}")

    assert session_mod._has_real_output(accum, _pending()) is False


def test_has_real_output_pre_start_noise_start_and_body() -> None:
    accum = _RollingBuffer()
    accum.append(f"earlier transaction noise\n{_START_TOKEN}\nowned body")

    assert session_mod._has_real_output(accum, _pending()) is True


@_PTY_SKIP
async def test_silent_raw_command_not_takeover() -> None:
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        started = monotonic()
        out = await tool.execute(
            command="python3 -c 'import tty, time; tty.setraw(0); time.sleep(1.0)'"
        )
        elapsed = monotonic() - started

        assert out == "[no output]"
        assert "[hint:" not in out
        assert elapsed >= 0.9
        assert await tool.execute(command="echo ok") == "ok"
    finally:
        await tool.close()


@_PTY_SKIP
@pytest.mark.skipif(
    stdin_probe_available(), reason="takeover gate fallback is probe-less only"
)
async def test_takeover_fires_with_real_output_probeless() -> None:
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        out = await tool.execute(
            command=(
                "printf 'user@host:~$ '; "
                "python3 -c 'import tty, time; tty.setraw(0); time.sleep(8)'"
            )
        )

        assert "[hint:" in out
        assert "interactive shell" in out
        assert tool.session._phase is _Phase.WAITING  # noqa: SLF001
        assert tool.session._proc is not None  # noqa: SLF001
        assert tool.session._proc.isalive()  # noqa: SLF001
    finally:
        await tool.close()


@_PTY_SKIP
async def test_shared_suffix_layer_preempts_probeless_weak_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_mod, "stdin_probe_available", lambda: False)
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="echo 'stuck? '; sleep 3; echo tail")

        # The shared Layer-2 union fires at the normal settle window; ADR-0045's
        # extended probeless window now governs only session-local weak shapes.
        assert "stuck?" in out
        assert "tail" not in out
        assert "[hint:" in out
        assert "[hint:" not in await bash_input.execute(line="^C")
        assert await tool.execute(command="echo healthy") == "healthy"
    finally:
        await tool.close()


@_PTY_SKIP
async def test_custom_prompt_still_hinted_probeless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_mod, "stdin_probe_available", lambda: False)
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        started = monotonic()
        out = await tool.execute(command="read -p 'name: ' X; echo got=$X")
        elapsed = monotonic() - started

        assert "[hint:" in out
        assert elapsed < 4.0
        assert (await bash_input.execute(line="val")).strip() == "got=val"
    finally:
        await tool.close()
