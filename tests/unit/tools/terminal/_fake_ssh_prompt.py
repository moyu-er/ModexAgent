"""Fake ssh-shaped program for the persistent-bash interactive tests.

Mimics the load-bearing ssh behaviors (not a real network):

* prints ``root@127.0.0.1's password: `` and reads the answer from
  ``/dev/tty`` (an fd of its own, echo off via termios) — the ssh
  password-read shape the kernel probe's fd-agnostic read rule targets;
* on the correct password prints a banner and a BRACKETED remote prompt
  ``[root@fakehost ~]# `` — the common Alibaba/RHEL default PS1 form;
* then line-serves: ``exit`` ends the process, anything else echoes
  ``ECHO:<line>`` and reprints the prompt.

Usage: ``python3 _fake_ssh_prompt.py <expected-password>``
"""

from __future__ import annotations

import os
import sys
import termios


def _open_tty() -> int:
    try:
        return os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return 0


def _readline(fd: int, *, echo: bool) -> str:
    saved = None
    if not echo:
        try:
            saved = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, new)
        except termios.error:
            saved = None
    buf = b""
    while True:
        ch = os.read(fd, 1)
        if ch in (b"\r", b"\n", b""):
            break
        buf += ch
    if saved is not None:
        termios.tcsetattr(fd, termios.TCSANOW, saved)
    return buf.decode("utf-8", errors="replace")


def main() -> int:
    expected = sys.argv[1] if len(sys.argv) > 1 else "pw"
    fd = _open_tty()
    os.write(fd, b"root@127.0.0.1's password: ")
    if _readline(fd, echo=False) != expected:
        os.write(fd, b"\nPermission denied (password).\n")
        return 1
    os.write(fd, b"\nWelcome to FakeOS 1.0 (GNU/Linux)\n\n")
    while True:
        # Modern bash (4.4+) readline enables bracketed paste right after
        # printing the prompt — the escape bytes land IN the prompt line.
        os.write(fd, b"[root@fakehost ~]# \x1b[?2004h")
        cmd = _readline(fd, echo=True).strip()
        if not cmd:
            continue
        if cmd == "exit":
            return 0
        os.write(fd, f"ECHO:{cmd}\n".encode())


if __name__ == "__main__":
    raise SystemExit(main())
