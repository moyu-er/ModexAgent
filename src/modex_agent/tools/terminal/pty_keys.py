from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import StrEnum


class CursorKeyMode(StrEnum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    APPLICATION = "application"


_SMKX = b"\x1b[?1h"
_RMKX = b"\x1b[?1l"

_DSR_PATTERN = re.compile(rb"\x1b\[\??6n")

_BRACKETED_PASTE_ENABLE = b"\x1b[?2004h"
_BRACKETED_PASTE_DISABLE = b"\x1b[?2004l"

# Enter key — the carriage return character.  When a program puts the
# terminal in raw mode (less, vim, nano, ssh), only \r is recognised as
# the Enter key; \n (line feed) is ignored.  \r is the universal Enter
# key code on all platforms — the terminal hardware always sends \r for
# the Enter key, and the terminal driver translates it to \n in canonical
# mode.
ENTER_KEY: str = "\r"

# Ctrl+C — the ASCII End-of-Text character.  Writing this byte to a PTY
# input stream triggers SIGINT in the foreground process group via the
# terminal driver.  This is the universal terminal protocol for "interrupt
# current process".
#
# Each backend uses its library's official API to send this byte:
#   pywinpty:  proc.sendintr() or proc.sendcontrol('c') — both call
#              pty.write('\\x03') internally (verified from pywinpty source)
#   pexpect:   proc.sendintr() — sends SIGINT via os.kill()
#   libtmux:   pane input API with this character
#
# Visible-windows backends use this constant as a socket-protocol marker;
# the host process detects it and calls proc.sendintr().
CTRL_C: str = "\x03"


def normalize_write_payload(data: str, submit: bool) -> str:
    """Append one Enter for submitted input; otherwise preserve raw text."""
    if not submit:
        return data
    return data.rstrip("\r\n") + ENTER_KEY


class _StdinWriter(ABC):
    @abstractmethod
    def write(self, data: bytes) -> object: ...


def detect_cursor_key_mode(raw: bytes) -> CursorKeyMode | None:
    last_smkx = raw.rfind(_SMKX)
    last_rmkx = raw.rfind(_RMKX)
    if last_smkx < 0 and last_rmkx < 0:
        return None
    if last_smkx > last_rmkx:
        return CursorKeyMode.APPLICATION
    return CursorKeyMode.NORMAL


def strip_smkx_rmkx(data: bytes) -> bytes:
    return data.replace(_SMKX, b"").replace(_RMKX, b"")


def strip_dsr_and_respond(
    data: bytes, stdin_write: _StdinWriter | None = None
) -> tuple[bytes, int]:
    count = len(_DSR_PATTERN.findall(data))
    cleaned = _DSR_PATTERN.sub(b"", data)
    if count > 0 and stdin_write is not None:
        for _ in range(count):
            stdin_write.write(b"\x1b[1;1R")
    return cleaned, count


def detect_bracketed_paste_mode(raw: bytes) -> bool | None:
    last_enable = raw.rfind(_BRACKETED_PASTE_ENABLE)
    last_disable = raw.rfind(_BRACKETED_PASTE_DISABLE)
    if last_enable < 0 and last_disable < 0:
        return None
    return last_enable > last_disable


def strip_bracketed_paste_mode(data: bytes) -> bytes:
    """Strip bracketed-paste enable/disable sequences from output bytes."""
    return data.replace(_BRACKETED_PASTE_ENABLE, b"").replace(_BRACKETED_PASTE_DISABLE, b"")
