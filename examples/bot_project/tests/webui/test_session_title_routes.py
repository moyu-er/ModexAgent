"""PA-01/PA-02 backend tests — PATCH /api/sessions/{id}/title + sessions_changed.

Route-level tests against the REAL WebUIServer assembly (routes registered by
``WebUIServer._setup_routes`` -> ``register_sessions_routes``), wired with the
REAL runtime registry (the workspace's ``SessionRegistry``), never a bypass
store. GC title-cache consistency is covered here end-to-end: deletion through
the real ``SessionGarbageCollector`` must clean the runtime registry so a
later rename cannot resurrect the record.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.session_gc import (
    SessionGarbageCollector,
    SessionGcConfig,
)
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.session_title import SessionTitleOps
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer

from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence.session_registry import InMemorySessionRegistry
from modex_agent.workspace.paths import WorkspacePaths


class _StubResources:
    """Minimal stand-in for PoolWorkspaceResources carrying the title wiring."""

    def __init__(self, title_ops: SessionTitleOps) -> None:
        self.title_ops = title_ops
        self.session_registry = title_ops.registry
        self.pools = {}
        self.scope_declaration_path = None
        self.session_pool_index = None


def _bot_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (workspace_root, sessions_dir, index_dir) for a home-style layout."""
    paths = WorkspacePaths(root=tmp_path / ".modex")
    return tmp_path, paths.sessions_dir, paths.session_index_dir


async def _make_server(
    tmp_path: Path,
    *,
    register_gc: bool = False,
) -> tuple[WebUIServer, InMemorySessionRegistry, WorkspacePoolSessionStore, SessionGarbageCollector | None]:
    workspace_root, sessions_dir, index_dir = _bot_layout(tmp_path)
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        input_adapter,
        store,
        static_dist=None,
        home_sessions_dir=sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(".modex")

    session_store = WorkspacePoolSessionStore(
        index_dir,
        pool_resolver=lambda session: "main",
        data_dir_name=".modex",
    )
    server.set_session_store(session_store)
    registry = InMemorySessionRegistry(store=session_store)
    await registry.load_all()
    title_ops = SessionTitleOps(registry=registry, on_changed=lambda: server.notify_sessions_changed(""))
    # home ("") resolves to THIS workspace's resources, like the production
    # graph-workspace resolver does; other paths resolve to None.
    from bot.workspace.handle import PoolWorkspaceResources

    stub = cast(PoolWorkspaceResources, _StubResources(title_ops))

    def _resolver(ws_id: str) -> PoolWorkspaceResources | None:
        if not ws_id or Path(ws_id).resolve() == workspace_root.resolve():
            return stub
        return None

    server.set_graph_workspace_resolver(_resolver)
    async def _resources(root: Path) -> PoolWorkspaceResources | None:
        return _resolver(str(root))

    server.set_workspace_resources_provider(_resources)

    gc: SessionGarbageCollector | None = None
    if register_gc:
        async def _resolve_store(_index: Path) -> WorkspacePoolSessionStore:
            return session_store

        async def _registry_cleanup(ws_root: Path, session_id: str) -> None:
            if Path(ws_root).resolve() == workspace_root.resolve():
                await registry.cleanup(session_id)

        gc = SessionGarbageCollector(
            workspace_roots_provider=lambda: [workspace_root],
            data_dir_name=".modex",
            config=SessionGcConfig(enabled=False, max_workers=1),
            transcript_store=store,
            session_store_resolver=_resolve_store,
            session_pool_resolver=lambda session: "main",
            registry_cleanup=_registry_cleanup,
        )
        server.set_session_gc(gc)
    return server, registry, session_store, gc


async def test_cold_history_rename_loads_the_runtime_registry(tmp_path: Path) -> None:
    from bot.workspace.handle import PoolWorkspaceResources

    server, _, store, _ = await _make_server(tmp_path)
    await store.save(SessionInfo(session_id="cold.main", agent_name="main"))
    registry = InMemorySessionRegistry(store=store)
    assert await registry.get("cold.main") is None
    server.set_graph_workspace_resolver(None)

    async def materialize(root: Path) -> PoolWorkspaceResources:
        assert root == tmp_path.resolve()
        await registry.load_all()
        return cast(PoolWorkspaceResources, _StubResources(SessionTitleOps(registry=registry)))

    server.set_workspace_resources_provider(materialize)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.patch("/api/sessions/cold.main/title", json={"title": "History title"})
        assert response.status == 200
    for record in (await registry.get("cold.main"), await store.get("cold.main")):
        assert record is not None and record.metadata["title"] == "History title"


async def _seed_session(
    registry: InMemorySessionRegistry,
    session_id: str = "f827db2b9945.default",
    title: str | None = None,
) -> None:
    metadata: dict[str, object] = {"pool": "default", "channel": "websocket"}
    if title is not None:
        metadata["title"] = title
    await registry.register(
        SessionInfo(
            session_id=session_id,
            agent_name="default",
            created_at=1000,
            updated_at=2000,
            metadata=metadata,
        )
    )


@pytest.mark.asyncio
async def test_patch_title_roundtrip_on_real_assembly(tmp_path: Path) -> None:
    server, registry, _, _ = await _make_server(tmp_path)
    await _seed_session(registry)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.patch(
            "/api/sessions/f827db2b9945.default/title?pool=default",
            json={"title": "  杭州旅行规划  "},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["title"] == "杭州旅行规划"

        session = await registry.get("f827db2b9945.default")
        assert session is not None
        assert session.metadata["title"] == "杭州旅行规划"
        assert session.metadata["pool"] == "default"
        assert session.parent_session_id is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_patch_title_validation_400_and_missing_404(tmp_path: Path) -> None:
    server, registry, _, _ = await _make_server(tmp_path)
    await _seed_session(registry)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        for bad in ["   ", "a\nb", "x" * 81, ""]:
            resp = await client.patch(
                "/api/sessions/f827db2b9945.default/title",
                json={"title": bad},
            )
            assert resp.status == 400, bad
        # non-string title
        resp = await client.patch(
            "/api/sessions/f827db2b9945.default/title",
            json={"title": 42},
        )
        assert resp.status == 400
        # missing body
        resp = await client.patch("/api/sessions/f827db2b9945.default/title")
        assert resp.status == 400
        # missing session
        resp = await client.patch(
            "/api/sessions/nope.default/title",
            json={"title": "ok title"},
        )
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_patch_title_scoped_to_workspace_registry(tmp_path: Path) -> None:
    """A ws= param pointing at another workspace must not touch THIS registry."""
    server, registry, _, _ = await _make_server(tmp_path)
    await _seed_session(registry)
    other_root = tmp_path / "other-ws"
    other_root.mkdir()
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.patch(
            f"/api/sessions/f827db2b9945.default/title?ws={other_root}&pool=default",
            json={"title": "别的"},
        )
        assert resp.status in (404, 503)
        session = await registry.get("f827db2b9945.default")
        assert session is not None
        assert "title" not in session.metadata
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_then_rename_does_not_resurrect(tmp_path: Path) -> None:
    """GC deletion must clean the runtime registry; later PATCH returns 404."""
    server, registry, session_store, gc = await _make_server(tmp_path, register_gc=True)
    assert gc is not None
    await _seed_session(registry, title="旧标题")
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.delete("/api/sessions/f827db2b9945.default?pool=default")
        assert resp.status == 200
        # runtime cache is gone AND the store row is gone
        assert await registry.get("f827db2b9945.default") is None
        assert await session_store.get("f827db2b9945.default") is None
        # rename after delete -> 404, never resurrect
        resp = await client.patch(
            "/api/sessions/f827db2b9945.default/title",
            json={"title": "复活"},
        )
        assert resp.status == 404
        assert await registry.get("f827db2b9945.default") is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_active_delete_rejection_keeps_registry(tmp_path: Path) -> None:
    """When the liveness gate blocks deletion, the registry cache stays intact."""
    from bot.service.liveness import LivenessProvider

    class _ActiveLiveness(LivenessProvider):
        async def try_reserve_deletion(self, session_id: str, workspace_root: Path) -> bool:
            return True

        async def is_session_active(self, session_id: str, workspace_root: Path) -> bool:
            return True

        async def release_deletion(self, session_id: str) -> None:
            return None

    workspace_root, _, _ = _bot_layout(tmp_path)
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    _, sessions_dir, index_dir = _bot_layout(tmp_path)
    server = WebUIServer(
        input_adapter, store, static_dist=None, home_sessions_dir=sessions_dir
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(".modex")
    session_store = WorkspacePoolSessionStore(
        index_dir, pool_resolver=lambda session: "main", data_dir_name=".modex"
    )
    server.set_session_store(session_store)
    registry = InMemorySessionRegistry(store=session_store)
    await registry.load_all()
    title_ops = SessionTitleOps(registry=registry)
    from bot.workspace.handle import PoolWorkspaceResources

    server.set_graph_workspace_resolver(
        lambda ws_id: cast(PoolWorkspaceResources, _StubResources(title_ops))
    )
    await _seed_session(registry, title="活跃标题")

    async def _resolve(index: Path):
        return session_store

    async def _registry_cleanup(ws_root: Path, session_id: str) -> None:
        await registry.cleanup(session_id)

    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [workspace_root],
        data_dir_name=".modex",
        config=SessionGcConfig(enabled=False, max_workers=1),
        transcript_store=store,
        session_store_resolver=_resolve,
        session_pool_resolver=lambda session: "main",
        liveness_provider=_ActiveLiveness(),
        registry_cleanup=_registry_cleanup,
    )
    server.set_session_gc(gc)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.delete("/api/sessions/f827db2b9945.default?pool=default")
        assert resp.status == 409
        # rejected deletion must NOT clean the cache
        session = await registry.get("f827db2b9945.default")
        assert session is not None
        assert session.metadata.get("title") == "活跃标题"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_patch_title_broadcasts_sessions_changed_control_message(tmp_path: Path) -> None:
    """PA-02: a successful save emits {type: sessions_changed, workspace} on WS."""
    server, registry, _, _ = await _make_server(tmp_path)
    await _seed_session(registry)
    notified: list[dict[str, object]] = []
    server.set_sessions_changed_notifier(
        lambda ws_raw: notified.append({"ws": ws_raw})
    )
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.patch(
            "/api/sessions/f827db2b9945.default/title?pool=default",
            json={"title": "新标题"},
        )
        assert resp.status == 200
        # home request (no ws param) notifies with the empty home ws value
        assert notified == [{"ws": ""}]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_sessions_changed_reaches_attached_connections(tmp_path: Path) -> None:
    """The sessions_changed control message fans out through the real WS layer."""
    server, registry, _, _ = await _make_server(tmp_path)
    await _seed_session(registry)
    # broadcast through the server's real WS broadcast seam
    server.set_sessions_changed_notifier(server._broadcast_sessions_changed)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "session_id": "f827db2b9945.default"})
        # consume the attached ack first
        _ = await ws.receive_json(timeout=2)
        resp = await client.patch(
            "/api/sessions/f827db2b9945.default/title?pool=default",
            json={"title": "标题"},
        )
        assert resp.status == 200
        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "sessions_changed"
        assert msg["workspace"] == str(tmp_path.resolve())
        await ws.close()
    finally:
        await client.close()


async def test_service_gc_callback_cleans_real_registry_and_joins_task(tmp_path: Path) -> None:
    """Exercise the production callback, not a test-local imitation of it."""
    import asyncio
    from types import SimpleNamespace

    from bot.service.web_ui_service import WebUIService

    server, registry, store, _ = await _make_server(tmp_path)
    await _seed_session(registry)
    assert server._graph_workspace_resolver is not None
    resources = server._graph_workspace_resolver("")
    assert resources is not None
    resources.target = tmp_path
    started = asyncio.Event()
    exited = asyncio.Event()

    async def naming() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    task = asyncio.create_task(naming())
    assert resources.title_ops is not None
    resources.title_ops.bind_naming_task("default", "f827db2b9945.default", task)
    await started.wait()
    service = SimpleNamespace(_home_resources=resources, workspace_stack=None)
    try:
        await WebUIService._registry_cleanup_for_gc(cast(WebUIService, service), tmp_path, "f827db2b9945.default")
        assert exited.is_set()
        assert await registry.get("f827db2b9945.default") is None
        assert await store.get("f827db2b9945.default") is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_create_then_rename_before_first_turn(tmp_path: Path) -> None:
    from modex_agent.core.session_id import SessionIdFactory

    server, registry, _, _ = await _make_server(tmp_path)
    server.set_session_factory(SessionIdFactory())
    server.set_available_pools_provider(lambda: {"default"})
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        created = await client.post("/api/sessions", json={"pool": "default"})
        assert created.status == 200
        session_id = (await created.json())["session_id"]
        renamed = await client.patch(f"/api/sessions/{session_id}/title", json={"title": "Before speaking"})
        assert renamed.status == 200
        session = await registry.get(session_id)
        assert session is not None
        assert session.metadata["title"] == "Before speaking"
    finally:
        await client.close()


async def test_rename_rejects_mismatched_pool_without_mutation(tmp_path: Path) -> None:
    server, registry, _, _ = await _make_server(tmp_path)
    await _seed_session(registry, title="Keep")
    server.set_pool_resolver(lambda prefix: "default")
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.patch("/api/sessions/f827db2b9945.default/title?pool=other", json={"title": "Wrong pool"})
        assert response.status == 404
        session = await registry.get("f827db2b9945.default")
        assert session is not None and session.metadata["title"] == "Keep"
    finally:
        await client.close()
