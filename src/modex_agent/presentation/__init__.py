"""Presentation layer — neutral event projection + per-turn hub +
transcript contract.

Projects the provider-neutral runtime event seam (the core ``TurnEvent``
union consumed by both execution planes) onto a closed
presentation-event vocabulary, owns the per-turn station that fans the
projected events out to ``PresentationSink`` consumers, and owns the
transcript persistence contract with a JSONL implementation. See
ADR-0053 and ADR-0054.

Curated facade — import real names from here:
``SessionEventHub``, ``PresentationSink``, ``ConsolePresenter``,
``DefaultTurnEventProjector``, ``PresentationEvent``, ``materialize_turns``.
"""

from modex_agent.presentation.console import ConsolePresenter
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
    TurnStarted,
    UsageSummary,
)
from modex_agent.presentation.hub import PresentationSink, SessionEventHub
from modex_agent.presentation.projector import (
    DefaultTurnEventProjector,
    TurnEventProjector,
)
from modex_agent.presentation.transcript import (
    TRANSIENT_PRESENTATION_KINDS,
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
    "ConsolePresenter",
    "DefaultTurnEventProjector",
    "JsonlTranscriptStore",
    "PresentationEvent",
    "PresentationSink",
    "PresentationTranscriptCodec",
    "PresentationTranscriptStore",
    "SessionEventHub",
    "TextBlock",
    "TextDelta",
    "ThinkingBlock",
    "ThinkingDelta",
    "ToolArgsDelta",
    "ToolBlock",
    "ToolCallStarted",
    "ToolResult",
    "TRANSIENT_PRESENTATION_KINDS",
    "TranscriptCodec",
    "TranscriptStore",
    "TurnErrored",
    "TurnEventProjector",
    "TurnFinished",
    "TurnStarted",
    "TurnView",
    "UsageSummary",
    "materialize_turns",
]
