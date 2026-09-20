"""Bot-side ACP output-surface tests (T07 output portion, acp-adapter DESIGN §7).

Covers the output half of the ACP adapter on the REAL seams:

- ``AcpEmitterHub`` — one registration per framework session carrying BOTH the
  ``TurnEvent`` listener and the optional approval handler; approval with no
  handler raises (owner cancels — never a hanging drop); child events route
  through the injected parent-chain resolver with source-namespaced tool ids
  and annotated text; sessions never cross-stream.
- ``AcpOutputAdapter`` — routes the REAL ``ApprovalRequestView`` off the
  ``IMUserInterface`` → ``OutputAdapter`` seam (no buffer, no fake request)
  and routes notice/error/command output as text events (nothing dropped).
- ``AcpTurnEmitter`` — subclasses ``WebBotEmitter`` so the segment/flush
  lifecycle and the transcript writer are the CANONICAL ones (single write);
  projects the same facts into full-fidelity ``TurnEvent``s for the editor.

Turn-level tests drive a real ``ReActTurnRunner`` pipeline with the scripted
LLM stub pattern from ``test_acp_driver.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from bot.acp.emitter import (
    AcpApprovalRouteError,
    AcpEmitterHub,
    AcpOutputAdapter,
    AcpTurnEmitter,
)
from bot.webui.events import AssistantTextEvent, ToolCallEvent, ToolResultEvent
from bot.webui.transcript_store import TranscriptStore

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.approval.ui import IMUserInterface
from modex_agent.approval.views import ApprovalRequestView
from modex_agent.core.llm_struct import LLMResponse, RuntimeSafetyPolicy
from modex_agent.core.message import ToolCall
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.tool_manager import Tool
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.factories.approval import build_approval_runtime
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.messaging.models import InputMessage, OutputMessage, OutputMessageType
from modex_agent.pipeline.approval_renderer import ApprovalRenderer, approval_output_message
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.utils.sanitizer import ContentSanitizer

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _ScriptedProvider(CallbackStreamProvider):
    """Scripted LLM: one canned LLMResponse per call (last one repeats)."""

    def __init__(self, script: list[LLMResponse]) -> None:
        super().__init__()
        self._script = list(script)
        self.calls = 0

    def get_default_model(self) -> str:
        return "scripted-test-model"

    async def chat_stream(
        self,
        messages: list[Any],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Any = None,
        on_reasoning_delta: Any = None,
        **kwargs: Any,
    ) -> LLMResponse:
        resp = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return resp


class _RecordingTool(Tool):
    def __init__(self, name: str, recorded: list[str]) -> None:
        super().__init__(
            name=name,
            description=f"{name} (recording stub)",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
        self._recorded = recorded

    async def execute(self, **kwargs: Any) -> str:
        path = str(kwargs.get("path", ""))
        self._recorded.append(path)
        return f"{self.name} {path}"


class _RecordingTranscriptStore(TranscriptStore):
    """In-memory TranscriptStore capturing appends per session."""

    def __init__(self) -> None:
        self.events: dict[str, list[Any]] = {}

    async def append(
        self, session_id: str, event: Any, *, pool: str = "main"
    ) -> None:
        self.events.setdefault(session_id, []).append(event)

    async def load(self, session_id: str) -> list[Any]:
        return list(self.events.get(session_id, []))

    async def load_sessions_by_prefix(
        self, session_prefix: str, *, pool: str | None = None
    ) -> list[Any]:
        merged: list[Any] = []
        for sid, events in self.events.items():
            if sid.startswith(session_prefix):
                merged.extend(events)
        return merged

    async def list_sessions(self) -> set[str]:
        return set(self.events)

    async def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        return {sid for sid in self.events if sid.startswith(session_prefix)}

    async def delete_session(self, session_id: str) -> None:
        self.events.pop(session_id, None)

    async def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        for sid in [s for s in self.events if s.startswith(session_prefix)]:
            self.events.pop(sid, None)


class _Collector:
    """TurnEvent listener recording every event it receives."""

    def __init__(self) -> None:
        self.events: list[TurnEvent] = []

    async def __call__(self, event: TurnEvent) -> None:
        self.events.append(event)


class _ApprovalRecorder:
    """ApprovalRequestHandler recording every routed view."""

    def __init__(self) -> None:
        self.views: list[ApprovalRequestView] = []
        self.sources: list[str] = []

    async def __call__(self, source_sid: str, view: ApprovalRequestView) -> None:
        self.views.append(view)
        self.sources.append(source_sid)


# ---------------------------------------------------------------------------
# Hub + output adapter (routing seams)
# ---------------------------------------------------------------------------


async def test_approval_routes_real_view_to_registered_handler() -> None:
    hub = AcpEmitterHub()
    approvals = _ApprovalRecorder()
    hub.register("sess-1", _Collector(), on_approval=approvals)
    output = AcpOutputAdapter(hub)
    view = ApprovalRequestView(
        tool_call_id="call-7",
        tool_name="write",
        tier="dangerous",
        arguments={"path": "/etc/hosts"},
    )

    await output.send(approval_output_message(view), "sess-1")

    assert len(approvals.views) == 1
    routed = approvals.views[0]
    assert isinstance(routed, ApprovalRequestView)
    assert routed.tool_call_id == "call-7"
    assert routed.tool_name == "write"
    assert routed.tier == "dangerous"
    assert routed.arguments == {"path": "/etc/hosts"}


async def test_approval_without_handler_raises_clear_error() -> None:
    hub = AcpEmitterHub()
    output = AcpOutputAdapter(hub)
    view = ApprovalRequestView(
        tool_call_id="call-7", tool_name="write", tier="dangerous", arguments={}
    )

    with pytest.raises(AcpApprovalRouteError, match="sess-404"):
        await output.send(approval_output_message(view), "sess-404")


@pytest.mark.parametrize(
    "message_type",
    [
        OutputMessageType.NOTICE,
        OutputMessageType.BUSY_NOTICE,
        OutputMessageType.ERROR,
        OutputMessageType.COMMAND_RESPONSE,
        OutputMessageType.TEXT,
    ],
)
async def test_notices_route_as_text_events_and_are_not_dropped(
    message_type: OutputMessageType,
) -> None:
    hub = AcpEmitterHub()
    collector = _Collector()
    hub.register("sess-1", collector)
    output = AcpOutputAdapter(hub)

    await output.send(
        OutputMessage(content="pool switched", message_type=message_type), "sess-1"
    )

    assert len(collector.events) == 1
    event = collector.events[0]
    assert isinstance(event, TurnTextEvent)
    assert event.text == "pool switched"


async def test_events_reach_only_the_registered_session() -> None:
    hub = AcpEmitterHub()
    first, second = _Collector(), _Collector()
    hub.register("sess-1", first)
    hub.register("sess-2", second)

    await hub.emit("sess-1", TurnTextEvent(text="for one"))

    assert [e.text for e in first.events] == ["for one"]
    assert second.events == []


async def test_hub_emit_without_listener_is_quietly_dropped() -> None:
    hub = AcpEmitterHub()

    await hub.emit("nobody", TurnTextEvent(text="lost"))  # no raise


async def test_child_events_forward_via_resolver_with_source_namespacing() -> None:
    root_collector = _Collector()
    hub = AcpEmitterHub(
        resolver=async_lambda("inv9.sub", "inv1.coder")
    )
    hub.register("inv1.coder", root_collector)

    await hub.emit(
        "inv9.sub",
        TurnToolCallEvent(tool_name="read", call_id="c1", arguments={"path": "a"}),
    )
    await hub.emit("inv9.sub", TurnTextEvent(text="partial finding"))

    tool_events = [e for e in root_collector.events if isinstance(e, TurnToolCallEvent)]
    text_events = [e for e in root_collector.events if isinstance(e, TurnTextEvent)]
    # Tool call ids are namespaced by the SOURCE child session — they can never
    # collide with root-native call ids.
    assert tool_events[0].call_id == "inv9.sub:c1"
    assert tool_events[0].tool_name == "read"
    # Child text is annotated with the child agent so it is not read as
    # root-native content.
    assert text_events[0].text.startswith("[sub]")
    assert "partial finding" in text_events[0].text


async def test_unresolvable_child_events_are_dropped_not_misrouted() -> None:
    first, second = _Collector(), _Collector()
    hub = AcpEmitterHub(resolver=async_lambda("unknown.child", None))
    hub.register("sess-1", first)
    hub.register("sess-2", second)

    await hub.emit("unknown.child", TurnTextEvent(text="stray"))

    assert first.events == []
    assert second.events == []


async def test_direct_listener_wins_over_resolver() -> None:
    collector = _Collector()
    hub = AcpEmitterHub(resolver=async_lambda("sess-1", "other-place"))
    hub.register("sess-1", collector)

    await hub.emit("sess-1", TurnTextEvent(text="direct"))

    assert [e.text for e in collector.events] == ["direct"]


async def test_unregister_removes_both_sinks() -> None:
    hub = AcpEmitterHub()
    approvals = _ApprovalRecorder()
    hub.register("sess-1", _Collector(), on_approval=approvals)
    hub.unregister("sess-1")

    with pytest.raises(AcpApprovalRouteError):
        await hub.approval("sess-1", _view())


async def test_child_approval_routes_to_root_with_source_session() -> None:
    """A child-session approval rides the SAME resolver as child events.

    The routed handler must receive the view together with the approval's
    SOURCE session id — routing to the root editor must not erase which
    session owns the suspended turn. The view itself is never rewritten:
    the wire tool id matches the child's namespaced tool events.
    """
    approvals = _ApprovalRecorder()
    hub = AcpEmitterHub(resolver=async_lambda("inv9.sub", "inv1.coder"))
    hub.register("inv1.coder", _Collector(), on_approval=approvals)
    output = AcpOutputAdapter(hub)
    view = ApprovalRequestView(
        tool_call_id="c_child", tool_name="write", tier="dangerous",
        arguments={"path": "/etc/hosts"}, approval_id="appr-child",
    )

    await output.send(approval_output_message(view), "inv9.sub")

    assert len(approvals.views) == 1
    assert approvals.views[0] is not None
    assert approvals.views[0].tool_call_id == "c_child"
    assert approvals.views[0].approval_id == "appr-child"
    assert approvals.sources == ["inv9.sub"]


async def test_child_approval_without_resolver_sink_raises() -> None:
    """Unroutable child approval raises (owner cancels) — never a silent drop."""
    hub = AcpEmitterHub(resolver=async_lambda("inv9.sub", None))
    hub.register("inv1.coder", _Collector(), on_approval=_ApprovalRecorder())
    output = AcpOutputAdapter(hub)

    with pytest.raises(AcpApprovalRouteError, match="inv9.sub"):
        await output.send(approval_output_message(_view()), "inv9.sub")


def _view() -> ApprovalRequestView:
    return ApprovalRequestView(
        tool_call_id="c", tool_name="t", tier="safe", arguments={}
    )


def async_lambda(source: str, target: str | None):
    """Typed ChildSessionResolver stub: source -> fixed target."""

    async def resolve(session_id: str) -> str | None:
        assert session_id == source
        return target

    return resolve


# ---------------------------------------------------------------------------
# AcpTurnEmitter on the WebBotEmitter canonical path (real pipeline turns)
# ---------------------------------------------------------------------------


def _build_pipeline(
    *,
    provider: Any,
    hub: AcpEmitterHub,
    output: AcpOutputAdapter,
    tool_manager: InMemoryToolManager,
    transcript_store: TranscriptStore,
    tmp_path: Path,
    approval_gated: bool = False,
) -> AgentPipeline:
    """Real ReActTurnRunner pipeline with the ACP emitter/output seams wired."""
    turn_store = InMemoryTurnStateStore()
    agent = ReActAgent(provider)
    safety = RuntimeSafetyPolicy()
    registry = TurnSessionRegistry()
    ui = IMUserInterface(output_adapter=output)
    runtime_services = AgentRuntimeServices(
        approval=(
            build_approval_runtime(
                ApprovalConfig(
                    enabled=True,
                    tools={"write": ToolApprovalEntry(allowed_paths=["./*"])},
                ),
                project_root=tmp_path,
            )
            if approval_gated
            else None
        ),
        turn_store=turn_store,
    )
    builder = TurnContextBuilder(
        agent=agent,
        tool_manager=tool_manager,
        sanitizer=ContentSanitizer.sanitize,
        command_processor=None,
        skill_resolver=None,
        context_builder=None,
        agent_descriptor=None,
        max_iterations=5,
        safety=safety,
        runtime_services=runtime_services,
        runtime_context_manager=None,
        governance=None,
        hook_runner=None,
        interceptor_chain=None,
        control_channel=None,
        emitter_factory=lambda session_id: AcpTurnEmitter(
            hub, session_id, transcript_store=transcript_store, pool="main"
        ),
        output_adapter=output,
        turn_store=turn_store,
        registry=registry,
    )
    runner = ReActTurnRunner(
        agent=agent,
        context_manager=InMemoryContextManager(),
        context_manager_factory=None,
        on_session_start=None,
        on_session_end=None,
        safety=safety,
        turn_store=turn_store,
        registry=registry,
        builder=builder,
        resumer=ApprovalResumer(agent=agent, turn_store=turn_store, user_interface=ui),
        approval=ApprovalRenderer(agent=agent, user_interface=ui),
        workspace_manager=None,
        pool_name=None,
        pool_data_resolver=None,
        agent_descriptor=None,
    )
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=runner,
        input_adapter=_NullInputAdapter(),
        output_adapter=output,
        registry=registry,
        safety=safety,
    )
    runner.bind_to_pipeline(pipeline)
    return pipeline


class _NullInputAdapter:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def receive(self):
        if False:
            yield None


class _Harness:
    """Pipeline + hub + real transcript store sharing one ACP output surface."""

    def __init__(
        self,
        *,
        provider: Any,
        tmp_path: Path,
        tool_manager: InMemoryToolManager | None = None,
        approval_gated: bool = False,
    ) -> None:
        self.hub = AcpEmitterHub()
        self.transcripts = _RecordingTranscriptStore()
        self.output = AcpOutputAdapter(self.hub)
        self.pipeline = _build_pipeline(
            provider=provider,
            hub=self.hub,
            output=self.output,
            tool_manager=tool_manager or InMemoryToolManager(),
            transcript_store=self.transcripts,
            tmp_path=tmp_path,
            approval_gated=approval_gated,
        )
        self.session: SessionInfo = SessionIdFactory().create(agent_name="main")

    def collect(self) -> _Collector:
        collector = _Collector()
        self.hub.register(self.session.session_id, collector)
        return collector

    async def prompt(self, text: str) -> Any:
        return await self.pipeline.process_message(
            InputMessage(
                content=text,
                session=self.session,
                channel="acp",
                source="acp",
                metadata={"message_id": "turn-1"},
            )
        )


async def test_plain_turn_streams_text_once_and_persists_transcript(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider([LLMResponse(content="hello back")])
    harness = _Harness(provider=provider, tmp_path=tmp_path)
    collector = harness.collect()

    result = await harness.prompt("hello")

    assert result is not None and result.stop_reason.value == "completed"
    texts = [e.text for e in collector.events if isinstance(e, TurnTextEvent)]
    assert "".join(texts) == "hello back"
    # Canonical single-write transcript: one AssistantTextEvent, not deltas.
    persisted = await harness.transcripts.load(harness.session.session_id)
    assistant_texts = [e for e in persisted if isinstance(e, AssistantTextEvent)]
    assert len(assistant_texts) == 1
    assert assistant_texts[0].text == "hello back"


async def test_tool_turn_projects_full_fidelity_tool_events(tmp_path: Path) -> None:
    big_args = {"path": "a.txt", "blob": "x" * 600}
    big_result = "r" * 1000
    recorded: list[str] = []

    class _BigTool(_RecordingTool):
        async def execute(self, **kwargs: Any) -> str:
            self._recorded.append(str(kwargs.get("path", "")))
            return big_result

    tool_manager = InMemoryToolManager()
    tool_manager.register(_BigTool("read", recorded))
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="let me look",
                tool_calls=[ToolCall(tool_name="read", arguments=big_args, call_id="c1")],
            ),
            LLMResponse(content="done reading"),
        ]
    )
    harness = _Harness(provider=provider, tmp_path=tmp_path, tool_manager=tool_manager)
    collector = harness.collect()

    result = await harness.prompt("read a.txt")

    assert result is not None and result.stop_reason.value == "completed"
    calls = [e for e in collector.events if isinstance(e, TurnToolCallEvent)]
    results = [e for e in collector.events if isinstance(e, TurnToolResultEvent)]
    assert len(calls) == 1 and len(results) == 1
    assert calls[0].arguments == big_args  # full args, not the 500-char WS truncation
    assert results[0].output == big_result  # full output, not the 200-char summary
    assert calls[0].call_id == "c1" and results[0].call_id == "c1"

    # The same turn wrote the canonical tool pair to the transcript — one
    # writer, two sinks.
    persisted = await harness.transcripts.load(harness.session.session_id)
    tc = [e for e in persisted if isinstance(e, ToolCallEvent)]
    tr = [e for e in persisted if isinstance(e, ToolResultEvent)]
    assert len(tc) == 1 and len(tr) == 1
    assert tc[0].call_id == "c1" and tr[0].call_id == "c1"
    assert tr[0].result == big_result
    # Text precedes the tool call on the editor stream (flush-before-tool).
    first_text = next(i for i, e in enumerate(collector.events) if isinstance(e, TurnTextEvent))
    first_tool = next(i for i, e in enumerate(collector.events) if isinstance(e, TurnToolCallEvent))
    assert first_text < first_tool


async def test_approval_suspension_routes_view_while_turn_suspended(
    tmp_path: Path,
) -> None:
    """The REAL renderer view reaches the registered handler mid-suspension."""
    write_recorded: list[str] = []
    tool_manager = InMemoryToolManager()
    tool_manager.register(_RecordingTool("write", write_recorded))
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
                        arguments={"path": "/etc/hosts"},
                        call_id="c_w",
                    )
                ],
            ),
            LLMResponse(content="approved"),
        ]
    )
    harness = _Harness(
        provider=provider, tmp_path=tmp_path, tool_manager=tool_manager, approval_gated=True
    )
    approvals = _ApprovalRecorder()

    async def decide(view: ApprovalRequestView) -> None:
        approvals.views.append(view)
        # Feeding the decision back is the driver's job (owned elsewhere);
        # here we only assert the view actually arrived while suspended.
        turn_task.cancel()

    turn_task = asyncio.create_task(
        _prompt_with_handler(harness, "write it", decide)
    )
    with pytest.raises(asyncio.CancelledError):
        await turn_task

    assert len(approvals.views) == 1
    assert approvals.views[0].tool_call_id == "c_w"
    assert approvals.views[0].tool_name == "write"


async def _prompt_with_handler(harness: _Harness, text: str, on_view) -> Any:
    """Run a prompt with an approval handler that inspects the live view."""

    async def handle(source_sid: str, view: ApprovalRequestView) -> None:
        await on_view(view)

    harness.hub.register(
        harness.session.session_id, _Collector(), on_approval=handle
    )
    return await harness.prompt(text)
