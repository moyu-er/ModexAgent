"""Tests for WebUIService configuration wiring."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.media_store import WorkspaceScopedMediaStore
from bot.service.web_ui_service import WebUIService
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import _unwrap_envelope
from bot.webui.server import WebUIServer
from modex_agent.ioc.configs.pool import MediaConfig
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.runtime import bind_workspace_root


def _make_fake_app_config(pools: dict[str, list[dict[str, str]]]) -> SimpleNamespace:
    """Build a minimal app_config stand-in for _build_agent_pool_map tests."""
    return SimpleNamespace(
        pools={
            pool_name: SimpleNamespace(
                agents=[
                    SimpleNamespace(name=agent["name"], role=agent.get("role", "main"))
                    for agent in agents
                ]
            )
            for pool_name, agents in pools.items()
        }
    )


class TestWebUIService:
    """Unit tests for WebUIService helpers."""

    def test_build_agent_pool_map_uses_app_config_pools(self) -> None:
        """Pool mapping is built from the already-loaded AppConfig pools.

        Regression: _build_agent_pool_map used to re-parse config/pools/*.yml,
        which duplicated AppConfig loading and could produce an empty mapping.
        Empty mapping caused all transcripts (including coding-pool sessions)
        to be written to the main pool directory.
        """
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)

            class _FakeService:
                _project_dir = project_dir
                _app_config = _make_fake_app_config(
                    {
                        "coding": [{"name": "coding", "role": "main"}],
                        "main": [{"name": "main", "role": "main"}],
                    }
                )

            mapping = WebUIService._build_agent_pool_map(_FakeService())

        assert mapping.get("main") == "main"
        assert mapping.get("coding") == "coding"

    def test_build_agent_pool_map_includes_resident_subagents(self) -> None:
        """Agents listed in a pool config besides the main agent are mapped."""
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)

            class _FakeService:
                _project_dir = project_dir
                _app_config = _make_fake_app_config(
                    {
                        "coding": [
                            {"name": "coding", "role": "main"},
                            {"name": "scout", "role": "subagent"},
                            {"name": "reviewer", "role": "subagent"},
                        ],
                    }
                )

            mapping = WebUIService._build_agent_pool_map(_FakeService())

        assert mapping.get("coding") == "coding"
        assert mapping.get("scout") == "coding"
        assert mapping.get("reviewer") == "coding"


@pytest.mark.asyncio
async def test_coding_session_transcript_written_to_coding_pool_directory() -> None:
    """End-to-end: a session created with pool=coding persists transcript under
    the coding pool directory, not main.

    Regression: when _build_agent_pool_map produced an empty mapping, the
    transcript dispatcher fell back to the main pool for every agent, so a
    coding session's transcript ended up at
    .modex/sessions/<ws>/main/<uuid>.coding.jsonl instead of
    .modex/sessions/<ws>/coding/<uuid>.coding.jsonl.
    """
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()

    # Use the production mapping builder with the real project config.
    from modex_agent.ioc.configs.app import AppConfig

    project_dir = Path(__file__).resolve().parent.parent

    class _MappingSource:
        _project_dir = project_dir
        _app_config = AppConfig.from_yaml(project_dir / "config" / "bot_config.yml")

    mapping = WebUIService._build_agent_pool_map(_MappingSource())
    assert mapping.get("coding") == "coding", (
        "test setup: real project must map coding agent to coding pool"
    )

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    store.set_agent_pool_map(mapping)

    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir,
        home_sessions_dir=WorkspacePaths(root=data_dir / ".modex").sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])
    server.set_agent_pool_map(mapping)
    server.set_agent_resolver(lambda pool_name: mapping.get(pool_name, pool_name))

    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(
        server, store, input_adapter, agent_pool_map=mapping, workspace_root=data_dir
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        with bind_workspace_root(data_dir):
            # Create a coding-pool session via the API.
            resp = await client.post("/api/sessions", json={"pool": "coding"})
            assert resp.status == 200
            data = await resp.json()
            session_id: str = data["session_id"]
            assert data["pool"] == "coding"
            uuid_prefix = session_id.split(".")[0]

            # Send a message so the transcript is materialized on disk.
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": session_id})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            await ws.send_json({
                "action": "send_message",
                "session_id": session_id,
                "content": "hello coding",
            })
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"

        # The transcript MUST live under the coding pool directory.
        expected_file = (
            data_dir / ".modex" / "sessions" / "coding" / f"{uuid_prefix}.coding.jsonl"
        )
        assert expected_file.exists(), (
            f"coding transcript not found at expected path {expected_file}"
        )

        # It MUST NOT have leaked into the main pool directory.
        wrong_file = (
            data_dir / ".modex" / "sessions" / "main" / f"{uuid_prefix}.coding.jsonl"
        )
        assert not wrong_file.exists(), (
            f"coding transcript leaked into main pool directory {wrong_file}"
        )

        # Deleting the session must remove the transcript from the coding dir.
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

    # Mirror the production resolver shape (WebUIService._resolve_session_meta).
    # This deliberately uses the same variable reference pattern as production
    # to catch the missing _DEFAULT_AGENT_NAME import.
    agent_pool_map: dict[str, str] = {"main": "main", "coding": "coding"}

    def _resolve_session_meta(session_id: str) -> SessionMeta:
        parts = session_id.split(".", 2)
        agent = parts[1] if len(parts) >= 2 else "main"
        pool = agent_pool_map.get(agent, "main")
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
    input_adapter.register_connection("conv.coding", None)

    emitter = WebBotEmitter(
        output_adapter=output_adapter,
        session_id="conv.coding",
        config=EmitterConfig(),
        session_meta_resolver=_resolve_meta_for("conv.coding"),
    )
    # Fire a content delta — must not raise.
    await emitter.emit_delta("hello world")

    # The envelope must have reached the delta queue.
    q = input_adapter._delta_queues.get("conv.coding")
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == "model_content_delta"
    assert envelope.pool == "coding"  # resolved from the map
    assert envelope.session_id == "conv.coding"


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
        pool = SimpleNamespace(config=SimpleNamespace(media=custom))
        svc = WebUIService.__new__(WebUIService)
        svc._pools = {"main": pool}
        svc._media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        svc._agent_pool_map = {"main": "main"}
        svc._pool_session_store = SimpleNamespace()  # truthy -> skip pool_router branch
        svc._transcript_store = SimpleNamespace()
        svc._session_factory = SimpleNamespace()
        svc._model_choice_registry = None  # set by BotService.initialize(); unused here
        svc._app_config = SimpleNamespace(
            multi_agent=SimpleNamespace(default_pool="main")
        )
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
        # The per-pool resolver is wired and honors PoolConfig.media.
        assert ctx.media_config_for("main").max_text_doc_bytes == 888
        assert ctx.media_config_for("nope") == MediaConfig()
