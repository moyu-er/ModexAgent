"""Integration test: server session list after workspace rebase.

This reproduces the user-reported issue: after ``/cd`` to another workspace,
GET /api/sessions still returns sessions from the home workspace.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.web_ui_service import WebUIService
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent
from bot.webui.server import WebUIServer


def _real_agent_pool_map() -> dict[str, str]:
    class _Source:
        _project_dir = Path(__file__).resolve().parent.parent.parent
    return WebUIService._build_agent_pool_map(_Source())


def _make_server(data_dir: Path) -> tuple[WebUIServer, WebSocketInputAdapter, object]:
    """Create a wired WebUIServer with a mock workspace context."""
    inp = WebSocketInputAdapter()
    mapping = _real_agent_pool_map()
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    store.set_agent_pool_map(mapping)

    ws_ctx = MagicMock()
    ws_ctx.current = Path("/home")
    ws_ctx.home = Path("/home")

    server = WebUIServer(inp, store, static_dist=None, data_dir=data_dir)
    server.set_workspace_context(ws_ctx)
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])
    server.set_agent_pool_map(mapping)
    server.set_agent_resolver(lambda pool_name: mapping.get(pool_name, pool_name))
    return server, inp, ws_ctx


@pytest.mark.asyncio
async def test_session_list_follows_workspace_rebase() -> None:
    """After rebase, GET /api/sessions?pool=main returns only the new workspace."""
    with tempfile.TemporaryDirectory() as tmp:
        home_dir = Path(tmp) / "home" / "sessions"
        other_dir = Path(tmp) / "other" / "sessions"
        home_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)

        server, _inp, ws_ctx = _make_server(home_dir)
        mapping = _real_agent_pool_map()

        # Seed home workspace with a main session.
        sid_home = "home-conv.main"
        server._store.append(
            sid_home,
            UserMessageEvent(session_id=sid_home, agent_name="main", content="home"),
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions?pool=main")
            assert resp.status == 200
            sessions = await resp.json()
            sids = {s["session_id"] for s in sessions}
            assert sid_home in sids

            # Simulate workspace switch: rebase store and update workspace context.
            store = server._store
            assert isinstance(store, WorkspaceScopedTranscriptStore)
            store.rebase(other_dir)
            ws_ctx.current = Path("E:\\download\\bot")  # mimic user target

            # Seed other workspace with a different main session.
            sid_other = "other-conv.main"
            server._store.append(
                sid_other,
                UserMessageEvent(session_id=sid_other, agent_name="main", content="other"),
            )

            resp = await client.get("/api/sessions?pool=main")
            assert resp.status == 200
            sessions = await resp.json()
            sids = {s["session_id"] for s in sessions}
            assert sid_other in sids, (
                f"Expected '{sid_other}' in other workspace, got {sids}"
            )
            assert sid_home not in sids, (
                f"Home session '{sid_home}' leaked after rebase, got {sids}"
            )
        finally:
            await client.close()
