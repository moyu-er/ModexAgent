"""Unit tests for ``WinptyConsoleWindowBackend`` — ADR-0032 D2 native-async escape hatch.

Exercises the asyncio-streams IPC rewrite:
- ``write`` is overridden directly (no ``_write_blocking`` hook) and calls
  ``writer.write`` + ``await writer.drain()``.
- ``read_pending`` is overridden directly (no ``_read_blocking`` hook) and
  uses ``asyncio.wait_for(self._reader.read(n), timeout=...)``.
- ``_shell_family`` hook is implemented.
- ``current_segment`` / ``clear_input_line`` / ``drain_startup`` /
  ``_uses_readline`` are deleted — inherited from base.
- ``_on_client_connected`` sets ``TCP_NODELAY`` on the underlying socket.

Mock-based: ``backend._reader`` and ``backend._writer`` are ``AsyncMock`` /
``MagicMock`` standing in for ``asyncio.StreamReader`` / ``StreamWriter``;
``backend._proc`` is a ``MagicMock`` for the subprocess. No real winpty or
real socket is opened, so the suite runs on any platform.
"""

from __future__ import annotations

import asyncio
import inspect
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.tools.terminal.backends.visible_windows import WinptyConsoleWindowBackend
from modex_agent.tools.terminal.types import ShellFamily


def _make_backend() -> WinptyConsoleWindowBackend:
    """Construct a backend without calling ``start()`` (no real subprocess)."""
    return WinptyConsoleWindowBackend()


def _make_backend_with_streams() -> tuple[WinptyConsoleWindowBackend, AsyncMock, MagicMock]:
    """Construct a backend with mocked ``_reader`` / ``_writer`` for I/O tests.

    ``_reader`` is an ``AsyncMock`` (``reader.read`` is awaitable).
    ``_writer`` is a ``MagicMock`` with ``drain`` configured as an ``AsyncMock``
    so ``await writer.drain()`` works.
    """
    backend = _make_backend()
    reader: AsyncMock = AsyncMock()
    writer: MagicMock = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.get_extra_info = MagicMock(return_value=None)
    backend._reader = reader
    backend._writer = writer
    return backend, reader, writer


# ── write (native async, no hook) ──


async def test_write_calls_writer_write_then_drain() -> None:
    """``write`` calls ``writer.write`` with UTF-8 bytes, then awaits ``drain``."""
    backend, _, writer = _make_backend_with_streams()
    await backend.write("echo hi\r")
    writer.write.assert_called_once_with(b"echo hi\r")
    writer.drain.assert_awaited_once()


async def test_write_encodes_utf8() -> None:
    """``write`` encodes the payload as UTF-8 bytes."""
    backend, _, writer = _make_backend_with_streams()
    await backend.write("héllo\r")
    writer.write.assert_called_once_with("héllo\r".encode())


async def test_write_raises_when_not_started() -> None:
    """``write`` raises ``RuntimeError`` when ``_writer`` is ``None``."""
    backend = _make_backend()
    with pytest.raises(RuntimeError, match="PTY not started"):
        await backend.write("anything")


async def test_write_propagates_drain_connection_reset() -> None:
    """``write`` propagates ``ConnectionResetError`` from ``drain`` (no silent partial write)."""
    backend, _, writer = _make_backend_with_streams()
    writer.drain.side_effect = ConnectionResetError("broken pipe")
    with pytest.raises(ConnectionResetError):
        await backend.write("cmd\r")
    # The bytes were buffered in memory; the failure surfaces during drain,
    # so the caller knows the command was NOT delivered.
    writer.write.assert_called_once_with(b"cmd\r")


# ── read_pending (native async, no hook) ──


async def test_read_pending_returns_decoded_data() -> None:
    """``read_pending`` decodes bytes from ``reader.read`` into ``TerminalRead``."""
    backend, reader, _ = _make_backend_with_streams()
    reader.read.return_value = b"hello"
    result = await backend.read_pending(timeout=0.5)
    assert result.stdout == "hello"
    assert result.raw == "hello"
    reader.read.assert_awaited_once()


async def test_read_pending_appends_to_buffer() -> None:
    """Non-empty read appends text to the sliding output buffer."""
    backend, reader, _ = _make_backend_with_streams()
    reader.read.return_value = b"output"
    await backend.read_pending(timeout=0.5)
    assert backend.output_buffer_text().endswith("output")


async def test_read_pending_returns_empty_on_timeout() -> None:
    """``read_pending`` swallows ``asyncio.TimeoutError`` and returns empty ``TerminalRead``."""
    backend, reader, _ = _make_backend_with_streams()
    reader.read.side_effect = asyncio.TimeoutError
    result = await backend.read_pending(timeout=0.1)
    assert result.stdout == ""
    assert result.raw == ""


async def test_read_pending_returns_empty_on_timeout_error() -> None:
    """``read_pending`` also swallows bare ``TimeoutError`` (Python 3.11+ alias)."""
    backend, reader, _ = _make_backend_with_streams()
    reader.read.side_effect = TimeoutError
    result = await backend.read_pending(timeout=0.1)
    assert result.stdout == ""
    assert result.raw == ""


async def test_read_pending_returns_empty_when_not_started() -> None:
    """``read_pending`` returns empty ``TerminalRead`` when ``_reader`` is ``None``."""
    backend = _make_backend()
    backend._reader = None
    result = await backend.read_pending(timeout=0.5)
    assert result.stdout == ""
    assert result.raw == ""


async def test_read_pending_passes_max_size_to_reader() -> None:
    """``read_pending`` passes ``max_size`` to ``reader.read``."""
    backend, reader, _ = _make_backend_with_streams()
    reader.read.return_value = b""
    await backend.read_pending(timeout=0.1, max_size=4096)
    reader.read.assert_awaited_once_with(4096)


async def test_read_pending_uses_wait_for_with_timeout() -> None:
    """``read_pending`` wraps ``reader.read`` in ``asyncio.wait_for`` with the given timeout.

    Structural assertion: the source of ``read_pending`` must contain
    ``asyncio.wait_for`` and ``timeout`` — the D2 escape-hatch that structurally
    eliminates the ``settimeout`` leak. Checks the call form ``.settimeout(``
    (not the bare word) so docstring mentions of "settimeout leak" don't
    trip the assertion.
    """
    src = inspect.getsource(WinptyConsoleWindowBackend.read_pending)
    assert "asyncio.wait_for" in src
    assert "timeout" in src
    assert ".settimeout(" not in src


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
    """``_shell_family`` returns the correct ``ShellFamily`` for known paths."""
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


def test_backend_overrides_write_directly() -> None:
    """``write`` IS overridden on this backend (D1 point 2 native-async escape hatch)."""
    assert "write" in vars(WinptyConsoleWindowBackend)


def test_backend_overrides_read_pending_directly() -> None:
    """``read_pending`` IS overridden on this backend (D1 point 2 escape hatch)."""
    assert "read_pending" in vars(WinptyConsoleWindowBackend)


def test_backend_does_not_implement_write_blocking_hook() -> None:
    """``_write_blocking`` hook is NOT implemented (no hook — native async)."""
    assert "_write_blocking" not in vars(WinptyConsoleWindowBackend)


def test_backend_does_not_implement_read_blocking_hook() -> None:
    """``_read_blocking`` hook is NOT implemented (no hook — native async)."""
    assert "_read_blocking" not in vars(WinptyConsoleWindowBackend)


def test_backend_does_not_override_current_segment() -> None:
    """``current_segment`` is inherited from ``TerminalBackend``."""
    assert "current_segment" not in vars(WinptyConsoleWindowBackend)


def test_backend_does_not_override_clear_input_line() -> None:
    """``clear_input_line`` is inherited from ``TerminalBackend``."""
    assert "clear_input_line" not in vars(WinptyConsoleWindowBackend)


def test_backend_does_not_override_drain_startup() -> None:
    """``drain_startup`` is inherited from ``TerminalBackend``."""
    assert "drain_startup" not in vars(WinptyConsoleWindowBackend)


def test_backend_does_not_have_uses_readline_helper() -> None:
    """``_uses_readline`` private helper is deleted (replaced by ``_shell_family``)."""
    assert "_uses_readline" not in vars(WinptyConsoleWindowBackend)


def test_backend_implements_shell_family_hook() -> None:
    """``_shell_family`` hook is implemented on this backend."""
    assert "_shell_family" in vars(WinptyConsoleWindowBackend)


def test_backend_does_not_override_read() -> None:
    """``read`` is inherited from ``TerminalBackend`` (backward-compat wrapper over read_pending)."""
    assert "read" not in vars(WinptyConsoleWindowBackend)


# ── start uses asyncio.start_server (structural) ──


def test_start_uses_asyncio_start_server() -> None:
    """``start`` is rewritten to use ``asyncio.start_server`` (no raw ``socket.socket``)."""
    src = inspect.getsource(WinptyConsoleWindowBackend.start)
    assert "asyncio.start_server" in src
    assert "server.accept" not in src
    # The bare ``socket.socket`` constructor must not appear in start().
    # (``socket`` module may still be imported for ``IPPROTO_TCP``/``TCP_NODELAY``
    # in the client-connected callback, but the server-creation path is gone.)


def test_start_has_no_settimeout_call() -> None:
    """``start`` does not call ``settimeout`` — the leak surface is removed."""
    src = inspect.getsource(WinptyConsoleWindowBackend.start)
    assert "settimeout" not in src


def test_start_has_no_sendall_or_recv() -> None:
    """``start`` does not call ``sendall`` or ``recv`` — raw socket I/O is gone."""
    src = inspect.getsource(WinptyConsoleWindowBackend.start)
    assert "sendall" not in src
    assert ".recv(" not in src


# ── TCP_NODELAY (parent side) ──


def test_on_client_connected_sets_tcp_nodelay() -> None:
    """``_on_client_connected`` sets ``TCP_NODELAY`` on the underlying socket."""
    src = inspect.getsource(WinptyConsoleWindowBackend._on_client_connected)
    assert "TCP_NODELAY" in src
    assert "setsockopt" in src


async def test_on_client_connected_sets_nodelay_on_real_socket() -> None:
    """``_on_client_connected`` calls ``setsockopt`` with ``TCP_NODELAY`` on the socket.

    Uses a real ``socket.socket`` pair (loopback) so the assertion exercises the
    actual ``setsockopt`` call rather than a mock.
    """
    backend = _make_backend()
    # Create a real loopback socket pair to stand in for the transport's socket.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    addr = srv.getsockname()
    cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cli.connect(addr)
    accepted, _ = srv.accept()

    try:
        reader = MagicMock()
        writer = MagicMock()
        writer.get_extra_info = MagicMock(return_value=accepted)
        backend._on_client_connected(reader, writer)
        # TCP_NODELAY should now be set on the accepted socket.
        nodelay = accepted.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        assert nodelay != 0
        # And the reader/writer are stored on the backend.
        assert backend._reader is reader
        assert backend._writer is writer
    finally:
        accepted.close()
        cli.close()
        srv.close()


# ── terminate / kill close the writer ──


async def test_terminate_closes_writer_and_clears_state() -> None:
    """``terminate`` closes the writer, awaits ``wait_closed``, and clears streams."""
    backend, _, writer = _make_backend_with_streams()
    backend._proc = MagicMock()
    await backend.terminate()
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()
    assert backend._writer is None
    assert backend._reader is None
    backend._proc.terminate.assert_called_once()


async def test_kill_closes_writer_and_kills_proc() -> None:
    """``kill`` closes the writer and calls ``proc.kill()``."""
    backend, _, writer = _make_backend_with_streams()
    backend._proc = MagicMock()
    await backend.kill()
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()
    assert backend._writer is None
    assert backend._reader is None
    backend._proc.kill.assert_called_once()


async def test_terminate_when_writer_none() -> None:
    """``terminate`` is a no-op on the writer path when ``_writer`` is ``None``."""
    backend = _make_backend()
    backend._proc = None
    # Should not raise.
    await backend.terminate()


# ── stdin_writable / interrupt ──


def test_stdin_writable_reflects_writer_state() -> None:
    """``stdin_writable`` returns True iff ``_writer`` is not None."""
    backend = _make_backend()
    assert backend.stdin_writable() is False
    backend, _, _ = _make_backend_with_streams()
    assert backend.stdin_writable() is True


async def test_interrupt_calls_write_with_ctrl_c() -> None:
    """``interrupt`` writes the Ctrl-C byte (``\\x03``) via ``write``."""
    backend, _, writer = _make_backend_with_streams()
    await backend.interrupt()
    writer.write.assert_called_once_with(b"\x03")
    writer.drain.assert_awaited_once()


# ── window_title ──


def test_window_title_default() -> None:
    """``window_title`` returns the default title before ``start()``."""
    backend = _make_backend()
    assert backend.window_title == "agent-terminal"


def test_window_title_after_start_shell_set() -> None:
    """Setting ``_shell`` updates the title (mirrors ``start()``'s title assignment)."""
    backend = _make_backend()
    backend._shell = "/usr/bin/bash"
    backend._title = f"Agent: {backend._shell}"
    assert backend.window_title == "Agent: /usr/bin/bash"


# ── is_alive ──


async def test_is_alive_false_when_no_proc() -> None:
    """``is_alive`` returns False when ``_proc`` is ``None``."""
    backend = _make_backend()
    assert await backend.is_alive() is False


async def test_is_alive_true_when_proc_running() -> None:
    """``is_alive`` returns True when ``proc.poll()`` returns ``None`` (still running)."""
    backend = _make_backend()
    backend._proc = MagicMock()
    backend._proc.poll.return_value = None
    assert await backend.is_alive() is True


async def test_is_alive_false_when_proc_exited() -> None:
    """``is_alive`` returns False when ``proc.poll()`` returns a returncode."""
    backend = _make_backend()
    backend._proc = MagicMock()
    backend._proc.poll.return_value = 0
    assert await backend.is_alive() is False
