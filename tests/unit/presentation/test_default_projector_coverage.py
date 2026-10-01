"""T-P1 — DefaultTurnEventProjector coverage anchor + JSONL round-trip.

Drives scripted turns through the REAL ``ReActAgent`` (scripted stream
provider, real graph nodes, real tool execution) with a recording sink
that captures every runtime turn event the emitter seam produces — the
runtime emits the core ``TurnEvent`` union directly — then feeds each
event through ``DefaultTurnEventProjector``.

Anchors:

1. **Disposition completeness** — every core ``TurnEvent`` kind is either
   mapped (``MAPPED_TURN_EVENT_KINDS``) or listed in the documented
   ignore-list (``IGNORED_TURN_EVENT_KINDS``). No silent drops are
   possible: a runtime kind outside both sets fails the test.
2. **Coverage** — the scripted turns exercise every content-bearing core
   kind the runtime emits on these paths.
3. **Mapping** — feeding any mapped runtime event yields >= 1 presentation
   event.
4. **Round-trip** — every presentation event appended to the JSONL
   transcript store reloads and ``materialize_turns`` reproduces the turn
   view (merged text/thinking segments, paired tool cards, terminal stop
   reason).
"""

from __future__ import annotations

import typing
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import TurnEvent, TurnEventSink
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
    StopReason,
    TurnFinishedEvent,
    TurnTextEvent,
)
from modex_agent.memory.history import ListMessageHistory
from modex_agent.presentation import (
    DefaultTurnEventProjector,
    PresentationEvent,
    PresentationTranscriptStore,
    TurnFinished,
    materialize_turns,
)
from modex_agent.presentation import (
    ToolResult as PresentationToolResult,
)
from modex_agent.tools.manager import InMemoryToolManager

# Core kinds with no presentation mapping (declared projector ignore-list):
# loop bookkeeping has no generic UI meaning; the terminal facts ride
# ``turn_finished``.
_IGNORED_RUNTIME_KINDS = frozenset({"iteration_started", "iteration_finished", "progress"})

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


class RecordingSink(TurnEventSink):
    """Records every runtime turn event the emitter seam receives."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[TurnEvent] = []

    def wants_streaming(self) -> bool:
        return True

    async def _dispatch(self, event: TurnEvent) -> None:
        self.events.append(event)


async def _run_turn(
    rounds: list[list[LLMStreamEvent] | Exception],
    *,
    max_iterations: int = 10,
) -> tuple[list[TurnEvent], Any]:
    provider = ScriptedProvider(rounds)
    sink = RecordingSink()
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
    result = await agent.run(ctx, sink)
    return sink.events, result


async def _record_all_turns() -> list[TurnEvent]:
    events_a, _ = await _run_turn(
        [[TextDelta(text="Hello "), TextDelta(text="world"), Finish(finish_reason=FinishReason.STOP)]]
    )
    events_b, _ = await _run_turn(
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
    events_c, _ = await _run_turn(
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
    events_d, _ = await _run_turn([RuntimeError("provider exploded")])
    return events_a + events_b + events_c + events_d


# ── Anchor 1 + 2: disposition completeness and scripted coverage ───────────


def _turn_event_kinds() -> set[str]:
    """Every ``kind`` literal in the core ``TurnEvent`` union."""
    union = typing.get_args(TurnEvent)[0]
    kinds: set[str] = set()
    for variant in typing.get_args(union):
        kinds.update(typing.get_args(variant.model_fields["kind"].annotation))
    return kinds


def test_turn_event_kind_dispositions_are_complete() -> None:
    mapped = DefaultTurnEventProjector.MAPPED_TURN_EVENT_KINDS
    ignored = DefaultTurnEventProjector.IGNORED_TURN_EVENT_KINDS
    all_kinds = _turn_event_kinds()
    assert mapped | ignored == all_kinds, (
        f"undisposed TurnEvent kinds: {all_kinds - (mapped | ignored)}"
    )
    assert not (mapped & ignored), "a kind cannot be both mapped and ignored"


async def test_scripted_turns_exercise_every_content_bearing_kind() -> None:
    """The scripted paths cover every runtime-produced mapped kind plus the
    declared ignore-list members (never a silent drop)."""
    events = await _record_all_turns()
    seen = {event.kind for event in events}
    assert {
        "turn_started",
        "turn_finished",
        "turn_errored",
        "text",
        "reasoning",
        "tool_args_delta",
        "tool_call",
        "tool_result",
        "iteration_started",
        "iteration_finished",
    } <= seen


# ── Anchor 3 + 4: every mapped event projects; JSONL round-trip ────────────


async def test_projector_maps_every_runtime_event_and_round_trips(
    tmp_path: Path,
) -> None:
    events = await _record_all_turns()
    projector = DefaultTurnEventProjector(session_id="conv.main")
    store = PresentationTranscriptStore(tmp_path)

    ignored_kinds = DefaultTurnEventProjector.IGNORED_TURN_EVENT_KINDS
    assert {"iteration_started", "iteration_finished", "progress"} == ignored_kinds
    for runtime_event in events:
        produced: list[PresentationEvent] = projector.feed(runtime_event)
        if runtime_event.kind in ignored_kinds:
            assert not produced, f"ignored kind produced output: {runtime_event}"
            continue
        assert produced, f"mapped runtime event produced nothing: {runtime_event}"
        for presentation in produced:
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

    finished = projector.feed(TurnFinishedEvent(stop_reason=StopReason.COMPLETED))
    assert len(finished) == 1
    assert isinstance(finished[0], TurnFinished)
    assert finished[0].turn_id == "turn-0001"
    assert finished[0].latency_ms == 0

    # Turn ended: identity resets until the next content event.
    assert projector.current_turn_id == ""
    next_events = projector.feed(TurnFinishedEvent(stop_reason=StopReason.COMPLETED))
    assert next_events[0].turn_id == ""


def test_projector_merges_tool_call_arguments_into_result_card() -> None:
    from modex_agent.core.turn_events import (
        TurnToolCallEvent,
        TurnToolResultEvent,
    )

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


def test_streaming_inputs_project_deltas() -> None:
    from modex_agent.core.turn_events import (
        ToolArgsDeltaEvent,
        TurnReasoningEvent,
    )

    cases: list[tuple[TurnEvent, str]] = [
        (TurnTextEvent(text="x"), "text_delta"),
        (TurnReasoningEvent(text="x"), "thinking_delta"),
        (
            ToolArgsDeltaEvent(call_id="c", tool_name="t", args_fragment='{"'),
            "tool_args_delta",
        ),
    ]
    for runtime_event, expected_kind in cases:
        projector = DefaultTurnEventProjector(session_id="conv.main")
        events = projector.feed(runtime_event)
        deltas = [e for e in events if type(e).__name__ != "TurnStarted"]
        assert len(deltas) == 1
        assert deltas[0].kind == expected_kind
