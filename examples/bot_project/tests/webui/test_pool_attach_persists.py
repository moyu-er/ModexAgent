"""TDD: WebSocket attach with uuid_prefix + pool MUST persist to PoolSessionStore.

Bug: `pool_sessions/4174bee9aee6.json` does not exist after creating a coding-pool
session. Without this file, PoolRouter defaults to "main" and the session's
memory lands in `memory/main/` instead of `memory/coding/`.

This test drives the REAL pipeline + REAL PoolSessionStore the way production does.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.pool_router import PoolRouter, PoolSessionStore
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import _unwrap_envelope
from bot.webui.server import WebUIServer
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.core.session_id import SessionIdFactory

_DATA_DIR_NAME = ".modex"


def _build_real_coding_server(
    tmp_root: Path,
) -> tuple[WebUIServer, WebSocketInputAdapter, PoolSessionStore]:
    """Build a server wired like production with a real PoolSessionStore."""
    sessions_dir = WorkspacePaths(root=tmp_root / _DATA_DIR_NAME).sessions_dir
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR_NAME)
    store.set_agent_pool_map({"main": "main", "coding": "coding"})

    server = WebUIServer(
        input_adapter,
        store,
        static_dist=None,
        data_dir=tmp_root,
        home_sessions_dir=sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(_DATA_DIR_NAME)
    server.set_pool_agent_names(["main", "coding"])
    server.set_agent_pool_map({"main": "main", "coding": "coding"})
    server.set_agent_resolver(lambda p: {"main": "main", "coding": "coding"}.get(p, p))
    server.set_session_factory(SessionIdFactory())

    server.set_session_store(
        WorkspacePoolSessionStore(
            base_dir=WorkspacePaths(root=tmp_root / _DATA_DIR_NAME).session_index_dir,
            pool_resolver=lambda s: {"main": "main", "coding": "coding"}.get(
                s.agent_name, "main"
            ),
        )
    )

    # REAL PoolSessionStore + PoolRouter — what production uses.
    # PoolSessionStore appends "/pool_sessions" to data_dir, so pass the
    # .modex root (NOT pool_sessions_dir) to avoid double nesting.
    pool_session_store = PoolSessionStore(
        data_dir=WorkspacePaths(root=tmp_root / _DATA_DIR_NAME).root
    )

    # PoolRouter needs at least an empty pools dict for set_pool to work.
    from unittest.mock import MagicMock

    pool_router = MagicMock(spec=PoolRouter)
    pool_router.set_pool = lambda sid, pn: pool_session_store.set(
        sid, pn
    )

    server.set_pool_switch_callback(pool_router.set_pool)

    # Inject the WebUI input pipeline so _ws_send_message works.
    from tests.webui._pipeline_fixture import attach_default_pipeline

    attach_default_pipeline(
        server,
        store,
        input_adapter,
        workspace_root=tmp_root,
        agent_pool_map={"main": "main", "coding": "coding"},
        pool_session_store=pool_session_store,
    )

    return server, input_adapter, pool_session_store


@pytest.mark.asyncio
async def test_new_conversation_attach_persists_pool_to_disk() -> None:
    """When the frontend attaches with uuid_prefix + pool='coding', the
    pool_sessions/<prefix>.json file MUST exist on disk with pool='coding'.

    This is the Phase 1 red test for the user's bug.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        server, _, pool_store = _build_real_coding_server(root)

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")

            # Exactly what the frontend does: send uuid_prefix + pool.
            await ws.send_json({
                "action": "attach",
                "uuid_prefix": "4174bee9aee6",
                "pool": "coding",
            })
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"
            assert attached["session_id"] == "4174bee9aee6.coding"

            # The pool_sessions file MUST now exist on disk.
            pool_file = (
                root / _DATA_DIR_NAME / "pool_sessions" / "4174bee9aee6.json"
            )
            assert pool_file.exists(), (
                f"BUG: pool_sessions/4174bee9aee6.json was NOT created. "
                f"The PoolSessionStore did not persist the coding pool mapping. "
                f"Without it, PoolRouter defaults to 'main'."
            )
            content = json.loads(pool_file.read_text())
            assert content["pool"] == "coding", (
                f"Expected pool='coding', got {content}"
            )
        finally:
            await client.close()
