"""Subagent loop_detected result must be routed to the parent inbox."""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

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


def _extract_xml_field(xml: str, tag: str) -> str:
    pattern = rf"<{tag}>(.*?)</{tag}>"
    m = re.search(pattern, xml, re.DOTALL)
    return m.group(1).strip() if m else ""


@pytest.mark.asyncio
async def test_loop_detected_sends_incomplete_notification(tmp_path: Path):
    """A subagent that stops with LOOP_DETECTED must notify the parent with
    status=incomplete and a loop-specific hint."""
    runtime_dir = tmp_path / "runtime"
    session_id = "a1b2c3d4.worker"

    bus = _make_bus(tmp_path)
    hook = SubagentAutoSendHook(
        agent_bus=bus,
        self_name="worker",
        parent_name="main",
        runtime_dir=runtime_dir,
    )
    ctx = _make_context(session_id)
    result = AgentResult(
        content="I am stuck doing the same thing.",
        stop_reason=StopReason.LOOP_DETECTED,
    )

    await hook.finally_turn(ctx, result)

    msgs = await bus.consume("conv123.main")
    assert len(msgs) == 1
    xml = msgs[0].payload["content"]
    assert "<subagent_notification>" in xml
    assert _extract_xml_field(xml, "status") == "incomplete"
    assert _extract_xml_field(xml, "is_normal") == "false"
    assert _extract_xml_field(xml, "stop_reason") == StopReason.LOOP_DETECTED
    hint = _extract_xml_field(xml, "hint")
    assert "loop" in hint.lower()
    assert "stuck" in hint.lower()


@pytest.mark.asyncio
async def test_loop_detected_includes_invocation_id_in_hint(tmp_path: Path):
    """The loop hint must include the invocation_id so the parent can resume."""
    runtime_dir = tmp_path / "runtime"
    session_id = "a1b2c3d4.worker"

    bus = _make_bus(tmp_path)
    hook = SubagentAutoSendHook(
        agent_bus=bus,
        self_name="worker",
        parent_name="main",
        runtime_dir=runtime_dir,
    )
    ctx = _make_context(session_id)
    result = AgentResult(
        content="Same tool call again.",
        stop_reason=StopReason.LOOP_DETECTED,
    )

    await hook.finally_turn(ctx, result)

    xml = (await bus.consume("conv123.main"))[0].payload["content"]
    hint = _extract_xml_field(xml, "hint")
    invocation_id = SessionInfo.from_str(session_id).session_id_prefix
    assert f"invocation_id={invocation_id}" in hint


@pytest.mark.asyncio
async def test_loop_detected_without_parent_logs_warning(tmp_path: Path, caplog):
    """No parent_session_id means there is nowhere to route; log and skip."""
    runtime_dir = tmp_path / "runtime"
    session_id = "a1b2c3d4.worker"

    bus = _make_bus(tmp_path)
    bus.send = AsyncMock()  # should not be called
    hook = SubagentAutoSendHook(
        agent_bus=bus,
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
        await hook.finally_turn(ctx, result)

    bus.send.assert_not_awaited()
    assert "no parent_session_id" in caplog.text.lower()
