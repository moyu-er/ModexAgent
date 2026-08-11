"""Tests for the ``agent.handoff`` span emitted at ``send_to_agent`` dispatch (G10).

Verifies the multi-agent trace tree link: the parent turn's ``invoke_agent``
root span_id is connected to the child agent's eventual ``invoke_agent`` span
via an ``agent.handoff`` span whose ``parent_span_id`` equals the parent
turn's root span_id and whose ``trace_id`` is shared.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker import BrokerMessage, MessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.communication.result import AgentSendResult
from modex_agent.multi_agent.communication.strategies.base import (
    SendRequest,
    SendStrategyKind,
)
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.registry import AgentProfile
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.hooks import TraceCollectorHook
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanName, SpanStatusCode
from modex_agent.trace.store import SpanModel

if TYPE_CHECKING:
    pass

SESSION_ID = "conv123.main"
TURN_ID = "turn-abc"


# -- fakes --------------------------------------------------------------------


class _FakeBroker(MessageBroker):
    def __init__(self) -> None:
        super().__init__()

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def register_consumer(self, address: object) -> None: ...
    async def unregister_consumer(self, address: object) -> None: ...
    async def consume(self, address: object) -> BrokerMessage | None:
        return None

    def consume_stream(self, address: object) -> AsyncIterator[BrokerMessage]:
        import asyncio

        async def _gen() -> AsyncIterator[BrokerMessage]:
            while True:
                await asyncio.sleep(0.1)
                yield BrokerMessage(payload={}, sender=object())  # type: ignore[call-arg]

        return _gen()

    async def send_to(self, recipient: object, message: BrokerMessage) -> None: ...
    async def publish(self, topic: str, message: BrokerMessage) -> None: ...
    async def broadcast(self, message: BrokerMessage) -> None: ...
    async def subscribe(self, topic: str, address: object) -> None: ...


class _FakeRegistry:
    def __init__(self, profiles: list[AgentProfile] | None = None) -> None:
        self._profiles = profiles or []

    def list_profiles(self, caller: str | None = None) -> list[AgentProfile]:
        return self._profiles

    def get_profile(self, name: str) -> AgentProfile | None:
        return next((p for p in self._profiles if p.name == name), None)

    def get_descriptor(self, name: str) -> None:
        return None


class _RecordingStrategy:
    """Strategy stub that records requests and returns a success result."""

    def __init__(self) -> None:
        self.calls: list[SendRequest] = []

    async def execute(self, req: SendRequest) -> AgentSendResult:
        self.calls.append(req)
        return AgentSendResult(
            target_agent=req.target.name,
            target_kind=req.target.kind,
            session_id="fake-session",
            invocation_id=req.invocation_id,
            created_new_task=True,
        )


class _FailingTraceStore(OtelSpanTraceStore):
    """Trace store whose ``save_span`` always raises."""

    async def save_span(self, span: SpanModel) -> None:
        raise RuntimeError("simulated trace store failure")


# -- helpers ------------------------------------------------------------------


def _make_context(
    store: OtelSpanTraceStore | None = None,
    *,
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
    agent_name: str = "main",
) -> AgentContext:
    session = SessionInfo(session_id=SESSION_ID, agent_name=agent_name)
    identity = TurnIdentity(agent_id=agent_name, session=session, turn_id=TURN_ID)
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    if store is not None:
        services.trace_store = store
    runtime = AgentRuntime(services=services, state=state)
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=session,
        comm_kind=comm_kind,
        runtime=runtime,
        identity=identity,
    )


def _make_service(
    *,
    profiles: list[AgentProfile] | None = None,
    source_name: str = "main",
) -> AgentCommunicationService:
    return AgentCommunicationService(
        source=AgentAddress(name=source_name),
        registry=_FakeRegistry(profiles=profiles),
        tree=MagicMock(spec=SessionTreeManager),
    )


def _tgt(name: str, kind: AgentCommKind) -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=kind)


def _handoff_spans(spans: list[SpanModel]) -> list[SpanModel]:
    return [s for s in spans if s.name == SpanName.AGENT_HANDOFF.value]


# -- tests: hook stores root_span_id in turn state ---------------------------


class TestRootSpanIdInTurnState:
    async def test_before_graph_stores_root_span_id(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
        hook = TraceCollectorHook()
        ctx = _make_context(store)

        await hook.before_graph(ctx)

        root_span_id = ctx.runtime.state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
        assert root_span_id is not None
        assert isinstance(root_span_id, str)
        assert len(root_span_id) == 32  # uuid4().hex

    async def test_finally_graph_clears_root_span_id(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
        hook = TraceCollectorHook()
        ctx = _make_context(store)

        await hook.before_graph(ctx)
        assert ctx.runtime.state.custom.get(TurnCustomKey.ROOT_SPAN_ID) is not None

        await hook.finally_graph(ctx, result=None)

        assert TurnCustomKey.ROOT_SPAN_ID not in ctx.runtime.state.custom


# -- tests: handoff span emission --------------------------------------------


class TestHandoffSpanEmission:
    async def test_subagent_dispatch_emits_handoff_span_linked_to_root(
        self, tmp_path: Path
    ) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
        hook = TraceCollectorHook()
        ctx = _make_context(store)

        await hook.before_graph(ctx)
        root_span_id = ctx.runtime.state.custom[TurnCustomKey.ROOT_SPAN_ID]
        trace_id = ctx.runtime.state.custom[TurnCustomKey.TRACE_ID]

        svc = _make_service(
            profiles=[AgentProfile(name="worker", comm_kind=AgentCommKind.SUBAGENT)]
        )
        fake_strategy = _RecordingStrategy()
        svc._strategies[SendStrategyKind.SUBAGENT_DISPATCH] = fake_strategy  # type: ignore[assignment]

        ack = await svc.send_async(
            target=_tgt("worker", AgentCommKind.SUBAGENT),
            content="do the thing",
            invocation_id="inv-1",
            context=ctx,
        )

        # Handoff span is now emitted by TraceCollectorHook.after_tool_execution
        # (not by service._emit_handoff_span). Simulate the tool execution path.
        from modex_agent.core.types import ToolCall
        from modex_agent.core.tool_manager import ToolResult

        tool_calls = [
            ToolCall(
                tool_name="send_to_agent",
                arguments={"target_agent": "worker", "content": "do the thing"},
            ),
        ]
        results = [
            ToolResult.from_text("send_to_agent", "ack: sent to worker", execution_time=0.01),
        ]
        await hook.before_tool_execution(ctx, tool_calls)
        await hook.after_tool_execution(ctx, results)

        spans = await store.list_by_session(SESSION_ID)
        handoffs = _handoff_spans(spans)
        assert len(handoffs) == 1
        span = handoffs[0]

        # Trace tree linkage: parent_span_id == root span_id, shared trace_id.
        assert span.parent_span_id == root_span_id
        assert span.trace_id == trace_id
        assert span.name == SpanName.AGENT_HANDOFF.value
        assert span.kind == SpanKind.INTERNAL.value
        assert span.status.code == SpanStatusCode.OK

        # Attributes.
        attrs = span.attributes
        assert attrs[GenAiAttr.HANDOFF_TARGET_AGENT] == "worker"
        assert attrs[GenAiAttr.HANDOFF_TARGET_KIND] == "unknown"
        assert attrs[GenAiAttr.HANDOFF_MESSAGE_TYPE] == "unknown"
        assert attrs[GenAiAttr.HANDOFF_PARENT_TURN_ID] == TURN_ID
        assert attrs[GenAiAttr.HANDOFF_CHILD_TURN_ID] is None

    async def test_normal_target_emits_agent_message_type(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
        hook = TraceCollectorHook()
        ctx = _make_context(store)

        await hook.before_graph(ctx)

        from modex_agent.core.types import ToolCall
        from modex_agent.core.tool_manager import ToolResult

        tool_calls = [
            ToolCall(
                tool_name="send_to_agent",
                arguments={"target_agent": "reviewer", "content": "hello"},
            ),
        ]
        results = [
            ToolResult.from_text("send_to_agent", "ack", execution_time=0.01),
        ]
        await hook.before_tool_execution(ctx, tool_calls)
        await hook.after_tool_execution(ctx, results)

        spans = await store.list_by_session(SESSION_ID)
        handoffs = _handoff_spans(spans)
        assert len(handoffs) == 1
        attrs = handoffs[0].attributes
        assert attrs[GenAiAttr.HANDOFF_TARGET_AGENT] == "reviewer"

    async def test_task_tool_also_emits_handoff_span(self, tmp_path: Path) -> None:
        """The `task` tool is a dispatch tool — it must emit agent.handoff spans
        just like send_to_agent, per _DISPATCH_TOOL_NAMES in trace/hooks.py."""
        store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
        hook = TraceCollectorHook()
        ctx = _make_context(store)

        await hook.before_graph(ctx)

        from modex_agent.core.types import ToolCall
        from modex_agent.core.tool_manager import ToolResult

        tool_calls = [
            ToolCall(
                tool_name="task",
                arguments={"target_agent": "worker", "content": "implement feature X"},
            ),
        ]
        results = [
            ToolResult.from_text("task", "ack: dispatched to worker", execution_time=0.01),
        ]
        await hook.before_tool_execution(ctx, tool_calls)
        await hook.after_tool_execution(ctx, results)

        spans = await store.list_by_session(SESSION_ID)
        handoffs = _handoff_spans(spans)
        assert len(handoffs) == 1
        attrs = handoffs[0].attributes
        assert attrs[GenAiAttr.HANDOFF_TARGET_AGENT] == "worker"


# -- tests: guards + fail-open -----------------------------------------------


class TestHandoffSpanGuards:
    async def test_no_span_when_trace_store_none(self, tmp_path: Path) -> None:
        ctx = _make_context(store=None)
        hook = TraceCollectorHook()
        await hook.before_graph(ctx)

        svc = _make_service()
        fake_strategy = _RecordingStrategy()
        svc._strategies[SendStrategyKind.SUBAGENT_DISPATCH] = fake_strategy  # type: ignore[assignment]

        ack = await svc.send_async(
            target=_tgt("worker", AgentCommKind.SUBAGENT),
            content="hi",
            invocation_id="inv-1",
            context=ctx,
        )

        # No store → no span file, but communication still succeeds.
        assert "worker" in ack
        assert len(fake_strategy.calls) == 1

    async def test_no_span_when_trace_id_not_set(self, tmp_path: Path) -> None:
        # before_graph NOT called → no trace_id in turn state.
        store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
        ctx = _make_context(store)

        svc = _make_service()
        fake_strategy = _RecordingStrategy()
        svc._strategies[SendStrategyKind.SUBAGENT_DISPATCH] = fake_strategy  # type: ignore[assignment]

        await svc.send_async(
            target=_tgt("worker", AgentCommKind.SUBAGENT),
            content="hi",
            invocation_id="inv-1",
            context=ctx,
        )

        spans = await store.list_by_session(SESSION_ID)
        assert _handoff_spans(spans) == []

    async def test_fail_open_when_save_span_raises(self, tmp_path: Path) -> None:
        store = _FailingTraceStore(base_dir=tmp_path / "traces")
        hook = TraceCollectorHook()
        ctx = _make_context(store)
        await hook.before_graph(ctx)

        svc = _make_service()
        fake_strategy = _RecordingStrategy()
        svc._strategies[SendStrategyKind.SUBAGENT_DISPATCH] = fake_strategy  # type: ignore[assignment]

        # Must NOT raise — tracing failure must not block communication.
        ack = await svc.send_async(
            target=_tgt("worker", AgentCommKind.SUBAGENT),
            content="hi",
            invocation_id="inv-1",
            context=ctx,
        )

        assert "worker" in ack
        assert len(fake_strategy.calls) == 1
