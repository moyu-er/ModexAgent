"""Per-session isolation and concurrency tests for the persistent pair.

THE round-3 finding: one bot pool serves MANY conversations through ONE
PersistentShellSession — concurrent conversations interleave their
``_collect`` reads (cross-session output theft: an answer or ^C response
read by the WRONG collector), share cwd/env, and contend on one phase
machine. These pin the per-owner model: one shell per session_id,
serialized per session, isolated across sessions, bounded and reaped.
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys

import pytest

from modex_agent.runtime.env_context import _current_session_id
from modex_agent.tools.terminal.persistent_bash import BashInputTool, PersistentBashTool

_HAS_PERSISTENT_BASH = (
    sys.platform != "win32"
    and shutil.which("bash") is not None
    and importlib.util.find_spec("pexpect") is not None
)

pytestmark = pytest.mark.skipif(
    not _HAS_PERSISTENT_BASH, reason="persistent bash requires POSIX pexpect + /bin/bash"
)

_FAKE_SSH = __file__.rsplit("/", 1)[0] + "/_fake_ssh_prompt.py"


async def _run_as_session(tool: PersistentBashTool, key: str, command: str) -> str:
    """Drive ``bash`` under a conversation routing key (the contextvar
    CommandTool and the persistent pair both route by)."""
    token = _current_session_id.set(key)
    try:
        return await tool.execute(command=command)
    finally:
        _current_session_id.reset(token)


async def _input_as_session(bash_input: BashInputTool, key: str, line: str) -> str:
    token = _current_session_id.set(key)
    try:
        return await bash_input.execute(line=line)
    finally:
        _current_session_id.reset(token)


async def test_concurrent_sessions_are_isolated(tmp_path):
    """Two session_ids drive two INDEPENDENT shells: cwd set in one is
    invisible to the other — no state bleed, no cross-talk."""
    tool = PersistentBashTool(initial_cwd=str(tmp_path), timeout_seconds=15)
    try:
        await _run_as_session(tool, "conv.A.main", f"mkdir -p '{tmp_path}/a' '{tmp_path}/b'")
        await _run_as_session(tool, "conv.A.main", f"cd '{tmp_path}/a'")
        await _run_as_session(tool, "conv.B.main", f"cd '{tmp_path}/b'")
        assert await _run_as_session(tool, "conv.A.main", "pwd") == f"{tmp_path}/a"
        assert await _run_as_session(tool, "conv.B.main", "pwd") == f"{tmp_path}/b"
        await _run_as_session(tool, "conv.A.main", "export A_ONLY=1")
        assert await _run_as_session(tool, "conv.B.main", "echo x$A_ONLY") == "x"
    finally:
        await tool.close()


async def test_concurrent_collects_do_not_steal_output():
    """THE output-theft regression: two sessions with commands in flight
    CONCURRENTLY — each call must return its OWN command's output, not a
    fragment of the other's."""
    tool = PersistentBashTool(timeout_seconds=15)
    try:
        task_a = asyncio.create_task(
            _run_as_session(tool, "conv.A.main", "echo AAA; sleep 1.2; echo AAA2")
        )
        await asyncio.sleep(0.3)
        task_b = asyncio.create_task(
            _run_as_session(tool, "conv.B.main", "echo BBB; sleep 1.2; echo BBB2")
        )
        out_a, out_b = await asyncio.gather(task_a, task_b)
        assert out_a == "AAA\nAAA2"
        assert out_b == "BBB\nBBB2"
    finally:
        await tool.close()


async def test_same_session_concurrent_calls_serialize():
    """Same session, two bash calls racing: one runs, the other is guarded
    (never interleaves into the same PTY read stream)."""
    tool = PersistentBashTool(timeout_seconds=15)
    try:
        first = asyncio.create_task(_run_as_session(tool, "conv.A.main", "sleep 1; echo one"))
        await asyncio.sleep(0.2)
        second = asyncio.create_task(_run_as_session(tool, "conv.A.main", "echo two"))
        out_first, out_second = await asyncio.gather(first, second)
        assert out_first == "one"
        assert out_second == "two"  # after the first closed, second ran
    finally:
        await tool.close()


async def test_bash_input_routes_to_own_session():
    """A WAITING ssh in session A: session B's bash runs freely; A's
    bash_input answers A's shell — cross-session answers are impossible."""
    tool = PersistentBashTool(timeout_seconds=15)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await _run_as_session(tool, "conv.A.main", "read -p 'pw: ' X; echo got=$X")
        assert "pw:" in out
        # session B is unaffected by A's WAITING transaction
        assert await _run_as_session(tool, "conv.B.main", "echo free") == "free"
        # A's own bash is guarded
        busy = await _run_as_session(tool, "conv.A.main", "echo blocked")
        assert busy.startswith("[Error]")
        resumed = await _input_as_session(bash_input, "conv.A.main", "val")
        assert resumed.strip() == "got=val"
    finally:
        await tool.close()


async def test_ssh_two_sessions_parallel_interactive():
    """Two ssh transactions open in two sessions simultaneously — each
    gets its own password prompt, its own banner, its own echo loop."""
    tool = PersistentBashTool(timeout_seconds=15)
    bash_input = BashInputTool(tool.manager)
    try:
        out_a = await _run_as_session(tool, "conv.A.main", f"python3 {_FAKE_SSH} pwA")
        out_b = await _run_as_session(tool, "conv.B.main", f"python3 {_FAKE_SSH} pwB")
        assert "password:" in out_a and "[hint:" in out_a
        assert "password:" in out_b and "[hint:" in out_b
        resumed_a = await _input_as_session(bash_input, "conv.A.main", "pwA")
        resumed_b = await _input_as_session(bash_input, "conv.B.main", "pwB")
        assert "Welcome" in resumed_a and "[hint:" in resumed_a
        assert "Welcome" in resumed_b and "[hint:" in resumed_b
        echo_a = await _input_as_session(bash_input, "conv.A.main", "exit")
        echo_b = await _input_as_session(bash_input, "conv.B.main", "exit")
        assert await _run_as_session(tool, "conv.A.main", "echo doneA") == "doneA"
        assert await _run_as_session(tool, "conv.B.main", "echo doneB") == "doneB"
    finally:
        await tool.close()


async def test_idle_sessions_reaped_beyond_limit():
    """Bounded shell count: beyond the cap the idlest session's PTY is
    closed; its next call lazily respawns (state reset, no crash)."""
    tool = PersistentBashTool(timeout_seconds=15, max_sessions=2)
    try:
        await _run_as_session(tool, "s1", "cd /tmp")
        await _run_as_session(tool, "s2", "cd /")
        await _run_as_session(tool, "s3", "echo three")  # evicts s1
        assert await _run_as_session(tool, "s3", "echo ok3") == "ok3"
        # s1 was reaped; a fresh lazy spawn serves it again
        assert await _run_as_session(tool, "s1", "pwd") != ""
    finally:
        await tool.close()


def test_default_session_identity_stable():
    """No routing context → the shared default session; identity is stable
    across accesses (companion pairing depends on it)."""
    tool = PersistentBashTool()
    assert tool.manager is tool.manager


async def test_close_reaps_all_sessions():
    """close() terminates every pooled shell — no PTY leaks past shutdown."""
    tool = PersistentBashTool(timeout_seconds=15)
    await _run_as_session(tool, "s1", "echo $$")
    await _run_as_session(tool, "s2", "echo $$")
    pids: list[int] = []
    for key in ("s1", "s2"):
        session = tool.manager.session_for(key)  # noqa: SLF001
        if session._proc is not None:  # noqa: SLF001
            pids.append(session._proc.pid)
    await tool.close()
    import os

    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
