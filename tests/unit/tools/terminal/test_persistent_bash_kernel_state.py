from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys

import pytest

from modex_agent.tools.terminal._persistent_session import (
    PersistentShellSession,
    _classify_terminal_state,
    _TerminalSignal,
    _with_hint,
)


@pytest.mark.parametrize(
    ("icanon_on", "shell_owns_foreground", "expected"),
    [
        (False, True, _TerminalSignal.SHELL_READLINE),
        (True, True, _TerminalSignal.SHELL_CANONICAL),
        (False, False, _TerminalSignal.CHILD_RAW),
        (True, False, _TerminalSignal.CHILD_CANONICAL),
    ],
)
def test_classify_terminal_state_truth_table(
    icanon_on: bool,
    shell_owns_foreground: bool,
    expected: _TerminalSignal,
) -> None:
    signal = _classify_terminal_state(icanon_on, shell_owns_foreground)

    assert signal is expected


def test_terminal_state_none_when_not_started() -> None:
    session = PersistentShellSession(timeout_seconds=10)

    signal = session._terminal_state()

    assert signal is None


@pytest.mark.skipif(
    sys.platform == "win32"
    or shutil.which("bash") is None
    or importlib.util.find_spec("pexpect") is None,
    reason="real PTY test requires POSIX pexpect + bash",
)
async def test_terminal_state_tracks_idle_shell_and_canonical_child() -> None:
    session = PersistentShellSession(timeout_seconds=10)
    try:
        await session.run_command("echo hi")

        assert session._terminal_state() is _TerminalSignal.SHELL_READLINE

        task = asyncio.create_task(session.run_command("sleep 2"))
        await asyncio.sleep(0.5)

        assert session._terminal_state() is _TerminalSignal.CHILD_CANONICAL
        await task
    finally:
        await session.close()


@pytest.mark.skipif(
    sys.platform == "win32"
    or shutil.which("bash") is None
    or importlib.util.find_spec("pexpect") is None,
    reason="real PTY test requires POSIX pexpect + bash",
)
async def test_terminal_state_none_on_termios_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios
    from typing import NoReturn

    def _raise_tcgetattr_error(_fd: int) -> NoReturn:
        raise termios.error("simulated tcgetattr descriptor race")

    session = PersistentShellSession(timeout_seconds=10)
    try:
        await session.run_command("echo hi")

        monkeypatch.setattr(termios, "tcgetattr", _raise_tcgetattr_error)
        assert session._terminal_state() is None
    finally:
        await session.close()


def test_with_hint_separator_contract() -> None:
    """Non-empty output gets a blank separator line before the hint; empty
    output yields the bare hint with no leading blank lines."""
    assert _with_hint("body", "[hint: x]") == "body\n\n[hint: x]"
    assert _with_hint("", "[hint: x]") == "[hint: x]"
