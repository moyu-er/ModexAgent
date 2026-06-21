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
from bot.service.web_ui_service import WebUIService
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import AssistantTurnEvent, UserMessageEvent
from bot.webui.server import WebUIServer
from framework.workspace.paths import WorkspacePaths
from framework.core.session_id import SessionIdFactory
from framework.workspace.runtime import bind_workspace_root


def _real_project_dir() -> Path:
    """Return the real bot project directory for config loading."""
    return Path(__file__).resolve().parent.parent.parent


def _real_agent_pool_map() -> dict[str, str]:
    """Build the production agent->pool mapping from the loaded AppConfig."""
    from framework.ioc.configs.app import AppConfig

    project_dir = _real_project_dir()

    class _Source:
        _project_dir = project_dir
        _app_config = AppConfig.from_yaml(project_dir / "config" / "bot_config.yml")

    return WebUIService._build_agent_pool_map(_Source())


def _make_server(
    data_dir: Path,
) -> tuple[WebUIServer, WebSocketInputAdapter]:
    """Create a fully wired WebUIServer with the real production pool map.

    ``data_dir`` is the workspace ROOT whose ``.modex/sessions`` becomes the
    server's ``home_sessions_dir`` (read when no ``?ws=`` is given).
    """
    inp = WebSocketInputAdapter()
    mapping = _real_agent_pool_map()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    store.set_agent_pool_map(mapping)
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
    server.set_pool_agent_names(["main", "coding"])
    server.set_agent_pool_map(mapping)
    server.set_agent_resolver(lambda pool_name: mapping.get(pool_name, pool_name))
    # Inject session store + factory so POST /api/sessions auto-saves.
    session_store = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: mapping.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)
    server.set_session_factory(SessionIdFactory())
    return server, inp


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
        store.append(
            session_id,
            UserMessageEvent(
                session_id=session_id,
                agent_name=agent_name,
                content=user_content,
            ),
        )
        store.append(
            session_id,
            AssistantTurnEvent(
                session_id=session_id,
                agent_name=agent_name,
                turn_id="turn-1",
                blocks=[{"type": "text", "text": assistant_content}],
                latency_ms=0,
            ),
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

    server2, _ = _make_server(data_dir)
    client2 = TestClient(TestServer(server2.app))
    await client2.start_server()

    try:
        # Writes route by the bound workspace root (ctxvar), not a resolver.
        await _simulate_qa_turn(
            server2._store, "conv-a", "main", "hi A", "hello A", root=ws_a
        )
        await _simulate_qa_turn(
            server2._store, "conv-b", "main", "hi B", "hello B", root=ws_b
        )

        # Verify physical layout
        assert (ws_a / ".modex" / "sessions" / "main" / "conv-a.main.jsonl").exists()
        assert (ws_b / ".modex" / "sessions" / "main" / "conv-b.main.jsonl").exists()

        # Query workspace A
        resp = await client2.get("/api/sessions", params={"ws": str(ws_a)})
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv-a.main" in sids, f"Expected conv-a.main in {sids}"
        assert "conv-b.main" not in sids, f"Expected conv-b.main NOT in {sids}"

        # Query workspace B
        resp = await client2.get("/api/sessions", params={"ws": str(ws_b)})
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv-a.main" not in sids, f"Expected conv-a.main NOT in {sids}"
        assert "conv-b.main" in sids, f"Expected conv-b.main in {sids}"

        # Query without ws parameter (should default to home)
        resp = await client2.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        # Both should be absent because home data_dir has no transcripts
        assert "conv-a.main" not in sids
        assert "conv-b.main" not in sids
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
    server, _ = _make_server(home)
    server.set_workspace_control(ws_ctrl)
    client = TestClient(TestServer(server.app))
    await client.start_server()

    try:
        # Write a transcript in the sub workspace; route via ctxvar binding.
        store_sub = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        store_sub.set_agent_pool_map(_real_agent_pool_map())
        await _simulate_qa_turn(
            store_sub, "conv-sub", "main", "hi sub", "hello sub", root=sub
        )

        # Query with relative path "sub" should resolve to home/sub
        resp = await client.get("/api/sessions", params={"ws": "sub"})
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv-sub.main" in sids, f"Expected conv-sub.main in {sids}"
    finally:
        await client.close()
