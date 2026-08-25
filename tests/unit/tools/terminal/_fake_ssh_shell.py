"""Fake ssh-to-a-real-bash for the persistent-bash passthrough test.

Mimics ``ssh root@host`` where the REMOTE end is a real bash shell:

* prints ``root@127.0.0.1's password: `` and reads the answer from
  ``/dev/tty`` (echo off via termios);
* on success prints a login banner, then EXECs into a real interactive
  bash (``--noprofile --norc -i``, PS1 ``fake-remote$ ``) sharing the
  same pty — every later line (wrappers included) is executed by real
  bash, markers flow through, ``exit`` ends the session exactly like a
  remote login shell inside ssh.

Usage: ``python3 _fake_ssh_shell.py <expected-password>``
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
    os.write(fd, b"\nWelcome to FakeRemote (GNU/Linux)\n\n")
    env = dict(os.environ)
    env["PS1"] = sys.argv[2] if len(sys.argv) > 2 else "fake-remote$ "
    env["PS2"] = ""
    os.execvpe("bash", ["bash", "--noprofile", "--norc", "-i"], env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
