"""Tests for SubagentAutoSendHook._notify_parent envelope metadata injection."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.session_tree.request_scope import REQUEST_SCOPE_ID_KEY
from modex_agent.tools.manager import InMemoryToolManager


def _make_context(graph_instance_id: int | None) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo(
            session_id="638aaa67.explore",
            agent_name="explore",
            parent_session_id="conv123.main",
        ),
        graph_instance_id=graph_instance_id,
    )


def _make_tree(sender_scope_id: str | None) -> AsyncMock:
    tree = AsyncMock(spec=SessionTreeManager)
    tree.sender_scope_id.return_value = sender_scope_id
    return tree


async def test_notify_parent_injects_graph_instance_id_into_metadata() -> None:
    """Given ctx.graph_instance_id=42, the delivered envelope carries it in metadata."""
    tree = _make_tree(sender_scope_id=None)
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
    tree = _make_tree(sender_scope_id=None)
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


async def test_notify_parent_stamps_sender_running_request_scope() -> None:
    """The auto-sent AGENT_RESULT is causal child traffic: it must carry the
    SENDER's running request scope, or the scoped deliver gate archives it
    and the parent never sees the subagent result (source-time attribution,
    same mechanism as SendStrategy.send)."""
    tree = _make_tree(sender_scope_id="scope-1")
    hook = SubagentAutoSendHook(
        tree=tree,
        self_name="explore",
        parent_name="main",
    )
    ctx = _make_context(graph_instance_id=None)

    await hook._notify_parent(ctx, session_id="638aaa67.explore", content="done")

    tree.sender_scope_id.assert_awaited_once_with("638aaa67.explore")
    delivered_envelope = tree.deliver.await_args[0][1]
    assert delivered_envelope.metadata[REQUEST_SCOPE_ID_KEY] == "scope-1"


async def test_notify_parent_omits_scope_stamp_outside_any_request() -> None:
    """Ordinary (non-request-scoped) sends carry no scope stamp."""
    tree = _make_tree(sender_scope_id=None)
    hook = SubagentAutoSendHook(
        tree=tree,
        self_name="explore",
        parent_name="main",
    )
    ctx = _make_context(graph_instance_id=None)

    await hook._notify_parent(ctx, session_id="638aaa67.explore", content="done")

    delivered_envelope = tree.deliver.await_args[0][1]
    assert REQUEST_SCOPE_ID_KEY not in delivered_envelope.metadata
