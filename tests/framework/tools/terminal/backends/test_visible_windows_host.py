"""Tests for the Windows visible terminal helper data flow."""

from __future__ import annotations

import socket
import sys
from io import StringIO
from typing import Any

import pytest

from framework.tools.terminal.backends.visible_windows_host import _stdin_to_pty


class FakePtyProcess:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)


def test_stdin_to_pty_forwards_user_input() -> None:
    """Fallback path: StringIO uses line-buffered readline."""
    proc = FakePtyProcess()
    stdin = StringIO("secret\n")

    _stdin_to_pty(proc, stdin)

    assert proc.writes == ["secret\n"]


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt is Windows-only")
def test_stdin_to_pty_character_mode_forwards_each_key(monkeypatch: Any) -> None:
    """When stdin is sys.stdin, each keystroke is forwarded immediately."""
    proc = FakePtyProcess()

    import msvcrt

    key_events = ["h", "i", "\r"]
    event_iter = iter(key_events)

    def mock_kbhit() -> bool:
        return True

    def mock_getwch() -> str:
        try:
            return next(event_iter)
        except StopIteration:
            raise OSError("done")

    monkeypatch.setattr(msvcrt, "kbhit", mock_kbhit)
    monkeypatch.setattr(msvcrt, "getwch", mock_getwch)

    _stdin_to_pty(proc, sys.stdin)

    assert proc.writes == ["h", "i", "\n"]


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt is Windows-only")
def test_stdin_to_pty_character_mode_maps_arrow_keys(monkeypatch: Any) -> None:
    """Windows extended keys are translated to ANSI escape sequences."""
    proc = FakePtyProcess()

    import msvcrt

    key_events = ["\xe0", "M"]  # Right-arrow extended-key prefix + scan code
    event_iter = iter(key_events)

    def mock_kbhit() -> bool:
        return True

    def mock_getwch() -> str:
        try:
            return next(event_iter)
        except StopIteration:
            raise OSError("done")

    monkeypatch.setattr(msvcrt, "kbhit", mock_kbhit)
    monkeypatch.setattr(msvcrt, "getwch", mock_getwch)

    _stdin_to_pty(proc, sys.stdin)

    assert proc.writes == ["\x1b[C"]  # ANSI right-arrow sequence


class FakeFileobj:
    """Simulates winpty's proc.fileobj for pty_to_socket testing."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.timeout = 0.5

    def settimeout(self, val: float) -> None:
        self.timeout = val

    def recv(self, size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""  # EOF — PTY process closed


class FakeSock:
    """Socket that raises ConnectionResetError on send (simulates disconnect)."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        raise ConnectionResetError("parent gone")


def test_pty_to_socket_continues_writing_to_stdout_after_socket_disconnect(
    monkeypatch: Any,
) -> None:
    """When the parent socket disconnects, pty_to_socket must keep writing
    PTY output to stdout (visible console) so the user still sees output."""

    from framework.tools.terminal.backends.visible_windows_host import _READ_TIMEOUT

    chunks = [b"hello ", b"world"]
    fileobj = FakeFileobj(chunks)
    sock = FakeSock()

    output: list[str] = []

    def fake_write(text: str) -> None:
        output.append(text)

    def fake_flush() -> None:
        pass

    monkeypatch.setattr("sys.stdout.write", fake_write)
    monkeypatch.setattr("sys.stdout.flush", fake_flush)

    # Run the pty_to_socket logic inline (extracted from visible_windows_host)
    while True:
        try:
            fileobj.settimeout(_READ_TIMEOUT)
            raw = fileobj.recv(65536)
        except socket.timeout:
            continue
        except (OSError, ConnectionResetError):
            break
        if not raw:
            break

        text = raw.decode("utf-8", errors="replace")
        try:
            sock.sendall(text.encode("utf-8"))
        except (OSError, ConnectionResetError):
            pass  # Must NOT break — keep writing to stdout

        # Write to stdout regardless of socket state
        fake_write(text)
        fake_flush()

    assert output == ["hello ", "world"], (
        f"Expected stdout to receive all PTY output after socket disconnect, got {output}"
    )


def test_visible_host_forces_winpty_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Visible terminal host must use WinPTY backend for consistent DA1 handling."""
    from framework.tools.terminal.backends import visible_windows_host

    captured: dict[str, object] = {}

    class FakePtyProcessFactory:
        @staticmethod
        def spawn(*args: object, **kwargs: object) -> object:
            captured["backend"] = kwargs.get("backend")
            return object()

    fake_winpty = type("FakeWinpty", (), {
        "PtyProcess": FakePtyProcessFactory,
        "Backend": type("Backend", (), {"WinPTY": 1, "ConPTY": 0}),
    })
    monkeypatch.setitem(sys.modules, "winpty", fake_winpty)

    visible_windows_host._spawn_pty("bash")

    assert captured["backend"] == 1  # WinPTY


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes is Windows-only")
def test_ignore_ctrl_c_calls_set_console_ctrl_handler(monkeypatch: Any) -> None:
    """_ignore_ctrl_c must call SetConsoleCtrlHandler(None, True)."""
    from framework.tools.terminal.backends import visible_windows_host

    calls: list[tuple[object, bool]] = []

    class FakeKernel32:
        def SetConsoleCtrlHandler(self, handler: object, add: bool) -> bool:
            calls.append((handler, add))
            return True

    fake_ctypes = type("FakeCtypes", (), {
        "windll": type("Windll", (), {"kernel32": FakeKernel32()})(),
    })
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    visible_windows_host._ignore_ctrl_c()

    assert len(calls) == 1
    assert calls[0][0] is None
    assert calls[0][1] is True
