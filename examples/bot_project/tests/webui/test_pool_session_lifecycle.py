"""End-to-end lifecycle tests for workspace + pool session isolation.

These tests simulate real user flows through the WebUI REST API and verify
that session lists are correctly filtered by pool and workspace.  They do not
invoke any LLM; Q/A turns are materialized by writing transcript events
directly to the shared store.
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
from bot.webui.events import AssistantTurnEvent, UserMessageEvent
from bot.webui.server import WebUIServer


def _real_project_dir() -> Path:
    """Return the real bot project directory for config loading."""
    return Path(__file__).resolve().parent.parent.parent


def _real_agent_pool_map() -> dict[str, str]:
    """Build the production agent->pool mapping from disk."""

    class _Source:
        _project_dir = _real_project_dir()

    return WebUIService._build_agent_pool_map(_Source())


def _make_server(
    data_dir: Path,
    workspace_resolver: callable = lambda: "",
) -> tuple[WebUIServer, WebSocketInputAdapter]:
    """Create a fully wired WebUIServer with the real production pool map."""
    inp = WebSocketInputAdapter()
    mapping = _real_agent_pool_map()
    store = WorkspaceScopedTranscriptStore(data_dir, workspace_resolver)
    store.set_agent_pool_map(mapping)
    server = WebUIServer(inp, store, static_dist=None, data_dir=data_dir)
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])
    server.set_agent_pool_map(mapping)
    server.set_agent_resolver(lambda pool_name: mapping.get(pool_name, pool_name))
    return server, inp


async def _simulate_qa_turn(
    store: WorkspaceScopedTranscriptStore,
    conv_id: str,
    agent_name: str,
    question: str,
    answer: str,
) -> None:
    """Persist one user question + one assistant answer for *conv_id*."""
    session_id = f"{conv_id}.{agent_name}"
    store.append(
        session_id,
        UserMessageEvent(
            session_id=session_id,
            agent_name=agent_name,
            content=question,
        ),
    )
    store.append(
        session_id,
        AssistantTurnEvent(
            session_id=session_id,
            agent_name=agent_name,
            blocks=[{"kind": "text", "text": answer}],
            turn_id="turn_1",
            latency_ms=0,
        ),
    )


@pytest.mark.asyncio
async def test_pool_filter_hides_and_shows_sessions() -> None:
    """End-to-end: switching the active pool filters the session list.

    Flow:
      1. Create a session in the coding pool and simulate a Q/A turn.
      2. Create a session in the main pool and simulate a Q/A turn.
      3. GET /api/sessions?pool=main  → only the main session.
      4. GET /api/sessions?pool=coding → only the coding session.
      5. GET /api/sessions            → both sessions.
    """
    data_dir = Path(tempfile.mkdtemp())
    server, _ = _make_server(data_dir)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Step 1: coding pool session with transcript.
        resp = await client.post("/api/sessions", json={"pool": "coding"})
        assert resp.status == 200
        coding = await resp.json()
        coding_sid: str = coding["session_id"]
        coding_conv = coding_sid.split(".")[0]
        await _simulate_qa_turn(
            server._store, coding_conv, "coding", "hi coding", "hello from coding"
        )

        # Step 2: main pool session with transcript.
        resp = await client.post("/api/sessions", json={"pool": "main"})
        assert resp.status == 200
        main = await resp.json()
        main_sid: str = main["session_id"]
        main_conv = main_sid.split(".")[0]
        await _simulate_qa_turn(
            server._store, main_conv, "main", "hi main", "hello from main"
        )

        # Step 3: main pool filter.
        resp = await client.get("/api/sessions?pool=main")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert main_sid in sids, "main session must be visible in main pool"
        assert coding_sid not in sids, "coding session must be hidden in main pool"

        # Step 4: coding pool filter.
        resp = await client.get("/api/sessions?pool=coding")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert coding_sid in sids, "coding session must be visible in coding pool"
        assert main_sid not in sids, "main session must be hidden in coding pool"

        # Step 5: no filter → both.
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert coding_sid in sids
        assert main_sid in sids
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_workspace_switch_hides_and_shows_sessions() -> None:
    """End-to-end: switching workspace changes the visible session list.

    In the new model each workspace has its own data dir, so sessions are
    physically separate.
    """
    data_dir_a = Path(tempfile.mkdtemp())
    data_dir_b = Path(tempfile.mkdtemp())

    ws_ctx = MagicMock()
    ws_ctx.current = Path("/ws-a")
    ws_ctx.home = Path("/ws-a")

    def _resolver() -> str:
        return str(ws_ctx.current)

    server, _ = _make_server(data_dir_a, workspace_resolver=_resolver)
    server.set_workspace_context(ws_ctx)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={"pool": "main"})
        assert resp.status == 200
        session_a = await resp.json()
        sid_a: str = session_a["session_id"]
        conv_a = sid_a.split(".")[0]
        await _simulate_qa_turn(server._store, conv_a, "main", "hi A", "hello A")

        # Switch to workspace B: recreate store with new data dir
        mapping = _real_agent_pool_map()
        server._store = WorkspaceScopedTranscriptStore(data_dir_b, _resolver)
        server._store.set_agent_pool_map(mapping)
        if server._workspace_index is not None:
            server.set_workspace_index(server._store)

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert sid_a not in sids, "workspace A session must be hidden in workspace B"

        resp = await client.post("/api/sessions", json={"pool": "main"})
        assert resp.status == 200
        session_b = await resp.json()
        sid_b: str = session_b["session_id"]
        conv_b = sid_b.split(".")[0]
        await _simulate_qa_turn(server._store, conv_b, "main", "hi B", "hello B")

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert sid_b in sids, "workspace B session must be visible"
        assert sid_a not in sids, "workspace A session must still be hidden"

        # Switch back to workspace A
        server._store = WorkspaceScopedTranscriptStore(data_dir_a, _resolver)
        server._store.set_agent_pool_map(mapping)
        if server._workspace_index is not None:
            server.set_workspace_index(server._store)

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert sid_a in sids, "workspace A session must reappear"
        assert sid_b not in sids, "workspace B session must be hidden"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pool_and_workspace_filter_combined() -> None:
    """End-to-end: pool filter works independently within each workspace."""
    data_dir_a = Path(tempfile.mkdtemp())
    data_dir_b = Path(tempfile.mkdtemp())

    ws_ctx = MagicMock()
    ws_ctx.current = Path("/ws-a")
    ws_ctx.home = Path("/ws-a")

    def _resolver() -> str:
        return str(ws_ctx.current)

    mapping = _real_agent_pool_map()
    server, _ = _make_server(data_dir_a, workspace_resolver=_resolver)
    server.set_workspace_context(ws_ctx)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={"pool": "coding"})
        assert resp.status == 200
        coding_a = await resp.json()
        sid_a: str = coding_a["session_id"]
        conv_a = sid_a.split(".")[0]
        await _simulate_qa_turn(server._store, conv_a, "coding", "hi", "hello")

        # Switch to workspace B
        server._store = WorkspaceScopedTranscriptStore(data_dir_b, _resolver)
        server._store.set_agent_pool_map(mapping)
        if server._workspace_index is not None:
            server.set_workspace_index(server._store)

        resp = await client.post("/api/sessions", json={"pool": "main"})
        assert resp.status == 200
        main_b = await resp.json()
        sid_b: str = main_b["session_id"]
        conv_b = sid_b.split(".")[0]
        await _simulate_qa_turn(server._store, conv_b, "main", "hi", "hello")

        resp = await client.get("/api/sessions?pool=coding")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert sid_a not in sids
        assert sid_b not in sids

        # Switch back to workspace A
        server._store = WorkspaceScopedTranscriptStore(data_dir_a, _resolver)
        server._store.set_agent_pool_map(mapping)
        if server._workspace_index is not None:
            server.set_workspace_index(server._store)

        resp = await client.get("/api/sessions?pool=coding")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert sid_a in sids
        assert sid_b not in sids
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
        resp = await client.post("/api/sessions", json={"pool": "main"})
        assert resp.status == 200
        session = await resp.json()
        sid: str = session["session_id"]
        conv = sid.split(".")[0]
        await _simulate_qa_turn(server._store, conv, "main", "hi", "hello")

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
