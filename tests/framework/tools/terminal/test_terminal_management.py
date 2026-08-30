"""Terminal management workflow: open/close/list/select.

HIDDEN parametrization focuses on the TerminalTool action surface and
strict status / list XML assertions across multiple tabs. Complementary
to the VISIBLE test in ``test_terminal_process_workflow.py`` which covers
env isolation, long-running tracking, and shell-exit restart.
"""

from __future__ import annotations

import re
import sys

import pytest

from modex_agent.tools.terminal.types import TerminalVisibility

from .conftest import output_of, run_command

# Real PTY workflow — Windows + bash required. Skipped silently on Linux CI
# (the framework still has full unit + architecture coverage there).
pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only real-PTY workflow"),
    # Real-PTY e2e: timing-sensitive under full-suite load. Deselected by
    # the default addopts (-m 'not integration'); run explicitly with
    # `pytest -m integration tests/framework/tools/terminal/…`.
    pytest.mark.integration,
]

_VIS = [pytest.param(TerminalVisibility.HIDDEN, id="hidden")]


def _tab_present(list_xml: str, tab: str) -> bool:
    return bool(re.search(rf'<tab name="{re.escape(tab)}"', list_xml))


@pytest.mark.parametrize("visibility", _VIS, indirect=True)
@pytest.mark.asyncio
async def test_hidden_terminal_management(tools) -> None:
    """Exercise every TerminalTool action with strict XML-shape assertions.

    Verifies:
    - open returns the new tab name and makes it default
    - list reflects open/close transitions (XML attribute match, not substring)
    - select switches the active tab; subsequent commands land in the right tab
    - list marks the selected tab after a command completes
    - close removes the tab from the registry
    """
    t = tools.terminal

    # ── open two tabs ───────────────────────────────────────────────────────
    open_a = await t.execute(action="open", name="alpha")
    assert "alpha" in open_a, f"open alpha did not ack:\n{open_a}"
    open_b = await t.execute(action="open", name="beta")
    assert "beta" in open_b, f"open beta did not ack:\n{open_b}"

    listed = await t.execute(action="list")
    assert _tab_present(listed, "alpha"), f"alpha missing from list:\n{listed}"
    assert _tab_present(listed, "beta"), f"beta missing from list:\n{listed}"

    # ── select alpha, run a command, verify marker lands on its own line ────
    await t.execute(action="select", name="alpha")
    marker_a = "ALPHA_4f1a"
    res_a = await run_command(tools, f"echo {marker_a}")
    assert output_of(res_a, marker_a), f"alpha did not produce its marker on its own line:\n{res_a}"

    # ── list marks alpha as selected after the command completes ───────────
    listed_alpha = await t.execute(action="list")
    assert '<tab name="alpha" default="true"' in listed_alpha, (
        f"alpha not marked as selected after marker ran:\n{listed_alpha}"
    )

    # ── select beta, run a command there, verify isolation from alpha ───────
    await t.execute(action="select", name="beta")
    marker_b = "BETA_4f1a"
    res_b = await run_command(tools, f"echo {marker_b}")
    assert output_of(res_b, marker_b), f"beta did not produce its marker:\n{res_b}"
    assert marker_a not in res_b, f"alpha marker leaked into beta — isolation broken:\n{res_b}"

    # ── switch back to alpha, command still works there ─────────────────────
    await t.execute(action="select", name="alpha")
    marker_back = "ALPHA_BACK_4f1a"
    res_back = await run_command(tools, f"echo {marker_back}")
    assert output_of(res_back, marker_back), f"alpha did not run after re-select:\n{res_back}"

    # ── close alpha, list reflects removal ──────────────────────────────────
    await t.execute(action="close", name="alpha")
    listed_after = await t.execute(action="list")
    assert not _tab_present(listed_after, "alpha"), (
        f"alpha still in list after close:\n{listed_after}"
    )
    assert _tab_present(listed_after, "beta"), (
        f"beta missing from list after alpha close:\n{listed_after}"
    )

    # ── close beta, list empty ──────────────────────────────────────────────
    await t.execute(action="close", name="beta")
    listed_final = await t.execute(action="list")
    assert not _tab_present(listed_final, "beta"), (
        f"beta still in list after close:\n{listed_final}"
    )
