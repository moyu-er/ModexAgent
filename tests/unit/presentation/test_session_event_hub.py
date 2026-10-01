"""SessionEventHub — the per-turn station contract (ADR-0054, W3).

The hub is the composition point between the runtime stream and
presentation consumers: it IS a ``TurnEventSink``, runs the default
projection, and fans every produced ``PresentationEvent`` out to the
registered ``PresentationSink`` consumers in registration order. These
tests pin:

- identity from the ``TurnBinding`` (session/pool/workspace envelope;
  ``turn_id`` adopted when non-empty, projector-lazy when empty);
- eager ``turn_started`` handling on a fresh binding;
- resume semantics — a ``resumed=True`` binding continues the suspended
  attempt's turn id and emits no second ``TurnStarted`` (neither for a
  replayed eager start nor for the lazy first-content announcement);
- consumer delivery order (sequential, registration order);
- gate filtering applied before projection;
- flush fan-out;
- the projector exposure for transcript-record helpers.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from modex_agent.core.emitter import KindGate, TurnBinding
from modex_agent.core.turn_events import (
    StopReason,
    TurnFinishedEvent,
    TurnStartedEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.presentation import (
    PresentationEvent,
    PresentationSink,
    SessionEventHub,
)
from modex_agent.presentation import (
    TextDelta as PresentationTextDelta,
)
from modex_agent.presentation import (
    TurnStarted as PresentationTurnStarted,
)


class RecordingPresentationSink(PresentationSink):
    """Records ``(consumer name, event kind, turn id)`` into a shared log."""

    def __init__(self, name: str, log: list[tuple[str, str, str]]) -> None:
        self.name = name
        self.log = log
        self.events: list[PresentationEvent] = []
        self.flush_count = 0

    async def handle(self, event: PresentationEvent) -> None:
        self.log.append((self.name, event.kind, event.turn_id))
        self.events.append(event)

    async def flush(self) -> None:
        self.flush_count += 1


def _hub(
    consumers: Sequence[PresentationSink],
    *,
    turn_id: str = "",
    resumed: bool = False,
    gate: KindGate | None = None,
) -> SessionEventHub:
    return SessionEventHub(
        TurnBinding(
            session_id="conv.reviewer.x1",
            agent_name="reviewer",
            pool="pool-a",
            workspace="/ws/alpha",
            turn_id=turn_id,
            resumed=resumed,
        ),
        consumers,
        gate=gate,
    )


# ── Identity from the binding ───────────────────────────────────────────────


async def test_binding_identity_carries_into_every_envelope() -> None:
    sink = RecordingPresentationSink("only", [])
    hub = _hub([sink])

    await hub.emit(TurnStartedEvent())
    await hub.emit(TurnTextEvent(text="hi"))

    delta = next(e for e in sink.events if isinstance(e, PresentationTextDelta))
    assert delta.session_id == "conv.reviewer.x1"
    assert delta.agent_name == "reviewer"
    assert delta.pool == "pool-a"
    assert delta.workspace == "/ws/alpha"
    assert hub.binding.session_id == "conv.reviewer.x1"


async def test_binding_agent_name_wins_over_derived_identity() -> None:
    """The envelope's agent identity has ONE assignment path: the binding.

    The projector can derive an agent name from the session id, but the
    hub passes the binding's ``agent_name`` explicitly — the two must
    never disagree, so a binding whose name differs from the session's
    derived segment still stamps the binding's name on every envelope.
    """
    sink = RecordingPresentationSink("only", [])
    hub = SessionEventHub(
        TurnBinding(session_id="conv.main", agent_name="reviewer"),
        [sink],
    )

    await hub.emit(TurnTextEvent(text="hi"))

    assert {event.agent_name for event in sink.events} == {"reviewer"}


async def test_binding_turn_id_adopted_when_non_empty() -> None:
    sink = RecordingPresentationSink("only", [])
    hub = _hub([sink], turn_id="runtime-turn-7")

    await hub.emit(TurnStartedEvent())
    await hub.emit(TurnTextEvent(text="hi"))

    kinds = [entry[1] for entry in sink.log]
    assert kinds == ["turn_started", "text_delta"]
    # Eager start + every envelope carry the binding's turn id.
    assert {entry[2] for entry in sink.log} == {"runtime-turn-7"}
    assert hub.projector.current_turn_id == "runtime-turn-7"


async def test_empty_binding_turn_id_stays_projector_lazy() -> None:
    sink = RecordingPresentationSink("only", [])
    hub = _hub([sink])

    await hub.emit(TurnTextEvent(text="hi"))

    # Lazy start: TurnStarted announced ahead of the first content event.
    assert [entry[1] for entry in sink.log] == ["turn_started", "text_delta"]
    lazy_ids = {entry[2] for entry in sink.log}
    assert len(lazy_ids) == 1
    assert lazy_ids != {""}, "lazy turn identity must be assigned"
    assert hub.projector.current_turn_id in lazy_ids


# ── Eager turn_started on a fresh binding ───────────────────────────────────


async def test_eager_turn_started_announced_once() -> None:
    sink = RecordingPresentationSink("only", [])
    hub = _hub([sink])

    await hub.emit(TurnStartedEvent())
    await hub.emit(TurnStartedEvent())  # duplicate eager start is idempotent
    await hub.emit(TurnTextEvent(text="hi"))

    assert [entry[1] for entry in sink.log] == [
        "turn_started",
        "text_delta",
    ], "a repeated turn_started must not re-announce the turn"


# ── Resume semantics ────────────────────────────────────────────────────────


async def test_resumed_binding_continues_turn_id_without_second_start() -> None:
    sink = RecordingPresentationSink("only", [])
    hub = _hub([sink], turn_id="suspended-turn-9", resumed=True)

    # The suspended attempt already announced the turn: the resumed leg's
    # first content event must NOT lazily re-announce it.
    await hub.emit(TurnTextEvent(text="continuing"))
    assert [entry[1] for entry in sink.log] == ["text_delta"]
    assert {entry[2] for entry in sink.log} == {"suspended-turn-9"}

    # A replayed eager start stays idempotent on a resumed hub.
    await hub.emit(TurnStartedEvent())
    assert [entry[1] for entry in sink.log] == ["text_delta"]

    await hub.emit(TurnFinishedEvent(stop_reason=StopReason.COMPLETED))
    assert [entry[1] for entry in sink.log] == ["text_delta", "turn_finished"]
    assert sink.log[-1][2] == "suspended-turn-9"


def test_resumed_binding_requires_the_suspended_turn_id() -> None:
    with pytest.raises(ValueError, match="turn_id"):
        _hub([RecordingPresentationSink("only", [])], resumed=True)


# ── Consumer fan-out ────────────────────────────────────────────────────────


async def test_consumers_receive_events_in_registration_order() -> None:
    log: list[tuple[str, str, str]] = []
    first = RecordingPresentationSink("first", log)
    second = RecordingPresentationSink("second", log)
    hub = _hub([first, second])

    # A tool call yields (lazy TurnStarted + ToolCallStarted) — each produced
    # event fans out to EVERY consumer before the next event is produced.
    await hub.emit(TurnToolCallEvent(tool_name="echo", call_id="c1", arguments={"text": "hi"}))

    assert log == [
        ("first", "turn_started", log[0][2]),
        ("second", "turn_started", log[0][2]),
        ("first", "tool_call_started", log[0][2]),
        ("second", "tool_call_started", log[0][2]),
    ]


class _RaisingPresentationSink(PresentationSink):
    """Consumer whose handle always raises (a buggy consumer)."""

    async def handle(self, event: PresentationEvent) -> None:
        raise RuntimeError(f"consumer exploded on {event.kind}")


async def test_raising_consumer_is_isolated_across_a_scripted_native_turn() -> None:
    """Fan-out failure policy: a buggy consumer must not corrupt the turn.

    Drives a scripted native turn (started -> text -> tool call ->
    tool result -> terminal COMPLETED) through a hub whose FIRST consumer
    raises on every event and whose second consumer records. Pinned:

    - the raise never propagates to the emitting node (emit returns);
    - the recording consumer still receives every presentation event;
    - the turn's terminal stays the single COMPLETED ``turn_finished`` —
      a consumer defect must not convert a completed turn into an errored
      one (no extra ``turn_errored`` / second terminal).
    """
    recorder = RecordingPresentationSink("healthy", [])
    hub = _hub([_RaisingPresentationSink(), recorder])

    await hub.emit(TurnStartedEvent())
    await hub.emit(TurnTextEvent(text="working"))
    await hub.emit(
        TurnToolCallEvent(tool_name="echo", call_id="c1", arguments={"text": "hi"})
    )
    await hub.emit(TurnToolResultEvent(tool_name="echo", call_id="c1", output="echo: hi"))
    await hub.emit(TurnFinishedEvent(stop_reason=StopReason.COMPLETED))

    assert [entry[1] for entry in recorder.log] == [
        "turn_started",
        "text_delta",
        "tool_call_started",
        "tool_result",
        "turn_finished",
    ]
    terminals = [e for e in recorder.events if e.kind in ("turn_finished", "turn_errored")]
    assert len(terminals) == 1, "a consumer defect must not add a terminal event"
    finished = terminals[0]
    assert finished.kind == "turn_finished"
    assert finished.stop_reason is StopReason.COMPLETED


async def test_gate_filters_before_projection() -> None:
    sink = RecordingPresentationSink("only", [])
    hub = _hub(
        [sink],
        gate=KindGate(disabled_kinds=frozenset({"tool_args_delta"})),
    )

    from modex_agent.core.turn_events import ToolArgsDeltaEvent

    await hub.emit(ToolArgsDeltaEvent(call_id="c1", tool_name="echo", args_fragment='{"te'))

    assert sink.log == []
    # Gated before projection: the content-bearing event never started a turn.
    assert hub.projector.current_turn_id == ""

    await hub.emit(TurnTextEvent(text="hi"))
    assert [entry[1] for entry in sink.log] == ["turn_started", "text_delta"]


async def test_flush_fans_out_to_every_consumer() -> None:
    first = RecordingPresentationSink("first", [])
    second = RecordingPresentationSink("second", [])
    hub = _hub([first, second])

    await hub.flush()

    assert first.flush_count == 1
    assert second.flush_count == 1


# ── Projector exposure ──────────────────────────────────────────────────────


async def test_projector_exposed_for_transcript_record_helpers() -> None:
    sink = RecordingPresentationSink("only", [])
    hub = _hub([sink])

    # A record helper that writes a transcript record without producing a
    # presentation event can still assign turn identity up front.
    assigned = hub.projector.ensure_turn_started()
    assert assigned == hub.projector.current_turn_id
    assert assigned != ""

    await hub.emit(TurnTextEvent(text="hi"))
    started = [e for e in sink.events if isinstance(e, PresentationTurnStarted)]
    assert started == [], "identity already assigned — no announcement"
    assert sink.log[0] == ("only", "text_delta", assigned)
