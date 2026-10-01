"""Unit tests for ``TurnEventValidator`` (unified turn-event stream, ADR-0054)."""

from __future__ import annotations

from modex_agent.core.llm_struct import TokenUsage
from modex_agent.core.turn_events import (
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    IterationFinishedEvent,
    IterationStartedEvent,
    ProgressEvent,
    StopReason,
    ToolArgsDeltaEvent,
    TurnErroredEvent,
    TurnEvent,
    TurnFinishedEvent,
    TurnReasoningEvent,
    TurnStartedEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
    UsageEvent,
)
from modex_agent.core.turn_validator import TurnEventValidator


def _legal_turn() -> list[TurnEvent]:
    return [
        TurnStartedEvent(),
        UsageEvent(usage=TokenUsage()),
        TurnReasoningEvent(text="thinking"),
        ToolArgsDeltaEvent(call_id="c1", tool_name="bash", args_fragment='{"a'),
        TurnToolCallEvent(tool_name="bash", call_id="c1", arguments={"a": 1}),
        TurnToolResultEvent(tool_name="bash", call_id="c1", output="ok"),
        IterationStartedEvent(iteration=0),
        IterationFinishedEvent(iteration=0, has_tool_calls=True),
        ProgressEvent(hint="settling", tool_hint=True),
        TurnErroredEvent(message="transient"),
        TurnTextEvent(text="done"),
        TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
    ]


def _feed_all(validator: TurnEventValidator, events: list[TurnEvent]) -> None:
    for event in events:
        validator.feed(event)


def test_legal_turn_produces_no_violations() -> None:
    validator = TurnEventValidator()
    _feed_all(validator, _legal_turn())
    assert validator.violations == []
    assert validator.finished


def test_legal_turn_with_approval_pair() -> None:
    validator = TurnEventValidator()
    _feed_all(
        validator,
        [
            TurnStartedEvent(),
            ApprovalRequestedEvent(tool_name="bash", call_id="c1", prompt="allow?"),
            ApprovalResolvedEvent(call_id="c1", approved=True),
            TurnToolCallEvent(tool_name="bash", call_id="c1", arguments={}),
            TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
        ],
    )
    assert validator.violations == []


def test_full_suspend_resume_finish_sequence_is_clean() -> None:
    """The W5 production sequence: one turn suspends for approval, the same
    turn resumes after the decision, and exactly one turn_finished closes it
    (no terminal between approval_requested and approval_resolved)."""
    validator = TurnEventValidator()
    _feed_all(
        validator,
        [
            TurnStartedEvent(),
            UsageEvent(usage=TokenUsage(input_tokens=10, output_tokens=5)),
            TurnTextEvent(text="running it"),
            TurnToolCallEvent(tool_name="write_file", call_id="c1", arguments={"p": "a"}),
            # Suspension observation — a pause, not a turn end.
            ApprovalRequestedEvent(
                tool_name="write_file", call_id="c1", prompt="Approval Required..."
            ),
            # Resumed leg: same call ids, same turn (no second turn_started).
            ApprovalResolvedEvent(call_id="c1", approved=True),
            TurnToolResultEvent(tool_name="write_file", call_id="c1", output="written"),
            UsageEvent(usage=TokenUsage(input_tokens=30, output_tokens=8)),
            TurnTextEvent(text="done"),
            TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
        ],
    )
    assert validator.violations == []
    assert validator.finished


def test_lenient_mode_auto_starts_on_content_without_turn_started() -> None:
    validator = TurnEventValidator()
    _feed_all(
        validator,
        [
            TurnTextEvent(text="bridge path"),
            TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
        ],
    )
    assert validator.violations == []
    assert validator.finished


def test_strict_mode_requires_turn_started_first() -> None:
    validator = TurnEventValidator(strict=True)
    _feed_all(
        validator,
        [
            TurnTextEvent(text="before start"),
            TurnStartedEvent(),
            TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
        ],
    )
    assert validator.violations == ["text before turn_started (strict mode)"]


def test_strict_mode_flags_terminal_before_any_content() -> None:
    validator = TurnEventValidator(strict=True)
    _feed_all(
        validator,
        [TurnStartedEvent(), TurnFinishedEvent(stop_reason=StopReason.COMPLETED)],
    )
    assert validator.violations == ["terminal before any content (strict mode)"]


def test_terminal_twice_is_a_violation() -> None:
    validator = TurnEventValidator()
    _feed_all(
        validator,
        [
            TurnTextEvent(text="hi"),
            TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
            TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
        ],
    )
    assert validator.violations == ["duplicate terminal turn_finished"]
    assert validator.finished


def test_events_after_terminal_are_violations() -> None:
    validator = TurnEventValidator()
    _feed_all(
        validator,
        [
            TurnTextEvent(text="hi"),
            TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
            TurnTextEvent(text="late"),
            TurnStartedEvent(),
        ],
    )
    assert validator.violations == [
        "event after terminal turn_finished: text",
        "turn_started after turn_finished",
    ]


def test_unresolved_approval_at_terminal_is_a_violation() -> None:
    validator = TurnEventValidator()
    _feed_all(
        validator,
        [
            TurnStartedEvent(),
            ApprovalRequestedEvent(tool_name="bash", call_id="c1", prompt="allow?"),
            TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
        ],
    )
    assert validator.violations == ["turn_finished with unresolved approval: c1"]


def test_approval_resolved_without_request_is_a_violation() -> None:
    validator = TurnEventValidator()
    _feed_all(
        validator,
        [
            TurnStartedEvent(),
            ApprovalResolvedEvent(call_id="ghost", approved=False),
            TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
        ],
    )
    assert validator.violations == [
        "approval_resolved without matching approval_requested: ghost"
    ]


def test_orphan_tool_result_is_a_violation() -> None:
    validator = TurnEventValidator()
    _feed_all(
        validator,
        [
            TurnStartedEvent(),
            TurnToolResultEvent(tool_name="bash", call_id="ghost", output="late"),
            TurnFinishedEvent(stop_reason=StopReason.COMPLETED),
        ],
    )
    assert validator.violations == ["tool_result without preceding tool_call: ghost"]
