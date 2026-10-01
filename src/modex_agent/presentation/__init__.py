"""Presentation layer — neutral event projection + transcript contract.

Projects the provider-neutral runtime event seam (the core ``TurnEvent``
union consumed by both execution planes) onto a closed
presentation-event vocabulary, and owns the transcript persistence
contract with a JSONL implementation. See ADR-0053 and ADR-0054.

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
    TurnStarted,
    UsageSummary,
)
from modex_agent.presentation.projector import (
    DefaultTurnEventProjector,
    TurnEventProjector,
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
    "TextBlock",
    "TextDelta",
    "ThinkingBlock",
    "ThinkingDelta",
    "ToolArgsDelta",
    "ToolBlock",
    "ToolCallStarted",
    "ToolResult",
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
