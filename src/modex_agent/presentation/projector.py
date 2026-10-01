"""Runtime-event → presentation-event projector (ADR-0053).

The projector is a pure, synchronous projection engine over the neutral
runtime seam: it consumes the core ``TurnEvent`` family plus the
presentation-owned lifecycle signals below, and produces
``PresentationEvent``s. It owns exactly the state a turn projection needs
— lazy turn identity, pending tool-call arguments, turn latency — so
consumers (WebUI emitters, editors, consoles) translate its output to
their sink without re-deriving turn bookkeeping.

Layering: this package may not import concrete agent strategies, so the
agent-specific enum stream (e.g. ``ReActEvent``) is translated to these
neutral inputs by the consumer's emitter. The disposition of every enum
value the ReAct runtime produces is declared below
(``MAPPED_RUNTIME_EVENTS`` / ``IGNORED_RUNTIME_EVENTS``) — never a silent
drop.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import ClassVar, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, JsonValue

from modex_agent.core.emitter import StopReason
from modex_agent.core.llm_struct import TokenUsage
from modex_agent.core.session_id import agent_of
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

from .events import (
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

# ── Runtime lifecycle signals (projector inputs beyond core TurnEvent) ─────


class _SignalBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolArgsDeltaSignal(_SignalBase):
    """A streamed tool-argument fragment (``TOOL_ARGS_DELTA`` payloads)."""

    kind: Literal["tool_args_delta"] = "tool_args_delta"

    tool_name: str
    call_id: str
    args_fragment: str


class TurnEndedSignal(_SignalBase):
    """The emitter reported turn completion (``emit_complete``)."""

    kind: Literal["turn_ended"] = "turn_ended"

    stop_reason: StopReason
    error: str | None = None


class TurnFailedSignal(_SignalBase):
    """An error surfaced through the emitter (``emit_error`` / ERROR event)."""

    kind: Literal["turn_failed"] = "turn_failed"

    message: str


class UsageReportedSignal(_SignalBase):
    """A token-usage snapshot for the current turn."""

    kind: Literal["usage_reported"] = "usage_reported"

    usage: TokenUsage


RuntimeTurnEvent = (
    TurnEvent
    | ToolArgsDeltaSignal
    | TurnEndedSignal
    | TurnFailedSignal
    | UsageReportedSignal
)
"""The closed input union: core ``TurnEvent``s plus lifecycle signals."""


# ── Projector contract ─────────────────────────────────────────────────────


class TurnEventProjector(ABC):
    """Project neutral runtime events onto presentation events."""

    @abstractmethod
    def feed(self, event: RuntimeTurnEvent) -> list[PresentationEvent]:
        """Feed one runtime event; return the presentation events it yields."""


# ── Default implementation ─────────────────────────────────────────────────


class _EnvelopeKwargs(TypedDict):
    """Keyword form of the identity envelope (internal construction helper)."""

    session_id: str
    agent_name: str
    turn_id: str
    pool: str | None
    workspace: str | None
    timestamp_ms: int


class DefaultTurnEventProjector(TurnEventProjector):
    """The default projection state machine.

    Semantics (pinned by ``tests/unit/presentation/``):

    - **Lazy turn identity** — the first content-bearing event assigns a
      turn id (``uuid4().hex[:12]`` by default) and emits ``TurnStarted``
      ahead of the content event. Turn identity resets after
      ``TurnEndedSignal``.
    - **Segment ids** — text/thinking deltas carry the runtime ``part_id``
      when present, else ``"_text"`` / ``"_reasoning"``.
    - **Tool pairing** — ``TurnToolCallEvent`` remembers the call's full
      arguments; ``TurnToolResultEvent`` merges them (or keeps its own
      ``arguments`` when the runtime result carries them, e.g. a resumed
      approval turn) into one complete ``ToolResult`` card. Orphan
      results keep ``arguments=None``.
    - **Latency** — ``TurnFinished.latency_ms`` is measured from turn
      start through the ``clock`` (injectable; wall clock by default).
    """

    MAPPED_RUNTIME_EVENTS: ClassVar[frozenset[str]] = frozenset(
        {
            "model_reasoning",
            "tool_args_delta",
            "tool_call_start",
            "tool_call_end",
            "error",
        }
    )
    """ReAct enum values that map onto a projector input.

    Text content reaches the projector via the emitter's delta/content
    entries (``TurnTextEvent``), and turn completion via
    ``TurnEndedSignal`` — both emitted alongside these enum values.
    """

    IGNORED_RUNTIME_EVENTS: ClassVar[frozenset[str]] = frozenset(
        {
            "start",
            "model_output",
            "iteration_start",
            "iteration_end",
            "progress",
            "final_output",
            "max_iterations",
        }
    )
    """Documented ignore-list — ReAct enum values with no presentation mapping.

    - ``start``: turn identity is assigned lazily on first content.
    - ``model_output``: duplicates the streaming delta / folded-content
      entries carrying the same text.
    - ``iteration_start`` / ``iteration_end`` / ``progress``: intra-turn
      loop bookkeeping with no generic UI meaning.
    - ``final_output`` / ``max_iterations``: terminal classification
      arrives via ``TurnEndedSignal`` (``StopReason``).
    """

    def __init__(
        self,
        session_id: str,
        *,
        pool: str | None = None,
        workspace: str | None = None,
        turn_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._session_id = session_id
        self._agent_name = agent_of(session_id, default="main")
        self._pool = pool
        self._workspace = workspace
        self._turn_id_factory = turn_id_factory or (lambda: uuid.uuid4().hex[:12])
        self._clock = clock or time.time
        self._current_turn_id = ""
        self._turn_active = False
        self._turn_started_at = self._clock()
        self._pending_calls: dict[str, dict[str, JsonValue]] = {}

    @property
    def current_turn_id(self) -> str:
        """The active turn id, or ``""`` while no turn is active."""
        return self._current_turn_id

    def ensure_turn_started(self) -> str:
        """Assign turn identity lazily if needed; return the current id.

        Public for consumers whose own entries produce transcript records
        but no presentation event (e.g. folded non-streaming content).
        """
        if not self._turn_active:
            self._current_turn_id = self._turn_id_factory()
            self._turn_active = True
            self._turn_started_at = self._clock()
        return self._current_turn_id

    def feed(self, event: RuntimeTurnEvent) -> list[PresentationEvent]:
        match event:
            case TurnTextEvent(text=text, part_id=part_id):
                segment = part_id if part_id else "_text"
                return self._content_events(
                    lambda: TextDelta(**self._envelope(), text=text, segment_id=segment)
                )
            case TurnReasoningEvent(text=text, part_id=part_id):
                segment = part_id if part_id else "_reasoning"
                return self._content_events(
                    lambda: ThinkingDelta(
                        **self._envelope(), text=text, segment_id=segment
                    )
                )
            case TurnToolCallEvent(
                tool_name=tool_name, call_id=call_id, arguments=arguments
            ):
                self._pending_calls[call_id] = dict(arguments)
                return self._content_events(
                    lambda: ToolCallStarted(
                        **self._envelope(),
                        tool_name=tool_name,
                        call_id=call_id,
                        arguments=dict(arguments),
                    )
                )
            case TurnToolResultEvent(
                tool_name=tool_name,
                call_id=call_id,
                output=output,
                error=error,
                seq=seq,
                arguments=arguments,
            ):
                merged = (
                    arguments
                    if arguments is not None
                    else self._pending_calls.pop(call_id, None)
                )
                return self._content_events(
                    lambda: ToolResult(
                        **self._envelope(),
                        tool_name=tool_name,
                        call_id=call_id,
                        output=output,
                        error=error,
                        seq=seq,
                        arguments=merged,
                    )
                )
            case ToolArgsDeltaSignal(
                tool_name=tool_name, call_id=call_id, args_fragment=fragment
            ):
                return self._content_events(
                    lambda: ToolArgsDelta(
                        **self._envelope(),
                        tool_name=tool_name,
                        call_id=call_id,
                        args_fragment=fragment,
                    )
                )
            case TurnEndedSignal(stop_reason=stop_reason, error=error):
                latency_ms = (
                    int((self._clock() - self._turn_started_at) * 1000)
                    if self._turn_active
                    else 0
                )
                finished = TurnFinished(
                    **self._envelope(),
                    stop_reason=stop_reason,
                    error=error,
                    latency_ms=latency_ms,
                )
                self._reset()
                return [finished]
            case TurnFailedSignal(message=message):
                return [TurnErrored(**self._envelope(), message=message)]
            case UsageReportedSignal(usage=usage):
                return [UsageSummary(**self._envelope(), usage=usage)]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _content_events(
        self, build: Callable[[], PresentationEvent]
    ) -> list[PresentationEvent]:
        """Lazy-start the turn, announcing it ahead of the content event.

        ``build`` runs AFTER the lazy start so the identity envelope
        carries the freshly assigned turn id.
        """
        started: list[PresentationEvent] = []
        if not self._turn_active:
            self.ensure_turn_started()
            started.append(TurnStarted(**self._envelope()))
        started.append(build())
        return started

    def _envelope(self) -> _EnvelopeKwargs:
        return {
            "session_id": self._session_id,
            "agent_name": self._agent_name,
            "turn_id": self._current_turn_id,
            "pool": self._pool,
            "workspace": self._workspace,
            "timestamp_ms": int(self._clock() * 1000),
        }

    def _reset(self) -> None:
        self._current_turn_id = ""
        self._turn_active = False
        self._turn_started_at = self._clock()
        self._pending_calls = {}


__all__ = [
    "DefaultTurnEventProjector",
    "RuntimeTurnEvent",
    "ToolArgsDeltaSignal",
    "TurnEndedSignal",
    "TurnEventProjector",
    "TurnFailedSignal",
    "UsageReportedSignal",
]
