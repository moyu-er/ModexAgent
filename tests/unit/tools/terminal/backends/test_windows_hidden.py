"""Unit tests for ``WinptyHiddenBackend`` — ADR-0032 D3 contract activation.

Exercises the three opt-in hooks (``_write_blocking`` / ``_read_blocking`` /
``_shell_family``) and asserts the six base-class behaviors (``write``,
``read``, ``read_pending``, ``current_segment``, ``clear_input_line``,
``drain_startup``) plus the ``_uses_readline`` private helper are no longer
overridden on this backend — the base-class template methods own them.

Mock-based: ``backend._proc`` is a ``MagicMock``; no real ``winpty`` is
imported, so the suite runs on any platform (``WinptyHiddenBackend.__init__``
defers the ``winpty`` import to ``start()``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.tools.terminal.backends.windows_hidden import WinptyHiddenBackend
from modex_agent.tools.terminal.types import ShellFamily


def _make_backend() -> WinptyHiddenBackend:
    """Construct a backend without calling ``start()`` (no real winpty needed)."""
    return WinptyHiddenBackend()


def _make_backend_with_proc() -> tuple[WinptyHiddenBackend, MagicMock]:
    """Construct a backend with a mocked ``_proc`` for I/O hook tests."""
    backend = _make_backend()
    mock_proc: MagicMock = MagicMock()
    backend._proc = mock_proc
    return backend, mock_proc


# ── _write_blocking hook ──


def test_write_blocking_writes_through_to_proc() -> None:
    """``_write_blocking`` calls ``proc.write`` with the given data verbatim."""
    backend, mock_proc = _make_backend_with_proc()
    backend._write_blocking("echo hi\r")
    mock_proc.write.assert_called_once_with("echo hi\r")


def test_write_blocking_raises_when_not_started() -> None:
    """``_write_blocking`` raises ``RuntimeError`` when ``_proc`` is ``None``."""
    backend = _make_backend()
    with pytest.raises(RuntimeError, match="PTY not started"):
        backend._write_blocking("anything")


# ── _read_blocking hook ──


def test_read_blocking_returns_decoded_data() -> None:
    """``_read_blocking`` decodes bytes from ``fileobj.recv`` into a string."""
    backend, mock_proc = _make_backend_with_proc()
    mock_proc.fileobj.recv.return_value = b"hello"
    result = backend._read_blocking(timeout=0.5, max_size=1024)
    assert result == "hello"
    mock_proc.fileobj.settimeout.assert_called_once_with(0.5)
    mock_proc.fileobj.recv.assert_called_once_with(1024)


def test_read_blocking_returns_empty_on_timeout() -> None:
    """``_read_blocking`` swallows ``TimeoutError`` and returns empty string."""
    backend, mock_proc = _make_backend_with_proc()
    mock_proc.fileobj.recv.side_effect = TimeoutError
    assert backend._read_blocking(timeout=0.1, max_size=64) == ""


def test_read_blocking_returns_empty_on_oserror() -> None:
    """``_read_blocking`` swallows ``OSError`` and returns empty string."""
    backend, mock_proc = _make_backend_with_proc()
    mock_proc.fileobj.recv.side_effect = OSError("boom")
    assert backend._read_blocking(timeout=0.1, max_size=64) == ""


def test_read_blocking_raises_when_not_started() -> None:
    """``_read_blocking`` raises ``RuntimeError`` when ``_proc`` is ``None``."""
    backend = _make_backend()
    with pytest.raises(RuntimeError, match="PTY not started"):
        backend._read_blocking(timeout=0.1, max_size=64)


# ── _shell_family hook ──


@pytest.mark.parametrize(
    "shell_path, expected",
    [
        ("/usr/bin/bash", ShellFamily.BASH),
        ("/usr/bin/zsh", ShellFamily.ZSH),
        ("/bin/sh", ShellFamily.SH),
        ("bash", ShellFamily.BASH),
        ("zsh", ShellFamily.ZSH),
        ("sh", ShellFamily.SH),
        ("cmd.exe", ShellFamily.CMD),
    ],
)
def test_shell_family_detected_from_path(shell_path: str, expected: ShellFamily) -> None:
    """``_shell_family`` returns the correct ``ShellFamily`` for known paths.

    Note: ``_family_from_path`` (in read-only ``types.py``) does not strip
    ``.exe`` suffixes, so ``bash.exe`` falls through to ``SH``. This test
    uses paths the production helper actually recognises.
    """
    backend = _make_backend()
    backend._shell = shell_path
    assert backend._shell_family() == expected


def test_shell_family_defaults_to_sh_when_shell_none() -> None:
    """``_shell_family`` falls back to ``SH`` when ``_shell`` is ``None``."""
    backend = _make_backend()
    backend._shell = None
    assert backend._shell_family() == ShellFamily.SH


def test_shell_family_defaults_to_sh_for_unknown_shell() -> None:
    """``_shell_family`` falls back to ``SH`` for unrecognized shell names."""
    backend = _make_backend()
    backend._shell = "/usr/local/bin/fish"
    assert backend._shell_family() == ShellFamily.SH


# ── Structural inheritance (the contract assertion) ──


def test_backend_does_not_override_write() -> None:
    """``write`` is inherited from ``TerminalBackend`` (the template method)."""
    assert "write" not in vars(WinptyHiddenBackend)


def test_backend_does_not_override_read() -> None:
    """``read`` is inherited from ``TerminalBackend``."""
    assert "read" not in vars(WinptyHiddenBackend)


def test_backend_does_not_override_read_pending() -> None:
    """``read_pending`` is inherited from ``TerminalBackend`` (the template method)."""
    assert "read_pending" not in vars(WinptyHiddenBackend)


def test_backend_does_not_override_current_segment() -> None:
    """``current_segment`` is inherited from ``TerminalBackend``."""
    assert "current_segment" not in vars(WinptyHiddenBackend)


def test_backend_does_not_override_clear_input_line() -> None:
    """``clear_input_line`` is inherited from ``TerminalBackend``."""
    assert "clear_input_line" not in vars(WinptyHiddenBackend)


def test_backend_does_not_override_drain_startup() -> None:
    """``drain_startup`` is inherited from ``TerminalBackend``."""
    assert "drain_startup" not in vars(WinptyHiddenBackend)


def test_backend_does_not_have_uses_readline_helper() -> None:
    """``_uses_readline`` private helper is deleted (replaced by ``_shell_family``)."""
    assert "_uses_readline" not in vars(WinptyHiddenBackend)


def test_backend_implements_write_blocking_hook() -> None:
    """``_write_blocking`` hook is implemented on this backend."""
    assert "_write_blocking" in vars(WinptyHiddenBackend)


def test_backend_implements_read_blocking_hook() -> None:
    """``_read_blocking`` hook is implemented on this backend."""
    assert "_read_blocking" in vars(WinptyHiddenBackend)


def test_backend_implements_shell_family_hook() -> None:
    """``_shell_family`` hook is implemented on this backend."""
    assert "_shell_family" in vars(WinptyHiddenBackend)


# ── Inherited clear_input_line behavior ──


async def test_clear_input_line_writes_ctrl_a_ctrl_k_for_bash() -> None:
    """Inherited ``clear_input_line`` sends ``\\x01\\x0b`` for readline shells (bash)."""
    backend, mock_proc = _make_backend_with_proc()
    backend._shell = "bash"
    await backend.clear_input_line()
    mock_proc.write.assert_called_once_with("\x01\x0b")


async def test_clear_input_line_writes_ctrl_a_ctrl_k_for_zsh() -> None:
    """Inherited ``clear_input_line`` sends ``\\x01\\x0b`` for readline shells (zsh)."""
    backend, mock_proc = _make_backend_with_proc()
    backend._shell = "/usr/bin/zsh"
    await backend.clear_input_line()
    mock_proc.write.assert_called_once_with("\x01\x0b")


async def test_clear_input_line_is_noop_for_cmd() -> None:
    """Inherited ``clear_input_line`` is a no-op for non-readline shells (cmd)."""
    backend, mock_proc = _make_backend_with_proc()
    backend._shell = "cmd.exe"
    await backend.clear_input_line()
    mock_proc.write.assert_not_called()
