from __future__ import annotations

import hashlib
from pathlib import Path

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import Tool
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.trace.agent_start_hook import AgentStartSpanHook
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.prompt_capture import FullPromptCapture
from modex_agent.trace.semconv import GenAiAttr, LangfuseObservationType, SpanKind, SpanName
from modex_agent.trace.session_state import TraceSessionState


class _SearchTool(Tool):
    async def execute(self, **kwargs: object) -> str:
        return "found"


def _make_context(*, with_trace: bool = True, with_tool: bool = False) -> AgentContext:
    session = SessionInfo(session_id="session.worker", agent_name="worker")
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="worker", session=session, turn_id="turn-1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    if with_trace:
        state.custom[TurnCustomKey.TRACE_ID] = "trace-1"
        state.custom[TurnCustomKey.ROOT_SPAN_ID] = "root-1"
    tool_manager = InMemoryToolManager()
    if with_tool:
        tool_manager.register(
            _SearchTool(
                name="search",
                description="Search records",
                parameters={"type": "object", "properties": {}},
            )
        )
    return AgentContext(
        system_prompt="Follow the project rules.",
        history=ListMessageHistory(),
        tool_manager=tool_manager,
        session=session,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )


def _make_hook(
    tmp_path: Path,
    *,
    capture_tools: bool = False,
) -> tuple[AgentStartSpanHook, OtelSpanTraceStore]:
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    hook = AgentStartSpanHook(
        session=TraceSessionState(),
        store=store,
        model="test-model",
        provider_name="test-provider",
        request_params=None,
        score_injector=None,
        prompt_capture=FullPromptCapture(),
        capture_tools=capture_tools,
    )
    return hook, store


async def test_agent_start_span_has_system_prompt(tmp_path: Path) -> None:
    hook, store = _make_hook(tmp_path)
    context = _make_context()

    await hook.start_node_turn(context)

    spans = await store.list_by_session("session.worker")
    assert len(spans) == 1
    span = spans[0]
    assert span.name == SpanName.AGENT_START.value
    assert span.kind == SpanKind.INTERNAL.value
    assert span.parent_span_id == "root-1"
    assert span.attributes[GenAiAttr.SYSTEM_INSTRUCTIONS] == context.system_prompt
    assert (
        span.attributes[GenAiAttr.SYSTEM_PROMPT_HASH]
        == hashlib.sha256(context.system_prompt.encode("utf-8")).hexdigest()[:16]
    )
    assert span.attributes[GenAiAttr.SYSTEM_PROMPT_LENGTH] == len(context.system_prompt)
    assert span.attributes[GenAiAttr.AGENT_NAME] == "worker"
    assert (
        span.attributes[GenAiAttr.LANGFUSE_OBSERVATION_TYPE] == LangfuseObservationType.SPAN.value
    )


async def test_agent_start_span_has_tool_definitions(tmp_path: Path) -> None:
    hook, store = _make_hook(tmp_path, capture_tools=True)
    context = _make_context(with_tool=True)

    await hook.start_node_turn(context)

    spans = await store.list_by_session("session.worker")
    definitions = spans[0].attributes[GenAiAttr.REQUEST_TOOLS]
    assert definitions == context.get_tool_descriptions()


async def test_agent_start_span_not_emitted_when_no_trace_id(tmp_path: Path) -> None:
    hook, store = _make_hook(tmp_path)
    context = _make_context(with_trace=False)

    await hook.start_node_turn(context)

    assert await store.list_by_session("session.worker") == []
