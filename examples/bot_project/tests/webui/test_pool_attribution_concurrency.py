from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.service.session_pool_index import SessionPoolIndex
from bot.webui.emitter import WebBotEmitter
from bot.webui.events import _unwrap_envelope

from modex_agent.core.emitter import EmitterConfig
from modex_agent.multi_agent.session_tree.models import (
    NodeVersionStatus,
    SessionTreeRecord,
    SessionTreeStatus,
    TreeNodeRecord,
)
from modex_agent.multi_agent.session_tree.store_node import InMemoryTreeNodeStore
from modex_agent.multi_agent.session_tree.store_tree import InMemorySessionTreeStore
from tests.webui._pipeline_fixture import attach_default_pipeline
from tests.webui.test_pool_attribution import (
    _new_pool_store,
    _server_with_opencode_session,
)

_DEFAULT_POOL = "default"
_OWNING_POOL = "opencode"
_SESSION_PREFIX = "1ea047244451"
_DEFAULT_SESSION_ID = f"{_SESSION_PREFIX}.{_DEFAULT_POOL}"
_PEER_SESSION_ID = f"{_SESSION_PREFIX}.{_OWNING_POOL}"
_NOW = 1_700_000_000_000


async def _emit_turn(emitter: WebBotEmitter, content: str) -> None:
    await emitter.emit_content(content)
    await emitter.emit_stream_end()


def _tree_record(tree_id: str, session_id: str, pool: str) -> SessionTreeRecord:
    return SessionTreeRecord(
        tree_id=tree_id,
        root_node_session_id=session_id,
        pool_name=pool,
        workspace_root=".",
        status=SessionTreeStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _node_record(tree_id: str, session_id: str) -> TreeNodeRecord:
    return TreeNodeRecord(
        tree_id=tree_id,
        session_id=session_id,
        parent_session_id=None,
        agent_name="main",
        version=1,
        parent_version=None,
        status=NodeVersionStatus.RUNNING,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_same_prefix_concurrent_emitters_keep_transcript_pool_ownership() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    transcript_store = MagicMock()
    transcript_store.append = AsyncMock()
    default_emitter = WebBotEmitter(
        output_adapter,
        _DEFAULT_SESSION_ID,
        config=EmitterConfig(),
        transcript_store=transcript_store,
        pool=_DEFAULT_POOL,
    )
    peer_emitter = WebBotEmitter(
        output_adapter,
        _PEER_SESSION_ID,
        config=EmitterConfig(),
        transcript_store=transcript_store,
        pool=_OWNING_POOL,
    )

    await asyncio.gather(
        _emit_turn(default_emitter, "default turn"),
        _emit_turn(peer_emitter, "opencode turn"),
    )

    ownership = {
        (call.args[0], call.kwargs["pool"])
        for call in transcript_store.append.await_args_list
    }
    assert ownership == {
        (_DEFAULT_SESSION_ID, _DEFAULT_POOL),
        (_PEER_SESSION_ID, _OWNING_POOL),
    }


async def test_workspace_session_pool_indexes_do_not_cross_resolve() -> None:
    default_index = SessionPoolIndex()
    default_trees = InMemorySessionTreeStore()
    default_nodes = InMemoryTreeNodeStore()
    default_index.register(_DEFAULT_POOL, default_trees, default_nodes)
    await default_trees.create(
        _tree_record("tree-default", _DEFAULT_SESSION_ID, _DEFAULT_POOL)
    )
    await default_nodes.create(_node_record("tree-default", _DEFAULT_SESSION_ID))

    peer_index = SessionPoolIndex()
    peer_trees = InMemorySessionTreeStore()
    peer_nodes = InMemoryTreeNodeStore()
    peer_index.register(_OWNING_POOL, peer_trees, peer_nodes)
    await peer_trees.create(
        _tree_record("tree-opencode", _PEER_SESSION_ID, _OWNING_POOL)
    )
    await peer_nodes.create(_node_record("tree-opencode", _PEER_SESSION_ID))

    assert await default_index.pool_of(_DEFAULT_SESSION_ID) == _DEFAULT_POOL
    assert await default_index.pool_of(_PEER_SESSION_ID) is None
    assert await peer_index.pool_of(_PEER_SESSION_ID) == _OWNING_POOL
    assert await peer_index.pool_of(_DEFAULT_SESSION_ID) is None


async def test_alternating_tabs_without_pool_keep_prefix_route_stable(
    tmp_path: Path,
) -> None:
    server, input_adapter, transcript_store = await _server_with_opencode_session(
        tmp_path
    )
    pool_store = _new_pool_store(tmp_path)
    server.set_pool_resolver(pool_store.get_pool)
    server.set_pool_switch_callback(pool_store.set_pool)
    attach_default_pipeline(
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
        default_tab = await client.ws_connect("/ws")
        peer_tab = await client.ws_connect("/ws")
        await default_tab.send_json(
            {"action": "attach", "session_id": _DEFAULT_SESSION_ID}
        )
        _unwrap_envelope(await default_tab.receive_json())
        await peer_tab.send_json(
            {"action": "attach", "session_id": _PEER_SESSION_ID}
        )
        _unwrap_envelope(await peer_tab.receive_json())

        await default_tab.send_json(
            {
                "action": "send_message",
                "session_id": _DEFAULT_SESSION_ID,
                "content": "from default tab",
            }
        )
        _unwrap_envelope(await default_tab.receive_json(timeout=2))
        assert pool_store.get_pool(_SESSION_PREFIX) == _DEFAULT_POOL

        await peer_tab.send_json(
            {
                "action": "send_message",
                "session_id": _PEER_SESSION_ID,
                "content": "from peer tab",
            }
        )
        _unwrap_envelope(await peer_tab.receive_json(timeout=2))
        assert pool_store.get_pool(_SESSION_PREFIX) == _DEFAULT_POOL
    finally:
        await client.close()
