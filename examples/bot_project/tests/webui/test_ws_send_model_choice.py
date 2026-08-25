"""WebUI send_message threads provider_name/model_name into the envelope metadata.

Mirrors test_ws_send_message_workspace.py: rather than mock the many real
dependencies of _ws_send_message in isolation, this drives the real WS path
end-to-end (attach_default_pipeline wires the actual input pipeline + context)
and captures the pipeline result envelope. This is the established pattern in
the suite and exercises the full metadata-threading contract faithfully.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import _unwrap_envelope
from bot.webui.server import WebUIServer

from modex_agent.workspace.paths import WorkspacePaths

sys.path.insert(0, str(Path(__file__).parents[2]))


@pytest.mark.asyncio
async def test_ws_send_message_threads_provider_model_into_metadata() -> None:
    """When send_message carries provider_name/model_name, the pipeline result
    envelope metadata must carry them under RoutingMeta.MODEL_PROVIDER/MODEL_MODEL."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        from tests.webui._pipeline_fixture import attach_default_pipeline

        await attach_default_pipeline(
            server, store, input_adapter, workspace_root=workspace_root
        )

        # Wrap pipeline.handle to capture the result envelope.
        captured: list = []
        original_handle = server._input_pipeline.handle

        async def _capturing_handle(envelope, ctx):
            result = await original_handle(envelope, ctx)
            captured.append(result)
            return result

        server._input_pipeline.handle = _capturing_handle

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            await ws.send_json({
                "action": "send_message",
                "session_id": "web:test.main",
                "content": "hello",
                "ws": str(workspace_root),
                "provider_name": "A",
                "model_name": "M2",
            })

            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"

            assert len(captured) == 1
            result = captured[0]
            meta = result.envelope().metadata
            assert meta[RoutingMeta.MODEL_PROVIDER] == "A"
            assert meta[RoutingMeta.MODEL_MODEL] == "M2"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_send_message_without_provider_model_leaves_metadata_unset() -> None:
    """When send_message omits provider_name/model_name, the metadata keys must
    be absent (ModelChoiceStage then falls back to the configured default)."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        from tests.webui._pipeline_fixture import attach_default_pipeline

        await attach_default_pipeline(
            server, store, input_adapter, workspace_root=workspace_root
        )

        captured: list = []
        original_handle = server._input_pipeline.handle

        async def _capturing_handle(envelope, ctx):
            result = await original_handle(envelope, ctx)
            captured.append(result)
            return result

        server._input_pipeline.handle = _capturing_handle

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            _unwrap_envelope(await ws.receive_json())

            await ws.send_json({
                "action": "send_message",
                "session_id": "web:test.main",
                "content": "hello",
                "ws": str(workspace_root),
            })

            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"

            assert len(captured) == 1
            meta = captured[0].envelope().metadata
            assert RoutingMeta.MODEL_PROVIDER not in meta
            assert RoutingMeta.MODEL_MODEL not in meta
        finally:
            await client.close()
