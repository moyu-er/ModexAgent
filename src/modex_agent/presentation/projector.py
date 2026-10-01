"""Runtime-event → presentation-event projector (ADR-0053, ADR-0054).

The projector is a pure, synchronous projection engine over the neutral
runtime seam: it consumes the core ``TurnEvent`` union (the runtime event
vocabulary BOTH execution planes emit onto) and produces
``PresentationEvent``s. It owns exactly the state a turn projection needs
— lazy turn identity, pending tool-call arguments, turn latency — so
consumers (WebUI emitters, editors, consoles) translate its output to
their sink without re-deriving turn bookkeeping.

Layering: both execution planes emit the core union at the source (the
ADR-0054 migration is complete); this package imports no agent strategy
by layer discipline. The disposition of every core event kind is
declared below (``MAPPED_TURN_EVENT_KINDS`` /
``IGNORED_TURN_EVENT_KINDS``) — never a silent drop.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import ClassVar, TypedDict

from pydantic import JsonValue

from modex_agent.core.session_id import agent_of
from modex_agent.core.turn_events import (
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    IterationFinishedEvent,
    IterationStartedEvent,
    ProgressEvent,
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

from .events import (
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

# ── Projector contract ─────────────────────────────────────────────────────


class TurnEventProjector(ABC):
    """Project core runtime turn events onto presentation events."""

    @abstractmethod
    def feed(self, event: TurnEvent) -> list[PresentationEvent]:
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

    - **Turn identity** — ``turn_started`` assigns it eagerly; a
      content-first stream (a bridge path without ``turn_started``)
      assigns it lazily — the first content-bearing event takes a turn
      id (``uuid4().hex[:12]`` by default) and emits ``TurnStarted``
      ahead of the content event. Turn identity resets after
      ``turn_finished``.
    - **Agent identity** — derived from the session id (second ``.``
      segment) unless an explicit ``agent_name`` overrides it (the hub
      passes its binding's, so the envelope never has two assignment
      paths that can disagree).
    - **Segment ids** — text/thinking deltas carry the runtime ``part_id``
      when present, else ``"_text"`` / ``"_reasoning"``.
    - **Tool pairing** — ``TurnToolCallEvent`` remembers the call's full
      arguments; ``TurnToolResultEvent`` merges them (or keeps its own
      ``arguments`` when the runtime result carries them, e.g. a resumed
      approval turn) into one complete ``ToolResult`` card. Orphan
      results keep ``arguments=None``.
    - **Latency** — ``TurnFinished.latency_ms`` is measured from turn
      start through the ``clock`` (injectable; wall clock by default).
      On a resumed turn (an approval resume re-invocation) the
      measurement starts at the resumed projector's construction — the
      resume time — not the original turn start: the projector has no
      memory of the suspended leg's start time.
    """

    MAPPED_TURN_EVENT_KINDS: ClassVar[frozenset[str]] = frozenset(
        {
            "turn_started",
            "turn_finished",
            "turn_errored",
            "text",
            "reasoning",
            "tool_args_delta",
            "tool_call",
            "tool_result",
            "approval_requested",
            "approval_resolved",
            "usage",
        }
    )
    """Core ``TurnEvent`` kinds that map onto a presentation event."""

    IGNORED_TURN_EVENT_KINDS: ClassVar[frozenset[str]] = frozenset(
        {"iteration_started", "iteration_finished", "progress"}
    )
    """Documented ignore-list — core event kinds with no presentation mapping.

    - ``iteration_started`` / ``iteration_finished``: intra-turn loop
      bookkeeping with no generic UI meaning.
    - ``progress``: long-settlement heartbeat; consumers that want it
      observe the core stream directly.

    Their union with ``MAPPED_TURN_EVENT_KINDS`` must equal the full core
    kind set — the architecture anchor enforces it.
    """

    def __init__(
        self,
        session_id: str,
        *,
        agent_name: str | None = None,
        pool: str | None = None,
        workspace: str | None = None,
        turn_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._session_id = session_id
        self._agent_name = agent_name or agent_of(session_id, default="main")
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

    def feed(self, event: TurnEvent) -> list[PresentationEvent]:
        match event:
            case TurnStartedEvent():
                if self._turn_active:
                    return []
                self.ensure_turn_started()
                return [TurnStarted(**self._envelope())]
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
            case ToolArgsDeltaEvent(
                call_id=call_id, tool_name=tool_name, args_fragment=fragment
            ):
                return self._content_events(
                    lambda: ToolArgsDelta(
                        **self._envelope(),
                        tool_name=tool_name,
                        call_id=call_id,
                        args_fragment=fragment,
                    )
                )
            case TurnFinishedEvent(stop_reason=stop_reason, error=error):
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
            case TurnErroredEvent(message=message):
                return [TurnErrored(**self._envelope(), message=message)]
            case ApprovalRequestedEvent(
                tool_name=tool_name, call_id=call_id, prompt=prompt
            ):
                return [
                    ApprovalRequested(
                        **self._envelope(),
                        tool_name=tool_name,
                        call_id=call_id,
                        prompt=prompt,
                    )
                ]
            case ApprovalResolvedEvent(call_id=call_id, approved=approved):
                return [
                    ApprovalResolved(
                        **self._envelope(), call_id=call_id, approved=approved
                    )
                ]
            case UsageEvent(usage=usage):
                return [UsageSummary(**self._envelope(), usage=usage)]
            case IterationStartedEvent() | IterationFinishedEvent() | ProgressEvent():
                # Declared ignore-list members (see IGNORED_TURN_EVENT_KINDS).
                return []

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
    "TurnEventProjector",
]
