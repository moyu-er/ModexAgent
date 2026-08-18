"""Tests for SubagentAutoSendHook._notify_parent envelope metadata injection."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager


def _make_context(graph_instance_id: int | None) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=SessionInfo(
            session_id="638aaa67.explore",
            agent_name="explore",
            parent_session_id="conv123.main",
        ),
        graph_instance_id=graph_instance_id,
    )


async def test_notify_parent_injects_graph_instance_id_into_metadata() -> None:
    """Given ctx.graph_instance_id=42, the delivered envelope carries it in metadata."""
    tree = AsyncMock(spec=SessionTreeManager)
    hook = SubagentAutoSendHook(
        tree=tree,
        self_name="explore",
        parent_name="main",
    )
    ctx = _make_context(graph_instance_id=42)

    await hook._notify_parent(ctx, session_id="638aaa67.explore", content="done")

    tree.deliver.assert_awaited_once()
    delivered_envelope = tree.deliver.await_args[0][1]
    assert delivered_envelope.metadata["graph_instance_id"] == 42
    assert delivered_envelope.metadata["reminder_kind"] is not None


async def test_notify_parent_omits_graph_instance_id_when_none() -> None:
    """Given ctx.graph_instance_id=None, metadata has no graph_instance_id key."""
    tree = AsyncMock(spec=SessionTreeManager)
    hook = SubagentAutoSendHook(
        tree=tree,
        self_name="explore",
        parent_name="main",
    )
    ctx = _make_context(graph_instance_id=None)

    await hook._notify_parent(ctx, session_id="638aaa67.explore", content="done")

    tree.deliver.assert_awaited_once()
    delivered_envelope: Any = tree.deliver.await_args[0][1]
    assert "graph_instance_id" not in delivered_envelope.metadata
