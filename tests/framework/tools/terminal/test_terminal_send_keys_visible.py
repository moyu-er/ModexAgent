"""Visible terminal send_keys + paste + kill workflow.

VISIBLE parametrization. Complementary to ``test_terminal_process_interaction.py``
(HIDDEN) which exercises the prompt-shape answers. This file targets the
byte-delivery contract that is unique to the visible path: single control
bytes (Ctrl-C / Ctrl-D / Ctrl-U / Tab) must reach bash through the
parent → asyncio stream → host → pywinpty chain without being dropped,
reordered, or coalesced.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from modex_agent.tools.terminal.types import TerminalCommandStatus, TerminalVisibility

from .conftest import output_of, run_command, wait_for_idle, wait_for_status

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only real-PTY workflow"),
    # Real-PTY e2e: timing-sensitive under full-suite load. Deselected by
    # the default addopts (-m 'not integration'); run explicitly with
    # `pytest -m integration tests/framework/tools/terminal/…`.
    pytest.mark.integration,
]

_VIS = [pytest.param(TerminalVisibility.VISIBLE, id="visible")]


@pytest.mark.parametrize("visibility", _VIS, indirect=True)
@pytest.mark.asyncio
async def test_visible_send_keys_and_paste(tools) -> None:
    """Byte-delivery pipeline on the visible PTY path.

    Stages:
      1. ``cat`` running, send Ctrl-D via send_keys → cat exits, prompt returns.
      2. ``sleep 60`` running, send Ctrl-C via send_keys → sleep interrupted,
         prompt returns.
      3. Partial input typed, send Ctrl-U via send_keys → cursor line cleared
         (readline kill-line received the byte).
      4. ``cat > file`` then paste multi-line payload + Ctrl-D → every line
         preserved in the file.
      5. ``sleep 60`` started, ``process kill`` clears the running registry.
    """
    t = tools.terminal

    # ── 1. Ctrl-D ends running cat ──────────────────────────────────────────
    await t.execute(action="open", name="cat_test")
    await run_command(tools, "cat", timeout=15.0)
    await wait_for_status(tools, TerminalCommandStatus.EXECUTING, timeout=4.0)

    await tools.process.execute(action="send_keys", hex=["04"])
    await wait_for_status(
        tools,
        frozenset({
            TerminalCommandStatus.IDLE,
            TerminalCommandStatus.UNKNOWN,
            TerminalCommandStatus.COMPLETED,
        }),
        timeout=6.0,
    )

    # ── 2. Ctrl-C interrupts running sleep ──────────────────────────────────
    await t.execute(action="open", name="cc_test")
    sleep_task = asyncio.create_task(run_command(tools, "sleep 60", timeout=20.0))
    # Accept EXECUTING or WAITING_INPUT — the idle-based detector may
    # classify ``sleep 60`` as input-waiting before we send Ctrl-C. Either
    # way the sleep is still running and Ctrl-C must interrupt it.
    await wait_for_status(
        tools,
        frozenset({TerminalCommandStatus.EXECUTING, TerminalCommandStatus.WAITING_INPUT}),
        timeout=6.0,
    )

    await tools.process.execute(action="send_keys", hex=["03"])
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

    # ── 3. Ctrl-U clears partial readline input ─────────────────────────────
    await t.execute(action="open", name="cu_test")
    await run_command(tools, "echo warmup_9c4b", timeout=15.0)
    await wait_for_idle(tools, timeout=5.0)

    session = await tools.terminal._manager.get_default()
    assert session is not None
    await session.write("garbage_partial_zz")
    # ConPTY (WSL bash) buffers PTY input briefly; wait long enough that
    # readline has actually received the bytes before we send Ctrl-U.
    await asyncio.sleep(1.0)

    await tools.process.execute(action="send_keys", hex=["15"])  # Ctrl-U
    await asyncio.sleep(0.8)

    seg_after = await session.current_segment()
    assert "garbage_partial_zz" not in seg_after.cursor_line, (
        f"Ctrl-U byte did not reach readline — cursor still shows garbage:\n"
        f"cursor={seg_after.cursor_line!r}"
    )

    # ── 4. Multi-line paste via cat > file ──────────────────────────────────
    # The trailing newline is load-bearing: without it the last line stays in
    # the PTY's canonical line buffer and one Ctrl-D delivers it WITHOUT EOF,
    # so cat never exits and the follow-up `cat` is guard-rejected.
    await t.execute(action="open", name="paste_test")
    lines = ["paste-one-7c1", "paste-two-7c1", "paste-three-7c1"]
    await run_command(tools, "cat > /tmp/modex_paste_visible_7c1.txt", timeout=15.0)
    await tools.process.execute(action="paste", text="\n".join(lines) + "\n")
    await tools.process.execute(action="send_keys", hex=["04"])  # Ctrl-D EOF
    await asyncio.sleep(0.5)
    cat_out = await run_command(tools, "cat /tmp/modex_paste_visible_7c1.txt", timeout=15.0)
    for line in lines:
        assert output_of(cat_out, line), (
            f"pasted line {line!r} missing from file:\n{cat_out}"
        )

    # ── 5. process kill clears the running registry ─────────────────────────
    # Earlier stages leave ProcessSessions in the registry even after the
    # underlying ``cat`` exited via EOF — the framework does not always
    # reconcile registry state with backend exit. ProcessTool.kill only
    # targets the default tab, so we walk each leftover session's tab,
    # select it, and kill there before measuring.
    for s in tools.registry.list_running():
        await tools.terminal.execute(action="select", name=s.terminal)
        await tools.process.execute(action="kill")

    await t.execute(action="open", name="kill_test")
    await run_command(tools, "sleep 60", timeout=15.0)
    running = tools.registry.list_running()
    assert len(running) == 1, (
        f"expected exactly 1 running session after sleep yield, got {len(running)}: {running}"
    )

    await tools.process.execute(action="kill")
    assert not tools.registry.list_running(), (
        "registry still has running sessions after process kill"
    )
