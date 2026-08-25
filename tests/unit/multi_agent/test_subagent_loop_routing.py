"""Subagent loop_detected result must be routed to the parent inbox."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook.builtin import SubagentAutoSendHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxServer
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager


def _mock_tree(bus: object) -> SessionTreeManager:
    tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: object) -> None:
        await bus.send(sid, env)  # type: ignore[attr-defined]

    tree.deliver = _deliver
    return tree



def _make_bus(tmpdir: Path) -> LocalAgentMessageBus:
    server = LocalFileInboxServer(workspace=tmpdir / "inbox")
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    return LocalAgentMessageBus(producer=producer, consumer=consumer)


def _make_context(
    session_id: str,
    agent_name: str = "worker",
    parent_session_id: str = "conv123.main",
) -> AgentContext:
    session = SessionInfo(
        session_id=session_id,
        agent_name=agent_name,
        parent_session_id=parent_session_id,
    )
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=session,
        comm_kind=AgentCommKind.SUBAGENT,
    )


@pytest.mark.asyncio
async def test_loop_detected_sends_incomplete_notification(tmp_path: Path):
    """A subagent that stops with LOOP_DETECTED must notify the parent with
    status=incomplete and a loop-specific hint."""
    runtime_dir = tmp_path / "runtime"
    session_id = "a1b2c3d4.worker"

    bus = _make_bus(tmp_path)
    hook = SubagentAutoSendHook(
    tree=_mock_tree(bus),
        self_name="worker",
        parent_name="main",
        runtime_dir=runtime_dir,
    )
    ctx = _make_context(session_id)
    result = AgentResult(
        content="I am stuck doing the same thing.",
        stop_reason=StopReason.LOOP_DETECTED,
    )

    await hook.finally_graph(ctx, result)

    msgs = await bus.consume("conv123.main")
    assert len(msgs) == 1
    xml = msgs[0].payload["content"]
    assert "Message from subagent" in xml
    assert "status: failed" in xml
    assert "Issue:" in xml
    assert "loop" in xml.lower()
    assert "stuck" in xml.lower()


@pytest.mark.asyncio
async def test_loop_detected_includes_invocation_id_in_guidance(tmp_path: Path):
    """The loop-detected notification must include the invocation_id in the
    continuation guidance so the parent can resume."""
    runtime_dir = tmp_path / "runtime"
    session_id = "a1b2c3d4.worker"

    bus = _make_bus(tmp_path)
    hook = SubagentAutoSendHook(
    tree=_mock_tree(bus),
        self_name="worker",
        parent_name="main",
        runtime_dir=runtime_dir,
    )
    ctx = _make_context(session_id)
    result = AgentResult(
        content="Same tool call again.",
        stop_reason=StopReason.LOOP_DETECTED,
    )

    await hook.finally_graph(ctx, result)

    xml = (await bus.consume("conv123.main"))[0].payload["content"]
    invocation_id = SessionInfo.from_str(session_id).session_id_prefix
    assert f"invocation_id='{invocation_id}'" in xml
    assert "The task is incomplete. To continue it, call task with" in xml


@pytest.mark.asyncio
async def test_loop_detected_without_parent_logs_warning(tmp_path: Path, caplog):
    """No parent_session_id means there is nowhere to route; log and skip."""
    runtime_dir = tmp_path / "runtime"
    session_id = "a1b2c3d4.worker"

    bus = _make_bus(tmp_path)
    bus.send = AsyncMock()  # should not be called
    hook = SubagentAutoSendHook(
    tree=_mock_tree(bus),
        self_name="worker",
        parent_name="main",
        runtime_dir=runtime_dir,
    )
    session = SessionInfo(
        session_id=session_id,
        agent_name="worker",
        parent_session_id=None,
    )
    ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=session,
        comm_kind=AgentCommKind.SUBAGENT,
    )
    result = AgentResult(
        content="Loop without parent.",
        stop_reason=StopReason.LOOP_DETECTED,
    )

    with caplog.at_level("WARNING"):
        await hook.finally_graph(ctx, result)

    bus.send.assert_not_awaited()
    assert "no parent_session_id" in caplog.text.lower()
