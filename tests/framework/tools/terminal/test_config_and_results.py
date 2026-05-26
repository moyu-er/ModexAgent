from __future__ import annotations

from framework.tools.terminal.config import (
    TerminalRuntimeConfig,
    clamp_int,
    resolve_command_timeout,
)
from framework.tools.terminal.results import CommandResult, TerminalRead, TerminalSegment
from framework.tools.terminal.types import (
    Platform,
    ProcessStatus,
    ShellFamily,
    TerminalVisibility,
)


def test_terminal_runtime_config_defaults_keep_inner_timeout_below_outer() -> None:
    cfg = TerminalRuntimeConfig()

    assert cfg.default_yield_ms == 10_000
    assert cfg.default_command_timeout_seconds == 60
    assert cfg.command_tool_outer_timeout_seconds == 70
    assert cfg.default_command_timeout_seconds < cfg.command_tool_outer_timeout_seconds


def test_resolve_command_timeout_clamps_below_outer_timeout() -> None:
    cfg = TerminalRuntimeConfig(command_tool_outer_timeout_seconds=30)

    assert resolve_command_timeout(999, cfg) == 25
    assert resolve_command_timeout(-5, cfg) == 1


def test_clamp_int_accepts_none_and_bounds_values() -> None:
    assert clamp_int(None, default=10, minimum=1, maximum=20) == 10
    assert clamp_int(0, default=10, minimum=1, maximum=20) == 1
    assert clamp_int(25, default=10, minimum=1, maximum=20) == 20


def test_result_dataclasses_are_structured() -> None:
    result = CommandResult(
        status=ProcessStatus.RUNNING,
        session_id="ps-1",
        terminal="default",
        output="hello",
        tail="hello",
        timed_out=False,
    )
    read = TerminalRead(stdout="out", stderr="", raw="out")
    segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    assert result.status is ProcessStatus.RUNNING
    assert read.stdout == "out"
    assert segment.is_empty_prompt is True


def test_new_enums_cover_windows_first_design() -> None:
    assert Platform.WINDOWS.value == "windows"
    assert ShellFamily.POWERSHELL.value == "powershell"
    assert TerminalVisibility.HIDDEN.value == "hidden"
