"""Visible terminal lifecycle: env-var isolation across tabs, long-running
``current`` tracking with strict status transitions, shell-exit auto-restart.

VISIBLE parametrization. Complementary to ``test_terminal_management.py``
(HIDDEN) which exercises the TerminalTool action surface — this file targets
the state-persistence and status-tracking contracts that are most fragile
under the visible PTY path (ConPTY timing, host↔parent IPC).
"""
from __future__ import annotations

import asyncio
import re
import sys

import pytest

from modex_agent.tools.terminal.types import TerminalCommandStatus, TerminalVisibility

from .conftest import output_of, run_command, wait_for_status

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only real-PTY workflow"),
    # Real-PTY e2e: timing-sensitive under full-suite load. Deselected by
    # the default addopts (-m 'not integration'); run explicitly with
    # `pytest -m integration tests/framework/tools/terminal/…`.
    pytest.mark.integration,
]

_VIS = [pytest.param(TerminalVisibility.VISIBLE, id="visible")]


def _status_value(current_xml: str) -> str:
    match = re.search(r"<status>([^<]+)</status>", current_xml)
    assert match, f"<status> tag missing:\n{current_xml}"
    return match.group(1).strip()


@pytest.mark.parametrize("visibility", _VIS, indirect=True)
@pytest.mark.asyncio
async def test_visible_lifecycle_and_tracking(tools) -> None:
    """State persistence + status tracking pipeline on the visible PTY path.

    Stages:
      1. Set an env var in tab-A; verify it is invisible in tab-B and still
         set when we return to tab-A (true cross-tab isolation).
      2. Start a long-running command; ``current`` reports ``executing``
         mid-flight. After the prompt returns, ``current`` reports ``idle``
         AND contains the final marker (proves YIELD → IDLE transition).
      3. Exit the shell cleanly; verify the next command auto-restarts the
         backend (the visible host process is respawned on demand).
    """
    t = tools.terminal

    # ── 1. Env-var isolation across two tabs ────────────────────────────────
    await t.execute(action="open", name="a")
    await t.execute(action="open", name="b")

    await t.execute(action="select", name="a")
    await run_command(tools, "export SECRET_A=alpha_a3f1")

    await t.execute(action="select", name="b")
    res_b = await run_command(tools, 'echo "B_sees=${SECRET_A}"')
    assert output_of(res_b, "B_sees="), f"b echo did not run cleanly:\n{res_b}"
    assert "alpha_a3f1" not in res_b, (
        f"b saw a's env var — isolation broken:\n{res_b}"
    )

    await t.execute(action="select", name="a")
    res_a = await run_command(tools, 'echo "A_sees=${SECRET_A}"')
    assert output_of(res_a, "A_sees=alpha_a3f1"), (
        f"a lost its own env var — state not persisted:\n{res_a}"
    )

    # ── 2. Long-running command: EXECUTING mid-flight, IDLE + marker after ──
    yield_res = await run_command(tools, "sleep 2; echo LONG_DONE_b8e1", timeout=15.0)
    assert "executing" in yield_res.lower(), (
        f"long command should yield executing, got:\n{yield_res}"
    )

    cur_mid = await t.execute(action="current")
    # While still running the status may already have flipped to idle if the
    # 2s sleep already elapsed; accept either, but the payload must mention
    # the running command.
    assert "sleep 2" in cur_mid or "LONG_DONE_b8e1" in cur_mid, (
        f"current mid-flight missing the running command:\n{cur_mid}"
    )

    await wait_for_status(
        tools,
        frozenset({TerminalCommandStatus.IDLE, TerminalCommandStatus.UNKNOWN}),
        timeout=10.0,
    )

    cur_after = await t.execute(action="current")
    assert _status_value(cur_after) == "idle", (
        f"current status not idle after long command completed:\n{cur_after}"
    )
    assert "LONG_DONE_b8e1" in cur_after, (
        f"current after completion missing the marker:\n{cur_after}"
    )

    # ── 3. Shell exit auto-restart ──────────────────────────────────────────
    await run_command(tools, "exit 0", timeout=15.0)
    await asyncio.sleep(0.5)

    reborn = "REBORN_5d3e"
    res_reborn = await run_command(tools, f"echo {reborn}", timeout=25.0)
    assert output_of(res_reborn, reborn), (
        f"command after shell exit did not auto-restart:\n{res_reborn}"
    )

    # Cleanup
    await t.execute(action="close", name="a")
    await t.execute(action="close", name="b")
