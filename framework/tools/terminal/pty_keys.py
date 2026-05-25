from __future__ import annotations

import re
from enum import StrEnum
from typing import Protocol, runtime_checkable


class CursorKeyMode(StrEnum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    APPLICATION = "application"


class ProcessAction(StrEnum):
    LIST = "list"
    # POLL removed: poll drains pending output but cannot detect command completion
    # reliably in PTY mode. After write+submit, use `terminal current` to see the
    # terminal screen state instead.
    # POLL = "poll"
    LOG = "log"
    WRITE = "write"
    SUBMIT = "submit"
    SEND_KEYS = "send_keys"
    PASTE = "paste"
    INTERRUPT = "interrupt"
    KILL = "kill"
    CLEAR = "clear"
    REMOVE = "remove"


_NORMAL_ARROW: dict[str, bytes] = {
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[1~",
    "end": b"\x1b[4~",
}

_APPLICATION_ARROW: dict[str, bytes] = {
    "up": b"\x1bOA",
    "down": b"\x1bOB",
    "right": b"\x1bOC",
    "left": b"\x1bOD",
    "home": b"\x1bOH",
    "end": b"\x1bOF",
}

_NAMED_KEYS: dict[str, bytes] = {
    "enter": b"\r",
    "return": b"\r",
    "tab": b"\t",
    "escape": b"\x1b",
    "esc": b"\x1b",
    "backspace": b"\x7f",
    "delete": b"\x1b[3~",
    "insert": b"\x1b[2~",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "space": b" ",
}

_FUNCTION_KEYS: dict[str, bytes] = {
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
}

_CURSOR_SENSITIVE_KEYS: frozenset[str] = frozenset(_NORMAL_ARROW.keys())

_CTRL_RE = re.compile(r"^c-(.+)$")
_ALT_RE = re.compile(r"^(?:m|alt)-(.+)$")
_CTRL_ALT_RE = re.compile(r"^c-(?:m|alt)-(.+)$")
_HEX_RE = re.compile(r"^hex:([0-9a-fA-F]+)$")

_SMKX = b"\x1b[?1h"
_RMKX = b"\x1b[?1l"

_DSR_PATTERN = re.compile(rb"\x1b\[\??6n")

_BRACKETED_PASTE_START = b"\x1b[200~"
_BRACKETED_PASTE_END = b"\x1b[201~"

_BRACKETED_PASTE_ENABLE = b"\x1b[?2004h"
_BRACKETED_PASTE_DISABLE = b"\x1b[?2004l"


@runtime_checkable
class _StdinWriter(Protocol):
    def write(self, data: bytes) -> object: ...


def _ctrl_byte(ch: str) -> bytes:
    code = ord(ch.upper())
    if 64 <= code <= 95:
        return bytes([code & 0x1F])
    return ch.encode("utf-8")


def _alt_byte(ch: str) -> bytes:
    return b"\x1b" + ch.encode("utf-8")


def encode_key(key_spec: str, cursor_mode: CursorKeyMode = CursorKeyMode.UNKNOWN) -> bytes:
    if len(key_spec) == 1 and key_spec.isprintable():
        return key_spec.encode("utf-8")

    lower = key_spec.lower()

    if lower in _NAMED_KEYS:
        return _NAMED_KEYS[lower]

    if lower in _FUNCTION_KEYS:
        return _FUNCTION_KEYS[lower]

    if lower in _NORMAL_ARROW:
        if cursor_mode == CursorKeyMode.APPLICATION:
            return _APPLICATION_ARROW[lower]
        return _NORMAL_ARROW[lower]

    m = _CTRL_ALT_RE.match(lower)
    if m:
        return b"\x1b" + _ctrl_byte(m.group(1))

    m = _CTRL_RE.match(lower)
    if m:
        return _ctrl_byte(m.group(1))

    m = _ALT_RE.match(lower)
    if m:
        return _alt_byte(m.group(1))

    m = _HEX_RE.match(lower)
    if m:
        return bytes([int(m.group(1), 16)])

    return key_spec.encode("utf-8")


def encode_key_sequence(
    keys: list[str],
    cursor_mode: CursorKeyMode = CursorKeyMode.UNKNOWN,
) -> bytes:
    return b"".join(encode_key(k, cursor_mode) for k in keys)


def needs_cursor_mode(keys: list[str]) -> bool:
    return any(k.lower() in _CURSOR_SENSITIVE_KEYS for k in keys)


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


def encode_paste(text: str, bracketed: bool = True) -> bytes:
    data = text.encode("utf-8")
    if bracketed:
        return _BRACKETED_PASTE_START + data + _BRACKETED_PASTE_END
    return data
