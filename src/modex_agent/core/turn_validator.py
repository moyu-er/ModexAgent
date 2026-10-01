"""Sequence validation for the unified turn-event stream.

``TurnEventValidator`` is a small per-session state machine over the core
``TurnEvent`` union. It checks the stream's shape — not its content —
against the legal sequence:

    optional turn_started
      -> any content / tool / iteration / progress / usage / turn_errored events
      -> (approval_requested -> approval_resolved)*
      -> exactly one turn_finished (terminal)

Two modes:

- ``strict`` — ``turn_started`` is required first; any event before it
  and any terminal before content are violations.
- ``lenient`` (default) — a content event before ``turn_started``
  auto-starts the turn, matching the presentation projector's
  lazy-identity reality during migration.

The validator never raises: ``feed`` accumulates violations and callers
inspect ``violations`` (empty list = legal sequence) and ``finished``.
"""

from __future__ import annotations

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

_CONTENT_KINDS: frozenset[str] = frozenset(
    {"text", "reasoning", "tool_call", "tool_result", "tool_args_delta"}
)
"""Content-bearing kinds — a lenient validator auto-starts the turn on them."""


class TurnEventValidator:
    """Accumulating sequence validator over one session's ``TurnEvent`` stream."""

    def __init__(self, *, strict: bool = False) -> None:
        self._strict = strict
        self._violations: list[str] = []
        self._started = False
        self._finished = False
        self._content_seen = False
        self._seen_call_ids: set[str] = set()
        self._pending_approvals: set[str] = set()

    @property
    def violations(self) -> list[str]:
        """Every violation observed so far (empty = legal sequence)."""
        return list(self._violations)

    @property
    def finished(self) -> bool:
        """Whether the terminal ``turn_finished`` was already observed."""
        return self._finished

    def feed(self, event: TurnEvent) -> None:
        """Validate one event against the current sequence state."""
        if event.kind in _CONTENT_KINDS:
            self._content_seen = True
        match event:
            case TurnStartedEvent():
                if self._finished:
                    self._note("turn_started after turn_finished")
                elif self._started:
                    self._note("duplicate turn_started")
                else:
                    self._started = True
            case TurnFinishedEvent():
                if self._finished:
                    self._note("duplicate terminal turn_finished")
                    return
                if self._strict and not self._started:
                    self._note("turn_finished without turn_started (strict mode)")
                if self._strict and not self._content_seen:
                    self._note("terminal before any content (strict mode)")
                if self._pending_approvals:
                    names = ", ".join(sorted(self._pending_approvals))
                    self._note(f"turn_finished with unresolved approval: {names}")
                self._finished = True
            case ApprovalRequestedEvent(call_id=call_id):
                if self._require_open(event):
                    self._pending_approvals.add(call_id)
            case ApprovalResolvedEvent(call_id=call_id):
                if not self._require_open(event):
                    return
                if call_id in self._pending_approvals:
                    self._pending_approvals.discard(call_id)
                else:
                    self._note(
                        f"approval_resolved without matching approval_requested: {call_id}"
                    )
            case TurnToolCallEvent(call_id=call_id):
                if self._require_open(event):
                    self._seen_call_ids.add(call_id)
            case TurnToolResultEvent(call_id=call_id):
                if self._require_open(event) and call_id not in self._seen_call_ids:
                    self._note(f"tool_result without preceding tool_call: {call_id}")
            case (
                TurnTextEvent()
                | TurnReasoningEvent()
                | ToolArgsDeltaEvent()
                | TurnErroredEvent()
                | UsageEvent()
                | IterationStartedEvent()
                | IterationFinishedEvent()
                | ProgressEvent()
            ):
                self._require_open(event)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_open(self, event: TurnEvent) -> bool:
        """Gate a non-terminal event; return whether processing proceeds.

        Flags events after the terminal; enforces the required
        ``turn_started`` in strict mode and auto-starts leniently on
        content kinds.
        """
        if self._finished:
            self._note(f"event after terminal turn_finished: {event.kind}")
            return False
        if not self._started:
            if self._strict:
                self._note(f"{event.kind} before turn_started (strict mode)")
                return False
            if event.kind in _CONTENT_KINDS:
                self._started = True
        return True

    def _note(self, message: str) -> None:
        self._violations.append(message)


__all__ = ["TurnEventValidator"]
