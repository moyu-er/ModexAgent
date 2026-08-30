"""Tests that WebUI send_message carries workspace into the envelope."""

from __future__ import annotations

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


@pytest.mark.asyncio
async def test_ws_send_message_with_ws_payload_sets_workspace() -> None:
    """When send_message carries a 'ws' field, the envelope metadata must contain
    the resolved workspace path."""
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

        # Wrap pipeline to capture the result for assertion
        captured_results: list = []
        original_handle = server._input_pipeline.handle

        async def _capturing_handle(envelope, ctx):
            result = await original_handle(envelope, ctx)
            captured_results.append(result)
            return result

        server._input_pipeline.handle = _capturing_handle

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            # Attach to a session
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            # Send a message with explicit ws payload
            await ws.send_json({
                "action": "send_message",
                "session_id": "web:test.main",
                "content": "hello",
                "ws": str(workspace_root),
            })

            # Should receive user_message echoed back
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"

            # Assert the pipeline result envelope carries the workspace
            assert len(captured_results) == 1
            result = captured_results[0]
            assert result.envelope().metadata[RoutingMeta.WORKSPACE] == str(workspace_root.resolve())
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_send_message_without_ws_payload_falls_back_to_home() -> None:
    """When send_message has no 'ws' field, the envelope metadata must fall back
    to the home workspace root."""
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

        # Wrap pipeline to capture the result for assertion
        captured_results: list = []
        original_handle = server._input_pipeline.handle

        async def _capturing_handle(envelope, ctx):
            result = await original_handle(envelope, ctx)
            captured_results.append(result)
            return result

        server._input_pipeline.handle = _capturing_handle

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            # Attach to a session
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            # Send a message without ws payload
            await ws.send_json({
                "action": "send_message",
                "session_id": "web:test.main",
                "content": "hello",
            })

            # Should receive user_message echoed back
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"

            # Assert the pipeline result envelope falls back to home workspace
            assert len(captured_results) == 1
            result = captured_results[0]
            expected_home = str(workspace_root.resolve())
            assert result.envelope().metadata[RoutingMeta.WORKSPACE] == expected_home
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_send_message_with_relative_ws_payload_resolves() -> None:
    """When send_message carries a relative 'ws' path, it resolves against home."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        ws_sub = home / "subworkspace"
        ws_sub.mkdir()

        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=home / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        from tests.webui._pipeline_fixture import attach_default_pipeline
        await attach_default_pipeline(
            server, store, input_adapter, workspace_root=home
        )

        # Wrap pipeline to capture the result for assertion
        captured_results: list = []
        original_handle = server._input_pipeline.handle

        async def _capturing_handle(envelope, ctx):
            result = await original_handle(envelope, ctx)
            captured_results.append(result)
            return result

        server._input_pipeline.handle = _capturing_handle

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            # Send with a relative ws path (should resolve against home)
            await ws.send_json({
                "action": "send_message",
                "session_id": "web:test.main",
                "content": "hello",
                "ws": "subworkspace",
            })

            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"

            # Assert the resolved relative path is in the envelope metadata
            # (Path("subworkspace").resolve() resolves against cwd, not home)
            assert len(captured_results) == 1
            result = captured_results[0]
            assert result.envelope().metadata[RoutingMeta.WORKSPACE] == str(Path("subworkspace").resolve())
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_send_message_attachment_only_empty_content_not_dropped() -> None:
    """Regression: a send_message with empty text BUT an attachment must not be
    silently dropped. The frontend enables Send on pending uploads with no text
    (ADR-0013: a file in a conversation is itself the message); the server's
    early-return guard must allow it through when an attachment payload is
    present, so the pipeline runs and the agent perceives the file.
    """
    from modex_agent.workspace.paths import WorkspacePaths

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
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

        # Stage a file under the workspace media _tmp dir (the upload endpoint's
        # layout) so it passes the C1 staging-containment guard.
        staging = workspace_root / ".modex" / "media" / "main" / "_tmp"
        staging.mkdir(parents=True, exist_ok=True)
        staged = staging / "abc123"
        staged.write_bytes(png)

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            _unwrap_envelope(await ws.receive_json())

            # Empty content + one attachment — must NOT be dropped.
            await ws.send_json({
                "action": "send_message",
                "session_id": "web:test.main",
                "content": "",
                "ws": str(workspace_root),
                "attachments": [{"local_path": str(staged), "filename": "x.png", "mime": "image/png"}],
            })
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"
            # The pipeline ran (message was not dropped at the empty-content guard).
            assert len(captured) == 1
        finally:
            await client.close()
