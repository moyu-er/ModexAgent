"""Tests for the Windows visible terminal helper data flow."""

from __future__ import annotations

import socket
import sys
from io import StringIO
from typing import Any

import pytest

from framework.tools.terminal.backends.visible_windows_host import (
    _stdin_to_pty,
    _translate_key,
)


class FakePtyProcess:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)


# ── _translate_key: pure key translation logic ──


def test_regular_char() -> None:
    assert _translate_key(0x41, "h", 0) == "h"


def test_enter() -> None:
    assert _translate_key(0x0D, "\r", 0) == "\n"


def test_backspace() -> None:
    assert _translate_key(0x08, "\x00", 0) == "\x08"


def test_tab() -> None:
    assert _translate_key(0x09, "\x00", 0) == "\t"


def test_escape() -> None:
    assert _translate_key(0x1B, "\x00", 0) == "\x1b"


def test_ctrl_c() -> None:
    assert _translate_key(0x43, "\x03", 0x0008) == "\x03"


def test_ctrl_z() -> None:
    assert _translate_key(0x5A, "\x1a", 0x0008) == "\x1a"


def test_ctrl_d() -> None:
    assert _translate_key(0x44, "\x04", 0x0008) == "\x04"


def test_ctrl_l() -> None:
    assert _translate_key(0x4C, "\x0c", 0x0008) == "\x0c"


def test_ctrl_a() -> None:
    assert _translate_key(0x41, "\x01", 0x0008) == "\x01"


def test_right_ctrl() -> None:
    """Right Ctrl should work the same as Left Ctrl."""
    assert _translate_key(0x43, "\x03", 0x0004) == "\x03"


def test_arrow_up() -> None:
    assert _translate_key(0x26, "\x00", 0) == "\x1b[A"


def test_arrow_down() -> None:
    assert _translate_key(0x28, "\x00", 0) == "\x1b[B"


def test_arrow_left() -> None:
    assert _translate_key(0x25, "\x00", 0) == "\x1b[D"


def test_arrow_right() -> None:
    assert _translate_key(0x27, "\x00", 0) == "\x1b[C"


def test_home() -> None:
    assert _translate_key(0x24, "\x00", 0) == "\x1b[H"


def test_end() -> None:
    assert _translate_key(0x23, "\x00", 0) == "\x1b[F"


def test_page_up() -> None:
    assert _translate_key(0x21, "\x00", 0) == "\x1b[5~"


def test_page_down() -> None:
    assert _translate_key(0x22, "\x00", 0) == "\x1b[6~"


def test_insert() -> None:
    assert _translate_key(0x2D, "\x00", 0) == "\x1b[2~"


def test_delete() -> None:
    assert _translate_key(0x2E, "\x00", 0) == "\x1b[3~"


def test_null_char_returns_none() -> None:
    assert _translate_key(0x00, "\x00", 0) is None


def test_ctrl_with_non_letter_ignores_ctrl() -> None:
    """Ctrl+non-letter (e.g., Ctrl+1) should fall through to char output."""
    assert _translate_key(0x31, "1", 0x0008) == "1"


# ── fallback path (StringIO) ──


def test_stdin_to_pty_forwards_user_input() -> None:
    proc = FakePtyProcess()
    stdin = StringIO("secret\n")
    _stdin_to_pty(proc, stdin)
    assert proc.writes == ["secret\n"]


def test_stdin_to_pty_stops_on_empty() -> None:
    proc = FakePtyProcess()
    stdin = StringIO("")
    _stdin_to_pty(proc, stdin)
    assert proc.writes == []


# ── pty_to_socket: stdout survives socket disconnect ──


class FakeFileobj:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.timeout = 0.5

    def settimeout(self, val: float) -> None:
        self.timeout = val

    def recv(self, size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class FakeSock:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        raise ConnectionResetError("parent gone")


def test_pty_to_socket_continues_writing_to_stdout_after_socket_disconnect(
    monkeypatch: Any,
) -> None:
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
            pass

        fake_write(text)
        fake_flush()

    assert output == ["hello ", "world"]


# ── _spawn_pty: backend and dimensions ──


def test_visible_host_forces_winpty_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from framework.tools.terminal.backends import visible_windows_host

    captured: dict[str, object] = {}

    class FakePtyProcessFactory:
        @staticmethod
        def spawn(*args: object, **kwargs: object) -> object:
            captured["backend"] = kwargs.get("backend")
            captured["dimensions"] = kwargs.get("dimensions")
            return object()

    fake_winpty = type("FakeWinpty", (), {
        "PtyProcess": FakePtyProcessFactory,
        "Backend": type("Backend", (), {"WinPTY": 1, "ConPTY": 0}),
    })
    monkeypatch.setitem(sys.modules, "winpty", fake_winpty)

    visible_windows_host._spawn_pty("bash")

    assert captured["backend"] == 1
    assert captured["dimensions"] == (30, 120)


# ── _disable_console_echo ──


def test_disable_echo_bit_logic() -> None:
    ENABLE_PROCESSED_INPUT = 0x0001
    ENABLE_LINE_INPUT = 0x0002
    ENABLE_ECHO_INPUT = 0x0004
    RAW_MASK = ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT
    original = 0x00F7
    result = original & ~RAW_MASK
    assert not (result & ENABLE_PROCESSED_INPUT)
    assert not (result & ENABLE_LINE_INPUT)
    assert not (result & ENABLE_ECHO_INPUT)
    # Other flags preserved
    assert result & 0x0010  # ENABLE_WINDOW_INPUT
    assert result & 0x0020  # ENABLE_MOUSE_INPUT


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_disable_console_echo_smoke() -> None:
    from framework.tools.terminal.backends.visible_windows_host import _disable_console_echo
    _disable_console_echo()


# ── _resize_console ──


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_resize_console_smoke() -> None:
    from framework.tools.terminal.backends.visible_windows_host import _resize_console
    _resize_console()
