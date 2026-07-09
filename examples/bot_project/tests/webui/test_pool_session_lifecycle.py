from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.web_ui_service import WebUIService
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import AssistantTurnEvent, UserMessageEvent
from bot.webui.server import WebUIServer
from modex_agent.workspace.paths import WorkspacePaths

from modex_agent.core.session_id import SessionIdFactory
from modex_agent.workspace.runtime import bind_workspace_root


def _real_project_dir() -> Path:
    """Return the real bot project directory for config loading."""
    return Path(__file__).resolve().parent.parent.parent


def _real_agent_pool_map() -> dict[str, str]:
    """Build the production agent->pool mapping from the loaded AppConfig."""
    from modex_agent.ioc.configs.app import AppConfig

    project_dir = _real_project_dir()

    class _Source:
        _project_dir = project_dir
        _app_config = AppConfig.from_yaml(project_dir / "config" / "bot_config.yml")

    return WebUIService._build_agent_pool_map(_Source())


def _make_server(
    data_dir: Path,
) -> tuple[WebUIServer, WebSocketInputAdapter]:
    """Create a fully wired WebUIServer with the real production pool map.

    ``data_dir`` is treated as the workspace root: writes (routed by the bound
    ctxvar root) land at ``<data_dir>/.modex/sessions/<pool>/`` and the server
    reads from that same directory via ``home_sessions_dir``.
    """
    inp = WebSocketInputAdapter()
    mapping = _real_agent_pool_map()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    store.set_agent_pool_map(mapping)
    server = WebUIServer(
        inp, store, static_dist=None, data_dir=data_dir,
        home_sessions_dir=WorkspacePaths(root=data_dir / ".modex").sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(".modex")
    server.set_pool_agent_names(["default", "coding"])
    server.set_agent_pool_map(mapping)
    server.set_agent_resolver(lambda pool_name: mapping.get(pool_name, pool_name))
    # Inject session store + factory so POST /api/sessions auto-saves.
    session_store = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: mapping.get(s.agent_name, "default"),
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
    workspace_root: Path,
) -> None:
    """Materialise one Q/A turn by writing user + assistant events.

    Writes route by the bound ctxvar root, so the caller passes the workspace
    root whose ``<root>/.modex/sessions`` should own this turn.
    """
    session_id = f"{conv_prefix}.{agent_name}"
    with bind_workspace_root(workspace_root):
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
async def test_pool_filter_hides_and_shows_sessions() -> None:
    """GET /api/sessions?pool=X only returns sessions whose agent maps to pool X."""
    data_dir = Path(tempfile.mkdtemp())
    server, _ = _make_server(data_dir)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Write transcripts directly (no session store entries — test transcript fallback).
        await _simulate_qa_turn(server._store, "conv1", "coding", "hi", "hello", data_dir)
        await _simulate_qa_turn(server._store, "conv2", "default", "hi", "hello", data_dir)

        # Without pool filter, both sessions visible.
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv1.coding" in sids
        assert "conv2.default" in sids

        # Filter to coding pool.
        resp = await client.get("/api/sessions?pool=coding")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv1.coding" in sids
        assert "conv2.default" not in sids

        resp = await client.get("/api/sessions?pool=default")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv1.coding" not in sids
        assert "conv2.default" in sids
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_workspace_switch_hides_and_shows_sessions() -> None:
    """After switching workspace, new writes go to the new workspace directory.

    Writes are routed by the bound ctxvar root: the turn for workspace A binds
    data_dir_a, the turn for workspace B binds data_dir_b.
    """
    data_dir_a = Path(tempfile.mkdtemp())
    data_dir_b = Path(tempfile.mkdtemp())
    data_dir_a.mkdir(parents=True, exist_ok=True)
    data_dir_b.mkdir(parents=True, exist_ok=True)

    server, _ = _make_server(data_dir_a)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={"pool": "default"})
        assert resp.status == 200
        session_a = await resp.json()
        sid_a: str = session_a["session_id"]
        conv_a = sid_a.split(".")[0]
        await _simulate_qa_turn(server._store, conv_a, "default", "hi A", "hello A", data_dir_a)

        # Verify workspace A transcript exists
        assert (data_dir_a / ".modex" / "sessions" / "default" / f"{sid_a}.jsonl").exists()

        # Switch to workspace B (route this turn's writes to data_dir_b).
        resp = await client.post("/api/sessions", json={"pool": "default"})
        assert resp.status == 200
        session_b = await resp.json()
        sid_b: str = session_b["session_id"]
        conv_b = sid_b.split(".")[0]
        await _simulate_qa_turn(server._store, conv_b, "default", "hi B", "hello B", data_dir_b)

        # Verify workspace B transcript exists and A's is not in B
        assert (data_dir_b / ".modex" / "sessions" / "default" / f"{sid_b}.jsonl").exists()
        assert not (data_dir_b / ".modex" / "sessions" / "default" / f"{sid_a}.jsonl").exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pool_and_workspace_filter_combined() -> None:
    """End-to-end: pool filter works independently within each workspace.

    Writes are routed by the bound ctxvar root per workspace.
    """
    data_dir_a = Path(tempfile.mkdtemp())
    data_dir_b = Path(tempfile.mkdtemp())
    data_dir_a.mkdir(parents=True, exist_ok=True)
    data_dir_b.mkdir(parents=True, exist_ok=True)

    server, _ = _make_server(data_dir_a)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={"pool": "coding"})
        assert resp.status == 200
        coding_a = await resp.json()
        sid_a: str = coding_a["session_id"]
        conv_a = sid_a.split(".")[0]
        await _simulate_qa_turn(server._store, conv_a, "coding", "hi", "hello", data_dir_a)

        # Switch to workspace B (route this turn's writes to data_dir_b).
        resp = await client.post("/api/sessions", json={"pool": "default"})
        assert resp.status == 200
        main_b = await resp.json()
        sid_b: str = main_b["session_id"]
        conv_b = sid_b.split(".")[0]
        await _simulate_qa_turn(server._store, conv_b, "default", "hi", "hello", data_dir_b)

        # Verify physical isolation: each session is in its workspace dir
        assert (data_dir_a / ".modex" / "sessions" / "coding" / f"{sid_a}.jsonl").exists()
        assert not (data_dir_b / ".modex" / "sessions" / "coding" / f"{sid_a}.jsonl").exists()
        assert (data_dir_b / ".modex" / "sessions" / "default" / f"{sid_b}.jsonl").exists()
        assert not (data_dir_a / ".modex" / "sessions" / "default" / f"{sid_b}.jsonl").exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sessions_list_includes_updated_at() -> None:
    """GET /api/sessions returns an updated_at timestamp for each session."""
    data_dir = Path(tempfile.mkdtemp())
    server, _ = _make_server(data_dir)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={"pool": "default"})
        assert resp.status == 200
        session = await resp.json()
        sid: str = session["session_id"]
        conv = sid.split(".")[0]
        await _simulate_qa_turn(server._store, conv, "default", "hi", "hello", data_dir)

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {s["session_id"]: s for s in sessions}
        assert sid in by_sid
        updated_at = by_sid[sid].get("updated_at")
        assert isinstance(updated_at, int), f"updated_at must be int ms, got {updated_at!r}"
        assert updated_at > 0
    finally:
        await client.close()
