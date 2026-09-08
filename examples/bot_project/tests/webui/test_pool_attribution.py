from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.register_websocket import get_ws_input
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.service.session_pool_index import SessionPoolIndex
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.web_ui_service import WebUIService
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.emitter import WebBotEmitter
from bot.webui.events import SessionMeta, WebUIEventType, _unwrap_envelope
from bot.webui.server import WebUIServer
from bot.workspace.handle import PoolWorkspaceResources

from modex_agent.core.events import EmitterConfig
from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.pool_router import PoolSessionStore
from modex_agent.multi_agent.session_tree.models import (
    NodeVersionStatus,
    SessionTreeRecord,
    SessionTreeStatus,
    TreeNodeRecord,
)
from modex_agent.multi_agent.session_tree.store_node import InMemoryTreeNodeStore
from modex_agent.multi_agent.session_tree.store_tree import InMemorySessionTreeStore
from modex_agent.workspace.paths import WorkspacePaths
from tests.webui._pipeline_fixture import attach_default_pipeline

_DATA_DIR_NAME: Final = ".modex"
_DEFAULT_POOL: Final = "default"
_OWNING_POOL: Final = "opencode"
_SESSION_PREFIX: Final = "1ea047244451"
_SESSION_ID: Final = f"{_SESSION_PREFIX}.{_OWNING_POOL}"


async def _server_with_opencode_session(
    root: Path,
) -> tuple[WebUIServer, WebSocketInputAdapter, WorkspaceScopedTranscriptStore]:
    input_adapter = WebSocketInputAdapter()
    transcript_store = WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR_NAME)
    paths = WorkspacePaths(root=root / _DATA_DIR_NAME)
    server = WebUIServer(
        input_adapter,
        transcript_store,
        static_dist=None,
        data_dir=root,
        home_sessions_dir=paths.sessions_dir,
    )
    server.set_workspace_index(transcript_store)
    server.set_data_dir_name(_DATA_DIR_NAME)
    server.set_pool_agent_names([_DEFAULT_POOL, _OWNING_POOL])
    session_store = WorkspacePoolSessionStore(
        base_dir=paths.session_index_dir,
        pool_resolver=lambda _session: _OWNING_POOL,
    )
    await session_store.save(
        SessionInfo(session_id=_SESSION_ID, agent_name=_OWNING_POOL)
    )
    server.set_session_store(session_store)
    tree_store = InMemorySessionTreeStore()
    node_store = InMemoryTreeNodeStore()
    tree_id = "tree-opencode"
    await tree_store.create(
        SessionTreeRecord(
            tree_id=tree_id,
            root_node_session_id=_SESSION_ID,
            pool_name=_OWNING_POOL,
            workspace_root=str(root),
            status=SessionTreeStatus.ACTIVE,
            created_at=1,
            updated_at=1,
        )
    )
    await node_store.create(
        TreeNodeRecord(
            tree_id=tree_id,
            session_id=_SESSION_ID,
            parent_session_id=None,
            agent_name=_OWNING_POOL,
            version=1,
            parent_version=None,
            status=NodeVersionStatus.RUNNING,
            created_at=1,
            updated_at=1,
        )
    )
    session_pool_index = SessionPoolIndex()
    session_pool_index.register(_OWNING_POOL, tree_store, node_store)
    resources = MagicMock(spec=PoolWorkspaceResources)
    resources.session_pool_index = session_pool_index
    server.set_graph_workspace_resolver(lambda _ws: resources)
    return server, input_adapter, transcript_store


def _new_pool_store(root: Path) -> PoolSessionStore:
    pool_store = PoolSessionStore(root / _DATA_DIR_NAME)
    pool_store.set_pool(_SESSION_PREFIX, _DEFAULT_POOL)
    return pool_store


@pytest.mark.asyncio
async def test_transcript_uses_emitter_owning_pool() -> None:
    """Transcript writes use the emitter's owning pool, not prefix routing."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    transcript_store = MagicMock()
    transcript_store.append = AsyncMock()

    emitter_constructor = MagicMock(wraps=WebBotEmitter)
    emitter = emitter_constructor(
        output_adapter,
        _SESSION_ID,
        config=EmitterConfig(),
        transcript_store=transcript_store,
        session_meta_resolver=SessionMeta,
        pool=_OWNING_POOL,
    )
    await emitter.emit_content("owned by opencode")
    await emitter.emit_stream_end()

    assert transcript_store.append.await_args.kwargs["pool"] == _OWNING_POOL


@pytest.mark.asyncio
async def test_websocket_envelope_uses_emitter_owning_pool() -> None:
    """WebSocket envelopes carry the emitter's owning pool, not prefix routing."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection(_SESSION_ID, None)

    emitter_constructor = MagicMock(wraps=WebBotEmitter)
    emitter = emitter_constructor(
        output_adapter,
        _SESSION_ID,
        config=EmitterConfig(),
        session_meta_resolver=SessionMeta,
        pool=_OWNING_POOL,
    )
    await emitter.emit_delta("streamed by opencode")

    queue = input_adapter.get_delta_queue(_SESSION_ID, None)
    assert queue is not None
    assert queue.get_nowait().pool == _OWNING_POOL


@pytest.mark.asyncio
async def test_conversation_created_uses_dispatched_pool_when_prefix_route_conflicts(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "bot_config.yml").write_text(
        "multi_agent: {}\npaths: {data_dir_name: .modex}\nworkspace: {enabled: false}\n",
        encoding="utf-8",
    )
    service = WebUIService(config_dir)
    pool_store = _new_pool_store(tmp_path)
    service._pool_session_store = pool_store
    parent_id = f"{_SESSION_PREFIX}.{_DEFAULT_POOL}"
    callback: Callable[[str, str, str], Awaitable[None]] | None = (
        service._on_subagent_created
    )

    assert pool_store.get_pool(_SESSION_PREFIX) == _DEFAULT_POOL
    assert callback is not None
    await callback(_SESSION_ID, parent_id, _OWNING_POOL)

    queues = get_ws_input().get_delta_queues(_SESSION_ID)
    assert len(queues) == 1
    envelope = queues[0].get_nowait()
    assert envelope.event_type == WebUIEventType.CONVERSATION_CREATED.value
    assert envelope.pool == _OWNING_POOL


@pytest.mark.asyncio
async def test_session_list_uses_session_tree_owning_pool(tmp_path: Path) -> None:
    """Session listing uses session-tree ownership when prefix routing conflicts."""
    server, _, _ = await _server_with_opencode_session(tmp_path)
    pool_store = _new_pool_store(tmp_path)
    server.set_pool_resolver(pool_store.get_pool)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.get("/api/sessions")
        assert response.status == 200
        sessions = await response.json()
    finally:
        await client.close()

    listed = next(session for session in sessions if session["session_id"] == _SESSION_ID)
    assert listed["pool"] == _OWNING_POOL


@pytest.mark.asyncio
async def test_attach_does_not_rewrite_existing_prefix_route(tmp_path: Path) -> None:
    """Attaching an existing peer session does not rewrite prefix routing."""
    server, _, _ = await _server_with_opencode_session(tmp_path)
    pool_store = _new_pool_store(tmp_path)
    server.set_pool_resolver(pool_store.get_pool)
    server.set_pool_switch_callback(pool_store.set_pool)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        websocket = await client.ws_connect("/ws")
        await websocket.send_json(
            {"action": "attach", "session_id": _SESSION_ID, "pool": _OWNING_POOL}
        )
        attached = _unwrap_envelope(await websocket.receive_json())
        assert attached["event"] == "attached"
    finally:
        await client.close()

    assert pool_store.get_pool(_SESSION_PREFIX) == _DEFAULT_POOL


@pytest.mark.asyncio
async def test_send_without_client_pool_does_not_rewrite_prefix_route(
    tmp_path: Path,
) -> None:
    """Sending to an existing peer without client pool leaves prefix routing intact."""
    server, input_adapter, transcript_store = await _server_with_opencode_session(
        tmp_path
    )
    pool_store = _new_pool_store(tmp_path)
    server.set_pool_resolver(lambda _prefix: _OWNING_POOL)
    server.set_pool_switch_callback(lambda _prefix, _pool: None)
    await attach_default_pipeline(
        server,
        transcript_store,
        input_adapter,
        pool_session_store=pool_store,
        workspace_root=tmp_path,
        available_pools=lambda: {_DEFAULT_POOL, _OWNING_POOL},
    )
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        websocket = await client.ws_connect("/ws")
        await websocket.send_json({"action": "attach", "session_id": _SESSION_ID})
        attached = _unwrap_envelope(await websocket.receive_json())
        assert attached["event"] == "attached"
        assert pool_store.get_pool(_SESSION_PREFIX) == _DEFAULT_POOL

        await websocket.send_json(
            {"action": "send_message", "session_id": _SESSION_ID, "content": "hello"}
        )
        echoed = _unwrap_envelope(await websocket.receive_json(timeout=2))
        assert echoed["event"] == "user_message"
    finally:
        await client.close()

    assert pool_store.get_pool(_SESSION_PREFIX) == _DEFAULT_POOL


@pytest.mark.asyncio
async def test_send_without_client_pool_routes_to_existing_tree_pool(
    tmp_path: Path,
) -> None:
    server, input_adapter, transcript_store = await _server_with_opencode_session(
        tmp_path
    )
    pool_store = _new_pool_store(tmp_path)
    server.set_pool_resolver(pool_store.get_pool)
    await attach_default_pipeline(
        server,
        transcript_store,
        input_adapter,
        pool_session_store=pool_store,
        workspace_root=tmp_path,
        available_pools=lambda: {_DEFAULT_POOL, _OWNING_POOL},
    )
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        websocket = await client.ws_connect("/ws")
        await websocket.send_json({"action": "attach", "session_id": _SESSION_ID})
        _unwrap_envelope(await websocket.receive_json())

        await websocket.send_json(
            {"action": "send_message", "session_id": _SESSION_ID, "content": "hello"}
        )
        echoed = _unwrap_envelope(await websocket.receive_json(timeout=2))
        queued = input_adapter._message_queue.get_nowait()
    finally:
        await client.close()

    assert echoed["event"] == "user_message"
    assert queued.metadata[RoutingMeta.RESOLVED_POOL] == _OWNING_POOL
