"""GET /api/sessions?ws=<path> filters sessions to the requested workspace.

Task 5: the backend honors the ``?ws=`` query parameter on ``GET /api/sessions``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import AssistantTurnEvent, UserMessageEvent
from bot.webui.server import WebUIServer

from modex_agent.core.session_id import SessionIdFactory
from modex_agent.multi_agent.pool_router import PoolSessionStore
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.runtime import bind_workspace_root


def _real_project_dir() -> Path:
    """Return the real bot project directory for config loading."""
    return Path(__file__).resolve().parent.parent.parent


def _make_server(
    data_dir: Path,
) -> tuple[WebUIServer, WebSocketInputAdapter, PoolSessionStore]:
    """Create a fully wired WebUIServer with the real production pool map.

    ``data_dir`` is the workspace ROOT whose ``.modex/sessions`` becomes the
    server's ``home_sessions_dir`` (read when no ``?ws=`` is given).
    """
    inp = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        data_dir=data_dir,
        home_sessions_dir=home_sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(".modex")
    server.set_pool_agent_names(["default", "coding"])
    server.set_agent_resolver(lambda pool_name: pool_name)
    routing_store = PoolSessionStore(data_dir=data_dir)
    server.set_pool_switch_callback(routing_store.set_pool)
    server.set_pool_resolver(routing_store.get_pool)
    # Inject session store + factory so POST /api/sessions auto-saves.
    session_store = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: "default",
    )
    server.set_session_store(session_store)
    server.set_session_factory(SessionIdFactory())
    return server, inp, routing_store


async def _simulate_qa_turn(
    store: WorkspaceScopedTranscriptStore,
    conv_prefix: str,
    agent_name: str,
    user_content: str,
    assistant_content: str,
    root: Path,
) -> None:
    """Materialise one Q/A turn by writing user + assistant events.

    Writes route to ``<root>/.modex/sessions/...`` via the ctxvar binding.
    """
    session_id = f"{conv_prefix}.{agent_name}"
    with bind_workspace_root(root):
        await store.append(
            session_id,
            UserMessageEvent(
                session_id=session_id,
                agent_name=agent_name,
                content=user_content,
            ),
            pool="default",
        )
        await store.append(
            session_id,
            AssistantTurnEvent(
                session_id=session_id,
                agent_name=agent_name,
                turn_id="turn-1",
                blocks=[{"type": "text", "text": assistant_content}],
                latency_ms=0,
            ),
            pool="default",
        )


@pytest.mark.asyncio
async def test_sessions_filter_by_workspace() -> None:
    """GET /api/sessions?ws=A only returns sessions from workspace A."""
    ws_a = Path(tempfile.mkdtemp())
    ws_b = Path(tempfile.mkdtemp())
    ws_a.mkdir(parents=True, exist_ok=True)
    ws_b.mkdir(parents=True, exist_ok=True)

    # ``data_dir`` is a distinct, empty tmp dir: it becomes the server's
    # ``home_sessions_dir`` base, so queries without ``?ws=`` find nothing.
    data_dir = Path(tempfile.mkdtemp())

    server2, _, routing_store = _make_server(data_dir)
    routing_store.set_pool("conv-a", "default")
    routing_store.set_pool("conv-b", "default")
    client2 = TestClient(TestServer(server2.app))
    await client2.start_server()

    try:
        # Writes route by the bound workspace root (ctxvar), not a resolver.
        await _simulate_qa_turn(
            server2._store, "conv-a", "default", "hi A", "hello A", root=ws_a
        )
        await _simulate_qa_turn(
            server2._store, "conv-b", "default", "hi B", "hello B", root=ws_b
        )

        # Verify physical layout
        assert (ws_a / ".modex" / "sessions" / "default" / "conv-a.default.jsonl").exists()
        assert (ws_b / ".modex" / "sessions" / "default" / "conv-b.default.jsonl").exists()

        # Query workspace A
        resp = await client2.get("/api/sessions", params={"ws": str(ws_a)})
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv-a.default" in sids, f"Expected conv-a.default in {sids}"
        assert "conv-b.default" not in sids, f"Expected conv-b.default NOT in {sids}"

        # Query workspace B
        resp = await client2.get("/api/sessions", params={"ws": str(ws_b)})
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv-a.default" not in sids, f"Expected conv-a.default NOT in {sids}"
        assert "conv-b.default" in sids, f"Expected conv-b.default in {sids}"

        # Query without ws parameter (should default to home)
        resp = await client2.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        # Both should be absent because home data_dir has no transcripts
        assert "conv-a.default" not in sids
        assert "conv-b.default" not in sids
    finally:
        await client2.close()


@pytest.mark.asyncio
async def test_sessions_filter_by_workspace_with_relative_path() -> None:
    """Relative ws paths are resolved against the home workspace."""
    home = Path(tempfile.mkdtemp())
    sub = home / "sub"
    sub.mkdir(parents=True, exist_ok=True)

    # Mock workspace control (its ``home`` anchors relative ``?ws=`` lookups).
    from unittest.mock import MagicMock

    ws_ctrl = MagicMock()
    ws_ctrl.home = home
    ws_ctrl.current = home

    # Pass the workspace ROOT so the server's home is home/.modex/sessions.
    server, _, routing_store = _make_server(home)
    routing_store.set_pool("conv-sub", "default")
    server.set_workspace_control(ws_ctrl)
    client = TestClient(TestServer(server.app))
    await client.start_server()

    try:
        # Write a transcript in the sub workspace; route via ctxvar binding.
        store_sub = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        await _simulate_qa_turn(
            store_sub, "conv-sub", "default", "hi sub", "hello sub", root=sub
        )

        # Query with relative path "sub" should resolve to home/sub
        resp = await client.get("/api/sessions", params={"ws": "sub"})
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv-sub.default" in sids, f"Expected conv-sub.default in {sids}"
    finally:
        await client.close()
