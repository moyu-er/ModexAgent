"""Unit tests for ``PexpectPtyBackend`` — ADR-0032 D3 contract activation.

Exercises the three opt-in hooks (``_write_blocking`` / ``_read_blocking`` /
``_shell_family``) and asserts the six base-class behaviors (``write``,
``read``, ``read_pending``, ``current_segment``, ``clear_input_line``,
``drain_startup``) plus the ``_uses_readline`` private helper are no longer
overridden on this backend — the base-class template methods own them.

Mock-based: ``backend._proc`` is a ``MagicMock`` (pexpect.spawn) and
``backend._pexpect`` is a ``MagicMock`` standing in for the lazily-imported
``pexpect`` module, so no real ``pexpect`` is imported and the suite runs on
any platform (``PexpectPtyBackend.__init__`` defers the ``pexpect`` import to
``start()``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from modex_agent.tools.terminal.backends.pexpect_pty import PexpectPtyBackend
from modex_agent.tools.terminal.types import ShellFamily


def _make_backend() -> PexpectPtyBackend:
    """Construct a backend without calling ``start()`` (no real pexpect needed)."""
    return PexpectPtyBackend()


def _make_pexpect_mock() -> MagicMock:
    """Build a MagicMock standing in for the ``pexpect`` module.

    ``pexpect.exceptions.TIMEOUT`` / ``EOF`` are real Exception subclasses so
    that ``MagicMock.side_effect = mock.exceptions.TIMEOUT`` actually raises
    something the production ``except`` clause can catch.
    """
    mock: MagicMock = MagicMock()
    mock.exceptions.TIMEOUT = type("TIMEOUT", (Exception,), {})
    mock.exceptions.EOF = type("EOF", (Exception,), {})
    return mock


def _make_backend_with_proc() -> tuple[PexpectPtyBackend, MagicMock, MagicMock]:
    """Construct a backend with mocked ``_proc`` and ``_pexpect`` for I/O hook tests."""
    backend = _make_backend()
    mock_proc: MagicMock = MagicMock()
    mock_pexpect: MagicMock = _make_pexpect_mock()
    backend._proc = mock_proc
    backend._pexpect = mock_pexpect
    return backend, mock_proc, mock_pexpect


# ── _write_blocking hook ──


def test_write_blocking_writes_through_to_proc() -> None:
    """``_write_blocking`` calls ``proc.send`` with the given data verbatim."""
    backend, mock_proc, _ = _make_backend_with_proc()
    backend._write_blocking("echo hi\r")
    mock_proc.send.assert_called_once_with("echo hi\r")


def test_write_blocking_raises_when_not_started() -> None:
    """``_write_blocking`` raises ``RuntimeError`` when ``_proc`` is ``None``."""
    backend = _make_backend()
    with pytest.raises(RuntimeError, match="PTY not started"):
        backend._write_blocking("anything")


# ── _read_blocking hook ──


def test_read_blocking_returns_data() -> None:
    """``_read_blocking`` returns the string from ``proc.read_nonblocking``."""
    backend, mock_proc, _ = _make_backend_with_proc()
    mock_proc.read_nonblocking.return_value = "hello"
    result = backend._read_blocking(timeout=0.5, max_size=1024)
    assert result == "hello"
    mock_proc.read_nonblocking.assert_called_once_with(1024, timeout=0.5)


def test_read_blocking_returns_empty_on_timeout() -> None:
    """``_read_blocking`` swallows ``pexpect.exceptions.TIMEOUT`` and returns ``""``."""
    backend, mock_proc, mock_pexpect = _make_backend_with_proc()
    mock_proc.read_nonblocking.side_effect = mock_pexpect.exceptions.TIMEOUT
    assert backend._read_blocking(timeout=0.1, max_size=64) == ""


def test_read_blocking_returns_empty_on_eof() -> None:
    """``_read_blocking`` swallows ``pexpect.exceptions.EOF`` and returns ``""``."""
    backend, mock_proc, mock_pexpect = _make_backend_with_proc()
    mock_proc.read_nonblocking.side_effect = mock_pexpect.exceptions.EOF
    assert backend._read_blocking(timeout=0.1, max_size=64) == ""


def test_read_blocking_raises_when_not_started() -> None:
    """``_read_blocking`` raises ``RuntimeError`` when ``_proc`` is ``None``."""
    backend = _make_backend()
    backend._pexpect = MagicMock()  # _pexpect loaded but _proc not started
    with pytest.raises(RuntimeError, match="PTY not started"):
        backend._read_blocking(timeout=0.1, max_size=64)


def test_read_blocking_raises_when_pexpect_not_loaded() -> None:
    """``_read_blocking`` raises ``RuntimeError`` when ``_pexpect`` is ``None``.

    Before ``start()`` runs, ``_pexpect`` is ``None`` — the hook must refuse
    rather than ``AttributeError`` on ``None.exceptions``.
    """
    backend = _make_backend()
    backend._proc = MagicMock()  # _proc set but _pexpect module not loaded
    backend._pexpect = None
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
    assert "write" not in vars(PexpectPtyBackend)


def test_backend_does_not_override_read() -> None:
    """``read`` is inherited from ``TerminalBackend``."""
    assert "read" not in vars(PexpectPtyBackend)


def test_backend_does_not_override_read_pending() -> None:
    """``read_pending`` is inherited from ``TerminalBackend`` (the template method)."""
    assert "read_pending" not in vars(PexpectPtyBackend)


def test_backend_does_not_override_current_segment() -> None:
    """``current_segment`` is inherited from ``TerminalBackend``."""
    assert "current_segment" not in vars(PexpectPtyBackend)


def test_backend_does_not_override_clear_input_line() -> None:
    """``clear_input_line`` is inherited from ``TerminalBackend``."""
    assert "clear_input_line" not in vars(PexpectPtyBackend)


def test_backend_does_not_override_drain_startup() -> None:
    """``drain_startup`` is inherited from ``TerminalBackend``."""
    assert "drain_startup" not in vars(PexpectPtyBackend)


def test_backend_does_not_have_uses_readline_helper() -> None:
    """``_uses_readline`` private helper is deleted (replaced by ``_shell_family``)."""
    assert "_uses_readline" not in vars(PexpectPtyBackend)


def test_backend_implements_write_blocking_hook() -> None:
    """``_write_blocking`` hook is implemented on this backend."""
    assert "_write_blocking" in vars(PexpectPtyBackend)


def test_backend_implements_read_blocking_hook() -> None:
    """``_read_blocking`` hook is implemented on this backend."""
    assert "_read_blocking" in vars(PexpectPtyBackend)


def test_backend_implements_shell_family_hook() -> None:
    """``_shell_family`` hook is implemented on this backend."""
    assert "_shell_family" in vars(PexpectPtyBackend)


def test_stdin_wait_evidence_returns_none_when_probe_unavailable() -> None:
    backend, _, _ = _make_backend_with_proc()

    with patch(
        "modex_agent.tools.terminal.backends.pexpect_pty.stdin_probe_available",
        return_value=False,
    ):
        result = backend.stdin_wait_evidence()

    assert result is None


def test_stdin_wait_evidence_probes_foreground_group() -> None:
    backend, proc, _ = _make_backend_with_proc()
    proc.pid = 123

    with (
        patch(
            "modex_agent.tools.terminal.backends.pexpect_pty.stdin_probe_available",
            return_value=True,
        ),
        patch(
            "modex_agent.tools.terminal.backends.pexpect_pty.foreground_pgid",
            return_value=456,
        ) as foreground,
        patch(
            "modex_agent.tools.terminal.backends.pexpect_pty.controlling_tty_device",
            return_value=(136, 0),
        ) as controlling_tty,
        patch(
            "modex_agent.tools.terminal.backends.pexpect_pty.is_stdin_waiting",
            return_value=True,
        ) as waiting,
    ):
        result = backend.stdin_wait_evidence()

    assert result is True
    foreground.assert_called_once_with(123)
    controlling_tty.assert_called_once_with(123)
    waiting.assert_called_once_with(456, (136, 0))


def test_stdin_wait_evidence_converts_os_error_to_absent_evidence() -> None:
    backend, _, _ = _make_backend_with_proc()

    with (
        patch(
            "modex_agent.tools.terminal.backends.pexpect_pty.stdin_probe_available",
            return_value=True,
        ),
        patch(
            "modex_agent.tools.terminal.backends.pexpect_pty.foreground_pgid",
            side_effect=OSError,
        ),
    ):
        result = backend.stdin_wait_evidence()

    assert result is None


# ── Inherited clear_input_line behavior ──


async def test_clear_input_line_writes_ctrl_a_ctrl_k_for_bash() -> None:
    """Inherited ``clear_input_line`` sends ``\\x01\\x0b`` for readline shells (bash)."""
    backend, mock_proc, _ = _make_backend_with_proc()
    backend._shell = "bash"
    await backend.clear_input_line()
    mock_proc.send.assert_called_once_with("\x01\x0b")


async def test_clear_input_line_writes_ctrl_a_ctrl_k_for_zsh() -> None:
    """Inherited ``clear_input_line`` sends ``\\x01\\x0b`` for readline shells (zsh)."""
    backend, mock_proc, _ = _make_backend_with_proc()
    backend._shell = "/usr/bin/zsh"
    await backend.clear_input_line()
    mock_proc.send.assert_called_once_with("\x01\x0b")


async def test_clear_input_line_is_noop_for_cmd() -> None:
    """Inherited ``clear_input_line`` is a no-op for non-readline shells (cmd)."""
    backend, mock_proc, _ = _make_backend_with_proc()
    backend._shell = "cmd.exe"
    await backend.clear_input_line()
    mock_proc.send.assert_not_called()
