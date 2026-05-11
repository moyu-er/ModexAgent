"""Tests for RuntimeCommandStore — durable command lifecycle."""
from __future__ import annotations

from framework.runtime.enums import ControlCommandKind, OperationStatus
from framework.runtime.models import ControlCommandState, StateQueryScope


async def test_command_store_lifecycle() -> None:
    from framework.runtime.store import InMemoryRuntimeCommandStore

    store = InMemoryRuntimeCommandStore()
    command = ControlCommandState(
        command_id="cmd-1",
        kind=ControlCommandKind.CANCEL_TURN,
        agent_id="bot",
        session_id="s1",
        payload={"reason": "user"},
    )

    await store.save_command(command)
    pending = await store.load_pending_commands(StateQueryScope(agent_id="bot", session_id="s1"))

    assert [item.command_id for item in pending] == ["cmd-1"]

    await store.mark_command_applied("cmd-1")
    assert command.status is OperationStatus.COMPLETED
    assert await store.load_pending_commands(StateQueryScope(agent_id="bot", session_id="s1")) == []
