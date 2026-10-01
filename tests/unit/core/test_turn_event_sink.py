"""Framework tests for the unified turn-event sink face (core/emitter.py).

Regression anchors for the W2 cutover:

- Composite fan-out delivers EVERY gate-passing event to EVERY child in
  order — the retired per-method fan-out silently dropped events on
  children that did not override the specific channel; the single
  ``_dispatch`` override point makes that failure mode structurally
  impossible.
- KindGate filtering composes: the composite applies its own gate first,
  each child applies its own.
"""

from __future__ import annotations

import pytest

from modex_agent.core.emitter import (
    CompositeTurnEventSink,
    KindGate,
    TurnBinding,
    TurnEventSink,
)
from modex_agent.core.turn_events import (
    StopReason,
    TurnErroredEvent,
    TurnEvent,
    TurnFinishedEvent,
    TurnReasoningEvent,
    TurnTextEvent,
)


class _RecordingSink(TurnEventSink):
    """Sink recording every dispatched event kind + payload."""

    def __init__(self, gate: KindGate | None = None, *, streaming: bool = False) -> None:
        super().__init__(gate)
        self._streaming = streaming
        self.texts: list[str] = []
        self.events: list[TurnEvent] = []

    def wants_streaming(self) -> bool:
        return self._streaming

    async def _dispatch(self, event: TurnEvent) -> None:
        self.events.append(event)
        if isinstance(event, TurnTextEvent):
            self.texts.append(event.text)


class _FailingSink(TurnEventSink):
    """Sink whose dispatch always raises."""

    async def _dispatch(self, event: TurnEvent) -> None:
        raise RuntimeError("child exploded")


class TestCompositeFanOut:
    @pytest.mark.asyncio
    async def test_text_events_reach_all_children_in_order(self) -> None:
        """The old silent-drop regression: every child receives every text
        event — no override omissions, no per-channel fan-out."""
        child_a = _RecordingSink()
        child_b = _RecordingSink()
        composite = CompositeTurnEventSink((child_a, child_b))

        for fragment in ("Hello ", "world"):
            await composite.emit(TurnTextEvent(text=fragment))
        await composite.emit(TurnFinishedEvent(stop_reason=StopReason.COMPLETED))

        assert child_a.texts == ["Hello ", "world"]
        assert child_b.texts == ["Hello ", "world"]
        assert [e.kind for e in child_a.events] == ["text", "text", "turn_finished"]
        assert child_b.events == child_a.events

    @pytest.mark.asyncio
    async def test_child_failure_does_not_starve_later_children(self) -> None:
        """Failure isolation: one child raising must not drop the event for
        the children after it."""
        failing = _FailingSink()
        healthy = _RecordingSink()
        composite = CompositeTurnEventSink((failing, healthy))

        await composite.emit(TurnTextEvent(text="must arrive"))
        await composite.emit(TurnErroredEvent(message="mid-flight"))

        assert healthy.texts == ["must arrive"]
        assert [e.kind for e in healthy.events] == ["text", "turn_errored"]

    @pytest.mark.asyncio
    async def test_child_gate_narrows_child_delivery_only(self) -> None:
        """Each child applies its own gate — a reasoning-only child drops
        text while the ungated child still receives it."""
        reasoning_only = _RecordingSink(
            KindGate(enabled_kinds=frozenset({"reasoning"}))
        )
        everything = _RecordingSink()
        composite = CompositeTurnEventSink((reasoning_only, everything))

        await composite.emit(TurnTextEvent(text="visible"))
        await composite.emit(TurnReasoningEvent(text="thinking"))

        assert reasoning_only.events == [TurnReasoningEvent(text="thinking")]
        assert [e.kind for e in everything.events] == ["text", "reasoning"]

    @pytest.mark.asyncio
    async def test_composite_gate_filters_before_dispatch(self) -> None:
        """The composite's own gate drops events before any child sees
        them."""
        child = _RecordingSink()
        composite = CompositeTurnEventSink(
            (child,), KindGate(disabled_kinds=frozenset({"text"}))
        )

        await composite.emit(TurnTextEvent(text="dropped"))
        await composite.emit(TurnReasoningEvent(text="kept"))

        assert [e.kind for e in child.events] == ["reasoning"]

    def test_wants_streaming_is_any_child_or(self) -> None:
        neither = CompositeTurnEventSink((_RecordingSink(), _RecordingSink()))
        one = CompositeTurnEventSink((_RecordingSink(), _RecordingSink(streaming=True)))
        assert neither.wants_streaming() is False
        assert one.wants_streaming() is True


class TestTurnBinding:
    def test_binding_is_a_frozen_value_object(self) -> None:
        binding = TurnBinding(
            session_id="conv.main", agent_name="main", pool="coder", turn_id="t42"
        )
        assert binding.resumed is False
        with pytest.raises(Exception):
            binding.turn_id = "other"  # type: ignore[misc]

    def test_resume_binding_carries_same_turn_id_flag(self) -> None:
        first = TurnBinding(session_id="conv.main", agent_name="main", turn_id="t42")
        resumed = first.model_copy(update={"resumed": True})
        assert resumed.turn_id == first.turn_id
        assert resumed.resumed is True
