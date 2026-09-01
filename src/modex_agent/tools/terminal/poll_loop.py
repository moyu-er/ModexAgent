from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.prompt import (
    detect_pager_entry,
    is_waiting_for_input,
    resolve_cursor_line,
)
from modex_agent.tools.terminal.results import TerminalSegment
from modex_agent.tools.terminal.session import TerminalSession
from modex_agent.tools.terminal.types import ProcessStatus


def mark_exited_if_finished(
    registry: ProcessRegistry,
    proc_id: str,
    outcome: PollOutcome,
) -> None:
    """Mark a process COMPLETED in the registry when a drain outcome proves it.

    Shared by ``CommandTool.execute`` and ``ProcessTool``'s post-write drain
    so the outcome→registry mapping exists in exactly one place. Only
    ``PROMPT_DETECTED`` and ``PROCESS_EXIT`` prove completion — every other
    outcome means the interaction is still live and the session stays RUNNING.
    """
    if outcome in (PollOutcome.PROMPT_DETECTED, PollOutcome.PROCESS_EXIT):
        registry.mark_exited(
            proc_id,
            exit_code=None,
            exit_signal=None,
            status=ProcessStatus.COMPLETED,
        )


class PollOutcome(StrEnum):
    PROMPT_DETECTED = "prompt_detected"
    PROCESS_EXIT = "process_exit"
    INPUT_WAIT = "input_wait"
    TIMED_OUT = "timed_out"


@dataclass
class PollResult:
    outcome: PollOutcome
    output_parts: list[str]
    elapsed_ms: int


async def poll_until_settled(
    session: TerminalSession,
    registry: ProcessRegistry,
    proc_id: str,
    config: TerminalRuntimeConfig,
    *,
    command: str = "",
    check_input_wait: bool = True,
) -> PollResult:
    """Poll until completion evidence, input-wait evidence, or the deadline."""
    del command
    start = time.monotonic()
    output_parts: list[str] = []
    output_received = False
    prompt_stable_since: float | None = None

    while True:
        elapsed_ms = int((time.monotonic() - start) * 1000)

        read = await session.poll_once(timeout=0.05)
        if read.stdout:
            registry.record_output(proc_id)
            output_parts.append(read.stdout)
            output_received = True
            prompt_stable_since = None
        if read.stderr:
            registry.record_output(proc_id)
            output_parts.append(read.stderr)
            output_received = True
            prompt_stable_since = None

        if not await session.is_alive():
            record = registry.get_running(proc_id) or registry.get_finished(proc_id)
            # A backend dying at or after its deadline is the timeout kill;
            # only a pre-deadline death proves a natural process exit.
            if record is not None and time.monotonic() >= record.deadline_at:
                return PollResult(PollOutcome.TIMED_OUT, output_parts, elapsed_ms)
            return PollResult(PollOutcome.PROCESS_EXIT, output_parts, elapsed_ms)

        segment: TerminalSegment | None = None
        if output_received:
            segment = await session.current_segment()
            if segment.is_empty_prompt:
                if prompt_stable_since is None:
                    prompt_stable_since = time.monotonic()
                elif (time.monotonic() - prompt_stable_since) * 1000 >= config.prompt_stabilize_ms:
                    return PollResult(PollOutcome.PROMPT_DETECTED, output_parts, elapsed_ms)
            else:
                prompt_stable_since = None

        idle_ms = registry.idle_ms(proc_id)
        if check_input_wait and idle_ms is not None and idle_ms >= config.input_wait_idle_ms:
            if segment is None:
                segment = await session.current_segment()
            has_evidence = (
                session._backend.stdin_wait_evidence() is True
                or is_waiting_for_input("".join(output_parts))
                or detect_pager_entry(resolve_cursor_line(segment))
            )
            if has_evidence:
                return PollResult(PollOutcome.INPUT_WAIT, output_parts, elapsed_ms)

        running = registry.get_running(proc_id)
        if running is not None and running.deadline_at <= time.monotonic():
            return PollResult(PollOutcome.TIMED_OUT, output_parts, elapsed_ms)
