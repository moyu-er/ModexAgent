"""Linux ``/proc``-based foreground-group stdin-wait evidence.

This module answers ONE question deterministically, from kernel state:

    Is the terminal's foreground process group blocked reading stdin
    RIGHT NOW?

No heuristics: a process is "waiting for input" only when one of its
threads is parked in ``read`` on a fd that is the session's controlling
terminal (ANY fd — password readers open ``/dev/tty`` as a non-zero fd),
``select``/``pselect`` with fd 0 in the read set, ``poll``/``ppoll`` with
fd 0 + ``POLLIN``, or ``epoll_wait`` whose fdinfo watches fd 0. Device
numbers prove the terminal identity; the ``/dev/tty`` alias is accepted.
For select/poll/epoll families, only an indefinite wait (NULL timeout
pointer or -1 timeout) is evidence. Bounded polling (ffmpeg key checks,
progress bars, event-loop ticks) is a running command, not an input wait.
A pipeline member like ``cmd | tail`` blocks on its PIPE, not the terminal,
and a silent command like ``pip install`` blocks in socket reads, so both
are correctly reported as running.

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
    "controlling_tty_device",
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
    tty_nr: int
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
    stat_rdev: Callable[[str], int | None]


def default_internals() -> ProbeInternals:
    """Real ``/proc`` bindings (``lseek``+``read`` — portable os members)."""

    def _open_mem(path: str) -> int:
        return os.open(path, os.O_RDONLY)

    def _read_mem(fd: int, length: int, address: int) -> bytes:
        os.lseek(fd, address, os.SEEK_SET)
        return os.read(fd, length)

    def _stat_rdev(path: str) -> int | None:
        if sys.platform == "win32":
            return None
        try:
            return os.stat(path).st_rdev
        except OSError:
            return None

    return ProbeInternals(
        read_file=lambda p: Path(p).read_text(encoding="utf-8", errors="replace"),
        read_dir=lambda p: os.listdir(p),
        open_mem=_open_mem,
        read_mem=_read_mem,
        close=os.close,
        readlink=os.readlink,
        stat_rdev=_stat_rdev,
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
_U64_MAX: int = (1 << 64) - 1


def _decode_kdev(raw: int) -> tuple[int, int]:
    major = ((raw >> 8) & 0xFFF) | ((raw >> 32) & ~0xFFF)
    minor = (raw & 0xFF) | ((raw >> 12) & 0xFFFFFF00)
    return major, minor


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
        tty_nr = int(rest[4])
        tpgid = int(rest[5])
    except ValueError:
        return None
    started = rest[19]
    return ProcStat(
        pid=pid,
        parent_pid=parent_pid,
        pgrp=pgrp,
        session=session,
        tty_nr=tty_nr,
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


def controlling_tty_device(
    shell_pid: int,
    internals: ProbeInternals | None = None,
) -> tuple[int, int] | None:
    """Kernel device identity of the shell's controlling terminal."""
    ops = internals if internals is not None else default_internals()
    try:
        stat = parse_proc_stat(ops.read_file(f"/proc/{shell_pid}/stat"))
    except OSError:
        return None
    if stat is None or stat.tty_nr == 0:
        return None
    return _decode_kdev(stat.tty_nr)


def _read_syscall(
    ops: ProbeInternals,
    pid: int,
    tid: int,
) -> tuple[int, list[int]] | None:
    """``(syscall_number, up-to-6 args)`` or ``None`` when it cannot be judged."""
    try:
        text = ops.read_file(f"/proc/{pid}/task/{tid}/syscall").strip()
    except OSError:
        return None
    if not text or text == "running" or text.startswith("-1 "):
        return None
    fields = text.split()
    try:
        number = int(fields[0])
        args = [int(field, 16) for field in fields[1:7]]
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


def _fd_matches_terminal(
    ops: ProbeInternals,
    pid: int,
    fd: int,
    expected: tuple[int, int],
) -> bool:
    try:
        target = ops.readlink(f"/proc/{pid}/fd/{fd}")
    except OSError:
        return False
    if target == "/dev/tty":
        return True
    rdev = ops.stat_rdev(f"/proc/{pid}/fd/{fd}")
    return rdev is not None and _decode_kdev(rdev) == expected


def _syscall_waits_on_stdin(
    ops: ProbeInternals,
    pid: int,
    number: int,
    args: list[int],
    table: dict[str, int],
    expected_tty: tuple[int, int],
) -> bool:
    def _arg(index: int) -> int:
        return args[index] if len(args) > index else 0

    a0 = _arg(0)
    a1 = _arg(1)
    a2 = _arg(2)
    if number == table["read"]:
        return _fd_matches_terminal(ops, pid, a0, expected_tty)
    if number in (table.get("select", -1), table.get("pselect", -1)):
        if len(args) < 5:
            return False
        return (
            a0 >= 1
            and _fd_set_has_stdin(ops, pid, a1)
            and _fd_matches_terminal(ops, pid, 0, expected_tty)
            and _arg(4) == 0
        )
    if number == table.get("poll", -1):
        if len(args) < 3:
            return False
        return (
            a1 >= 1
            and _poll_has_stdin(ops, pid, a0, a1)
            and _fd_matches_terminal(ops, pid, 0, expected_tty)
            and a2 == _U64_MAX
        )
    if number == table.get("ppoll", -1):
        if len(args) < 3:
            return False
        return (
            a1 >= 1
            and _poll_has_stdin(ops, pid, a0, a1)
            and _fd_matches_terminal(ops, pid, 0, expected_tty)
            and a2 == 0
        )
    if number in (table.get("epoll_wait", -1), table.get("epoll_pwait", -1)):
        if len(args) < 4:
            return False
        return (
            a2 >= 1
            and _epoll_has_stdin(ops, pid, a0)
            and _fd_matches_terminal(ops, pid, 0, expected_tty)
            and _arg(3) == _U64_MAX
        )
    return False


def _numeric_entries(ops: ProbeInternals, path: str) -> list[int]:
    try:
        names = ops.read_dir(path)
    except OSError:
        return []
    return [int(name) for name in names if name.isdigit()]


def is_stdin_waiting(
    pgid: int,
    expected_tty: tuple[int, int],
    internals: ProbeInternals | None = None,
) -> bool:
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
            if _syscall_waits_on_stdin(ops, pid, number, args, table, expected_tty):
                return True
    return False
