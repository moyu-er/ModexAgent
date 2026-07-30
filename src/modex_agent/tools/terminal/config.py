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
    long_running_timeout_seconds: int = 300
    command_tool_outer_timeout_seconds: int = 70
    input_wait_idle_ms: int = 10_000
    min_input_wait_idle_ms: int = 1_000
    max_input_wait_idle_ms: int = 600_000
    poll_max_wait_ms: int = 30_000
    max_output_chars: int = 200_000
    pending_max_output_chars: int = 30_000
    finished_ttl_ms: int = 1_800_000
    # Solution A: prompt stabilization window for auxiliary completion detection
    prompt_stabilize_ms: int = 100
    # Solution C: early input-wait detection via consecutive empty reads
    empty_read_threshold: int = 5
    empty_read_interval_ms: int = 50
    output_velocity_window_s: int = 5
    output_velocity_active_threshold: int = 2
    # Tiered idle timeout thresholds (replaces input_wait_early_min_elapsed_ms)
    initial_idle_threshold_ms: int = 5_000
    active_idle_threshold_ms: int = 15_000
    # Global memory pressure
    max_total_buffer_chars: int = 1_000_000
    # Pager auto-scroll
    pager_auto_scroll_max_pages: int = 10
    pager_auto_scroll_max_chars: int = 100_000
    pager_idle_detect_seconds: float = 2.0
    # No-output timeout: how long with zero bytes before declaring STUCK
    no_output_timeout_ms: int = 30_000
    # LONG_RUNNING: elapsed time threshold for long-running detection
    long_running_threshold_ms: int = 300_000
    # No-output whitelist: commands that are expected to produce no output
    # for extended periods (e.g. ``sleep 60``). When the command matches a
    # pattern in this list, the STUCK (no-output timeout) check is skipped
    # entirely — the poll loop relies on the hard timeout instead. Patterns
    # are matched against the raw command string with ``re.search``.
    no_output_whitelist: tuple[str, ...] = (
        r"^\s*sleep\s+\d",
        r"^\s*sleep\s+-",
    )


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
