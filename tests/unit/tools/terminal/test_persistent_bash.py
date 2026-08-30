"""Real-protocol tests for PersistentBashTool / BashInputTool.

These spawn a REAL interactive bash through pexpect — the marker protocol
IS the deliverable, so the shell is not mocked.  They require a POSIX
host with bash and pexpect; skipped elsewhere (Windows CI).  On Linux
they additionally exercise the real /proc stdin-wait probe.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
from time import monotonic

import pytest

from modex_agent.tools.terminal._foreground_probe import stdin_probe_available as _probe_available
from modex_agent.tools.terminal.persistent_bash import BashInputTool, PersistentBashTool

_HAS_PERSISTENT_BASH = (
    sys.platform != "win32"
    and shutil.which("bash") is not None
    and importlib.util.find_spec("pexpect") is not None
)

pytestmark = pytest.mark.skipif(
    not _HAS_PERSISTENT_BASH, reason="persistent bash requires POSIX pexpect + /bin/bash"
)


async def test_cwd_persists_across_calls(tmp_path):
    """`cd` in one call is visible to `pwd` in the next — the core property."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        sub = tmp_path / "sub"
        sub.mkdir()
        await tool.execute(command=f"cd '{sub}'")
        assert await tool.execute(command="pwd") == str(sub)
    finally:
        await tool.close()


async def test_initial_cwd_is_used(tmp_path):
    """The constructor's initial_cwd becomes the shell's starting directory."""
    tool = PersistentBashTool(initial_cwd=str(tmp_path), timeout_seconds=30)
    try:
        assert await tool.execute(command="pwd") == str(tmp_path)
    finally:
        await tool.close()


async def test_environment_persists_across_calls():
    """`export` in one call is visible to a later command."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        await tool.execute(command="export FOO=bar")
        assert await tool.execute(command="echo $FOO") == "bar"
    finally:
        await tool.close()


async def test_spawn_env_has_pager_cat_and_no_color(monkeypatch):
    """Env parity with SubprocessTool: the spawn env goes through
    build_full_env (PAGER=cat when the parent has none) plus NO_COLOR=1."""
    for var in ("PAGER", "MANPAGER", "GIT_PAGER", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        assert await tool.execute(command="echo $PAGER") == "cat"
        assert await tool.execute(command="echo $NO_COLOR") == "1"
    finally:
        await tool.close()


async def test_spawn_env_carries_modex_env_overrides():
    """_modex_env overrides set before the first command (spawn) are visible."""
    from modex_agent.runtime.env_context import _modex_env

    token = _modex_env.set({"MODEX_TEST_SPAWN_OVERRIDE": "visible-42"})
    try:
        tool = PersistentBashTool(timeout_seconds=30)
        try:
            out = await tool.execute(command="echo $MODEX_TEST_SPAWN_OVERRIDE")
            assert out == "visible-42"
        finally:
            await tool.close()
    finally:
        _modex_env.reset(token)


async def test_modex_env_set_after_spawn_does_not_reach_shell():
    """Persistent-shell semantics: the env is fixed at spawn; overrides set
    after the shell exists do NOT reach it (exports mutate it instead)."""
    from modex_agent.runtime.env_context import _modex_env

    tool = PersistentBashTool(timeout_seconds=30)
    try:
        await tool.execute(command="true")
        token = _modex_env.set({"MODEX_TEST_LATE_OVERRIDE": "late"})
        try:
            assert await tool.execute(command="echo $MODEX_TEST_LATE_OVERRIDE") == "[no output]"
        finally:
            _modex_env.reset(token)
    finally:
        await tool.close()


async def test_exit_code_marker_only_for_failure():
    """Non-zero exit appends `[exit code: N]` — always newline-separated,
    including when the body is the `[no output]` placeholder; zero exit
    appends nothing (and a no-output success is `[no output]`)."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        assert await tool.execute(command="false") == "[no output]\n[exit code: 1]"
        assert await tool.execute(command="true") == "[no output]"
    finally:
        await tool.close()


async def test_no_output_placeholder_preserves_whitespace():
    """Only a LENGTH-ZERO result becomes `[no output]`; the leading/
    trailing whitespace of real output is meaningful and preserved
    verbatim through the real PTY."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        assert await tool.execute(command="echo") == "[no output]"
        assert await tool.execute(command="printf ' '") == " "
        assert await tool.execute(command="printf '  pad  '") == "  pad  "
    finally:
        await tool.close()


async def test_marker_protocol_completion_and_stripping():
    """A multi-second command completes via its marker; the marker line is stripped."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        out = await tool.execute(command="sleep 1.5 && echo done")
        assert out == "done"
        assert "__DONE" not in out
        assert "__MODEX_PS1__" not in out
    finally:
        await tool.close()


async def test_stdout_stderr_interleaved_in_order():
    """The PTY merges streams — write order to stdout/stderr is preserved."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        out = await tool.execute(command="echo out1; echo err1 >&2; echo out2")
        assert out == "out1\nerr1\nout2"
    finally:
        await tool.close()


# ── stdin-wait evidence (probe layer on Linux; content fallback test below) ──


async def test_stdin_wait_returns_partial_and_bash_input_completes():
    """A stdin-reading command returns its prompt output plus the advisory
    ``[hint: ...]`` line; the kernel probe catches it within one 3s tick."""
    tool = PersistentBashTool(timeout_seconds=30)
    bash_input = BashInputTool(tool.manager)
    try:
        started = monotonic()
        out = await tool.execute(command="read -p 'Continue? ' X; echo got=$X")
        elapsed = monotonic() - started
        assert "Continue?" in out
        assert "[hint:" in out
        assert elapsed < 6.0
        resumed = await bash_input.execute(line="yes")
        assert resumed.strip() == "got=yes"
        assert "exit code" not in resumed
    finally:
        await tool.close()


@pytest.mark.skipif(
    not _probe_available(), reason="silent stdin waits are kernel-probe evidence (Linux only)"
)
async def test_silent_stdin_wait_caught_by_probe():
    """`read -s X` prints NOTHING — only kernel evidence can catch it; the
    probe does (bash blocks in read(0)) within one 3s tick. The empty
    partial becomes `[no output]` and the advisory hint follows after a
    blank separator line — never a standalone hint."""
    tool = PersistentBashTool(timeout_seconds=30)
    bash_input = BashInputTool(tool.manager)
    try:
        started = monotonic()
        out = await tool.execute(command="read -s X; echo got=$X")
        elapsed = monotonic() - started
        assert out.startswith("[no output]\n\n[hint:")
        assert elapsed < 6.0
        resumed = await bash_input.execute(line="abc")
        assert resumed.strip() == "got=abc"
        assert "exit code" not in resumed
    finally:
        await tool.close()


async def test_password_prompt_stdin_wait():
    """read -sp: the question line IS printed, the answer never echoes;
    the probe catches the wait and bash_input completes it."""
    tool = PersistentBashTool(timeout_seconds=30)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="read -sp 'Password: ' X; echo ok=$X")
        assert "Password:" in out
        assert "[hint:" in out
        resumed = await bash_input.execute(line="hunter2")
        assert resumed.strip() == "ok=hunter2"
    finally:
        await tool.close()


async def test_content_fallback_when_probe_unavailable(monkeypatch):
    """Probe-unavailable hosts (macOS shape): the content keyword fallback
    still surfaces the prompt — never a hard wait to timeout."""
    import modex_agent.tools.terminal._persistent_session as session_mod

    monkeypatch.setattr(session_mod, "stdin_probe_available", lambda: False)
    tool = PersistentBashTool(timeout_seconds=30)
    bash_input = BashInputTool(tool.manager)
    try:
        started = monotonic()
        out = await tool.execute(command="read -p 'Continue? ' X; echo got=$X")
        elapsed = monotonic() - started
        assert "Continue?" in out
        assert "[hint:" in out
        assert elapsed < 5.0
        resumed = await bash_input.execute(line="y")
        assert resumed.strip() == "got=y"
    finally:
        await tool.close()


# ── zombie-shell regression (the eval-run failure mode) ──


async def test_silent_long_command_never_misjudged_as_stdin_wait():
    """THE zombie regression: a command silent longer than the probe
    interval (pip-download shape) runs to completion — kernel evidence
    says nanosleep/socket, not stdin."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        started = monotonic()
        out = await tool.execute(command="sleep 3.5 && echo done")
        elapsed = monotonic() - started
        assert out == "done"
        assert elapsed >= 3.4  # truly waited; no premature return
        assert "waiting for input" not in out
    finally:
        await tool.close()


async def test_sparse_output_long_command_completes():
    """Sparse progress output across probe ticks completes via its marker."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        out = await tool.execute(command="for i in 1 2 3 4; do echo line$i; sleep 1.2; done")
        assert out == "line1\nline2\nline3\nline4"
        assert "waiting for input" not in out
    finally:
        await tool.close()


async def test_pipeline_tail_pipe_read_is_not_stdin_wait():
    """THE eval-run zombie regression: `producer | tail -3` — tail blocks in
    read(0) on the PIPE for the whole production window (past one probe
    tick); the fd-0-is-tty guard must keep the probe silent so the command
    completes via its marker instead of early-returning empty at 3s."""
    from time import monotonic as _mono

    tool = PersistentBashTool(timeout_seconds=30)
    try:
        started = _mono()
        out = await tool.execute(command="(sleep 6; echo done) | tail -1")
        elapsed = _mono() - started
        assert out == "done"
        assert elapsed >= 6.0  # no premature 3s-tick return
    finally:
        await tool.close()


async def test_ps1_eaten_command_self_heals():
    """A new command sent while a stdin-waiter is pending is REJECTED by
    the phase guard (its wrapper would be eaten as the waiter's input);
    answering via bash_input completes the pending transaction and the
    shell is healthy again."""
    tool = PersistentBashTool(timeout_seconds=30)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="read -p 'value: ' X; echo got=$X")
        assert "[hint:" in out
        busy = await tool.execute(command="echo hi")
        assert busy.startswith("[Error]")
        resumed = await bash_input.execute(line="val")
        assert "got=val" in resumed
        final = await tool.execute(command="echo healthy")
        assert final == "healthy"
    finally:
        await tool.close()


# ── timeout: kill session + reset + three-part message ──


async def test_timeout_kills_resets_and_reports(tmp_path):
    """On timeout the process session dies, the shell resets (cwd back to
    initial_cwd, env lost), and the result carries the notice."""
    tool = PersistentBashTool(initial_cwd=str(tmp_path), timeout_seconds=2)
    try:
        await tool.execute(command="export TB_MARKER=1")
        await tool.execute(command="cd /")
        out = await tool.execute(command="sleep 30")
        assert "timed out after 2 seconds" in out
        assert "partial output" in out
        assert "shell was reset" in out
        assert "NOT preserved" in out
        # empty partial converged onto the placeholder; the old
        # "(no output captured)" special-case branch is gone
        assert "[no output]" in out
        assert "(no output captured)" not in out
        # fresh shell: initial cwd restored, exported env gone
        assert await tool.execute(command="pwd") == str(tmp_path)
        assert await tool.execute(command="echo $TB_MARKER") == "[no output]"
    finally:
        await tool.close()


async def test_default_timeout_allows_long_command():
    """Default 480s deadline: a multi-second command is nowhere near it."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        out = await tool.execute(command="sleep 2.5 && echo done")
        assert out == "done"
        assert "timed out" not in out
        assert "exit code" not in out
    finally:
        await tool.close()


# ── output overflow contract ──


async def test_output_overflow_head_tail_elision():
    """Oversized output clips via the framework's single-source ratios
    (``split_head_tail`` 10%/15% — same policy as the overflow
    interceptor; the session picks no ratios of its own): head + explicit
    elision marker + tail (render_overflow_text). Production pools pass
    ``max_output_chars=None`` and the interceptor owns truncation."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        out = await tool.execute(command="printf 'a%.0s' {1..20000}; echo")
        assert "OUTPUT ELIDED" in out
        assert out.startswith("a")  # head kept
        assert "Full output (20000 chars total) NOT saved" in out  # tail notice
        from modex_agent.tools.overflow.truncate import split_head_tail

        head, tail = split_head_tail(16_000)
        assert len(out) <= head + tail + 400  # framework budget + markers
        assert out.count("a") >= head + tail - 100
    finally:
        await tool.close()


async def test_no_internal_clip_when_max_output_chars_none():
    """max_output_chars=None: no internal clipping — the full text flows to
    the framework overflow interceptor."""
    tool = PersistentBashTool(timeout_seconds=30, max_output_chars=None)
    try:
        out = await tool.execute(command="printf 'a%.0s' {1..20000}; echo")
        assert "OUTPUT ELIDED" not in out
        assert len(out.strip()) == 20_000
    finally:
        await tool.close()


# ── lifecycle ──


async def test_background_process_persists_between_calls():
    """A backgrounded process stays alive across tool calls."""
    tool = PersistentBashTool(timeout_seconds=30)
    try:
        out = await tool.execute(command="sleep 30 & echo bg=$!")
        match = re.search(r"bg=(\d+)", out)
        assert match is not None
        pid = int(match.group(1))
        check = await tool.execute(command=f"kill -0 {pid} && echo running")
        assert "running" in check
        await tool.execute(command=f"kill {pid} || true")
    finally:
        await tool.close()


async def test_close_terminates_shell_no_zombie():
    """close() kills and reaps the shell process."""
    tool = PersistentBashTool(timeout_seconds=30)
    out = await tool.execute(command="echo $$")
    pid = int(out.strip())
    await tool.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_error_paths():
    """Empty command and bash_input without a pending wait return clear errors."""
    tool = PersistentBashTool(timeout_seconds=30)
    bash_input = BashInputTool(tool.manager)
    try:
        assert "[Error]" in await tool.execute(command="   ")
        assert "[Error]" in await bash_input.execute(line="x")
    finally:
        await tool.close()
