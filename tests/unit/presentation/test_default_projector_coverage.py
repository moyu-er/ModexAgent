"""T-P1 — DefaultTurnEventProjector coverage anchor + JSONL round-trip.

Drives scripted turns through the REAL ``ReActAgent`` (scripted stream
provider, real graph nodes, real tool execution) with a recording emitter
that captures every runtime event the emitter seam produces — enum
``ReActEvent`` emissions, ``TurnEvent``s, streaming deltas, completion —
then feeds each through ``DefaultTurnEventProjector``.

Anchors:

1. **Disposition completeness** — every ``ReActEvent`` value is either
   mapped to a projector input kind (``MAPPED_RUNTIME_EVENTS``) or listed
   in the documented ignore-list (``IGNORED_RUNTIME_EVENTS``). No silent
   drops are possible: a runtime event outside both sets fails the test.
2. **Coverage** — the scripted turns exercise ALL ``ReActEvent`` values.
3. **Mapping** — feeding any mapped runtime event yields >= 1 presentation
   event.
4. **Round-trip** — every presentation event appended to the JSONL
   transcript store reloads and ``materialize_turns`` reproduces the turn
   view (merged text/thinking segments, paired tool cards, terminal stop
   reason).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from modex_agent.agents.react.agent import ReActAgent, ReActEvent
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, ContentEmitter, StopReason
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import FinishReason
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    TextDelta,
    ToolCallComplete,
    ToolCallDelta,
)
from modex_agent.core.tool_manager import Tool
from modex_agent.core.turn_events import (
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.memory.history import ListMessageHistory
from modex_agent.presentation import (
    DefaultTurnEventProjector,
    PresentationEvent,
    PresentationTranscriptStore,
    RuntimeTurnEvent,
    ToolArgsDeltaSignal,
    TurnEndedSignal,
    TurnFailedSignal,
    TurnFinished,
    materialize_turns,
)
from modex_agent.presentation import (
    ToolResult as PresentationToolResult,
)
from modex_agent.tools.manager import InMemoryToolManager

# ── Scripted fixtures ──────────────────────────────────────────────────────


class ScriptedProvider(LLMProvider):
    """Yields one scripted ``LLMStreamEvent`` round per ``stream()`` call.

    A round may also be an ``Exception`` instance — raised out of the
    stream to script provider failures.
    """

    def __init__(self, rounds: list[list[LLMStreamEvent] | Exception]) -> None:
        super().__init__()
        self._rounds = list(rounds)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        if not self._rounds:
            raise RuntimeError("script exhausted")
        entry = self._rounds.pop(0)
        if isinstance(entry, Exception):
            raise entry
        for event in entry:
            yield event

    def get_default_model(self) -> str:
        return "scripted"


class EchoTool(Tool):
    """Minimal real tool the tool node executes."""

    def __init__(self) -> None:
        super().__init__(
            name="echo",
            description="echo the text argument back",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

    async def execute(self, **kwargs: Any) -> str:
        return f"echo: {kwargs.get('text', '')}"


@dataclass
class RecordedCall:
    """One emitter-seam call as the presentation consumer sees it."""

    kind: str  # "emit" | "delta" | "turn_event" | "complete" | "error" | "stream_end"
    name: str  # enum value / TurnEvent kind / "" for lifecycle entries
    data: Any = None


class RecordingEmitter(ContentEmitter[ReActEvent]):
    """Records every runtime event the emitter seam receives."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[RecordedCall] = []

    def wants_streaming(self) -> bool:
        return True

    async def emit(self, event: ReActEvent, data: Any = None) -> None:
        await super().emit(event, data)
        self.calls.append(
            RecordedCall("emit", event.value if isinstance(event, ReActEvent) else str(event), data)
        )

    async def emit_delta(self, delta: str) -> None:
        self.calls.append(RecordedCall("delta", "", delta))

    async def emit_content(self, full_content: str) -> None:
        self.calls.append(RecordedCall("content", "", full_content))

    async def emit_turn_event(self, event: Any) -> None:
        self.calls.append(RecordedCall("turn_event", event.kind, event))

    async def emit_complete(self, result: AgentResult) -> None:
        self.calls.append(RecordedCall("complete", "", result))

    async def emit_error(self, error: str) -> None:
        self.calls.append(RecordedCall("error", "", error))

    async def emit_stream_end(self, resuming: bool = False) -> None:
        self.calls.append(RecordedCall("stream_end", "", resuming))


async def _run_turn(
    rounds: list[list[LLMStreamEvent] | Exception],
    *,
    max_iterations: int = 10,
) -> tuple[list[RecordedCall], AgentResult]:
    provider = ScriptedProvider(rounds)
    emitter = RecordingEmitter()
    tool_manager = InMemoryToolManager()
    tool_manager.register(EchoTool())
    ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=tool_manager,
        session=SessionInfo.from_str("conv.main"),
        max_iterations=max_iterations,
    )
    agent = ReActAgent(provider)
    result = await agent.run(ctx, emitter)
    return emitter.calls, result


def _translate(call: RecordedCall) -> RuntimeTurnEvent | None:
    """Consumer-side translation: recorded seam call -> projector input.

    Returns ``None`` for inputs with no presentation meaning (ignore-list
    members and emitter-internal lifecycle entries like ``stream_end``).
    """
    match call.kind:
        case "delta":
            return TurnTextEvent(text=call.data)
        case "turn_event":
            return call.data
        case "emit":
            match call.name:
                case ReActEvent.MODEL_REASONING.value:
                    return TurnReasoningEvent(text=call.data)
                case ReActEvent.TOOL_ARGS_DELTA.value:
                    return ToolArgsDeltaSignal(
                        tool_name=call.data.tool_name,
                        call_id=call.data.call_id,
                        args_fragment=call.data.args_fragment,
                    )
                case ReActEvent.TOOL_CALL_START.value:
                    return TurnToolCallEvent(
                        tool_name=call.data.tool_name,
                        call_id=call.data.call_id,
                        arguments=call.data.arguments,
                    )
                case ReActEvent.TOOL_CALL_END.value:
                    payload = call.data
                    return TurnToolResultEvent(
                        tool_name=payload.tool_call.tool_name,
                        call_id=payload.tool_call.call_id,
                        output=payload.result.message_content(),
                        error=payload.result.error,
                        seq=payload.seq,
                        arguments=payload.tool_call.arguments,
                    )
                case ReActEvent.ERROR.value:
                    return TurnFailedSignal(message=str(call.data))
                case _:
                    return None
        case "complete":
            return TurnEndedSignal(
                stop_reason=call.data.stop_reason, error=call.data.error
            )
        case "error":
            return TurnFailedSignal(message=call.data)
    return None


async def _record_all_turns() -> list[RecordedCall]:
    calls_a, _ = await _run_turn(
        [[TextDelta(text="Hello "), TextDelta(text="world"), Finish(finish_reason=FinishReason.STOP)]]
    )
    calls_b, _ = await _run_turn(
        [
            [
                ReasoningDelta(text="let me think"),
                ToolCallDelta(call_id="c1", tool_name="echo", args_fragment='{"te'),
                ToolCallDelta(call_id="c1", tool_name="echo", args_fragment='xt": "hi"}'),
                ToolCallComplete(
                    call_id="c1", tool_name="echo", arguments={"text": "hi"}
                ),
                Finish(finish_reason=FinishReason.TOOL_CALLS),
            ],
            [TextDelta(text="all done"), Finish(finish_reason=FinishReason.STOP)],
        ]
    )
    calls_c, _ = await _run_turn(
        [
            [
                ToolCallComplete(
                    call_id="c1", tool_name="echo", arguments={"text": "again"}
                ),
                Finish(finish_reason=FinishReason.TOOL_CALLS),
            ]
        ]
        * 3,
        max_iterations=1,
    )
    calls_d, _ = await _run_turn([RuntimeError("provider exploded")])
    return calls_a + calls_b + calls_c + calls_d


# ── Anchor 1 + 2: disposition completeness and scripted coverage ───────────


def test_react_event_dispositions_are_complete() -> None:
    mapped = DefaultTurnEventProjector.MAPPED_RUNTIME_EVENTS
    ignored = DefaultTurnEventProjector.IGNORED_RUNTIME_EVENTS
    all_values = {event.value for event in ReActEvent}
    assert mapped | ignored == all_values, (
        f"undisposed ReActEvent values: {all_values - (mapped | ignored)}"
    )
    assert not (mapped & ignored), "a value cannot be both mapped and ignored"


async def test_scripted_turns_exercise_every_react_event_value() -> None:
    calls = await _record_all_turns()
    seen = {call.name for call in calls if call.kind == "emit"}
    expected = {event.value for event in ReActEvent}
    assert seen == expected, f"script missed: {sorted(expected - seen)}"


# ── Anchor 3 + 4: every mapped event projects; JSONL round-trip ────────────


async def test_projector_maps_every_runtime_event_and_round_trips(
    tmp_path: Path,
) -> None:
    calls = await _record_all_turns()
    projector = DefaultTurnEventProjector(session_id="conv.main")
    store = PresentationTranscriptStore(tmp_path)

    ignored = DefaultTurnEventProjector.IGNORED_RUNTIME_EVENTS
    for call in calls:
        runtime_event = _translate(call)
        if runtime_event is None:
            # Never a silent drop: the untranslated call must be a declared
            # ignore-list member or an emitter-internal lifecycle entry.
            assert call.name in ignored or call.kind in {
                "stream_end",
                "content",
            }, f"silent drop: {call}"
            continue
        events: list[PresentationEvent] = projector.feed(runtime_event)
        assert events, f"mapped runtime event produced nothing: {call}"
        for presentation in events:
            await store.append("conv.main", presentation)

    reloaded = await store.load("conv.main")
    assert len(reloaded) > 0
    views = materialize_turns(reloaded)

    # Four scripted turns -> four turn views (each produced content events).
    assert len(views) == 4

    view_a, view_b, view_c, view_d = views

    # Turn A: one merged text block.
    texts_a = [b.text for b in view_a.blocks if getattr(b, "kind", "") == "text"]
    assert texts_a == ["Hello world"]
    assert view_a.stop_reason == StopReason.COMPLETED

    # Turn B: thinking segment, tool card, final text — in arrival order.
    kinds_b = [b.kind for b in view_b.blocks]
    assert kinds_b == ["thinking", "tool", "text"]
    tool_block = view_b.blocks[1]
    assert tool_block.tool_name == "echo"
    assert tool_block.arguments == {"text": "hi"}
    assert tool_block.output == "echo: hi"
    assert [b.text for b in view_b.blocks if b.kind == "text"] == ["all done"]

    # Turn C: iteration cap reached.
    assert view_c.stop_reason == StopReason.MAX_ITERATIONS

    # Turn D: error surface + terminal ERROR finish.
    assert view_d.stop_reason == StopReason.ERROR
    assert view_d.error is not None and "provider exploded" in view_d.error

    # Every tool result kept its full-fidelity call identity.
    tool_results = [e for e in reloaded if isinstance(e, PresentationToolResult)]
    assert tool_results, "scripted turns produced tool result cards"
    assert all(e.call_id == "c1" for e in tool_results)


# ── Turn-identity semantics pinned on the default projector ────────────────


def test_projector_lazy_turn_identity_and_latency() -> None:
    projector = DefaultTurnEventProjector(
        session_id="conv.main",
        turn_id_factory=lambda: "turn-0001",
        clock=lambda: 1000.0,
    )
    assert projector.current_turn_id == ""

    started = projector.feed(TurnTextEvent(text="hi"))
    # Lazy start: first content event announces the turn before the delta.
    assert [type(e).__name__ for e in started] == ["TurnStarted", "TextDelta"]
    assert projector.current_turn_id == "turn-0001"

    finished = projector.feed(TurnEndedSignal(stop_reason=StopReason.COMPLETED))
    assert len(finished) == 1
    assert isinstance(finished[0], TurnFinished)
    assert finished[0].turn_id == "turn-0001"
    assert finished[0].latency_ms == 0

    # Turn ended: identity resets until the next content event.
    assert projector.current_turn_id == ""
    next_events = projector.feed(TurnEndedSignal(stop_reason=StopReason.COMPLETED))
    assert next_events[0].turn_id == ""


def test_projector_merges_tool_call_arguments_into_result_card() -> None:
    projector = DefaultTurnEventProjector(session_id="conv.main")
    projector.feed(
        TurnToolCallEvent(tool_name="echo", call_id="c1", arguments={"text": "hi"})
    )
    events = projector.feed(
        TurnToolResultEvent(
            tool_name="echo", call_id="c1", output="echo: hi", seq=3
        )
    )
    results = [e for e in events if isinstance(e, PresentationToolResult)]
    assert len(results) == 1
    # The result card carries the call's full arguments (paired card) and seq.
    assert results[0].arguments == {"text": "hi"}
    assert results[0].seq == 3

    # A result with no preceding call keeps arguments=None (orphan result —
    # consumers must not fabricate an empty-args call record).
    orphan = projector.feed(
        TurnToolResultEvent(tool_name="echo", call_id="nope", output="late")
    )
    orphan_results = [e for e in orphan if isinstance(e, PresentationToolResult)]
    assert orphan_results[0].arguments is None


@pytest.mark.parametrize(
    ("runtime_event", "expected_kind"),
    [
        (TurnTextEvent(text="x"), "text_delta"),
        (TurnReasoningEvent(text="x"), "thinking_delta"),
        (
            ToolArgsDeltaSignal(tool_name="t", call_id="c", args_fragment='{"'),
            "tool_args_delta",
        ),
    ],
)
def test_streaming_inputs_project_deltas(
    runtime_event: RuntimeTurnEvent, expected_kind: str
) -> None:
    projector = DefaultTurnEventProjector(session_id="conv.main")
    events = projector.feed(runtime_event)
    deltas = [e for e in events if type(e).__name__ != "TurnStarted"]
    assert len(deltas) == 1
    assert deltas[0].kind == expected_kind
