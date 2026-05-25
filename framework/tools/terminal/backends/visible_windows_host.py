"""Windows visible terminal host process.

Launched by VisibleWindowsPtyBackend with CREATE_NEW_CONSOLE so it owns a
visible console window.  Creates a winpty.PtyProcess and forwards I/O via
TCP socket so the parent process and the visible window share the same data
stream.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from typing import Protocol, TextIO

_READ_TIMEOUT = 0.5  # seconds for each socket recv()

# Map Windows extended-key scan codes → ANSI escape sequences.
_EXT_KEY_MAP: dict[str, str] = {
    "H": "\x1b[A",   # Up
    "P": "\x1b[B",   # Down
    "K": "\x1b[D",   # Left
    "M": "\x1b[C",   # Right
    "G": "\x1b[H",   # Home
    "O": "\x1b[F",   # End
    "R": "\x1b[2~",  # Insert
    "S": "\x1b[3~",  # Delete
    "I": "\x1b[5~",  # Page Up
    "Q": "\x1b[6~",  # Page Down
}


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


def _ignore_ctrl_c() -> None:
    """Ignore Windows CTRL_C_EVENT so Ctrl+C is forwarded to PTY instead of
    killing this host process.
    """
    try:
        import ctypes
    except ImportError:
        return

    kernel32 = ctypes.windll.kernel32
    CTRL_C_EVENT = 0

    # SetConsoleCtrlHandler with NULL (0) disables default handling
    # and prevents the process from being killed by Ctrl+C.
    kernel32.SetConsoleCtrlHandler(None, True)


def _stdin_to_pty(proc: WritablePty, stdin: TextIO) -> None:
    """Forward user input from the visible console to the PTY.

    When *stdin* is the real system console, reads keystrokes character-by-
    character so interactive programs (python, vim, password prompts) work
    correctly.  Falls back to line-buffered ``readline()`` for tests or
    redirected input.
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

    # Real Windows console — use raw character input.
    import msvcrt

    while True:
        if msvcrt.kbhit():
            try:
                ch = msvcrt.getwch()
            except OSError:
                break
            if ch == "\r":
                proc.write("\n")
            elif ch == "\x08":          # Backspace
                proc.write("\x08")
            elif ch == "\t":            # Tab
                proc.write("\t")
            elif ch == "\x03":          # Ctrl+C
                proc.write("\x03")
            elif ch == "\x1a":          # Ctrl+Z
                proc.write("\x1a")
            elif ch == "\x00" or ch == "\xe0":
                # Extended-key prefix (arrow keys, function keys, etc.)
                try:
                    code = msvcrt.getwch()
                except OSError:
                    break
                seq = _EXT_KEY_MAP.get(code)
                if seq:
                    proc.write(seq)
            else:
                proc.write(ch)
        else:
            time.sleep(0.01)


def _spawn_pty(shell: str, cwd: str | None = None, env: dict[str, str] | None = None) -> VisiblePtyProcess:
    """Spawn the visible host PTY using pywinpty's WinPTY backend."""
    import winpty

    kwargs: dict = {"dimensions": (30, 120), "backend": winpty.Backend.WinPTY}
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
    _ignore_ctrl_c()

    # Use a generous default size so bash readline doesn't wrap prematurely
    # and corrupt the cursor position.  The real console window may be resized
    # by the user; pywinpty handles the mismatch gracefully.
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
    t1.start()
    t2.start()
    t3.start()
    t1.join()
    t2.join()
    sock.close()


if __name__ == "__main__":
    main()
