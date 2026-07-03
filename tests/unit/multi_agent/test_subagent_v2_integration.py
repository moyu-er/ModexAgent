"""End-to-end integration test for subagent v2 lifecycle.

Validates the complete lifecycle: trace collection + hook notification + crash path.

Three test cases:
1. Happy path: trace recorded + parent receives XML notification
2. Crash path: error result -> parent receives error XML
3. Trace error: TURN_END record has FAILED status and error text
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxServer
from modex_agent.runtime.enums import AgentKind, OperationKind, OperationStatus, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.core.session_id import SessionInfo
from modex_agent.trace import JsonFileTraceStore, TraceCollectorHook


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SESSION_ID = "a1b2.worker"


def _make_context(
    session_id: str = SESSION_ID,
    agent_name: str = "worker",
    parent_session_id: str = "conv123.main",
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
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
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


def _extract_xml_field(xml: str, tag: str) -> str:
    pattern = rf"<{tag}>(.*?)</{tag}>"
    m = re.search(pattern, xml, re.DOTALL)
    return m.group(1).strip() if m else ""


def _mock_output_exists(runtime_dir: Path, session_id: str):
    """Patch Path.exists so that the OUTPUT.md for *session_id* appears to exist.

    Colons in session_id make it illegal as a Windows path component, so we
    cannot create the real file and must mock instead.
    """
    expected = runtime_dir / "output" / session_id / "OUTPUT.md"

    def _exists(self):
        if self == expected:
            return True
        return Path.__exists__(self) if hasattr(Path, "__exists__") else False

    return patch.object(Path, "exists", _exists)


# ---------------------------------------------------------------------------
# Test 1: Full lifecycle — happy path
# ---------------------------------------------------------------------------


class TestFullLifecycleNotification:
    """Happy path: subagent finishes with result + OUTPUT.md -> trace + XML."""

    async def test_full_lifecycle(self, tmp_path: Path) -> None:
        """Use path-safe session for trace store (Windows colon-in-path issue)."""
        session_id = "conv123-worker-a1b2"
        runtime_dir = tmp_path / "runtime"

        # --- infrastructure ---
        bus = _make_bus(tmp_path)
        store = JsonFileTraceStore(base_dir=runtime_dir / "trace")
        trace_hook = TraceCollectorHook(store=store)
        auto_hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )

        ctx = _make_context(session_id=session_id)
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        # Step 1: trace_hook.before_turn -> generates trace_id, writes TURN_START
        await trace_hook.before_turn(ctx)

        # Step 2: auto_hook.finally_turn -> sends XML notification
        with _mock_output_exists(runtime_dir, session_id):
            await auto_hook.finally_turn(ctx, result)

        # --- Verify trace ---
        records = await store.list_by_session(session_id)
        assert len(records) >= 1

        turn_start = next(
            (r for r in records if r.kind == OperationKind.TURN_START), None,
        )
        assert turn_start is not None
        assert turn_start.session_id == session_id
        assert turn_start.agent_name == "worker"
        assert turn_start.status == OperationStatus.COMPLETED

        # --- Verify bus ---
        # The notification is sent to the parent's inbox via parent_session_id.
        parent_inbox = "conv123.main"
        envelopes = await bus.consume(parent_inbox)
        assert len(envelopes) == 1

        content = envelopes[0].payload["content"]
        assert "<subagent_notification>" in content
        assert "<agent>worker</agent>" in content
        assert "<output_status>written</output_status>" in content


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
        # No OUTPUT.md on disk; error result simulates crash
        result = AgentResult(error="something broke", stop_reason=StopReason.ERROR)

        await auto_hook.finally_turn(ctx, result)

        envelopes = await bus.consume("conv123.main", limit=10)
        assert len(envelopes) == 1

        content = envelopes[0].payload["content"]
        assert "<subagent_notification>" in content
        assert _extract_xml_field(content, "is_normal") == "false"
        assert "crashed" in _extract_xml_field(content, "hint").lower()
        assert _extract_xml_field(content, "error") == "something broke"


# ---------------------------------------------------------------------------
# Test 3: Trace collector records error TURN_END
# ---------------------------------------------------------------------------


class TestTraceCollectorRecordsErrorTurnEnd:
    """Trace: error TURN_END recorded correctly."""

    async def test_trace_collector_records_error_turn_end(self, tmp_path: Path) -> None:
        # Use a path-safe session ID for the trace store to avoid Windows
        # issues with colons in directory names.
        session_id = "conv123-worker-a1b2"
        store = JsonFileTraceStore(base_dir=tmp_path / "trace")
        trace_hook = TraceCollectorHook(store=store)

        ctx = _make_context(session_id=session_id)

        # Step 1: before_turn -> TURN_START
        await trace_hook.before_turn(ctx)

        # Step 2: finally_turn with error -> TURN_END(FAILED)
        await trace_hook.finally_turn(
            ctx, AgentResult(error="timeout", stop_reason=StopReason.ERROR),
        )

        records = await store.list_by_session(session_id)
        assert len(records) == 2

        turn_end = next(
            (r for r in records if r.kind == OperationKind.TURN_END), None,
        )
        assert turn_end is not None
        assert turn_end.status == OperationStatus.FAILED
        assert turn_end.error == "timeout"
