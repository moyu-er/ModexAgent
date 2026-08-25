"""Unit tests for the Linux stdin-wait probe (fake /proc tree — host-agnostic)."""

from __future__ import annotations

import platform

import pytest

from modex_agent.tools.terminal._foreground_probe import (
    ProbeInternals,
    ProcStat,
    default_internals,
    foreground_pgid,
    is_stdin_waiting,
    parse_proc_stat,
    stdin_probe_available,
)


@pytest.fixture(autouse=True)
def _pin_x64_syscall_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the syscall-number table to x64.

    The fake syscalls below carry x64 syscall numbers; without pinning,
    an arm64 host would decode them against the arm64 table and every
    positive case would fail for host reasons, not probe reasons.
    """

    monkeypatch.setattr(platform, "machine", lambda: "x86_64")


def _stat_line(
    pid: int,
    *,
    comm: str = "bash",
    state: str = "S",
    pgrp: int = 100,
    session: int = 100,
    tpgid: int = 100,
) -> str:
    # Real layout: pid (comm) state ppid pgrp session tty_nr tpgid ... [19]=starttime
    filler = " ".join(["0"] * 13)
    return f"{pid} ({comm}) {state} {pid - 1} {pgrp} {session} 3 {tpgid} {filler} 12345"


class FakeProc:
    """In-memory /proc tree: file table + per-pid mem table + fd symlinks."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.mem: dict[int, bytes] = {}
        self.links: dict[str, str] = {}
        self.fds: dict[int, tuple[int, int]] = {}  # fd -> (address, length)
        self._next_fd = 10

    def add_process(
        self,
        pid: int,
        *,
        pgrp: int,
        session: int = 100,
        tpgid: int = 0,
        syscall: str | None = None,
        threads: list[str] | None = None,
        fdinfo: dict[int, str] | None = None,
        stdin_tty: bool = True,
    ) -> None:
        self.files[f"/proc/{pid}/stat"] = _stat_line(pid, pgrp=pgrp, session=session, tpgid=tpgid)
        self.links[f"/proc/{pid}/fd/0"] = "/dev/pts/0" if stdin_tty else "pipe:[4161910]"
        thread_ids = threads or ([str(pid)] if syscall is not None else [])
        for tid in thread_ids:
            if syscall is not None:
                self.files[f"/proc/{pid}/task/{tid}/syscall"] = syscall
        if fdinfo:
            for epfd, text in fdinfo.items():
                self.files[f"/proc/{pid}/fdinfo/{epfd}"] = text

    def add_mem(self, address: int, data: bytes) -> None:
        self.mem[address] = data

    def internals(self) -> ProbeInternals:
        fake = self

        def _read_file(path: str) -> str:
            try:
                return fake.files[path]
            except KeyError as exc:
                raise OSError(f"missing {path}") from exc

        def _readlink(path: str) -> str:
            try:
                return fake.links[path]
            except KeyError as exc:
                raise OSError(f"missing link {path}") from exc

        def _read_dir(path: str) -> list[str]:
            # /proc and /proc/<pid>/task listings derived from the file table
            if path == "/proc":
                pids = {
                    p.split("/")[2]
                    for p in fake.files
                    if p.startswith("/proc/") and p.count("/") == 3
                }
                return sorted(pids)
            prefix = path + "/"
            tids = {p[len(prefix) :].split("/")[0] for p in fake.files if p.startswith(prefix)}
            return sorted(tids)

        def _open_mem(path: str) -> int:
            del path  # the fake honors addresses in read_mem, not at open
            fd = fake._next_fd
            fake._next_fd += 1
            fake.fds[fd] = (0, 0)
            return fd

        def _read_mem(fd: int, length: int, address: int) -> bytes:
            del fd
            data = fake.mem.get(address)
            if data is None:
                raise OSError(f"no memory mapped at {address:#x}")
            return data[:length]

        def _close(fd: int) -> None:
            fake.fds.pop(fd, None)

        return ProbeInternals(
            read_file=_read_file,
            read_dir=_read_dir,
            open_mem=_open_mem,
            read_mem=_read_mem,
            close=_close,
            readlink=_readlink,
        )


# ---------------------------------------------------------------------------
# parse_proc_stat
# ---------------------------------------------------------------------------


def test_parse_proc_stat_with_spaces_in_comm() -> None:
    stat = parse_proc_stat(_stat_line(3732, comm="kworker/0:1"))
    assert stat is not None
    assert stat.pid == 3732
    assert stat.pgrp == 100
    assert stat.session == 100
    assert stat.tpgid == 100
    assert stat.state == "S"
    assert stat.started == "12345"


def test_parse_proc_stat_with_nested_parens() -> None:
    stat = parse_proc_stat(_stat_line(1, comm="(systemd)"))
    assert stat is not None
    assert stat.pid == 1


def test_parse_proc_stat_malformed() -> None:
    assert parse_proc_stat("") is None
    assert parse_proc_stat("no parens here") is None
    assert parse_proc_stat("12 (bash)") is None  # truncated after comm


# ---------------------------------------------------------------------------
# syscall judging
# ---------------------------------------------------------------------------


def _fake_with_syscall(pgid: int, syscall: str, *, arch_table: str = "x64") -> FakeProc:
    del arch_table
    fake = FakeProc()
    fake.add_process(200, pgrp=pgid, tpgid=pgid, syscall=syscall)
    return fake


@pytest.mark.parametrize(
    ("syscall", "expected"),
    [
        ("0 0x0 0x7fff 0x100", True),  # read(0, ...)
        ("0 0x5 0x7fff 0x100", False),  # read(5, ...) — not stdin
        ("0 0x1 0x7fff 0x100", False),  # read(1) — stdout
        ("running", False),  # running — cannot judge
        ("-1 0x0 0x0 0x0", False),  # exit-in-progress marker
    ],
)
def test_read_syscall_judgement(syscall: str, expected: bool) -> None:
    fake = _fake_with_syscall(100, syscall)
    assert is_stdin_waiting(100, fake.internals()) is expected


def test_select_stdin_in_fd_set() -> None:
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="23 0x1 0x7f00 0x0")
    fake.add_mem(0x7F00, b"\x01\x00\x00\x00\x00\x00\x00\x00")  # fd0 bit set
    assert is_stdin_waiting(100, fake.internals()) is True


def test_select_stdin_not_in_fd_set() -> None:
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="23 0x1 0x7f00 0x0")
    fake.add_mem(0x7F00, b"\x00\x02\x00\x00\x00\x00\x00\x00")  # fd1 bit only
    assert is_stdin_waiting(100, fake.internals()) is False


def test_select_address_zero() -> None:
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="23 0x1 0x0 0x0")
    assert is_stdin_waiting(100, fake.internals()) is False


def test_poll_stdin_pollin() -> None:
    import struct

    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="7 0x7f00 0x1 0x0")
    fake.add_mem(0x7F00, struct.pack("<ihh", 0, 0x001, 0).ljust(8, b"\x00"))
    assert is_stdin_waiting(100, fake.internals()) is True


def test_poll_other_fd() -> None:
    import struct

    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="7 0x7f00 0x1 0x0")
    fake.add_mem(0x7F00, struct.pack("<ihh", 3, 0x001, 0).ljust(8, b"\x00"))
    assert is_stdin_waiting(100, fake.internals()) is False


def test_poll_stdin_no_pollin() -> None:
    import struct

    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="7 0x7f00 0x1 0x0")
    fake.add_mem(0x7F00, struct.pack("<ihh", 0, 0x000, 0).ljust(8, b"\x00"))
    assert is_stdin_waiting(100, fake.internals()) is False


def test_epoll_watches_stdin() -> None:
    fake = FakeProc()
    fake.add_process(
        200,
        pgrp=100,
        tpgid=100,
        syscall="232 0x4 0x7f00 0x10",
        fdinfo={4: "tfd:      0\ninotify:..."},
    )
    assert is_stdin_waiting(100, fake.internals()) is True


def test_epoll_watches_other_fd() -> None:
    fake = FakeProc()
    fake.add_process(
        200, pgrp=100, tpgid=100, syscall="232 0x4 0x7f00 0x10", fdinfo={4: "tfd:      5\n"}
    )
    assert is_stdin_waiting(100, fake.internals()) is False


def test_socket_read_is_not_stdin_wait() -> None:
    # recvfrom on x64 is syscall 47 reading fd 3 — the pip-install shape
    fake = _fake_with_syscall(100, "47 0x3 0x7fff 0x100")
    assert is_stdin_waiting(100, fake.internals()) is False


# ---------------------------------------------------------------------------
# fd-0-is-tty guard (pipeline regression: `cmd | tail` false positive)
# ---------------------------------------------------------------------------


def test_pipeline_tail_read0_on_pipe_is_not_stdin_wait() -> None:
    # THE eval-run regression: tail blocks in read(0) but its fd 0 is the
    # PIPE from the producer — a running pipeline, not an input wait.
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="0 0x0 0x7fff 0x100", stdin_tty=False)
    assert is_stdin_waiting(100, fake.internals()) is False


def test_read0_on_tty_is_stdin_wait() -> None:
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="0 0x0 0x7fff 0x100", stdin_tty=True)
    assert is_stdin_waiting(100, fake.internals()) is True


def test_read_on_nonzero_tty_fd_is_stdin_wait() -> None:
    """ssh/sudo password reads open /dev/tty as a NON-zero fd and read(4) —
    the tty-backed fd proves the input wait the old fd-0-only rule missed."""
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="0 0x4 0x7fff 0x100")
    fake.links["/proc/200/fd/4"] = "/dev/tty"
    assert is_stdin_waiting(100, fake.internals()) is True


def test_read_on_nonzero_pipe_fd_is_not_stdin_wait() -> None:
    """A pipeline member reading a non-zero PIPE fd is a running pipeline,
    not an input wait — the tty check applies per-fd, not just fd 0."""
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="0 0x4 0x7fff 0x100")
    fake.links["/proc/200/fd/4"] = "pipe:[4161910]"
    assert is_stdin_waiting(100, fake.internals()) is False


def test_poll_stdin_on_pipe_is_not_stdin_wait() -> None:
    import struct

    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="7 0x7f00 0x1 0x0", stdin_tty=False)
    fake.add_mem(0x7F00, struct.pack("<ihh", 0, 0x001, 0).ljust(8, b"\x00"))
    assert is_stdin_waiting(100, fake.internals()) is False


def test_epoll_stdin_on_pipe_is_not_stdin_wait() -> None:
    fake = FakeProc()
    fake.add_process(
        200,
        pgrp=100,
        tpgid=100,
        syscall="232 0x4 0x7f00 0x10",
        fdinfo={4: "tfd:      0\n"},
        stdin_tty=False,
    )
    assert is_stdin_waiting(100, fake.internals()) is False


def test_missing_fd0_link_is_not_stdin_wait() -> None:
    # Unreadable fd 0 (permission / gone) — absent evidence, keep waiting.
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="0 0x0 0x7fff 0x100")
    del fake.links["/proc/200/fd/0"]
    assert is_stdin_waiting(100, fake.internals()) is False


def test_read0_on_console_tty_is_stdin_wait() -> None:
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="0 0x0 0x7fff 0x100")
    fake.links["/proc/200/fd/0"] = "/dev/console"
    assert is_stdin_waiting(100, fake.internals()) is True


def test_unknown_syscall_not_waiting() -> None:
    fake = _fake_with_syscall(100, "999 0x0 0x0 0x0")
    assert is_stdin_waiting(100, fake.internals()) is False


# ---------------------------------------------------------------------------
# group scanning semantics
# ---------------------------------------------------------------------------


def test_is_stdin_waiting_scans_pgrp_members() -> None:
    fake = FakeProc()
    # 200: group member blocked in read(0); 300: same group, running; 400: other group
    fake.add_process(200, pgrp=100, tpgid=100, syscall="0 0x0 0x7fff 0x100")
    fake.add_process(300, pgrp=100, tpgid=100)
    fake.add_process(400, pgrp=500, tpgid=500, syscall="0 0x0 0x7fff 0x100")
    assert is_stdin_waiting(100, fake.internals()) is True
    assert is_stdin_waiting(500, fake.internals()) is True
    assert is_stdin_waiting(999, fake.internals()) is False


def test_is_stdin_waiting_no_matching_group() -> None:
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=100, syscall="0 0x5 0x7fff 0x100")
    assert is_stdin_waiting(100, fake.internals()) is False


def test_is_stdin_waiting_multi_thread_scans_all_threads() -> None:
    fake = FakeProc()
    fake.add_process(
        200,
        pgrp=100,
        tpgid=100,
        syscall="47 0x3 0x7fff 0x100",  # main thread: socket
        threads=["200", "201"],
    )
    fake.files["/proc/200/task/201/syscall"] = "0 0x0 0x7fff 0x100"  # worker: stdin
    assert is_stdin_waiting(100, fake.internals()) is True


# ---------------------------------------------------------------------------
# foreground_pgid
# ---------------------------------------------------------------------------


def test_foreground_pgid_positive() -> None:
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=300)
    assert foreground_pgid(200, fake.internals()) == 300


def test_foreground_pgid_zero_is_none() -> None:
    fake = FakeProc()
    fake.add_process(200, pgrp=100, tpgid=0)
    assert foreground_pgid(200, fake.internals()) is None


def test_foreground_pgid_missing_pid() -> None:
    fake = FakeProc()
    assert foreground_pgid(999, fake.internals()) is None


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def test_default_internals_constructs() -> None:
    internals = default_internals()
    assert isinstance(internals, ProbeInternals)


def test_stdin_probe_available_is_bool() -> None:
    assert isinstance(stdin_probe_available(), bool)


def test_proc_stat_is_frozen() -> None:
    stat = ProcStat(1, 0, 1, 1, "S", 1, "123")
    with pytest.raises(Exception):  # noqa: B017 — frozen dataclass contract
        stat.pid = 2  # type: ignore[misc]
