"""Linux ``/proc``-based foreground-group stdin-wait evidence.

This module answers ONE question deterministically, from kernel state:

    Is the terminal's foreground process group blocked reading stdin
    RIGHT NOW?

No heuristics: a process is "waiting for input" only when one of its
threads is parked in ``read`` on a tty-backed fd (ANY fd — password
readers open ``/dev/tty`` as a non-zero fd), ``select``/``pselect`` with
fd 0 in the read set, ``poll``/``ppoll`` with fd 0 + ``POLLIN``, or
``epoll_wait`` whose fdinfo watches fd 0 — AND the watched fd is the
controlling terminal (a pipeline member like ``cmd | tail`` also blocks
in ``read``, but on its PIPE: that is a running pipeline, not an input
wait).  A silent long-running command (``pip install``) blocks in socket
reads — NOT stdin — so it is correctly reported as "running".

All functions are pure synchronous /proc reads (cheap: a few ms) and
never raise: unreadable entries are absent evidence, and the caller
treats absence as "not waiting" (conservative keep-waiting).
"""

from __future__ import annotations

import contextlib
import os
import platform
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ProcStat",
    "ProbeInternals",
    "default_internals",
    "foreground_pgid",
    "is_stdin_waiting",
    "parse_proc_stat",
    "stdin_probe_available",
]


@dataclass(frozen=True)
class ProcStat:
    """Fields used from Linux ``/proc/<pid>/stat``."""

    pid: int
    parent_pid: int
    pgrp: int
    session: int
    state: str
    tpgid: int
    started: str


@dataclass(frozen=True)
class ProbeInternals:
    """Injectable OS file boundary (deterministic unit tests)."""

    read_file: Callable[[str], str]
    read_dir: Callable[[str], list[str]]
    open_mem: Callable[[str], int]
    read_mem: Callable[[int, int, int], bytes]
    close: Callable[[int], None]
    readlink: Callable[[str], str]


def default_internals() -> ProbeInternals:
    """Real ``/proc`` bindings (``lseek``+``read`` — portable os members)."""

    def _open_mem(path: str) -> int:
        return os.open(path, os.O_RDONLY)

    def _read_mem(fd: int, length: int, address: int) -> bytes:
        os.lseek(fd, address, os.SEEK_SET)
        return os.read(fd, length)

    return ProbeInternals(
        read_file=lambda p: Path(p).read_text(encoding="utf-8", errors="replace"),
        read_dir=lambda p: os.listdir(p),
        open_mem=_open_mem,
        read_mem=_read_mem,
        close=os.close,
        readlink=os.readlink,
    )


# x64: read(0) select(23) pselect(270) poll(7) ppoll(271) epoll_wait(232) epoll_pwait(281)
# arm64: read(63) pselect(72) ppoll(73) epoll_pwait(22)
_SYSCALL_TABLES: dict[str, dict[str, int]] = {
    "x64": {
        "read": 0,
        "select": 23,
        "pselect": 270,
        "poll": 7,
        "ppoll": 271,
        "epoll_wait": 232,
        "epoll_pwait": 281,
    },
    "arm64": {
        "read": 63,
        "pselect": 72,
        "ppoll": 73,
        "epoll_pwait": 22,
    },
}

_POLLIN: int = 0x001


def _arch_key(machine: str) -> str:
    normalized = machine.lower()
    if normalized in ("x86_64", "amd64"):
        return "x64"
    if normalized in ("aarch64", "arm64"):
        return "arm64"
    return ""


def stdin_probe_available() -> bool:
    """True on a Linux host with a readable ``/proc`` (probe layer usable)."""
    return sys.platform.startswith("linux") and Path("/proc/self/stat").exists()


def parse_proc_stat(text: str) -> ProcStat | None:
    """Parse the fields the probe needs; ``None`` for malformed input.

    ``comm`` sits between the FIRST ``(`` and the LAST ``)`` of the line
    (comm itself may contain spaces and parentheses).
    """
    open_at = text.find("(")
    close_at = text.rfind(")")
    if open_at <= 0 or close_at <= open_at:
        return None
    try:
        pid = int(text[:open_at].strip())
    except ValueError:
        return None
    rest = text[close_at + 1 :].split()
    if len(rest) <= 19:
        return None
    state = rest[0]
    if len(state) != 1:
        return None
    try:
        parent_pid = int(rest[1])
        pgrp = int(rest[2])
        session = int(rest[3])
        tpgid = int(rest[5])
    except ValueError:
        return None
    started = rest[19]
    return ProcStat(
        pid=pid,
        parent_pid=parent_pid,
        pgrp=pgrp,
        session=session,
        state=state,
        tpgid=tpgid,
        started=started,
    )


def foreground_pgid(shell_pid: int, internals: ProbeInternals | None = None) -> int | None:
    """The kernel-recorded foreground process group of the shell's tty (``tpgid``)."""
    ops = internals if internals is not None else default_internals()
    try:
        stat = parse_proc_stat(ops.read_file(f"/proc/{shell_pid}/stat"))
    except OSError:
        return None
    if stat is None:
        return None
    return stat.tpgid if stat.tpgid > 0 else None


def _read_syscall(
    ops: ProbeInternals,
    pid: int,
    tid: int,
) -> tuple[int, list[int]] | None:
    """``(syscall_number, first-3 args)`` or ``None`` when it cannot be judged."""
    try:
        text = ops.read_file(f"/proc/{pid}/task/{tid}/syscall").strip()
    except OSError:
        return None
    if not text or text == "running" or text.startswith("-1 "):
        return None
    fields = text.split()
    try:
        number = int(fields[0])
        args = [int(field, 16) for field in fields[1:4]]
    except (IndexError, ValueError):
        return None
    return number, args


def _fd_set_has_stdin(ops: ProbeInternals, pid: int, address: int) -> bool:
    """fd_set bit test for fd 0 (byte 0, bit 0) at ``address`` in the process mem."""
    if address == 0:
        return False
    try:
        fd = ops.open_mem(f"/proc/{pid}/mem")
    except OSError:
        return False
    try:
        data = ops.read_mem(fd, 8, address)
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            ops.close(fd)
    return bool(data) and (data[0] & 0x1) == 0x1


def _poll_has_stdin(ops: ProbeInternals, pid: int, address: int, count: int) -> bool:
    """pollfd array scan: any entry with fd==0 and POLLIN in events."""
    if address == 0 or count <= 0:
        return False
    try:
        fd = ops.open_mem(f"/proc/{pid}/mem")
    except OSError:
        return False
    try:
        data = ops.read_mem(fd, min(count, 1024) * 8, address)
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            ops.close(fd)
    for offset in range(0, len(data) - 7, 8):
        poll_fd = struct.unpack_from("<i", data, offset)[0]
        events = struct.unpack_from("<h", data, offset + 4)[0]
        if poll_fd == 0 and (events & _POLLIN) != 0:
            return True
    return False


def _epoll_has_stdin(ops: ProbeInternals, pid: int, epfd: int) -> bool:
    """``fdinfo`` scan: does the epoll watch tfd 0?"""
    try:
        text = ops.read_file(f"/proc/{pid}/fdinfo/{epfd}")
    except OSError:
        return False
    for line in text.splitlines():
        if line.strip().startswith("tfd:") and line.split(":", 1)[1].strip().split()[:1] == ["0"]:
            return True
    return False


def _fd_is_tty(ops: ProbeInternals, pid: int, fd: int) -> bool:
    """Is the process's *fd* the controlling terminal?

    Pipeline members (``cmd | tail``) also block in ``read`` — but their
    fd is the PIPE, not the tty. Waiting "for terminal input" requires
    the fd being read to actually BE the terminal. This applies to ANY
    fd: password readers (ssh/sudo) open ``/dev/tty`` as a non-zero fd
    and read THAT — an input wait the fd-0-only rule missed.
    """
    try:
        target = ops.readlink(f"/proc/{pid}/fd/{fd}")
    except OSError:
        return False
    return (
        target.startswith("/dev/pts/") or target.startswith("/dev/tty") or target == "/dev/console"
    )


def _syscall_waits_on_stdin(
    ops: ProbeInternals,
    pid: int,
    number: int,
    args: list[int],
    table: dict[str, int],
) -> bool:
    a0 = args[0] if len(args) > 0 else 0
    a1 = args[1] if len(args) > 1 else 0
    a2 = args[2] if len(args) > 2 else 0
    if number == table["read"]:
        return _fd_is_tty(ops, pid, a0)
    # select/poll/epoll paths watch fd 0 through their sets — fd 0 must
    # be the tty for the wait to be a terminal-input wait.
    if not _fd_is_tty(ops, pid, 0):
        return False
    if number in (table.get("select", -1), table.get("pselect", -1)):
        return a0 >= 1 and _fd_set_has_stdin(ops, pid, a1)
    if number in (table.get("poll", -1), table.get("ppoll", -1)):
        return a1 >= 1 and _poll_has_stdin(ops, pid, a0, a1)
    if number in (table.get("epoll_wait", -1), table.get("epoll_pwait", -1)):
        return a2 >= 1 and _epoll_has_stdin(ops, pid, a0)
    return False


def _numeric_entries(ops: ProbeInternals, path: str) -> list[int]:
    try:
        names = ops.read_dir(path)
    except OSError:
        return []
    return [int(name) for name in names if name.isdigit()]


def is_stdin_waiting(pgid: int, internals: ProbeInternals | None = None) -> bool:
    """True when ANY thread of ANY process in *pgid* blocks reading stdin.

    Unknown architecture (empty syscall table) → always False (absent
    evidence). Read failures are skipped, never raised.
    """
    table = _SYSCALL_TABLES.get(_arch_key(platform.machine()))
    if table is None or pgid <= 0:
        return False
    ops = internals if internals is not None else default_internals()
    for pid in _numeric_entries(ops, "/proc"):
        try:
            stat = parse_proc_stat(ops.read_file(f"/proc/{pid}/stat"))
        except OSError:
            continue
        if stat is None or stat.pgrp != pgid:
            continue
        for tid in _numeric_entries(ops, f"/proc/{pid}/task"):
            syscall = _read_syscall(ops, pid, tid)
            if syscall is None:
                continue
            number, args = syscall
            if _syscall_waits_on_stdin(ops, pid, number, args, table):
                return True
    return False
