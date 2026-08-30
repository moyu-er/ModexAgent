"""Persistent-bash contract tests — description examples + kill semantics.

Two pinned contracts from the TB v5 post-mortem (cluster A: teardown
SIGKILLed session-scoped background services before the verifier ran):

1. The ``bash`` tool description teaches the four background/service
   example classes — long job + bounded log polling, setsid-detached
   service, bounded readiness wait, client-side check.  A pure string
   contract; runs on every host.
2. ``_kill_process_session`` scans by SESSION id: a setsid-detached
   process (own session id) deliberately survives the kill — the escape
   hatch that keeps agent-started services alive across timeout resets
   and session teardown — while a plain background job (same session)
   dies with the shell.  Proven against a REAL
   ``PersistentShellSession`` on Linux (/proc session scan); skipped
   elsewhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import re
import shutil
import sys
from pathlib import Path
from time import monotonic

import pytest

from modex_agent.tools.terminal._foreground_probe import ProcStat, parse_proc_stat
from modex_agent.tools.terminal._persistent_session import PersistentShellSession
from modex_agent.tools.terminal.persistent_bash import PersistentBashTool

# The kill-semantics proof needs the Linux /proc session-id scan — the
# non-Linux fallback targets the session leader's process GROUP, and a
# plain background job owns a different pgid under job control, so the
# contract's "plain job dies" half is not guaranteed there — plus the
# real session's tools (bash/pexpect/setsid).
_KILL_CONTRACT_HOST = (
    sys.platform != "win32"
    and Path("/proc").is_dir()
    and shutil.which("bash") is not None
    and shutil.which("setsid") is not None
    and importlib.util.find_spec("pexpect") is not None
)


def test_description_contains_background_service_examples() -> None:
    """The four example classes the v5 failures demanded: backgrounded long
    job with bounded log polling, setsid-detached service, bounded readiness
    wait, and client-side progress checks."""
    tool = PersistentBashTool(timeout_seconds=480, max_output_chars=16_000)
    desc = tool.description
    for keyword in ("setsid nohup", "tail -n 20", "seq 1 30", "curl -sf"):
        assert keyword in desc, f"description lost the {keyword!r} background example"


def test_description_declares_memory_hard_limit_and_cpu_guidance() -> None:
    """TB2.1 contract (190 tesseract workers vs 1 CPU / 2 GB — exit 137
    OOM-kill, twice): memory is a HARD limit statement (unambiguous,
    overcommit's tool result is a silent kill); CPU is guidance with a
    bounded-batch pattern — the exact numbers live in the system prompt's
    ``## Runtime`` section."""
    tool = PersistentBashTool(timeout_seconds=480, max_output_chars=16_000)
    desc = tool.description
    for keyword in ("Memory is a hard limit", "OOM-killed", "$(nproc)"):
        assert keyword in desc, f"description lost the {keyword!r} resource-limit marker"
    assert "IO-bound" in desc


# ── kill-semantics contract (real session; Linux only) ────────────────


def _stat_of(pid: int) -> ProcStat | None:
    """Live /proc stat for *pid* — None once the process is gone."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_proc_stat(text)


def _alive(pid: int) -> bool:
    """Alive = /proc entry exists and is not a zombie/dead entry."""
    stat = _stat_of(pid)
    return stat is not None and stat.state not in ("Z", "X")


async def _await_pid_file(path: Path, timeout_s: float = 10.0) -> int:
    """Poll for the setsid sleeper's pid file (it writes its own pid)."""
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            await asyncio.sleep(0.2)
    raise AssertionError(f"setsid sleeper never wrote its pid file: {path}")


async def _eventually_dead(pid: int, timeout_s: float = 10.0) -> bool:
    """Poll for the process to disappear (SIGKILL + reap is not instant)."""
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.2)
    return False


@pytest.mark.skipif(
    not _KILL_CONTRACT_HOST,
    reason="kill-contract proof needs the Linux /proc session scan + bash/pexpect/setsid",
)
async def test_setsid_descendant_survives_session_kill(tmp_path: Path) -> None:
    """THE contract: the session kill (the timeout-reset / teardown path)
    SIGKILLs every process still IN the PTY session and deliberately does
    NOT chase descendants that left it via ``setsid`` (own session id).
    A plain background job dies with the shell; the setsid-detached
    service survives — the escape hatch the description teaches."""
    session = PersistentShellSession(timeout_seconds=30)
    pid_file = tmp_path / "setsid_sleeper.pid"
    setsid_pid: int | None = None
    try:
        # One command starts both sleepers inside the session's shell:
        # the setsid sleeper records its own pid (it becomes the NEW
        # session's leader); $! reports the plain background job's pid.
        out = await session.run_command(
            f"setsid sh -c 'echo $$ > {pid_file}; exec sleep 300' & sleep 300 & echo plain=$!"
        )
        match = re.search(r"plain=(\d+)", out)
        assert match is not None, f"plain sleeper pid not reported: {out!r}"
        plain_pid = int(match.group(1))
        setsid_pid = await _await_pid_file(pid_file)
        assert _alive(plain_pid), "plain background sleeper never came up"
        assert _alive(setsid_pid), "setsid sleeper never came up"

        # Precondition — the escape hatch itself: the plain job stayed in
        # the shell's session; the setsid sleeper leads a session of its
        # own (sid == pid), away from the shell's.
        shell_pid = session._proc.pid  # noqa: SLF001 — the scan key itself
        plain_stat = _stat_of(plain_pid)
        setsid_stat = _stat_of(setsid_pid)
        assert plain_stat is not None and setsid_stat is not None
        assert plain_stat.session == shell_pid, "plain job is not in the shell session"
        assert setsid_stat.session == setsid_pid != shell_pid, (
            "setsid sleeper did not leave the shell session"
        )

        # The production teardown path: close() → _terminate_session_sync
        # → _kill_process_session (session-id scan).
        await session.close()

        assert await _eventually_dead(plain_pid), (
            f"plain background job {plain_pid} survived the session kill"
        )
        assert _alive(setsid_pid), f"setsid sleeper {setsid_pid} was chased out of its own session"
    finally:
        if setsid_pid is not None:
            # The test must not leak the surviving service it just proved.
            with contextlib.suppress(OSError):
                os.kill(setsid_pid, 9)
            await _eventually_dead(setsid_pid, timeout_s=5.0)
        with contextlib.suppress(Exception):
            await session.close()
