"""Neutral presentation event vocabulary (ADR-0053).

The runtime emits the provider-neutral core ``TurnEvent`` union (both
execution planes); this module owns the *presentation* half of that
projection: a closed, frozen discriminated union of UI-facing events
covering the generic agent-turn envelope:

- turn lifecycle — started / finished (``StopReason``) / errored
  (interruption classification rides ``TurnFinished.stop_reason``;
  resume observation rides ``ApprovalResolved``)
- streaming deltas — text / thinking / tool-call arguments
- tool cards — call started / result (error carried on the card)
- approval lifecycle — requested / resolved
- usage summary
- identity envelope — session / agent / pool / workspace / turn id

Bot-specific concepts (pool-attribution display, attachments, block
materialization for a specific frontend) stay consumer-side as
enrichments layered on these events. Every kind has a default runtime
producer on the core stream (approval lifecycle from the turn runner /
approval resumer, usage from the LLM client); the projector's
disposition tables (``DefaultTurnEventProjector``) declare the mapping
for each runtime input kind.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from modex_agent.core.llm_struct import TokenUsage
from modex_agent.core.turn_events import StopReason


class PresentationEventBase(BaseModel):
    """Identity envelope shared by every presentation event.

    ``session_id`` is the full receiver-owned session identifier; the
    projector derives ``agent_name`` from it. ``turn_id`` is assigned by
    the projector (lazy, on first content event) and is ``""`` while no
    turn is active. ``pool`` / ``workspace`` carry optional deployment
    identity; ``timestamp_ms`` is an optional millisecond epoch the
    projector fills from its clock.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    agent_name: str
    turn_id: str
    pool: str | None = None
    workspace: str | None = None
    timestamp_ms: int | None = None


# ── Turn lifecycle ─────────────────────────────────────────────────────────


class TurnStarted(PresentationEventBase):
    """A turn's identity was assigned (lazy, on first content event)."""

    kind: Literal["turn_started"] = "turn_started"


class TurnFinished(PresentationEventBase):
    """A turn reached its terminal state.

    ``stop_reason`` is the provider-neutral terminal classification;
    ``error`` carries the failure text for ``StopReason.ERROR`` turns.
    """

    kind: Literal["turn_finished"] = "turn_finished"
    stop_reason: StopReason
    error: str | None = None
    latency_ms: int = 0


class TurnErrored(PresentationEventBase):
    """An error surfaced mid-turn (the turn may still terminate later)."""

    kind: Literal["turn_errored"] = "turn_errored"
    message: str


# ── Streaming deltas (transient) ───────────────────────────────────────────


class TextDelta(PresentationEventBase):
    """One streamed body-text fragment.

    ``segment_id`` groups deltas of one output segment (mirrors the
    runtime's ``part_id``; ``"_text"`` when unidentified).
    """

    kind: Literal["text_delta"] = "text_delta"
    text: str
    segment_id: str = "_text"


class ThinkingDelta(PresentationEventBase):
    """One streamed reasoning/thinking fragment (``"_reasoning"`` default)."""

    kind: Literal["thinking_delta"] = "thinking_delta"
    text: str
    segment_id: str = "_reasoning"


class ToolArgsDelta(PresentationEventBase):
    """One streamed tool-argument fragment (pre-``tool_call_started`` warm-up)."""

    kind: Literal["tool_args_delta"] = "tool_args_delta"
    tool_name: str
    call_id: str
    args_fragment: str


# ── Tool cards ─────────────────────────────────────────────────────────────


class ToolCallStarted(PresentationEventBase):
    """A tool call card opened — full-fidelity arguments."""

    kind: Literal["tool_call_started"] = "tool_call_started"
    tool_name: str
    call_id: str
    arguments: dict[str, JsonValue]


class ToolResult(PresentationEventBase):
    """A tool call completed — the paired card, full fidelity.

    ``arguments`` carries the originating call's arguments (merged from
    the preceding call event when the runtime result does not include
    them); ``None`` marks an orphan result with no call record — consumers
    must not fabricate an empty-args call for it. ``error`` set means the
    tool failed; ``output`` is the rendered text either way. ``seq`` is
    the runtime's ordering hint when available.
    """

    kind: Literal["tool_result"] = "tool_result"
    tool_name: str
    call_id: str
    output: str
    error: str | None = None
    seq: int | None = None
    arguments: dict[str, JsonValue] | None = None


# ── Approval lifecycle ─────────────────────────────────────────────────────


class ApprovalRequested(PresentationEventBase):
    """A tool call awaits human approval.

    Produced by the turn runner at suspension time (one per suspension,
    before the turn pauses).
    """

    kind: Literal["approval_requested"] = "approval_requested"
    tool_name: str
    call_id: str
    prompt: str


class ApprovalResolved(PresentationEventBase):
    """A pending approval was decided."""

    kind: Literal["approval_resolved"] = "approval_resolved"
    call_id: str
    approved: bool


# ── Usage ──────────────────────────────────────────────────────────────────


class UsageSummary(PresentationEventBase):
    """A token-usage snapshot for the turn."""

    kind: Literal["usage_summary"] = "usage_summary"
    usage: TokenUsage


PresentationEvent = Annotated[
    TurnStarted
    | TurnFinished
    | TurnErrored
    | TextDelta
    | ThinkingDelta
    | ToolArgsDelta
    | ToolCallStarted
    | ToolResult
    | ApprovalRequested
    | ApprovalResolved
    | UsageSummary,
    Field(discriminator="kind"),
]
"""Closed union of every presentation event (discriminated by ``kind``)."""


__all__ = [
    "ApprovalRequested",
    "ApprovalResolved",
    "PresentationEvent",
    "TextDelta",
    "ThinkingDelta",
    "ToolArgsDelta",
    "ToolCallStarted",
    "ToolResult",
    "TurnErrored",
    "TurnFinished",
    "TurnStarted",
    "UsageSummary",
]
