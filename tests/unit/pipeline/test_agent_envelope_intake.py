from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import MessageRole, ReminderKind
from modex_agent.messaging.broker import AddressKind
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.adapters.output import OutputAdapter
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry


def _make_builder() -> TurnContextBuilder:
    agent = MagicMock()
    agent.name = "main"
    return TurnContextBuilder(
        agent=agent,
        tool_manager=InMemoryToolManager(),
        sanitizer=None,
        command_processor=None,
        skill_manager=None,
        context_builder=None,
        agent_descriptor=None,
        max_iterations=5,
        safety=MagicMock(),
        runtime_services=None,
        runtime_context_manager=None,
        governance=None,
        hook_runner=None,
        interceptor_chain=None,
        control_channel=None,
        emitter_factory=None,
        output_adapter=MagicMock(spec=OutputAdapter),
        turn_store=None,
        registry=TurnSessionRegistry(),
    )


@pytest.mark.asyncio
async def test_agent_envelope_metadata_reaches_system_reminder_history() -> None:
    envelope = AgentMessageEnvelope(
        payload={"content": "task complete"},
        source=AgentAddress(kind=AddressKind.AGENT, name="worker"),
        target=AgentAddress(kind=AddressKind.AGENT, name="main"),
        message_type=AgentMessageType.AGENT_RESULT,
        session_id="task-42.worker",
        agent_session_id="conv-1.main",
        invocation_id="task-42",
        metadata={"reminder_kind": ReminderKind.SUBAGENT_RESULT},
    )

    metadata = envelope.to_input_metadata()

    assert metadata["source_agent"] == "worker"
    assert metadata["message_type"] == AgentMessageType.AGENT_RESULT
    assert metadata["invocation_id"] == "task-42"
    assert metadata["reminder_kind"] == ReminderKind.SUBAGENT_RESULT

    input_msg = envelope.to_input_message(session=SessionInfo.from_str("conv-1.main"))
    state = await _make_builder().assemble(
        "conv-1.main",
        input_msg,
        input_msg.metadata,
        input_msg.content,
        InMemoryContextManager(),
        None,
        False,
    )

    history = await state.history.to_list()
    assert len(history) == 1
    record = history[0]
    assert record.get("role") == MessageRole.SYSTEM_REMINDER
    assert record.get("source_agent") == "worker"
    assert record.get("content") == ("<system-reminder>\ntask complete\n</system-reminder>")
    assert record.get("reminder_kind") == ReminderKind.SUBAGENT_RESULT
    assert record.get("invocation_id") == "task-42"
