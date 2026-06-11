"""Shared poll loop for CommandTool and ProcessTool write/submit drain.

Both CommandTool.execute() and ProcessTool._drain_terminal_after_action()
use the same poll-detect-yield pattern. This module extracts that into
a single reusable function.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.prompt import (
    detect_pager_entry,
    is_waiting_for_input,
    resolve_cursor_line,
)
from framework.tools.terminal.session import TerminalSession


class PollOutcome(StrEnum):
    PROMPT_DETECTED = "prompt_detected"
    YIELDED = "yielded"
    TIMED_OUT = "timed_out"
    INPUT_WAIT = "input_wait"
    STUCK = "stuck"
    LONG_RUNNING = "long_running"
    PROCESS_EXIT = "process_exit"
    PAGINATED = "paginated"


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
    yield_ms: int,
    timeout_seconds: int,
    check_input_wait: bool = False,
) -> PollResult:
    """Poll the terminal until a completion condition is met.

    Returns a PollResult indicating why the loop ended and all collected output.
    """
    start = time.monotonic()
    output_parts: list[str] = []
    output_received = False
    prompt_stable_since: float | None = None

    while True:
        elapsed_ms = int((time.monotonic() - start) * 1000)

        read = await session.poll_once(timeout=0.05)
        if read.stdout:
            registry.append_output(proc_id, "stdout", read.stdout)
            output_parts.append(read.stdout)
            output_received = True
            prompt_stable_since = None
        if read.stderr:
            registry.append_output(proc_id, "stderr", read.stderr)
            output_parts.append(read.stderr)

        # 1. Process exit
        if not await session.is_alive():
            return PollResult(PollOutcome.PROCESS_EXIT, output_parts, elapsed_ms)

        # 2. Content-based input wait (fast path)
        if check_input_wait and output_received:
            if is_waiting_for_input("".join(output_parts)):
                return PollResult(PollOutcome.INPUT_WAIT, output_parts, elapsed_ms)

        # 3. Pager detection — before prompt so pagers don't look like idle
        if output_received:
            segment = await session.current_segment()
            cursor = resolve_cursor_line(segment)
            if detect_pager_entry(cursor):
                return PollResult(PollOutcome.PAGINATED, output_parts, elapsed_ms)

        # 4. Prompt detection
        if output_received:
            segment = await session.current_segment()
            if segment.is_empty_prompt:
                if prompt_stable_since is None:
                    prompt_stable_since = time.monotonic()
                elif (time.monotonic() - prompt_stable_since) * 1000 >= config.prompt_stabilize_ms:
                    return PollResult(PollOutcome.PROMPT_DETECTED, output_parts, elapsed_ms)
            else:
                prompt_stable_since = None

        # 5. No-output timeout → STUCK (replaces old 15s hardcoded check)
        raw_idle_ms = int((time.monotonic() - session.last_byte_at) * 1000)
        if raw_idle_ms >= config.no_output_timeout_ms:
            if not is_waiting_for_input("".join(output_parts)):
                return PollResult(PollOutcome.STUCK, output_parts, elapsed_ms)

        # 5.5 Long-running detection (before yield)
        if elapsed_ms >= config.long_running_threshold_ms:
            if output_received and await session.is_alive():
                return PollResult(PollOutcome.LONG_RUNNING, output_parts, elapsed_ms)

        # 6. Yield window
        if elapsed_ms >= yield_ms:
            return PollResult(PollOutcome.YIELDED, output_parts, elapsed_ms)

        # 7. Hard timeout
        if elapsed_ms >= timeout_seconds * 1000:
            return PollResult(PollOutcome.TIMED_OUT, output_parts, elapsed_ms)
