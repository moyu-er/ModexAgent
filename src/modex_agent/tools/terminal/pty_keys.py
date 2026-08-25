from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import StrEnum


class CursorKeyMode(StrEnum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    APPLICATION = "application"


class ProcessAction(StrEnum):
    # LOG removed — merged into terminal current
    # LIST removed — merged into terminal list
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
#   libtmux:   pane.send_keys(data) with this character
#
# Visible-windows backends use this constant as a socket-protocol marker;
# the host process detects it and calls proc.sendintr().
CTRL_C: str = "\x03"


def normalize_write_payload(data: str, submit: bool) -> str:
    """Normalize ProcessTool ``write`` input to a deterministic payload.

    Agents habitually pass ``data="y\n"`` (their mental model of "I typed y
    and pressed Enter"). In raw mode (sudo/pager/TUI) a bare ``\\n`` is
    ignored by the PTY, and in line-buffered mode (``read``) it submits
    early leaving the trailing ``\\r`` to submit an empty line — two-step
    confirmations then answer the second prompt with an empty string.

    Normalization (line-oriented semantics for ``write`` only):

    1. Strip ALL trailing newline sequences (``\\r\\n``, ``\\n``, ``\\r``).
    2. ``effective_submit = submit or had_newline`` — a trailing newline IS
       submit intent.
    3. Append ``ENTER_KEY`` (``\\r``) iff ``effective_submit``.

    | agent input            | payload | meaning                    |
    |------------------------|---------|----------------------------|
    | ``"y\\n"`` + submit=true  | ``y\\r`` | single submit              |
    | ``"y\\n"`` + submit=false | ``y\\r`` | trailing \\n = submit      |
    | ``"y"``  + submit=true  | ``y\\r`` | explicit submit            |
    | ``"y"``  + submit=false | ``y``    | pure typing (no Enter)     |

    Scope: ``write`` only. ``submit`` (bare Enter), ``send_keys`` (byte
    exact), and ``paste`` (multiline preserved) are untouched.
    """
    stripped = data.rstrip("\r\n")
    had_newline = len(stripped) != len(data)
    effective_submit = submit or had_newline
    return stripped + (ENTER_KEY if effective_submit else "")


class _StdinWriter(ABC):
    @abstractmethod
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
