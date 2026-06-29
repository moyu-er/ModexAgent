"""Tests for the G8 attachment endpoints: media-config + upload (§8.2 + upload).

- ``GET /api/media/config`` returns the active ``MediaConfig`` numbers.
- ``POST /api/sessions/{session_id}/attachments`` is a temp-file receiver that
  saves the upload under the workspace media ``_tmp`` dir and returns a ref the
  frontend passes back as an ``AttachmentRef``. The authoritative perception
  gate stays in the ingest stage (not here).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.input_pipeline.context import BotInputContext
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer
from modex_agent.ioc.configs.pool import MediaConfig
from modex_agent.workspace.paths import WorkspacePaths


def _make_server(workspace_root: Path, *, media_config: MediaConfig | None = None
                 ) -> tuple[WebUIServer, WorkspaceScopedTranscriptStore]:
    """Build a minimal WebUIServer wired with an input context (media_config)."""
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_agent_pool_map({"main": "main"})
    server.set_pool_agent_names(["main"])

    ctx = BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=store,
        enqueue_message=lambda *_a, **_k: None,
        command_adapter=input_adapter,
        current_ws_provider=(lambda root=workspace_root: root),
        media_config=media_config,
    )
    server.set_input_context(ctx)
    server.set_data_dir_name(".modex")
    return server, store


# ── GET /api/media/config ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_media_config_returns_default_numbers() -> None:
    """With no override, the endpoint returns the frozen MediaConfig() defaults."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server, _ = _make_server(workspace_root)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/media/config")
            assert resp.status == 200
            data = await resp.json()
            default = MediaConfig()
            assert data["max_image_bytes"] == default.max_image_bytes
            assert data["max_text_doc_bytes"] == default.max_text_doc_bytes
            assert data["session_budget_bytes"] == default.session_budget_bytes
            assert data["max_outbound_bytes"] == default.max_outbound_bytes
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_media_config_returns_configured_numbers() -> None:
    """An injected MediaConfig override is reflected in the response."""
    custom = MediaConfig(
        max_image_bytes=111,
        max_text_doc_bytes=222,
        session_budget_bytes=333,
        max_outbound_bytes=444,
    )
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server, _ = _make_server(workspace_root, media_config=custom)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/media/config")
            assert resp.status == 200
            data = await resp.json()
            assert data["max_image_bytes"] == 111
            assert data["max_text_doc_bytes"] == 222
            assert data["session_budget_bytes"] == 333
            assert data["max_outbound_bytes"] == 444
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_media_config_falls_back_when_no_input_context() -> None:
    """When no input context is wired (minimal server), defaults are returned."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/media/config")
            assert resp.status == 200
            data = await resp.json()
            assert data["max_image_bytes"] == MediaConfig().max_image_bytes
        finally:
            await client.close()


# ── POST /api/sessions/{session_id}/attachments ──────────────────────────────


@pytest.mark.asyncio
async def test_upload_saves_temp_file_and_returns_ref() -> None:
    """A multipart upload is saved to the workspace media _tmp dir and the
    response carries local_path/filename/size the frontend passes back as an
    AttachmentRef."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server, _ = _make_server(workspace_root)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            form = FormData()
            form.add_field(
                "file", b"hello-bytes", filename="note.txt",
                content_type="text/plain",
            )
            resp = await client.post(
                "/api/sessions/abc.main/attachments",
                data=form,
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["filename"] == "note.txt"
            assert data["size"] == len("hello-bytes")
            assert data["mime"] == "text/plain"

            local_path = Path(data["local_path"])
            assert local_path.is_file(), "temp file must exist on disk"
            assert local_path.read_bytes() == b"hello-bytes"
            # Temp file lives under the workspace media _tmp dir.
            expected_tmp_root = (
                workspace_root / ".modex" / "media" / "main" / "_tmp"
            )
            assert local_path.parent == expected_tmp_root
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_upload_rejects_missing_file_part() -> None:
    """A request without the 'file' part is rejected with 400."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server, _ = _make_server(workspace_root)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            form = FormData()
            form.add_field("not_file", b"x", filename="x")
            resp = await client.post(
                "/api/sessions/abc.main/attachments", data=form
            )
            assert resp.status == 400
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_upload_rejects_oversize_with_413() -> None:
    """An upload exceeding the loose early cap is rejected (413); the temp file
    is removed. The authoritative per-kind gate is the pipeline's — this is
    only an early absurd-size guard."""
    tiny_cap = MediaConfig(
        max_image_bytes=4,
        max_text_doc_bytes=4,
        session_budget_bytes=100,
        max_outbound_bytes=100,
    )
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server, _ = _make_server(workspace_root, media_config=tiny_cap)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            form = FormData()
            form.add_field("file", b"way-too-many-bytes", filename="big.bin")
            resp = await client.post(
                "/api/sessions/abc.main/attachments", data=form
            )
            assert resp.status == 413
            # No leftover temp files.
            tmp_dir = workspace_root / ".modex" / "media" / "main" / "_tmp"
            if tmp_dir.is_dir():
                assert not any(tmp_dir.iterdir()), "temp file must be cleaned up"
        finally:
            await client.close()


# ── startup _tmp orphan sweep (debt ①) ───────────────────────────────────────


def test_sweep_media_tmp_orphans_clears_stale_only() -> None:
    """The startup sweep removes leftover ``_tmp`` uploads but leaves accepted
    files (``uploads/``) and the ``_tmp`` dir itself intact (ADR-0013 §13)."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server, _ = _make_server(workspace_root)

        media_main = workspace_root / ".modex" / "media" / "main"
        tmp_dir = media_main / "_tmp"
        uploads = media_main / "uploads"
        tmp_dir.mkdir(parents=True)
        uploads.mkdir(parents=True)

        stale = tmp_dir / "deadbeef"
        stale.write_bytes(b"orphan")
        kept = uploads / "s1" / "aid"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"persisted")

        server.sweep_media_tmp_orphans()

        assert not stale.exists()          # orphan reclaimed
        assert tmp_dir.is_dir()            # dir kept (recreated lazily on upload)
        assert kept.is_file()              # accepted bytes untouched
