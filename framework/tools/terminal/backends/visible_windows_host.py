"""Windows visible terminal host process.

Launched by VisibleWindowsPtyBackend with CREATE_NEW_CONSOLE so it owns a
visible console window.  Creates a winpty.PtyProcess and forwards I/O via
TCP socket so the parent process and the visible window share the same data
stream.
"""

# TODO(terminal-human-input): Detect human keyboard input in visible terminal
# and notify the parent process via out-of-band socket marker (\x00HUMAN\x00).
# Parent filters the marker and sets a flag on the backend.
# Session layer checks the flag and appends a note to command results.
# Only affects VisibleWindowsPtyBackend (not hidden or tmux).
# Requires: socket write lock in host process to prevent marker interleaving.
# See: docs/superpowers/specs/2026-05-30-terminal-system-improvements-design.md §4

from __future__ import annotations

import os
import socket
import sys
import threading
from typing import Protocol, TextIO

_READ_TIMEOUT = 0.5  # seconds for each socket recv()
_PTY_ROWS = 30
_PTY_COLS = 120


class WritablePty(Protocol):
    def write(self, data: str) -> None:
        """Write text to the PTY process."""


class ReadablePtyFile(Protocol):
    def settimeout(self, val: float) -> None:
        """Set the PTY socket read timeout."""

    def recv(self, size: int) -> bytes:
        """Receive bytes from the PTY socket."""


class VisiblePtyProcess(WritablePty, Protocol):
    fileobj: ReadablePtyFile

    def isalive(self) -> bool:
        """Return True if the underlying process is still running."""
        ...


def _enable_vt_mode() -> None:
    """Enable ANSI escape-sequence processing in the Windows console."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return

    kernel32 = ctypes.windll.kernel32
    STD_OUTPUT_HANDLE = -11
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

    h_stdout = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    if h_stdout in (-1, None):
        return

    mode = wintypes.DWORD()
    if kernel32.GetConsoleMode(h_stdout, ctypes.byref(mode)):
        kernel32.SetConsoleMode(
            h_stdout, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        )


def _disable_console_echo() -> None:
    """Put the console in raw input mode for ``ReadConsoleInputW``.

    Disables ENABLE_PROCESSED_INPUT, ENABLE_LINE_INPUT, and ENABLE_ECHO_INPUT
    so that ALL key events (including Ctrl+C, Backspace, Enter, arrow keys)
    go into the input buffer and are read by ``ReadConsoleInputW``.  The PTY's
    shell handles line editing and echo through the output path.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return

    kernel32 = ctypes.windll.kernel32
    ENABLE_PROCESSED_INPUT = 0x0001
    ENABLE_LINE_INPUT = 0x0002
    ENABLE_ECHO_INPUT = 0x0004
    RAW_MASK = ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT

    h_stdin = kernel32.GetStdHandle(-10)
    if h_stdin in (-1, None):
        return

    mode = wintypes.DWORD()
    if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
        kernel32.SetConsoleMode(h_stdin, mode.value & ~RAW_MASK)


def _resize_console(rows: int = _PTY_ROWS, cols: int = _PTY_COLS) -> None:
    """Resize the visible console window to match PTY dimensions.

    Without this, the console defaults to 80×25 while the PTY is 30×120,
    causing cursor-position drift and garbled output.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return

    kernel32 = ctypes.windll.kernel32
    STD_OUTPUT_HANDLE = -11

    h_stdout = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    if h_stdout in (-1, None):
        return

    class COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [
            ("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
            ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT),
        ]

    # argtypes needed so ctypes passes COORD by value (not by reference).
    kernel32.SetConsoleScreenBufferSize.argtypes = [wintypes.HANDLE, COORD]
    kernel32.SetConsoleScreenBufferSize.restype = wintypes.BOOL

    # Screen buffer size must be >= window size; set buffer first.
    buffer_size = COORD(X=cols, Y=rows * 3)
    kernel32.SetConsoleScreenBufferSize(h_stdout, buffer_size)

    # Set window to exactly rows × cols
    window = SMALL_RECT(Left=0, Top=0, Right=cols - 1, Bottom=rows - 1)
    kernel32.SetConsoleWindowInfo(h_stdout, True, ctypes.byref(window))


def _get_console_size() -> tuple[int, int] | None:
    """Return the current console window size as (rows, cols) or None."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    class COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [
            ("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
            ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT),
        ]

    class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
        _fields_ = [
            ("dwSize", COORD),
            ("dwCursorPosition", COORD),
            ("wAttributes", wintypes.WORD),
            ("srWindow", SMALL_RECT),
            ("dwMaximumWindowSize", COORD),
        ]

    kernel32 = ctypes.windll.kernel32
    h_stdout = kernel32.GetStdHandle(-11)
    if h_stdout in (-1, None):
        return None

    csbi = CONSOLE_SCREEN_BUFFER_INFO()
    if not kernel32.GetConsoleScreenBufferInfo(h_stdout, ctypes.byref(csbi)):
        return None

    cols = csbi.srWindow.Right - csbi.srWindow.Left + 1
    rows = csbi.srWindow.Bottom - csbi.srWindow.Top + 1
    return (rows, cols) if rows > 0 and cols > 0 else None


def _resize_monitor(proc: VisiblePtyProcess, stop: threading.Event) -> None:
    """Periodically sync console window size to PTY.

    When the user resizes the visible console window, this thread detects
    the change and calls ``setwinsize`` so the shell inside the PTY sees
    the new dimensions.
    """
    last: tuple[int, int] | None = None
    while not stop.is_set():
        size = _get_console_size()
        if size and size != last:
            try:
                if hasattr(proc, "setwinsize"):
                    proc.setwinsize(size[0], size[1])  # type: ignore[union-attr]
            except (OSError, AttributeError):
                pass
            last = size
        stop.wait(1.0)


# Virtual-key → ANSI escape sequence
_VK_MAP: dict[int, str] = {
    0x25: "\x1b[D",  # VK_LEFT
    0x26: "\x1b[A",  # VK_UP
    0x27: "\x1b[C",  # VK_RIGHT
    0x28: "\x1b[B",  # VK_DOWN
    0x21: "\x1b[5~", # VK_PRIOR (Page Up)
    0x22: "\x1b[6~", # VK_NEXT  (Page Down)
    0x23: "\x1b[F",  # VK_END
    0x24: "\x1b[H",  # VK_HOME
    0x2D: "\x1b[2~", # VK_INSERT
    0x2E: "\x1b[3~", # VK_DELETE
}

_LEFT_CTRL = 0x0008
_RIGHT_CTRL = 0x0004


def _translate_key(vk: int, ch: str, ctrl_state: int) -> str | None:
    """Translate a console key event to the text to send to the PTY.

    Returns ``None`` for unhandled keys (key-up, non-key events).
    """
    ctrl = bool(ctrl_state & (_LEFT_CTRL | _RIGHT_CTRL))

    # Ctrl+letter → control character (Ctrl+A=\x01 … Ctrl+Z=\x1a)
    if ctrl and 0x41 <= vk <= 0x5A:
        return chr(vk - 0x40)

    # Extended keys (arrows, home, end, insert, delete, pgup, pgdn)
    if vk in _VK_MAP:
        return _VK_MAP[vk]

    # Enter
    if vk == 0x0D:
        return "\n"

    # Backspace
    if vk == 0x08:
        return "\x08"

    # Tab
    if vk == 0x09:
        return "\t"

    # Escape
    if vk == 0x1B:
        return "\x1b"

    # Regular printable character
    if ch and ch != "\x00":
        return ch

    return None


def _stdin_to_pty(proc: WritablePty, stdin: TextIO) -> None:
    """Forward user input from the visible console to the PTY.

    Uses ``ReadConsoleInputW`` on ``CONIN$`` to read raw key events
    directly from the console input buffer.  This bypasses ``sys.stdin``
    and ``msvcrt`` entirely, guaranteeing correct input regardless of
    console mode settings or handle inheritance quirks.

    Falls back to line-buffered ``readline()`` for tests (StringIO) or
    redirected stdin.
    """
    # Fallback for tests (StringIO) or redirected stdin.
    if stdin is not sys.stdin:
        while True:
            try:
                data = stdin.readline()
            except OSError:
                break
            if not data:
                break
            proc.write(data)
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    # Open CONIN$ — always refers to THIS console's input buffer.
    GENERIC_READ = 0x80000000
    h_conin = kernel32.CreateFileW(
        "CONIN$", GENERIC_READ,
        0x0003,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None, 3, 0, None,  # OPEN_EXISTING
    )
    if h_conin in (-1, None) or h_conin == ctypes.c_void_p(-1).value:
        return

    KEY_EVENT = 0x0001

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", wintypes.BOOL),
            ("wRepeatCount", wintypes.WORD),
            ("wVirtualKeyCode", wintypes.WORD),
            ("wVirtualScanCode", wintypes.WORD),
            ("uChar", wintypes.WCHAR),
            ("dwControlKeyState", wintypes.DWORD),
        ]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [
            ("EventType", wintypes.WORD),
            ("Padding", wintypes.WORD),
            ("Event", KEY_EVENT_RECORD),
        ]

    rec = INPUT_RECORD()
    read_count = wintypes.DWORD()

    try:
        while True:
            if not kernel32.ReadConsoleInputW(
                h_conin, ctypes.byref(rec), 1, ctypes.byref(read_count)
            ):
                break

            if rec.EventType != KEY_EVENT:
                continue
            ke = rec.Event
            if not ke.bKeyDown:
                continue

            # Skip VT-generated events (cursor position reports, etc.).
            # Real keyboard events always have a non-zero VK code or non-zero
            # control key state (e.g. NumLock = 0x0020).  VT responses injected
            # by the console have vk=0 and ctrl_state=0.
            if ke.wVirtualKeyCode == 0 and ke.dwControlKeyState == 0:
                continue

            text = _translate_key(
                ke.wVirtualKeyCode, ke.uChar, ke.dwControlKeyState,
            )
            if text is not None:
                proc.write(text)
    except (OSError, KeyboardInterrupt):
        pass
    finally:
        kernel32.CloseHandle(h_conin)


def _spawn_pty(shell: str, cwd: str | None = None, env: dict[str, str] | None = None) -> VisiblePtyProcess:
    """Spawn the visible host PTY using pywinpty's WinPTY backend."""
    import winpty

    kwargs: dict = {"dimensions": (_PTY_ROWS, _PTY_COLS), "backend": winpty.Backend.WinPTY}
    if cwd:
        kwargs["cwd"] = cwd
    if env:
        kwargs["env"] = env
    return winpty.PtyProcess.spawn(shell, **kwargs)


def main() -> None:
    shell = sys.argv[1] if len(sys.argv) > 1 else "cmd.exe"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    cwd = sys.argv[3] if len(sys.argv) > 3 else None

    _enable_vt_mode()
    _disable_console_echo()
    _resize_console()

    proc = _spawn_pty(shell, cwd=cwd, env=dict(os.environ))

    # Connect back to the parent process
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", port))
    sock.settimeout(None)

    # PTY output  -> socket (parent) + stdout (visible console window)
    def pty_to_socket() -> None:
        while True:
            try:
                # Exit when the shell process dies so parent is_alive() is accurate.
                if hasattr(proc, "isalive") and not proc.isalive():
                    break
                proc.fileobj.settimeout(_READ_TIMEOUT)  # type: ignore[union-attr]
                raw = proc.fileobj.recv(65536)  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except (OSError, ConnectionResetError):
                break

            if not raw:
                # Empty read can happen when pywinpty recv is non-blocking
                # (settimeout ineffective). Treat it like a timeout and keep
                # polling — the isalive() check above handles actual death.
                continue
            if raw == b"0011Ignore":
                continue

            text = raw.decode("utf-8", errors="replace")
            # Normalize CRLF to LF before writing to stdout.
            # Windows console auto-expands \n to \r\n; writing \r\n directly
            # produces \r\r\n which renders as a blank line.
            text = text.replace("\r\n", "\n")
            # Always write to stdout (visible console) regardless of socket state
            sys.stdout.write(text)
            sys.stdout.flush()
            # Try to forward to parent socket; keep running if parent disconnects
            # so the visible terminal window stays usable independently.
            try:
                sock.sendall(text.encode("utf-8"))
            except (OSError, ConnectionResetError):
                pass

    # socket input (parent) -> PTY
    def socket_to_pty() -> None:
        while True:
            try:
                data = sock.recv(65536)
                if not data:
                    break
                proc.write(data.decode("utf-8", errors="replace"))
            except (OSError, ConnectionResetError):
                break

    t1 = threading.Thread(target=pty_to_socket, daemon=True)
    t2 = threading.Thread(target=socket_to_pty, daemon=True)
    t3 = threading.Thread(target=_stdin_to_pty, args=(proc, sys.stdin), daemon=True)
    resize_stop = threading.Event()
    t4 = threading.Thread(target=_resize_monitor, args=(proc, resize_stop), daemon=True)
    t1.start()
    t2.start()
    t3.start()
    t4.start()
    t1.join()
    t2.join()
    resize_stop.set()
    sock.close()


if __name__ == "__main__":
    main()
