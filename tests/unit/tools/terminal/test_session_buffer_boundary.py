"""Unit tests for TerminalSession command-boundary buffer semantics.

Pins the contract consumed by bash result output and guard snapshots: after
running a command, ``last_command_output()`` must still return that command's output
(extracted from the second-to-last prompt anchor). ``submit_command`` must
seal the previous command's block (``mark_command_boundary``) instead of
wiping the buffer (``clear_buffer``) — wiping removes the prompt line the
extractor anchors on, making the last command's output invisible to the
agent (regression e3cbc1d3, surfaced by the Windows real-PTY workflow
tests ``test_hidden_terminal_management`` / ``test_hidden_process_interaction``).
"""

from __future__ import annotations

from collections import deque

from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.results import SlidingOutputBuffer
from modex_agent.tools.terminal.session import TerminalSession
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    TerminalVisibility,
)

_PROMPT = "user@host:~$ "


class _FakeByteStreamBackend(TerminalBackend):
    """Scriptable byte-stream backend exercising the REAL base-class
    ``read_pending`` → ``_append_to_buffer`` → ``SlidingOutputBuffer`` path
    plus ``current_segment``/``last_command_output`` extraction."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: deque[str] = deque()
        self.written: list[str] = []

    def _shell_family(self) -> ShellFamily:
        return ShellFamily.BASH

    @property
    def platform(self) -> Platform:
        return Platform.LINUX

    @property
    def visibility(self) -> TerminalVisibility:
        return TerminalVisibility.HIDDEN

    def _write_blocking(self, data: str) -> None:
        self.written.append(data)

    def _read_blocking(self, timeout: float, max_size: int) -> str:
        if self.pending:
            return self.pending.popleft()
        return ""

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def kill(self) -> None:
        return None

    def stdin_writable(self) -> bool:
        return True

    async def is_alive(self) -> bool:
        return True

    async def terminate(self) -> None:
        return None


def _make_session() -> tuple[TerminalSession, _FakeByteStreamBackend]:
    backend = _FakeByteStreamBackend()
    backend._output_buffer = SlidingOutputBuffer()
    session = TerminalSession(
        name="default",
        backend=backend,
        shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
    )
    return session, backend


async def _run_command(
    session: TerminalSession, backend: _FakeByteStreamBackend, echo_line: str
) -> None:
    """submit_command + feed the shell echo/output/prompt bytes into the buffer."""
    await session.submit_command(echo_line)
    backend.pending.append(f"{echo_line}\r\n{echo_line.split(' ', 1)[1]}\r\n{_PROMPT}")
    for _ in range(3):
        await session.poll_once(timeout=0.01)


async def test_submit_command_preserves_last_command_output_for_current() -> None:
    """After running two commands, last_command_output() must contain the
    second command's output — the pre-command prompt anchor must survive
    submit_command's buffer handling."""
    session, backend = _make_session()

    await _run_command(session, backend, "echo AAA_1")
    await _run_command(session, backend, "echo BBB_2")

    output = await session.last_command_output()
    assert "BBB_2" in output.splitlines() or "BBB_2" in output, (
        f"last command output lost after next submit_command:\n{output!r}"
    )
    # The command's own echo line (with its output on the next line) is the
    # minimum the agent-facing `current` payload must show.
    assert "echo BBB_2" in output


async def test_submit_command_seals_boundary_instead_of_clearing() -> None:
    """Direct seam assertion: submit_command seals the previous block; the
    buffer retains prior command text (bounded by SlidingOutputBuffer)."""
    session, backend = _make_session()

    await _run_command(session, backend, "echo AAA_1")
    text_after_first = backend.output_buffer_text()
    assert "AAA_1" in text_after_first

    await session.submit_command("echo BBB_2")
    text_after_submit = backend.output_buffer_text()
    # The prompt line preceding the FIRST command block must survive —
    # clearing it destroys the extractor's second-to-last-prompt anchor.
    assert "AAA_1" in text_after_submit, (
        "submit_command wiped prior command output from the buffer; "
        "use mark_command_boundary instead of clear_buffer"
    )
