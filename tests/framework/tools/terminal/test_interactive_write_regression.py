"""Interactive write regression e2e (split-brain fix D4+D5).

Real PTY, real shared registry (conftest ``tools`` fixture), the exact
agent-facing flows that were broken by the registry split-brain:

- SSH-style SILENT password prompt (``read -s -p "... password: "``) —
  the original user report: ``process write`` was completely ineffective.
- Trailing-newline input normalization: ``write data="y\\n" submit=true``
  must answer exactly ONE prompt (no empty-line double submit).
- Registry hygiene: after the command completes, no RUNNING residue.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from modex_agent.tools.terminal.types import TerminalCommandStatus

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only"),
    # Real-PTY e2e: timing-sensitive under full-suite load. Deselected by
    # the default addopts (-m 'not integration'); run explicitly with
    # `pytest -m integration tests/framework/tools/terminal/…`.
    pytest.mark.integration,
]


def _line_marker(result: str, marker: str) -> bool:
    """Marker on its own line (echo stdout, not input echo)."""
    return any(line.strip() == marker for line in result.splitlines())


async def _await_waiting_input(tools, timeout: float = 12.0) -> None:
    """Block until the default session reports WAITING_INPUT."""
    session = await tools.terminal._manager.get_default()
    assert session is not None
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await session.command_status(config=tools.cfg)
        if status is TerminalCommandStatus.WAITING_INPUT:
            return
        await asyncio.sleep(0.2)
    raise AssertionError("session never reached WAITING_INPUT")


async def test_ssh_style_silent_password_prompt_write(tools) -> None:
    """The original bug report: SSH asks for a password, ``process write``
    did nothing. ``read -s`` reproduces ssh's silent prompt exactly."""
    cmd = (
        'read -s -p "user@remotehost\'s password: " pw; echo; '
        'if [ "$pw" = "s3cret" ]; then echo "LOGIN_OK"; '
        'else echo "ACCESS_DENIED"; fi'
    )
    result = await tools.command.execute(command=cmd)
    if "waiting_input" not in result.lower():
        await _await_waiting_input(tools)

    result = await tools.process.execute(action="write", data="s3cret", submit=True)

    assert _line_marker(result, "LOGIN_OK"), f"password write failed:\n{result}"
    assert "ACCESS_DENIED" not in result


async def test_write_trailing_newline_single_submit_and_registry_hygiene(
    tools,
) -> None:
    """D4+D5: ``data="y\\n"`` answers exactly one prompt; after the
    command completes the registry holds no RUNNING residue."""
    cmd = 'read -p "continue? " a; read -p "proceed? " b; echo "got_${a}_${b}"'
    result = await tools.command.execute(command=cmd)
    if "waiting_input" not in result.lower():
        await _await_waiting_input(tools)

    result = await tools.process.execute(action="write", data="y\n", submit=True)
    assert "got_y_" not in result, f"'y\\n' must not answer BOTH prompts (double submit):\n{result}"

    result = await tools.process.execute(action="write", data="n", submit=True)
    assert _line_marker(result, "got_y_n"), f"second prompt write failed:\n{result}"

    # D5: completion via the write drain must move the session out of
    # RUNNING — no ghost entries remain after the write drain completes.
    session = await tools.terminal._manager.get_default()
    assert session is not None
    residue = tools.registry.get_running_by_terminal(session.name)
    assert residue is None, f"stale RUNNING entry after completion: {residue}"
