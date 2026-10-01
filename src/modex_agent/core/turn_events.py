"""Provider-neutral runtime turn events — the unified event-stream seam.

This module owns the canonical runtime event vocabulary consumed by BOTH
execution planes: the native ReAct graph runtime and the external
coding-agent harness. Emitters translate their plane-specific observations
onto this union; downstream consumers (the presentation projector,
transcripts, consoles) project it without knowing which plane produced it.

Two invariants (mirroring ``core/stream_events.py``):

1. **Closed union, append-only discipline** — the ``kind`` discriminator
   literals form a closed set. New event variants are only ever appended
   (and the union extended in lock-step); existing variants' fields and
   semantics are never modified.
2. **No identity on the events** — variants carry no session/agent/turn
   identity. Identity is assigned downstream by the presentation
   projector, so producers stay per-plane and replay-safe.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from modex_agent.core.llm_struct import TokenUsage


class StopReason(StrEnum):
    """Reason an agent turn ended."""

    COMPLETED = "completed"
    ERROR = "error"
    MAX_ITERATIONS = "max_iterations"
    TURN_CANCELLED = "turn_cancelled"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MISSED_COMMUNICATION = "missed_communication"
    COMMAND_INTERCEPTED = "command_intercepted"
    DUPLICATE = "duplicate"
    LOOP_DETECTED = "loop_detected"


class _TurnEventBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TurnTextEvent(_TurnEventBase):
    kind: Literal["text"] = "text"
    text: str
    part_id: str | None = None


class TurnReasoningEvent(_TurnEventBase):
    kind: Literal["reasoning"] = "reasoning"
    text: str
    part_id: str | None = None


class TurnToolCallEvent(_TurnEventBase):
    kind: Literal["tool_call"] = "tool_call"
    tool_name: Annotated[str, Field(min_length=1)]
    call_id: Annotated[str, Field(min_length=1)]
    arguments: dict[str, JsonValue]
    part_id: str | None = None


class TurnToolResultEvent(_TurnEventBase):
    """A tool call completed (ADR-0053 presentation seam enrichment).

    ``error`` / ``seq`` carry the tool-error fact and the runtime's
    ordering hint; ``arguments`` carries the originating call's arguments
    when the producer knows them without a preceding
    ``TurnToolCallEvent`` (e.g. a resumed approval turn re-emits only the
    END). All three are optional — older producers keep constructing the
    event with ``output`` alone.
    """

    kind: Literal["tool_result"] = "tool_result"
    tool_name: Annotated[str, Field(min_length=1)]
    call_id: Annotated[str, Field(min_length=1)]
    output: str
    error: str | None = None
    seq: int | None = None
    arguments: dict[str, JsonValue] | None = None
    part_id: str | None = None


class TurnStartedEvent(_TurnEventBase):
    """The turn began executing (emitted before any content event)."""

    kind: Literal["turn_started"] = "turn_started"


class TurnFinishedEvent(_TurnEventBase):
    """The turn reached its terminal state — exactly one per turn.

    ``stop_reason`` is the provider-neutral terminal classification and
    ``error`` carries the failure text for errored turns. ``attachments``
    lists the output artifacts the turn produced; consumers deliver them
    alongside the terminal event.
    """

    kind: Literal["turn_finished"] = "turn_finished"
    stop_reason: StopReason
    error: str | None = None
    attachments: tuple[str, ...] = ()


class TurnErroredEvent(_TurnEventBase):
    """An error observed mid-turn.

    This is an observation, not a terminal classification: the turn may
    still terminate later, and its terminal classification arrives via
    ``turn_finished``.
    """

    kind: Literal["turn_errored"] = "turn_errored"
    message: str


class ToolArgsDeltaEvent(_TurnEventBase):
    """A streamed tool-argument increment (transient, display-only).

    An empty ``args_fragment`` is an identity announcement — the tool
    name and call id have arrived, argument streaming has not started.
    """

    kind: Literal["tool_args_delta"] = "tool_args_delta"
    call_id: Annotated[str, Field(min_length=1)]
    tool_name: Annotated[str, Field(min_length=1)]
    args_fragment: str


class ApprovalRequestedEvent(_TurnEventBase):
    """A tool call awaits human approval before executing."""

    kind: Literal["approval_requested"] = "approval_requested"
    tool_name: Annotated[str, Field(min_length=1)]
    call_id: Annotated[str, Field(min_length=1)]
    prompt: str


class ApprovalResolvedEvent(_TurnEventBase):
    """A pending approval was decided (the decision flowed back)."""

    kind: Literal["approval_resolved"] = "approval_resolved"
    call_id: Annotated[str, Field(min_length=1)]
    approved: bool


class UsageEvent(_TurnEventBase):
    """A token-usage snapshot for the current turn."""

    kind: Literal["usage"] = "usage"
    usage: TokenUsage


class IterationStartedEvent(_TurnEventBase):
    """One ReAct iteration (thought-action-observation cycle) began."""

    kind: Literal["iteration_started"] = "iteration_started"
    iteration: int


class IterationFinishedEvent(_TurnEventBase):
    """One ReAct iteration finished.

    ``has_tool_calls`` tells whether the iteration ended in tool calls
    (execution continues) or in a final answer candidate.
    """

    kind: Literal["iteration_finished"] = "iteration_finished"
    iteration: int
    has_tool_calls: bool


class ProgressEvent(_TurnEventBase):
    """A long-running settlement heartbeat.

    ``hint`` is the human-readable progress text; ``tool_hint`` marks
    progress produced by tool settlement (vs the model loop).
    """

    kind: Literal["progress"] = "progress"
    hint: str
    tool_hint: bool


TurnEvent = Annotated[
    TurnTextEvent
    | TurnReasoningEvent
    | TurnToolCallEvent
    | TurnToolResultEvent
    | TurnStartedEvent
    | TurnFinishedEvent
    | TurnErroredEvent
    | ToolArgsDeltaEvent
    | ApprovalRequestedEvent
    | ApprovalResolvedEvent
    | UsageEvent
    | IterationStartedEvent
    | IterationFinishedEvent
    | ProgressEvent,
    Field(discriminator="kind"),
]
"""Closed union of every runtime turn event (discriminated by ``kind``).

Append-only: adding a variant means appending it here and extending the
consumers' declared dispositions — never a silent drop."""


__all__ = [
    "ApprovalRequestedEvent",
    "ApprovalResolvedEvent",
    "IterationFinishedEvent",
    "IterationStartedEvent",
    "ProgressEvent",
    "StopReason",
    "ToolArgsDeltaEvent",
    "TurnErroredEvent",
    "TurnEvent",
    "TurnFinishedEvent",
    "TurnReasoningEvent",
    "TurnStartedEvent",
    "TurnTextEvent",
    "TurnToolCallEvent",
    "TurnToolResultEvent",
    "UsageEvent",
]
