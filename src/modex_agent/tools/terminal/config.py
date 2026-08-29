from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalRuntimeConfig:
    command_deadline_seconds: int = 480
    input_wait_idle_ms: int = 10_000
    max_output_chars: int = 200_000
    pending_max_output_chars: int = 30_000
    finished_ttl_ms: int = 1_800_000
    prompt_stabilize_ms: int = 100
    max_total_buffer_chars: int = 1_000_000
