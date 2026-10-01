"""T-P2 — second-consumer proof: a plain-text renderer over framework types.

``PlainTextTurnRenderer`` (below, ~45 lines) consumes ONLY
``modex_agent.presentation`` types — no bot code, no ReAct types, no
emitter subclassing. Driving a scripted ReAct turn through the recording
emitter + ``DefaultTurnEventProjector`` and rendering the resulting
presentation events end-to-end is the mechanical proof that a second UI
needs nothing from the example layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.core.agent import AgentContext
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import FinishReason
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ToolCallComplete,
)
from modex_agent.core.stream_events import (
    TextDelta as StreamTextDelta,
)
from modex_agent.core.tool_manager import Tool
from modex_agent.core.turn_events import TurnTextEvent
from modex_agent.memory.history import ListMessageHistory
from modex_agent.presentation import (
    DefaultTurnEventProjector,
    PresentationEvent,
    TextDelta,
    ThinkingDelta,
    ToolArgsDelta,
    ToolCallStarted,
    ToolResult,
    TurnEndedSignal,
    TurnErrored,
    TurnFailedSignal,
    TurnFinished,
)
from modex_agent.tools.manager import InMemoryToolManager
from tests.unit.presentation.test_default_projector_coverage import (
    RecordingEmitter,
    _translate,
)

# ── The renderer: framework presentation types ONLY ────────────────────────


class PlainTextTurnRenderer:
    """Render presentation events as plain text (a console UI)."""

    def render_event(self, event: PresentationEvent) -> str:
        match event:
            case TextDelta(text=text):
                return text
            case ThinkingDelta(text=text):
                return f"[thinking] {text}"
            case ToolArgsDelta():
                return ""  # transient warm-up noise
            case ToolCallStarted(tool_name=name, arguments=args):
                return f"\n> {name} {args}\n"
            case ToolResult(output=out, error=err):
                return f"< {'ERROR: ' + err if err else out}\n"
            case TurnErrored(message=msg):
                return f"\n!! error: {msg}\n"
            case TurnFinished(stop_reason=reason):
                return "" if reason.value == "completed" else f"\n-- {reason.value} --\n"
            case _:
                return ""

    def render_stream(self, events: list[PresentationEvent]) -> str:
        return "".join(self.render_event(e) for e in events)


# ── The scripted turn (same harness shape as T-P1) ─────────────────────────


class _StaticProvider(LLMProvider):
    def __init__(self, rounds: list[list[LLMStreamEvent]]) -> None:
        super().__init__()
        self._rounds = list(rounds)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        for event in self._rounds.pop(0):
            yield event

    def get_default_model(self) -> str:
        return "scripted"


class _EchoTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="echo",
            description="echo back",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs: Any) -> str:
        return "echo: hi"


async def _scripted_presentation_events() -> list[PresentationEvent]:
    provider = _StaticProvider(
        [
            [
                StreamTextDelta(text="Checking..."),
                ToolCallComplete(call_id="c1", tool_name="echo", arguments={}),
                Finish(finish_reason=FinishReason.TOOL_CALLS),
            ],
            [StreamTextDelta(text=" done"), Finish(finish_reason=FinishReason.STOP)],
        ]
    )
    emitter = RecordingEmitter()
    tool_manager = InMemoryToolManager()
    tool_manager.register(_EchoTool())
    ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=tool_manager,
        session=SessionInfo.from_str("conv.main"),
    )
    await ReActAgent(provider).run(ctx, emitter)

    projector = DefaultTurnEventProjector(session_id="conv.main")
    rendered: list[PresentationEvent] = []
    for call in emitter.calls:
        runtime_event = _translate(call)
        if runtime_event is not None:
            rendered.extend(projector.feed(runtime_event))
    return rendered


async def test_plain_renderer_renders_scripted_turn_end_to_end() -> None:
    events = await _scripted_presentation_events()
    assert events, "scripted turn produced presentation events"

    out = PlainTextTurnRenderer().render_stream(events)

    # Streaming text arrived in order around the tool card.
    assert "Checking..." in out
    assert " done" in out
    assert out.index("Checking...") < out.index("> echo") < out.index(" done")
    # The tool card renders the executed result.
    assert "< echo: hi" in out
    # The completed turn adds no terminal noise.
    assert "-- error --" not in out


async def test_plain_renderer_renders_error_and_stop_surfaces() -> None:
    from modex_agent.core.emitter import StopReason

    projector = DefaultTurnEventProjector(session_id="conv.main")
    events = [
        *projector.feed(TurnTextEvent(text="partial")),
        *projector.feed(TurnFailedSignal(message="boom")),
        *projector.feed(TurnEndedSignal(stop_reason=StopReason.ERROR, error="boom")),
    ]
    out = PlainTextTurnRenderer().render_stream(events)
    assert "!! error: boom" in out
    assert "-- error --" in out
