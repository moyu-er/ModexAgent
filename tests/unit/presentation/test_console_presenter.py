"""Unit tests for ``ConsolePresenter`` — the reference PresentationSink.

A scripted presentation stream drives the presenter with an injected
writer; the exact written-string concatenation pins the line discipline
(delta appends, thinking prefix, one line per tool card, approval prompt
line, terminator line).
"""

from __future__ import annotations

from modex_agent.core.llm_struct import TokenUsage
from modex_agent.core.turn_events import StopReason
from modex_agent.presentation import (
    ApprovalRequested,
    ConsolePresenter,
    PresentationEvent,
    TextDelta,
    ThinkingDelta,
    ToolArgsDelta,
    ToolCallStarted,
    ToolResult,
    TurnFinished,
    TurnStarted,
    UsageSummary,
)


async def _render(events: list[PresentationEvent]) -> str:
    """Drive the presenter over ``events`` with a recording writer."""
    written: list[str] = []
    presenter = ConsolePresenter(write=written.append)
    for event in events:
        await presenter.handle(event)
    return "".join(written)


def _env() -> dict[str, str]:
    return {"session_id": "demo.main", "agent_name": "main", "turn_id": "t1"}


async def test_text_deltas_append_into_one_line_and_terminator_closes_it() -> None:
    text = await _render(
        [
            TurnStarted(**_env()),
            TextDelta(**_env(), text="Hello "),
            TextDelta(**_env(), text="world"),
            TurnFinished(**_env(), stop_reason=StopReason.COMPLETED, latency_ms=12),
        ]
    )
    assert text == "Hello world\nturn finished: completed (12 ms)\n"


async def test_thinking_opens_a_prefixed_line() -> None:
    text = await _render(
        [
            ThinkingDelta(**_env(), text="pondering"),
            ThinkingDelta(**_env(), text="..."),
            TurnFinished(**_env(), stop_reason=StopReason.COMPLETED, latency_ms=1),
        ]
    )
    assert text == "[thinking] pondering...\nturn finished: completed (1 ms)\n"


async def test_tool_cards_render_one_line_each_with_status() -> None:
    text = await _render(
        [
            ToolCallStarted(**_env(), tool_name="read", call_id="c1", arguments={}),
            ToolResult(**_env(), tool_name="read", call_id="c1", output="ok"),
            ToolCallStarted(**_env(), tool_name="bash", call_id="c2", arguments={}),
            ToolResult(
                **_env(), tool_name="bash", call_id="c2", output="boom", error="boom"
            ),
            TurnFinished(**_env(), stop_reason=StopReason.COMPLETED, latency_ms=3),
        ]
    )
    assert text == (
        "tool read [c1] in_progress\n"
        "tool read [c1] completed\n"
        "tool bash [c2] in_progress\n"
        "tool bash [c2] failed\n"
        "turn finished: completed (3 ms)\n"
    )


async def test_open_delta_line_closes_before_the_next_card_line() -> None:
    text = await _render(
        [
            TextDelta(**_env(), text="let me look"),
            ToolCallStarted(**_env(), tool_name="read", call_id="c1", arguments={}),
            ToolResult(**_env(), tool_name="read", call_id="c1", output="ok"),
            TurnFinished(**_env(), stop_reason=StopReason.COMPLETED, latency_ms=2),
        ]
    )
    assert text == (
        "let me look\n"
        "tool read [c1] in_progress\n"
        "tool read [c1] completed\n"
        "turn finished: completed (2 ms)\n"
    )


async def test_approval_request_renders_a_prompt_line() -> None:
    text = await _render(
        [
            ApprovalRequested(
                **_env(), tool_name="write", call_id="c9", prompt="Allow write?"
            ),
        ]
    )
    assert text == "approval needed: write [c9] Allow write?\n"


async def test_kinds_without_a_console_surface_are_ignored() -> None:
    text = await _render(
        [
            TurnStarted(**_env()),
            ToolArgsDelta(**_env(), tool_name="read", call_id="c1", args_fragment="{"),
            UsageSummary(**_env(), usage=TokenUsage(input_tokens=1)),
            TurnFinished(**_env(), stop_reason=StopReason.CANCELLED, latency_ms=0),
        ]
    )
    assert text == "turn finished: cancelled (0 ms)\n"
