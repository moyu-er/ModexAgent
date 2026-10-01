"""Presentation layer — neutral event projection + transcript contract.

Projects the provider-neutral runtime event seam (core ``TurnEvent`` plus
lifecycle signals) onto a closed presentation-event vocabulary, and owns
the transcript persistence contract with a JSONL implementation. See
ADR-0053.

Curated facade — import real names from here:
``DefaultTurnEventProjector``, ``PresentationEvent``, ``materialize_turns``.
"""

from modex_agent.presentation.events import (
    ApprovalRequested,
    ApprovalResolved,
    PresentationEvent,
    TextDelta,
    ThinkingDelta,
    ToolArgsDelta,
    ToolCallStarted,
    ToolResult,
    TurnErrored,
    TurnFinished,
    TurnInterrupted,
    TurnResumed,
    TurnStarted,
    UsageSummary,
)
from modex_agent.presentation.projector import (
    DefaultTurnEventProjector,
    RuntimeTurnEvent,
    ToolArgsDeltaSignal,
    TurnEndedSignal,
    TurnEventProjector,
    TurnFailedSignal,
    UsageReportedSignal,
)
from modex_agent.presentation.transcript import (
    JsonlTranscriptStore,
    PresentationTranscriptCodec,
    PresentationTranscriptStore,
    TextBlock,
    ThinkingBlock,
    ToolBlock,
    TranscriptCodec,
    TranscriptStore,
    TurnView,
    materialize_turns,
)

__all__ = [
    "ApprovalRequested",
    "ApprovalResolved",
    "DefaultTurnEventProjector",
    "JsonlTranscriptStore",
    "PresentationEvent",
    "PresentationTranscriptCodec",
    "PresentationTranscriptStore",
    "RuntimeTurnEvent",
    "TextBlock",
    "TextDelta",
    "ThinkingBlock",
    "ThinkingDelta",
    "ToolArgsDelta",
    "ToolArgsDeltaSignal",
    "ToolBlock",
    "ToolCallStarted",
    "ToolResult",
    "TranscriptCodec",
    "TranscriptStore",
    "TurnEndedSignal",
    "TurnErrored",
    "TurnEventProjector",
    "TurnFailedSignal",
    "TurnFinished",
    "TurnInterrupted",
    "TurnResumed",
    "TurnStarted",
    "TurnView",
    "UsageReportedSignal",
    "UsageSummary",
    "materialize_turns",
]
