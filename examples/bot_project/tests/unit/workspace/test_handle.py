"""Tests for bot.workspace.handle — WorkspaceHandle + PoolWorkspaceResources."""

from __future__ import annotations

from pathlib import Path

from bot.workspace.handle import PoolWorkspaceResources, WorkspaceHandle
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.core.session_store import LocalFileSessionStore
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxServer
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore


def test_workspace_handle_exposes_current_and_data_dir(tmp_path: Path) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    data_root = tmp_path / "ws" / ".modex"
    handle = WorkspaceHandle(target=target, data_root=data_root)
    assert handle.current == target.resolve()
    assert handle.data_dir == data_root


def _build_resources(tmp_path: Path) -> PoolWorkspaceResources:
    target = tmp_path / "ws"
    target.mkdir()
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=tmp_path)
    inbox_server = LocalFileInboxServer(workspace=ctx.paths.inbox_dir)
    broker = InMemoryMessageBroker()
    producer = InboxProducer(server=inbox_server)
    consumer = InboxConsumer(server=inbox_server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer, broker=broker)
    return PoolWorkspaceResources(
        target=target,
        ctx=ctx,
        inbox_server=inbox_server,
        overflow_store=LocalFileToolOverflowStore(workspace=ctx.paths.overflow_dir),
        session_index_store=LocalFileSessionStore(root=ctx.paths.session_index_dir),
        broker=broker,
        inbox_producer=producer,
        inbox_consumer=consumer,
        agent_bus=bus,
    )


def test_resources_resolve_workspace_returns_self(tmp_path: Path) -> None:
    r = _build_resources(tmp_path)
    assert r.resolve_workspace() is r


def test_resources_default_collections_empty(tmp_path: Path) -> None:
    r = _build_resources(tmp_path)
    assert r.pool_data == {}
    assert r.pools == {}
    assert r.pool_router is None
