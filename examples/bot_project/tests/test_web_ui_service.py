"""Tests for WebUIService configuration wiring."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.media_store import WorkspaceScopedMediaStore
from bot.service.web_ui_service import WebUIService
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import _unwrap_envelope
from bot.webui.server import WebUIServer

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.multi_agent.pool_config.media import MediaConfig
from modex_agent.multi_agent.pool_router import PoolSessionStore
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.runtime import bind_workspace_root


@pytest.mark.asyncio
async def test_coder_session_transcript_written_to_coder_pool_directory() -> None:
    """End-to-end: a session created with pool=coder persists transcript under
    the coder pool directory, not main.

    Regression: when coder pool attribution was missing, the transcript
    dispatcher fell back to the main pool, so a
    coder session's transcript ended up at
    .modex/sessions/<ws>/main/<uuid>.orchestrator.jsonl instead of
    .modex/sessions/<ws>/coder/<uuid>.orchestrator.jsonl.

    Note: the coder pool's main agent is named `orchestrator` (the directory
    stays `coder/`, but `main_agent_name: orchestrator` in pool.yml overrides
    the default). Session IDs and transcript filenames carry the agent name,
    so the file is `<uuid>.orchestrator.jsonl` under the `coder/` pool dir.
    """
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()

    project_dir = Path(__file__).resolve().parent.parent

    # pool_name -> main_agent_name, mirroring WebUIService production wiring.
    from modex_agent.multi_agent.pool_config import PoolStore

    _pool_store = PoolStore(base_dir=project_dir)
    _pool_to_main_agent = {
        s.name: _pool_store.read_pool(s.name).main.agent_name
        for s in _pool_store.list_pools()
    }

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

    server = WebUIServer(
        input_adapter,
        store,
        static_dist=None,
        data_dir=data_dir,
        home_sessions_dir=WorkspacePaths(root=data_dir / ".modex").sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "orchestrator"])
    server.set_agent_resolver(
        lambda pool_name: _pool_to_main_agent.get(pool_name, pool_name)
    )
    pool_session_store = PoolSessionStore(data_dir / ".modex")
    server.set_pool_switch_callback(pool_session_store.set)
    server.set_pool_resolver(pool_session_store.get_pool)

    from bot.service.session_gc import SessionGarbageCollector, SessionGcConfig

    server.set_session_gc(
        SessionGarbageCollector(
            workspace_roots_provider=lambda: [data_dir],
            data_dir_name=".modex",
            config=SessionGcConfig(),
        )
    )

    from tests.webui._pipeline_fixture import attach_default_pipeline

    attach_default_pipeline(
        server,
        store,
        input_adapter,
        pool_session_store=pool_session_store,
        workspace_root=data_dir,
        available_pools=lambda: set(_pool_to_main_agent.keys()),
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        with bind_workspace_root(data_dir):
            # Create a coder-pool session via the API.
            resp = await client.post("/api/sessions", json={"pool": "coder"})
            assert resp.status == 200
            data = await resp.json()
            session_id: str = data["session_id"]
            assert data["pool"] == "coder"
            uuid_prefix = session_id.split(".")[0]

            # Send a message so the transcript is materialized on disk.
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": session_id})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            await ws.send_json(
                {
                    "action": "send_message",
                    "session_id": session_id,
                    "content": "hello coder",
                }
            )
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message", echoed

        # The transcript MUST live under the coder pool directory.
        # Filename suffix is the main agent name (orchestrator), not the pool name.
        expected_file = data_dir / ".modex" / "sessions" / "coder" / f"{uuid_prefix}.orchestrator.jsonl"
        assert expected_file.exists(), (
            f"orchestrator transcript not found at expected path {expected_file}"
        )

        # It MUST NOT have leaked into the main pool directory.
        wrong_file = data_dir / ".modex" / "sessions" / "main" / f"{uuid_prefix}.orchestrator.jsonl"
        assert not wrong_file.exists(), (
            f"orchestrator transcript leaked into main pool directory {wrong_file}"
        )

        # Deleting the session must remove the transcript from the coder dir.
        resp = await client.delete(f"/api/sessions/{session_id}")
        assert resp.status == 200
        assert not expected_file.exists()
    finally:
        await client.close()


# ── Resolver wiring regression tests ─────────────────────────────────────────


def test_resolver_fallback_default_is_importable_from_module() -> None:
    """The resolver closure in WebUIService.start() references
    ``_DEFAULT_AGENT_NAME`` as the fallback pool.  This name must be
    importable from the module — otherwise live emitters silently crash
    with NameError and streaming output stops."""
    import bot.service.web_ui_service as wuis

    # Direct access: if _DEFAULT_AGENT_NAME is not defined in the module,
    # this raises AttributeError → RED.
    assert wuis._DEFAULT_AGENT_NAME == "main"


@pytest.mark.asyncio
async def test_production_style_resolver_does_not_crash_emitter() -> None:
    """Regression: the resolver wired in WebUIService.start() must not raise
    a NameError (missing _DEFAULT_AGENT_NAME import) or any other exception.

    The resolver is the module-level _session_meta_resolver in
    register_websocket, set via set_session_meta_resolver(). It is called
    lazily by every WebBotEmitter._send_event(). If it crashes, the emitter
    silently stops — no streaming output reaches the frontend.
    """
    from bot.adapters.register_websocket import set_session_meta_resolver
    from bot.webui.events import SessionMeta

    from modex_agent.core.session_id import session_id_prefix_of

    pool_by_prefix: dict[str, str] = {"conv": "coder"}

    def _resolve_session_meta(session_id: str) -> SessionMeta:
        pool = pool_by_prefix.get(session_id_prefix_of(session_id), "main")
        return SessionMeta(pool=pool, parent_session_id=None)

    set_session_meta_resolver(_resolve_session_meta)

    # Create an emitter with the resolver wired through the normal factory
    # closure path (register_websocket._resolve_meta_for → global resolver).
    from bot.adapters.register_websocket import _resolve_meta_for
    from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
    from bot.webui.emitter import WebBotEmitter

    from modex_agent.core.emitter import EmitterConfig

    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("conv.coder", None)

    emitter = WebBotEmitter(
        output_adapter=output_adapter,
        session_id="conv.coder",
        config=EmitterConfig(),
        session_meta_resolver=_resolve_meta_for("conv.coder"),
    )
    # Fire a content delta — must not raise.
    await emitter.emit_delta("hello world")

    # The envelope must have reached the delta queue.
    q = input_adapter._delta_queues.get("conv.coder")
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == "model_content_delta"
    assert envelope.pool == "coder"  # resolved from the map
    assert envelope.session_id == "conv.coder"


class TestAttachmentWiring:
    """Regression guards for the production media-wiring seam (ADR-0013).

    The inbound attachment pipeline was once dead in production because
    ``media_store`` / the per-pool ``MediaConfig`` resolver were never passed
    into ``BotInputContext`` (the ingest stage silently no-op'd on
    ``ctx.media_store is None``). These tests call the extracted
    ``_build_input_context`` / ``_media_config_for_pool`` directly so a future
    refactor that drops the wiring line cannot pass undetected.
    """

    @staticmethod
    def _fake_service() -> WebUIService:
        """A WebUIService built without __init__ (heavy: reads config/.env),
        carrying only the attrs the wiring methods read. Using the real class
        lets ``self._media_config_for_pool`` resolve to the actual method."""
        custom = MediaConfig(max_image_bytes=999, max_text_doc_bytes=888)
        from modex_agent.multi_agent.pool_instance import PoolInstance

        pool = PoolInstance(
            name="main",
            media=custom,
            subagent_count=0,
            pool=None,
            broker_bridge=None,
            tool_manager=None,
            skill_manager=None,
            mcp_manager=None,
            terminal_manager=None,
            main_agent_name="main",
            main_execution_strategy=ExecutionStrategyKind.REACT,
            provider=None,
            notification_service=None,
            communication_service=None,
            tree_manager=MagicMock(),
            target_store=None,
        )
        svc = WebUIService.__new__(WebUIService)
        svc._pools = {"main": pool}
        svc._default_pool = "main"
        svc._media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        svc._pool_session_store = SimpleNamespace()  # truthy -> skip pool_router branch
        svc._transcript_store = SimpleNamespace()
        svc._session_factory = SimpleNamespace()
        svc._model_choice_registry = None
        return svc

    def test_media_config_for_pool_returns_pool_override_and_default(self) -> None:
        svc = self._fake_service()
        assert svc._media_config_for_pool("main").max_image_bytes == 999
        # Unknown pool degrades to the frozen default.
        assert svc._media_config_for_pool("missing") == MediaConfig()

    def test_build_input_context_wires_media_store_and_per_pool_resolver(self) -> None:
        svc = self._fake_service()
        inp = SimpleNamespace(name="ws", put_input_message=lambda *_a, **_k: None)
        ctx = svc._build_input_context(inp, agent_resolver=lambda p: p)
        # The wired store is the service singleton, not None (the dead-path bug).
        assert ctx.media_store is svc._media_store
        # The per-pool resolver is wired and honors PoolAssemblyDeps.media.
        assert ctx.media_config_for("main").max_text_doc_bytes == 888
        assert ctx.media_config_for("nope") == MediaConfig()


# ── Cross-pool same-name subagent transcript partitioning ────────────────────


@pytest.mark.asyncio
async def test_cross_pool_same_name_subagent_transcript_partitioning() -> None:
    """Two pools (coder, review) both have an `explore` subagent.

    A session routed to the `review` pool must persist its transcript under
    ``sessions/review/``, NOT under ``sessions/coder/`` — even though the
    agent_name segment of the session_id is identical (`explore`) in both
    pools.

    This is the original bug: ``_agent_pool_map["explore"]`` was overwritten
    by the last pool iterated, so all `explore` sessions were misattributed.
    The fix: pool is carried by the request / resolved from PoolSessionStore,
    never inferred from agent_name.

    This test exercises the REAL production write path:
    - WS attach with explicit pool → PoolSessionStore persists the route
    - WS send_message → S5 stamps RESOLVED_POOL → S7 persists with pool=
    - No test wrapper injects pool — the production code must carry it.
    """
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()

    project_dir = Path(__file__).resolve().parent.parent

    from modex_agent.multi_agent.pool_config import PoolStore

    _pool_store = PoolStore(base_dir=project_dir)
    _pool_to_main_agent = {
        s.name: _pool_store.read_pool(s.name).main.agent_name
        for s in _pool_store.list_pools()
    }

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

    server = WebUIServer(
        input_adapter,
        store,
        static_dist=None,
        data_dir=data_dir,
        home_sessions_dir=WorkspacePaths(root=data_dir / ".modex").sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "orchestrator", "reviewer"])
    server.set_agent_resolver(
        lambda pool_name: _pool_to_main_agent.get(pool_name, pool_name)
    )
    pool_session_store = PoolSessionStore(data_dir / ".modex")
    server.set_pool_switch_callback(pool_session_store.set)
    server.set_pool_resolver(pool_session_store.get_pool)

    from bot.service.session_gc import SessionGarbageCollector, SessionGcConfig

    server.set_session_gc(
        SessionGarbageCollector(
            workspace_roots_provider=lambda: [data_dir],
            data_dir_name=".modex",
            config=SessionGcConfig(),
        )
    )

    from tests.webui._pipeline_fixture import attach_default_pipeline

    attach_default_pipeline(
        server,
        store,
        input_adapter,
        pool_session_store=pool_session_store,
        workspace_root=data_dir,
        available_pools=lambda: set(_pool_to_main_agent.keys()),
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        with bind_workspace_root(data_dir):
            # Create a review-pool session via the API.
            resp = await client.post("/api/sessions", json={"pool": "review"})
            assert resp.status == 200
            data = await resp.json()
            session_id: str = data["session_id"]
            assert data["pool"] == "review"
            uuid_prefix = session_id.split(".")[0]

            # Attach + send a message to trigger transcript persistence.
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": session_id})
            attached_raw = await ws.receive_json()
            assert attached_raw["event_type"] == "attached"
            assert attached_raw["pool"] == "review"

            await ws.send_json(
                {
                    "action": "send_message",
                    "session_id": session_id,
                    "content": "review pool message",
                    "pool": "review",
                }
            )
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message", echoed

        # The transcript MUST live under the review pool directory.
        expected_file = data_dir / ".modex" / "sessions" / "review" / f"{uuid_prefix}.reviewer.jsonl"
        assert expected_file.exists(), (
            f"reviewer transcript not found at expected path {expected_file}; "
            f"available dirs: {list((data_dir / '.modex' / 'sessions').iterdir())}"
        )

        # It MUST NOT have leaked into the coder pool directory.
        wrong_coder_file = data_dir / ".modex" / "sessions" / "coder" / f"{uuid_prefix}.reviewer.jsonl"
        assert not wrong_coder_file.exists(), (
            f"reviewer transcript leaked into coder pool directory {wrong_coder_file}"
        )

        # It MUST NOT have leaked into the main pool directory either.
        wrong_main_file = data_dir / ".modex" / "sessions" / "main" / f"{uuid_prefix}.reviewer.jsonl"
        assert not wrong_main_file.exists(), (
            f"reviewer transcript leaked into main pool directory {wrong_main_file}"
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cross_pool_same_name_subagent_emitter_partitioning() -> None:
    """Emitter writes for a review-pool session must land under review/, not coder/.

    Exercises the WebBotEmitter._persist() path — the emitter reads pool from
    SessionMeta (resolved via PoolSessionStore) and passes it to
    transcript_store.append(). Before the fix, _persist() did NOT pass pool,
    so the store defaulted to _DEFAULT_POOL ("main") for every non-main write.
    """
    from bot.adapters.web_socket import WebSocketOutputAdapter
    from bot.webui.emitter import WebBotEmitter
    from bot.webui.events import SessionMeta, UserMessageEvent
    from modex_agent.core.emitter import EmitterConfig
    from modex_agent.core.session_id import session_id_prefix_of

    data_dir = Path(tempfile.mkdtemp())
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    pool_session_store = PoolSessionStore(data_dir / ".modex")

    # Simulate: a review-pool session with prefix "rev_conv"
    session_prefix = "rev_conv"
    session_id = f"{session_prefix}.reviewer"
    pool_session_store.set(session_prefix, "review")

    # Wire the SessionMeta resolver the same way WebUIService.start() does
    def _resolve_session_meta() -> SessionMeta:
        prefix = session_id_prefix_of(session_id)
        pool = pool_session_store.get_pool(prefix) or "main"
        return SessionMeta(pool=pool, parent_session_id=None)

    from bot.adapters.register_websocket import set_session_meta_resolver

    set_session_meta_resolver(_resolve_session_meta)

    output = MagicMock()
    output.send_envelope = AsyncMock()

    with bind_workspace_root(data_dir):
        emitter = WebBotEmitter(
            output_adapter=output,
            session_id=session_id,
            config=EmitterConfig(),
            transcript_store=store,
            session_meta_resolver=_resolve_session_meta,
            sessions_dir_provider=lambda: WorkspacePaths(root=data_dir / ".modex").sessions_dir,
        )

        # Emit a content delta + stream end to trigger _persist()
        await emitter.emit_delta("hello from review")
        await emitter.emit_stream_end(resuming=False)

    # The transcript file MUST be under sessions/review/
    review_file = data_dir / ".modex" / "sessions" / "review" / f"{session_id}.jsonl"
    assert review_file.exists(), (
        f"Emitter transcript not found at {review_file}; "
        f"available: {list((data_dir / '.modex' / 'sessions').iterdir()) if (data_dir / '.modex' / 'sessions').exists() else 'no sessions dir'}"
    )

    # MUST NOT be under sessions/main/ (the old default-path bug)
    main_file = data_dir / ".modex" / "sessions" / "main" / f"{session_id}.jsonl"
    assert not main_file.exists(), (
        f"Emitter transcript leaked into main pool directory {main_file}"
    )

    # MUST NOT be under sessions/coder/ (the cross-pool same-name bug)
    coder_file = data_dir / ".modex" / "sessions" / "coder" / f"{session_id}.jsonl"
    assert not coder_file.exists(), (
        f"Emitter transcript leaked into coder pool directory {coder_file}"
    )
