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


def _real_pool_to_main_agent() -> dict[str, str]:
    """Build pool_name -> main_agent_name from the real project config.

    Mirrors WebUIService production wiring
    (_agent_map = {name: pi.root_agent_name for ...}). The coder pool's
    root agent is `orchestrator` (a declared name that differs from the
    pool key); the pool key is the pool identity, not the agent name.
    """
    from modex_agent.scope.loader import load_scope_declaration

    project_dir = _real_project_dir()
    spec = load_scope_declaration(project_dir / "config" / "scopes" / "bot.yml")
    pools = spec.workspace.pools if spec.workspace is not None else []
    return {pool.name: pool.root_agent.name for pool in pools}


def _make_server(
    data_dir: Path,
) -> tuple[WebUIServer, WebSocketInputAdapter, PoolSessionStore]:
    """Create a fully wired WebUIServer with the real production pool map.

    ``data_dir`` is treated as the workspace root: writes (routed by the bound
    ctxvar root) land at ``<data_dir>/.modex/sessions/<pool>/`` and the server
    reads from that same directory via ``home_sessions_dir``.
    """
    inp = WebSocketInputAdapter()
    pool_to_main_agent = _real_pool_to_main_agent()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        data_dir=data_dir,
        home_sessions_dir=WorkspacePaths(root=data_dir / ".modex").sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(".modex")
    server.set_pool_agent_names(list(pool_to_main_agent.values()))
    server.set_agent_resolver(
        lambda pool_name: pool_to_main_agent.get(pool_name, pool_name)
    )
    routing_store = PoolSessionStore(data_dir=data_dir)
    server.set_pool_switch_callback(routing_store.set_pool)
    server.set_pool_resolver(routing_store.get_pool)
    # Inject session store + factory so POST /api/sessions auto-saves.
    session_store = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: next(
            (
                pool
                for pool, main_agent in pool_to_main_agent.items()
                if main_agent == s.agent_name
            ),
            "default",
        ),
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
    workspace_root: Path,
    pool: str,
) -> None:
    """Materialise one Q/A turn by writing user + assistant events.

    Writes route by the bound ctxvar root, so the caller passes the workspace
    root whose ``<root>/.modex/sessions`` should own this turn.
    """
    session_id = f"{conv_prefix}.{agent_name}"
    with bind_workspace_root(workspace_root):
        await store.append(
            session_id,
            UserMessageEvent(
                session_id=session_id,
                agent_name=agent_name,
                content=user_content,
            ),
            pool=pool,
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
            pool=pool,
        )


@pytest.mark.asyncio
async def test_pool_filter_hides_and_shows_sessions() -> None:
    """GET /api/sessions?pool=X only returns sessions whose agent maps to pool X."""
    data_dir = Path(tempfile.mkdtemp())
    server, _, routing_store = _make_server(data_dir)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Write transcripts directly (no session store entries — test transcript fallback).
        routing_store.set_pool("conv1", "coder")
        routing_store.set_pool("conv2", "default")
        await _simulate_qa_turn(server._store, "conv1", "orchestrator", "hi", "hello", data_dir, "coder")
        await _simulate_qa_turn(server._store, "conv2", "default", "hi", "hello", data_dir, "default")

        # Without pool filter, both sessions visible.
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv1.orchestrator" in sids
        assert "conv2.default" in sids

        # Filter to coder pool.
        resp = await client.get("/api/sessions?pool=coder")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv1.orchestrator" in sids
        assert "conv2.default" not in sids

        resp = await client.get("/api/sessions?pool=default")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert "conv1.orchestrator" not in sids
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

    server, _, _ = _make_server(data_dir_a)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={"pool": "default"})
        assert resp.status == 200
        session_a = await resp.json()
        sid_a: str = session_a["session_id"]
        conv_a = sid_a.split(".")[0]
        await _simulate_qa_turn(server._store, conv_a, "default", "hi A", "hello A", data_dir_a, "default")

        # Verify workspace A transcript exists
        assert (data_dir_a / ".modex" / "sessions" / "default" / f"{sid_a}.jsonl").exists()

        # Switch to workspace B (route this turn's writes to data_dir_b).
        resp = await client.post("/api/sessions", json={"pool": "default"})
        assert resp.status == 200
        session_b = await resp.json()
        sid_b: str = session_b["session_id"]
        conv_b = sid_b.split(".")[0]
        await _simulate_qa_turn(server._store, conv_b, "default", "hi B", "hello B", data_dir_b, "default")

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

    server, _, _ = _make_server(data_dir_a)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={"pool": "coder"})
        assert resp.status == 200
        coder_a = await resp.json()
        sid_a: str = coder_a["session_id"]
        conv_a = sid_a.split(".")[0]
        await _simulate_qa_turn(server._store, conv_a, "orchestrator", "hi", "hello", data_dir_a, "coder")

        # Switch to workspace B (route this turn's writes to data_dir_b).
        resp = await client.post("/api/sessions", json={"pool": "default"})
        assert resp.status == 200
        main_b = await resp.json()
        sid_b: str = main_b["session_id"]
        conv_b = sid_b.split(".")[0]
        await _simulate_qa_turn(server._store, conv_b, "default", "hi", "hello", data_dir_b, "default")

        # Verify physical isolation: each session is in its workspace dir.
        # Pool dir is `coder` (pool name); filename suffix is `orchestrator` (agent name).
        assert (data_dir_a / ".modex" / "sessions" / "coder" / f"{sid_a}.jsonl").exists()
        assert not (data_dir_b / ".modex" / "sessions" / "coder" / f"{sid_a}.jsonl").exists()
        assert (data_dir_b / ".modex" / "sessions" / "default" / f"{sid_b}.jsonl").exists()
        assert not (data_dir_a / ".modex" / "sessions" / "default" / f"{sid_b}.jsonl").exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sessions_list_includes_updated_at() -> None:
    """GET /api/sessions returns an updated_at timestamp for each session."""
    data_dir = Path(tempfile.mkdtemp())
    server, _, _ = _make_server(data_dir)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={"pool": "default"})
        assert resp.status == 200
        session = await resp.json()
        sid: str = session["session_id"]
        conv = sid.split(".")[0]
        await _simulate_qa_turn(server._store, conv, "default", "hi", "hello", data_dir, "default")

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
