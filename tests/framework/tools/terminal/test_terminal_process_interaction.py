"""Process interaction workflow: password / y / yes / n / no prompts,
multi-line paste, send_keys (Ctrl-C / Ctrl-D), interrupt recovery, pager.

HIDDEN parametrization. Complementary to the VISIBLE test in
``test_terminal_send_keys_visible.py`` which exercises the visible-only
byte-delivery challenges (Ctrl-U / Tab via ConPTY).
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from modex_agent.tools.terminal.types import TerminalCommandStatus, TerminalVisibility

from .conftest import drain_current, output_of, run_command, wait_for_status

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only real-PTY workflow"),
]

_VIS = [pytest.param(TerminalVisibility.HIDDEN, id="hidden")]


async def _start_interactive(tools, command: str) -> None:
    """Run an interactive command, expect WAITING_INPUT via either content
    marker detection or idle-based fallback (catches ``read -s``).
    """
    res = await run_command(tools, command, timeout=15.0)
    lowered = res.lower()
    if "waiting_input" in lowered:
        return
    if "executing" in lowered or "timed_out" in lowered:
        await wait_for_status(
            tools,
            TerminalCommandStatus.WAITING_INPUT,
            timeout=12.0,
        )
        return
    raise AssertionError(f"interactive command unexpectedly finished:\n{res}")


async def _answer_and_verify(
    tools,
    answer: str,
    expected_marker: str,
    *,
    negative_marker: str | None = None,
) -> None:
    """Send ``answer`` via process write; verify ``expected_marker`` appears on
    its own line and ``negative_marker`` (if given) does NOT appear on its own.
    """
    write_res = await tools.process.execute(action="write", data=answer, submit=True)
    assert "rejected" not in write_res.lower(), (
        f"process write rejected:\n{write_res}"
    )
    drained = await drain_current(tools, expected_marker, attempts=30, delay=0.3)
    assert output_of(drained, expected_marker), (
        f"answer {answer!r} did not produce {expected_marker!r} on its own line:\n{drained}"
    )
    if negative_marker is not None:
        assert not output_of(drained, negative_marker), (
            f"unexpected branch {negative_marker!r} taken after answer {answer!r}:\n{drained}"
        )


@pytest.mark.parametrize("visibility", _VIS, indirect=True)
@pytest.mark.asyncio
async def test_hidden_process_interaction(tools) -> None:
    """One pipeline covering all five prompt shapes + paste + interrupt + pager.

    Stages:
      1. Password via ``read -s`` — silent prompt detected by idle fallback;
         secret is never echoed back.
      2. ``y`` answer takes the yes branch (negative marker ``B2`` excluded).
      3. ``yes`` long-form answer — same yes branch.
      4. ``n`` answer takes the no branch (negative marker ``B1`` excluded).
      5. ``no`` long-form answer — same no branch.
      6. Multi-line paste via ``cat > file`` — every line preserved.
      7. ``sleep`` interrupted via process interrupt — next command recovers.
      8. Pager (``less``) entered, scrolled with Space, dismissed with ``q``.
    """
    # ── 1. Password (silent prompt — exercises idle-based detection) ────────
    secret = "S3cret_pwd_7f1a"
    await _start_interactive(tools, 'bash -c \'read -s v; echo; echo got_"$v"\'')
    await _answer_and_verify(tools, secret, f"got_{secret}")

    # Verify the password value never appears as standalone output (only
    # inside the ``got_<secret>`` marker we constructed).
    recent = await tools.terminal.execute(action="current")
    bare_secret_lines = [ln for ln in recent.splitlines() if ln.strip() == secret]
    assert not bare_secret_lines, (
        f"password value leaked onto its own line (silent read failed):\n{recent}"
    )

    # ── 2. y short answer → yes branch ──────────────────────────────────────
    await _start_interactive(
        tools,
        'bash -c \'read -p "x:" a; case "$a" in y) echo B1;; *) echo B2;; esac\'',
    )
    await _answer_and_verify(tools, "y", "B1", negative_marker="B2")

    # ── 3. yes long answer → yes branch ─────────────────────────────────────
    await _start_interactive(
        tools,
        'bash -c \'read -p "x:" a; case "$a" in y|yes|Y|YES) echo B1;; *) echo B2;; esac\'',
    )
    await _answer_and_verify(tools, "yes", "B1", negative_marker="B2")

    # ── 4. n short answer → no branch ───────────────────────────────────────
    await _start_interactive(
        tools,
        'bash -c \'read -p "x:" a; case "$a" in y) echo B1;; *) echo B2;; esac\'',
    )
    await _answer_and_verify(tools, "n", "B2", negative_marker="B1")

    # ── 5. no long answer → no branch ───────────────────────────────────────
    await _start_interactive(
        tools,
        'bash -c \'read -p "x:" a; case "$a" in y|yes|Y|YES) echo B1;; *) echo B2;; esac\'',
    )
    await _answer_and_verify(tools, "no", "B2", negative_marker="B1")

    # ── 6. Multi-line paste — every line preserved ──────────────────────────
    lines = ["line-one-7c1", "line-two-7c1", "line-three-7c1"]
    await run_command(tools, "cat > /tmp/modex_paste_test_7c1.txt", timeout=15.0)
    await tools.process.execute(action="paste", text="\n".join(lines))
    await tools.process.execute(action="send_keys", hex=["04"])  # Ctrl-D EOF
    await asyncio.sleep(0.5)
    cat_out = await run_command(tools, "cat /tmp/modex_paste_test_7c1.txt", timeout=15.0)
    for line in lines:
        assert output_of(cat_out, line), (
            f"pasted line {line!r} missing from file:\n{cat_out}"
        )

    # ── 7. Interrupt recovery — sleep 60 + interrupt + next command works ───
    import asyncio as _asyncio

    sleep_task = _asyncio.create_task(run_command(tools, "sleep 60", timeout=20.0))
    await wait_for_status(tools, TerminalCommandStatus.EXECUTING, timeout=4.0)
    await tools.process.execute(action="interrupt")
    await wait_for_status(
        tools,
        frozenset({
            TerminalCommandStatus.IDLE,
            TerminalCommandStatus.UNKNOWN,
            TerminalCommandStatus.COMPLETED,
        }),
        timeout=6.0,
    )
    sleep_task.cancel()
    try:
        await sleep_task
    except (asyncio.CancelledError, BaseException):
        pass

    recover_marker = "RECOVERED_8e2d"
    res_recover = await run_command(tools, f"echo {recover_marker}", timeout=15.0)
    assert output_of(res_recover, recover_marker), (
        f"command after interrupt did not produce marker:\n{res_recover}"
    )

    # ── 8. Pager — less entered, scrolled with Space, dismissed with q ──────
    await run_command(tools, "seq 1 200 | less", timeout=15.0)
    await asyncio.sleep(1.0)

    session = await tools.terminal._manager.get_default()
    assert session is not None
    status_during_pager = await session.command_status(config=tools.cfg)
    assert status_during_pager != TerminalCommandStatus.IDLE, (
        f"less should still be running, status={status_during_pager}"
    )

    # Space scroll (batch with repeat), then quit.
    await tools.process.execute(action="write", data=" ", repeat=3)
    await asyncio.sleep(0.5)
    await tools.process.execute(action="write", data="q")

    await wait_for_status(
        tools,
        frozenset({TerminalCommandStatus.IDLE, TerminalCommandStatus.UNKNOWN}),
        timeout=8.0,
    )
