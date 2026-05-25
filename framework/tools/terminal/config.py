from __future__ import annotations

from dataclasses import dataclass


def clamp_int(value: int | None, *, default: int, minimum: int, maximum: int) -> int:
    candidate = default if value is None else int(value)
    return max(minimum, min(maximum, candidate))


@dataclass(frozen=True)
class TerminalRuntimeConfig:
    default_yield_ms: int = 10_000
    min_yield_ms: int = 10
    max_yield_ms: int = 120_000
    default_command_timeout_seconds: int = 60
    command_tool_outer_timeout_seconds: int = 70
    input_wait_idle_ms: int = 15_000
    min_input_wait_idle_ms: int = 1_000
    max_input_wait_idle_ms: int = 600_000
    poll_max_wait_ms: int = 30_000
    max_output_chars: int = 200_000
    pending_max_output_chars: int = 30_000
    finished_ttl_ms: int = 1_800_000


def resolve_yield_ms(value: int | None, config: TerminalRuntimeConfig) -> int:
    return clamp_int(
        value,
        default=config.default_yield_ms,
        minimum=config.min_yield_ms,
        maximum=config.max_yield_ms,
    )


def resolve_command_timeout(value: int | None, config: TerminalRuntimeConfig) -> int:
    max_inner_timeout = max(1, config.command_tool_outer_timeout_seconds - 5)
    return clamp_int(
        value,
        default=config.default_command_timeout_seconds,
        minimum=1,
        maximum=max_inner_timeout,
    )


def resolve_poll_wait_ms(value: int | None, config: TerminalRuntimeConfig) -> int:
    return clamp_int(value, default=0, minimum=0, maximum=config.poll_max_wait_ms)
