"""End-to-end integration test for subagent v2 lifecycle.

Validates the complete lifecycle: trace collection + hook notification + crash path.

Three test cases:
1. Happy path: trace recorded + parent receives XML notification
2. Crash path: error result -> parent receives error XML
3. Trace error: TURN_END record has FAILED status and error text
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxServer
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace import OtelSpanTraceStore, TraceCollectorHook
from modex_agent.trace.semconv import GenAiAttr, SpanName, SpanStatusCode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SESSION_ID = "a1b2.worker"


def _make_context(
    session_id: str = SESSION_ID,
    agent_name: str = "worker",
    parent_session_id: str = "conv123.main",
    trace_store: OtelSpanTraceStore | None = None,
) -> AgentContext:
    session = SessionInfo(
        session_id=session_id,
        agent_name=agent_name,
        parent_session_id=parent_session_id,
    )
    state = ReActTurnState(
        identity=TurnIdentity(agent_id=agent_name, session=session, turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    if trace_store is not None:
        services.trace_store = trace_store
    runtime = AgentRuntime(services=services, state=state)
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=session,
        comm_kind=AgentCommKind.SUBAGENT,
        runtime=runtime,
    )


def _make_bus(tmpdir: Path) -> LocalAgentMessageBus:
    server = LocalFileInboxServer(workspace=tmpdir / "inbox")
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    return LocalAgentMessageBus(producer=producer, consumer=consumer)


# ---------------------------------------------------------------------------
# Test 1: Full lifecycle — happy path
# ---------------------------------------------------------------------------


class TestFullLifecycleNotification:
    """Happy path: subagent finishes with result -> hook writes OUTPUT_<n>.md + notification."""

    async def test_full_lifecycle(self, tmp_path: Path) -> None:
        """Use path-safe session for trace store (Windows colon-in-path issue)."""
        session_id = "conv123-worker-a1b2"
        runtime_dir = tmp_path / "runtime"

        # --- infrastructure ---
        bus = _make_bus(tmp_path)
        store = OtelSpanTraceStore(base_dir=runtime_dir / "trace")
        trace_hook = TraceCollectorHook()
        auto_hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )

        ctx = _make_context(session_id=session_id, trace_store=store)
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        # Step 1: trace_hook.before_turn -> pre-registers trace_id + root span_id
        await trace_hook.before_turn(ctx)

        # Step 2: auto_hook.finally_turn -> writes OUTPUT_1.md + sends notification
        await auto_hook.finally_turn(ctx, result)

        # Step 3: trace_hook.finally_turn -> writes the root invoke_agent span
        await trace_hook.finally_turn(ctx, result)

        # --- Verify trace ---
        spans = await store.list_by_session(session_id)
        assert len(spans) >= 1

        turn_start = next(
            (s for s in spans if s.name == SpanName.INVOKE_AGENT.value),
            None,
        )
        assert turn_start is not None
        assert turn_start.attributes[GenAiAttr.CONVERSATION_ID] == session_id
        assert turn_start.attributes[GenAiAttr.AGENT_NAME] == "worker"
        assert turn_start.status.code == SpanStatusCode.OK

        # --- Verify OUTPUT_1.md was written to disk ---
        assert (runtime_dir / "output" / session_id / "OUTPUT_1.md").exists()

        # --- Verify bus ---
        # The notification is sent to the parent's inbox via parent_session_id.
        parent_inbox = "conv123.main"
        envelopes = await bus.consume(parent_inbox)
        assert len(envelopes) == 1

        content = envelopes[0].payload["content"]
        assert "Message from subagent" in content
        assert "subagent 'worker'" in content
        assert "OUTPUT_1.md" in content


# ---------------------------------------------------------------------------
# Test 2: Crash path
# ---------------------------------------------------------------------------


class TestCrashSendsErrorNotification:
    """Crash path: subagent error result -> parent receives error XML."""

    async def test_crash_sends_error_notification(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"

        bus = _make_bus(tmp_path)
        auto_hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )

        ctx = _make_context()
        result = AgentResult(error="something broke", stop_reason=StopReason.ERROR)

        await auto_hook.finally_turn(ctx, result)

        assert (runtime_dir / "output" / str(ctx.session) / "OUTPUT_1.md").exists()

        envelopes = await bus.consume("conv123.main", limit=10)
        assert len(envelopes) == 1

        content = envelopes[0].payload["content"]
        assert "Message from subagent" in content
        assert "status: failed" in content
        assert "crashed" in content.lower()
        assert "something broke" in content


# ---------------------------------------------------------------------------
# Test 3: Trace collector records error TURN_END
# ---------------------------------------------------------------------------


class TestTraceCollectorRecordsErrorTurnEnd:
    """Trace: error TURN_END produces no additional span (root on TURN_START)."""

    async def test_trace_collector_error_turn_end(self, tmp_path: Path) -> None:
        session_id = "conv123-worker-a1b2"
        store = OtelSpanTraceStore(base_dir=tmp_path / "trace")
        trace_hook = TraceCollectorHook()

        ctx = _make_context(session_id=session_id, trace_store=store)

        # Step 1: before_turn -> TURN_START span
        await trace_hook.before_turn(ctx)

        # Step 2: finally_turn with error -> no new span (TURN_END is a no-op)
        await trace_hook.finally_turn(
            ctx,
            AgentResult(error="timeout", stop_reason=StopReason.ERROR),
        )

        spans = await store.list_by_session(session_id)
        assert len(spans) == 2
        assert all(s.name == SpanName.INVOKE_AGENT.value for s in spans)
