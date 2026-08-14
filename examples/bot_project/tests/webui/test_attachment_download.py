"""G6 tests — attachment download endpoint.

Covers (6.1) MEDIA + WORKSPACE locator dispatch serving bytes, unknown-id 404,
missing-file 404; (6.2) MIME allow-list (image/* + video/* real content-type,
everything else octet-stream), SVG CSP header, Range/206 streaming via the HTTP
layer; (6.3) symmetric 404 fallback for evicted-inbound and deleted-outbound;
(I1) negative cross-workspace isolation; (I2) upload → WS → ingest → preprocess
end-to-end; (I3) ingest → HTTP download round-trip; (R1) refresh-recovery: the
history-replay API returns outbound Attachment records so the frontend can
re-render download cards after a page refresh / backend restart.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.input_pipeline.assembly import build_webui_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.skill_parse import ParsedSkill, SkillRegistry
from bot.service.media_store import WorkspaceScopedMediaStore
from bot.service.model_config import BotModelConfig, ModelCfg, ProviderCfg
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import AssistantTurnEvent, UserMessageEvent
from bot.webui.server import WebUIServer

from modex_agent.core.types import InputMessage
from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.media.models import Attachment, AttachmentLocator, Kind
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.runtime import bind_workspace_root

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DATA_DIR = ".modex"

# PNG magic — perception gate accepts this as an image (ADR-0013 §7).
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _bot_model_config() -> BotModelConfig:
    return BotModelConfig(
        default_provider="A",
        default_model="M1",
        providers=[
            ProviderCfg(
                key="a", name="A", url="u", api_key="k",
                models=[ModelCfg(name="M1", model="m1")],
            )
        ],
    )


class _NoSkill(SkillRegistry):
    """Skill registry stub that resolves nothing (skill parsing is not under
    test in the integration cases)."""

    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        return None


def _full_pipeline_server(tmp_path: Path) -> tuple[WebUIServer, WorkspaceScopedMediaStore]:
    """Build a WebUIServer whose input pipeline + context are wired with a real
    WorkspaceScopedMediaStore (the default fixture wires none). Used by the
    integration cases that exercise ingest → perception → download end-to-end.
    """
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR)
    home_sessions_dir = WorkspacePaths(root=tmp_path / _DATA_DIR).sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(_DATA_DIR)

    media_store = WorkspaceScopedMediaStore(data_dir_name=_DATA_DIR)
    pool_store = MagicMock()
    pool_store.get.return_value = "main"
    cmd = MagicMock()
    pipe = build_webui_pipeline(
        skill_registry=_NoSkill(), bot_model_config=_bot_model_config()
    )
    ctx = BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main"},
        pool_session_store=pool_store,
        agent_resolver=lambda p: p,
        transcript_store=store,
        enqueue_message=input_adapter.put_input_message,
        command_adapter=cmd,
        current_ws_provider=(lambda root=tmp_path: root),
        media_store=media_store,
    )
    server.set_input_pipeline(pipe)
    server.set_input_context(ctx)
    return server, media_store


def _media_dir(ws_root: Path, pool: str) -> Path:
    """The pool media dir under <ws_root>/<data_dir>/media/<pool>/."""
    return WorkspacePaths(root=ws_root / _DATA_DIR).media_dir(pool)


def _sessions_dir(ws_root: Path) -> Path:
    return WorkspacePaths(root=ws_root / _DATA_DIR).sessions_dir


def _input_ctx(store: WorkspaceScopedTranscriptStore, root: Path) -> BotInputContext:
    """Minimal BotInputContext exposing media_store (None of the other deps are
    exercised by the download handler)."""
    from unittest.mock import MagicMock

    media_store = WorkspaceScopedMediaStore(data_dir_name=_DATA_DIR)
    return BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main"},
        pool_session_store=MagicMock(),
        agent_resolver=lambda p: p,
        transcript_store=store,
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
        media_store=media_store,
    )


def _build_server(tmp_path: Path) -> WebUIServer:
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR)
    home_sessions_dir = WorkspacePaths(root=tmp_path / _DATA_DIR).sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(_DATA_DIR)
    server.set_input_context(_input_ctx(store, tmp_path))
    return server


async def _append_user_message_with_attachment(
    store: WorkspaceScopedTranscriptStore,
    sessions_dir: Path,
    session_id: str,
    att: Attachment,
) -> None:
    await store.append(
        session_id,
        UserMessageEvent(
            session_id=session_id,
            agent_name="main",
            content="hi",
            attachments=[att.to_dict()],
        ),
        sessions_dir=sessions_dir,
    )


async def _append_assistant_turn_with_attachment(
    store: WorkspaceScopedTranscriptStore,
    sessions_dir: Path,
    session_id: str,
    att: Attachment,
) -> None:
    await store.append(
        session_id,
        AssistantTurnEvent(
            session_id=session_id,
            agent_name="main",
            blocks=[{"kind": "text", "text": "done"}],
            attachments=[att.to_dict()],
        ),
        sessions_dir=sessions_dir,
    )


# ---------------------------------------------------------------------------
# 6.1: locator dispatch + 404s
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_locator_serves_bytes() -> None:
    """MEDIA locator resolves via WorkspaceScopedMediaStore and serves bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)
        store = server._store  # type: ignore[attr-defined]
        ctx = server._input_ctx  # type: ignore[attr-defined]

        session_id = "abc123.main"
        att = Attachment(
            id="att-1",
            kind=Kind.IMAGE,
            name="cat.png",
            mime="image/png",
            size=11,
            path="media/main/uploads/abc123.main/cat.png",
            locator=AttachmentLocator.MEDIA,
        )
        # Persist the byte file through the media resolver (same path the
        # ingest stage writes), then record the Attachment in the transcript.
        ms = ctx.media_store.store_for("main", media_dir=_media_dir(ws_root, "main"))
        ms.save(session_id, att.id, b"png-bytes-here")
        await _append_user_message_with_attachment(
            store, _sessions_dir(ws_root), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/attachments/{att.id}")
            assert resp.status == 200
            assert (await resp.read()) == b"png-bytes-here"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_workspace_locator_serves_bytes() -> None:
    """WORKSPACE locator reads the literal absolute path the agent wrote."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)
        store = server._store  # type: ignore[attr-defined]

        # A file the agent wrote somewhere on the filesystem (in-place).
        outbound_file = ws_root / "outputs" / "report.txt"
        outbound_file.parent.mkdir(parents=True, exist_ok=True)
        outbound_file.write_bytes(b"report-body")

        session_id = "abc123.main"
        att = Attachment(
            id="att-out-1",
            kind=Kind.OTHER,
            name="report.txt",
            mime="text/plain",
            size=len(b"report-body"),
            path=str(outbound_file),
            locator=AttachmentLocator.WORKSPACE,
        )
        await _append_assistant_turn_with_attachment(
            store, _sessions_dir(ws_root), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/attachments/{att.id}")
            assert resp.status == 200
            assert (await resp.read()) == b"report-body"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_unknown_attachment_id_returns_404() -> None:
    """An attachment id not in the transcript returns 404."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(
                "/api/sessions/abc123.main/attachments/does-not-exist"
            )
            assert resp.status == 404
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_media_locator_missing_file_returns_404() -> None:
    """Inbound record present but the byte file was evicted → 404."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)
        store = server._store  # type: ignore[attr-defined]

        session_id = "abc123.main"
        att = Attachment(
            id="att-evict",
            kind=Kind.IMAGE,
            name="cat.png",
            mime="image/png",
            size=10,
            path="media/main/uploads/abc123.main/cat.png",
            locator=AttachmentLocator.MEDIA,
        )
        # Record exists in transcript; the byte file is NOT written (evicted).
        await _append_user_message_with_attachment(
            store, _sessions_dir(ws_root), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/attachments/{att.id}")
            assert resp.status == 404
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_workspace_locator_deleted_file_returns_404() -> None:
    """Outbound record present but the workspace file was removed → 404."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)
        store = server._store  # type: ignore[attr-defined]

        session_id = "abc123.main"
        att = Attachment(
            id="att-del",
            kind=Kind.OTHER,
            name="gone.txt",
            mime="text/plain",
            size=3,
            path=str(ws_root / "gone.txt"),
            locator=AttachmentLocator.WORKSPACE,
        )
        # File does not exist; only the transcript record remains.
        await _append_assistant_turn_with_attachment(
            store, _sessions_dir(ws_root), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/attachments/{att.id}")
            assert resp.status == 404
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 6.2: MIME allow-list + SVG CSP + Range
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_served_with_real_content_type() -> None:
    """image/* attachment is served with its real Content-Type."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)
        store = server._store  # type: ignore[attr-defined]
        ctx = server._input_ctx  # type: ignore[attr-defined]

        session_id = "abc123.main"
        att = Attachment(
            id="att-img",
            kind=Kind.IMAGE,
            name="photo.png",
            mime="image/png",
            size=8,
            path="media/main/uploads/abc123.main/photo.png",
            locator=AttachmentLocator.MEDIA,
        )
        ms = ctx.media_store.store_for("main", media_dir=_media_dir(ws_root, "main"))
        ms.save(session_id, att.id, b"png-data")
        await _append_user_message_with_attachment(
            store, _sessions_dir(ws_root), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/attachments/{att.id}")
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "image/png"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_video_served_with_real_content_type() -> None:
    """video/* attachment is served with its real Content-Type."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)
        store = server._store  # type: ignore[attr-defined]
        ctx = server._input_ctx  # type: ignore[attr-defined]

        session_id = "abc123.main"
        att = Attachment(
            id="att-vid",
            kind=Kind.OTHER,
            name="clip.mp4",
            mime="video/mp4",
            size=8,
            path="media/main/uploads/abc123.main/clip.mp4",
            locator=AttachmentLocator.MEDIA,
        )
        ms = ctx.media_store.store_for("main", media_dir=_media_dir(ws_root, "main"))
        ms.save(session_id, att.id, b"mp4-data")
        await _append_user_message_with_attachment(
            store, _sessions_dir(ws_root), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/attachments/{att.id}")
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "video/mp4"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_non_image_served_as_octet_stream() -> None:
    """A non-image/video type (text/plain) is served as application/octet-stream."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)
        store = server._store  # type: ignore[attr-defined]

        outbound_file = ws_root / "doc.txt"
        outbound_file.write_bytes(b"text-body")

        session_id = "abc123.main"
        att = Attachment(
            id="att-txt",
            kind=Kind.EXTRACTABLE_DOCUMENT,
            name="doc.txt",
            mime="text/plain",
            size=len(b"text-body"),
            path=str(outbound_file),
            locator=AttachmentLocator.WORKSPACE,
        )
        await _append_assistant_turn_with_attachment(
            store, _sessions_dir(ws_root), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/attachments/{att.id}")
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "application/octet-stream"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_svg_carries_strict_csp() -> None:
    """An SVG attachment carries the strict CSP header (no XSS via inline svg)."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)
        store = server._store  # type: ignore[attr-defined]
        ctx = server._input_ctx  # type: ignore[attr-defined]

        session_id = "abc123.main"
        att = Attachment(
            id="att-svg",
            kind=Kind.IMAGE,
            name="logo.svg",
            mime="image/svg+xml",
            size=8,
            path="media/main/uploads/abc123.main/logo.svg",
            locator=AttachmentLocator.MEDIA,
        )
        ms = ctx.media_store.store_for("main", media_dir=_media_dir(ws_root, "main"))
        ms.save(session_id, att.id, b"<svg/>")
        await _append_user_message_with_attachment(
            store, _sessions_dir(ws_root), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/attachments/{att.id}")
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "image/svg+xml"
            csp = resp.headers.get("Content-Security-Policy", "")
            assert "default-src 'none'" in csp
            assert "sandbox" in csp
            assert "img-src 'self' data:" in csp
            assert "style-src 'unsafe-inline'" in csp
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_range_request_returns_206() -> None:
    """A Range request streams a partial body with 206 + Content-Range."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)
        store = server._store  # type: ignore[attr-defined]
        ctx = server._input_ctx  # type: ignore[attr-defined]

        body = b"0123456789abcdef"  # 16 bytes
        session_id = "abc123.main"
        att = Attachment(
            id="att-rng",
            kind=Kind.IMAGE,
            name="blob.bin",
            mime="image/png",  # image so Content-Type is the real mime
            size=len(body),
            path="media/main/uploads/abc123.main/blob.bin",
            locator=AttachmentLocator.MEDIA,
        )
        ms = ctx.media_store.store_for("main", media_dir=_media_dir(ws_root, "main"))
        ms.save(session_id, att.id, body)
        await _append_user_message_with_attachment(
            store, _sessions_dir(ws_root), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/sessions/{session_id}/attachments/{att.id}",
                headers={"Range": "bytes=4-7"},
            )
            assert resp.status == 206
            assert resp.headers.get("Content-Range") == f"bytes 4-7/{len(body)}"
            assert (await resp.read()) == body[4:8]
            # Accept-Ranges advertises resumable support.
            assert resp.headers.get("Accept-Ranges") == "bytes"
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 6.3: ws= routing + cross-locator fallback (already covered above, plus ws=)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_query_routes_to_non_home_workspace() -> None:
    """?ws= resolves the workspace the media + transcript live under."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp) / "alt_ws"
        ws_root.mkdir()
        server = _build_server(Path(tmp))
        store = server._store  # type: ignore[attr-defined]
        ctx = server._input_ctx  # type: ignore[attr-defined]

        session_id = "abc123.main"
        att = Attachment(
            id="att-ws",
            kind=Kind.IMAGE,
            name="pic.png",
            mime="image/png",
            size=5,
            path="media/main/uploads/abc123.main/pic.png",
            locator=AttachmentLocator.MEDIA,
        )
        ms = ctx.media_store.store_for("main", media_dir=_media_dir(ws_root, "main"))
        ms.save(session_id, att.id, b"hello")
        await _append_user_message_with_attachment(
            store, _sessions_dir(ws_root), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/sessions/{session_id}/attachments/{att.id}?ws={ws_root}"
            )
            assert resp.status == 200
            assert (await resp.read()) == b"hello"
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# I1: negative cross-workspace isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_workspace_isolation_404() -> None:
    """I1: an attachment written under workspace ws-A is UNREACHABLE via a
    download request that resolves ``?ws=`` to a DIFFERENT workspace ws-B.

    The download handler resolves the transcript sessions_dir from ``?ws=``,
    and ``find_attachment`` scans THAT dir only. A record under ws-A's
    sessions_dir is invisible to ws-B → 404. Locks the cross-workspace
    isolation property (a workspace cannot read another workspace's records).
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ws_a = root / "ws-A"
        ws_b = root / "ws-B"
        ws_a.mkdir()
        ws_b.mkdir()
        server = _build_server(root)  # home = root; ws-A / ws-B are non-home
        store = server._store  # type: ignore[attr-defined]
        ctx = server._input_ctx  # type: ignore[attr-defined]

        session_id = "abc123.main"
        att = Attachment(
            id="att-iso",
            kind=Kind.IMAGE,
            name="pic.png",
            mime="image/png",
            size=5,
            path="media/main/uploads/abc123.main/pic.png",
            locator=AttachmentLocator.MEDIA,
        )
        # Write BOTH the bytes AND the transcript record under ws-A.
        ms = ctx.media_store.store_for("main", media_dir=_media_dir(ws_a, "main"))
        ms.save(session_id, att.id, b"hello")
        await _append_user_message_with_attachment(
            store, _sessions_dir(ws_a), session_id, att
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            # Same session_id + attachment_id, but routed to ws-B's sessions dir
            # (which has neither the transcript record nor the byte file).
            resp = await client.get(
                f"/api/sessions/{session_id}/attachments/{att.id}?ws={ws_b}"
            )
            assert resp.status == 404, (
                "a record under ws-A must not be reachable via ws-B's ?ws="
            )
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# I2: upload → WS send_message → ingest → preprocess (end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_ws_send_ingest_preprocess_e2e() -> None:
    """I2: the real upload → WS → ingest → preprocess chain.

    POST a file to the upload endpoint → take the returned ``local_path`` →
    send a WS ``send_message`` carrying it as an attachment → run the pipeline
    (ingest stage persists bytes + builds the Attachment record) → invoke
    ``TurnContextBuilder.preprocess`` on the enqueued message → assert the
    agent-perceived content contains ``[Attachment: ... @ <abs_path>]`` AND that
    ``<abs_path>`` is a real file on disk (the staged bytes survived to
    perception). Wires the HTTP upload → WS → ingest → preprocess seam and
    validates C1 (an in-staging path is accepted).
    """
    from aiohttp.formdata import FormData

    png_bytes = _PNG_MAGIC + b"\x00" * 40
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        server, media_store = _full_pipeline_server(root)
        session_id = "web:i2.main"

        # Capture the InputMessage EnqueueStage builds so preprocess runs on the
        # real post-ingest carriage (attachments_resolved populated by G3).
        enqueued: list[InputMessage] = []
        server._input_ctx.enqueue_message = enqueued.append  # type: ignore[assignment]

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            # 1) Upload the file — the endpoint writes it under the ws media
            #    staging _tmp dir and returns the local_path the frontend hands
            #    to send_message.
            form = FormData()
            form.add_field("file", png_bytes, filename="photo.png", content_type="image/png")
            upload_resp = await client.post(
                f"/api/sessions/{session_id}/attachments?ws={root}",
                data=form,
            )
            assert upload_resp.status == 200
            upload_body = await upload_resp.json()
            local_path = upload_body["local_path"]
            # C1 sanity: the upload returned a path INSIDE the staging dir.
            staging = _media_dir(root, "main") / "_tmp"
            assert Path(local_path).resolve().is_relative_to(staging.resolve())

            # 2) WS send_message carrying the uploaded local_path as an
            #    attachment → the pipeline runs ingest + enqueue.
            ws = await client.ws_connect("/ws")
            try:
                await ws.send_json({"action": "attach", "session_id": session_id})
                # Drain the attached ack.
                await ws.receive_json(timeout=2)
                await ws.send_json({
                    "action": "send_message",
                    "session_id": session_id,
                    "content": "look at this photo",
                    "ws": str(root),
                    "attachments": [
                        {"local_path": local_path, "filename": "photo.png", "mime": "image/png"},
                    ],
                })
                # Drain the echoed user_message so the handler completes.
                await ws.receive_json(timeout=2)
            finally:
                await ws.close()
        finally:
            await client.close()

        # 3) The ingest stage produced exactly one resolved Attachment and the
        #    enqueued message carries it.
        assert len(enqueued) == 1, "EnqueueStage ran once"
        msg = enqueued[0]
        assert len(msg.attachments_resolved) == 1, "ingest accepted the PNG"
        rec = msg.attachments_resolved[0]
        assert rec.kind is Kind.IMAGE
        assert rec.locator is AttachmentLocator.MEDIA

        # 4) preprocess injects the transient [Attachment: ... @ <abs>] line,
        #    and the abs path it references is a real file (staged bytes
        #    survived to perception).
        with bind_workspace_root(root):
            builder = _make_turn_builder()
            full_sid = msg.metadata["full_session_id"]
            sanitized, media_blocks, _ = await builder.preprocess(
                msg, full_sid, {}, None
            )
        assert sanitized is not None
        assert "[Attachment: photo.png (image/png, " in sanitized, (
            f"preprocess must inject the path reference; got {sanitized!r}"
        )
        assert media_blocks == [], "mechanism A is dormant in v1"
        # The injected absolute path is a real file holding the original bytes.
        abs_path = Path(str(sanitized).rsplit("@ ", 1)[1].rstrip("]"))
        assert abs_path.is_file(), f"perceived path must exist: {abs_path}"
        assert abs_path.read_bytes() == png_bytes, "bytes survived to perception"


def _make_turn_builder() -> TurnContextBuilder:
    """A minimal TurnContextBuilder wired only for preprocess (the unit the I2
    assertion exercises). Mirrors test_attachment_injection_asymmetry."""
    return TurnContextBuilder(
        agent=MagicMock(name="agent"),
        tool_manager=MagicMock(name="tool_manager"),
        sanitizer=None,
        command_processor=None,
        skill_manager=None,
        context_builder=None,
        agent_descriptor=None,
        max_iterations=5,
        safety=MagicMock(name="safety"),
        runtime_services=None,
        runtime_context_manager=None,
        governance=None,
        hook_runner=None,
        interceptor_chain=None,
        control_channel=None,
        emitter_factory=None,
        output_adapter=MagicMock(name="output_adapter"),
        turn_store=None,
        registry=TurnSessionRegistry(),
    )


# ---------------------------------------------------------------------------
# I3: ingest → HTTP download round-trip (MEDIA)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_then_download_round_trip() -> None:
    """I3: an attachment ingested through the REAL pipeline (record + bytes
    written by the ingest stage, not hand-fabricated) is served byte-for-byte
    by the download endpoint.

    Catches any path-encoding / locator seam bug between the ingest stage's
    workspace-relative ``path`` (forward-slash as_posix) and the download
    resolver — especially Windows backslash in the relative path field, which
    would break ``media_store.read`` reconstruction.
    """
    png_bytes = _PNG_MAGIC + b"\x00" * 40
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        server, _ = _full_pipeline_server(root)
        session_id = "web:i3.main"

        # Ingest directly through the pipeline (the WS adapter would also work,
        # but the envelope is the minimal seam: ingest + persist + enqueue).
        enqueued: list[InputMessage] = []
        server._input_ctx.enqueue_message = enqueued.append  # type: ignore[assignment]

        envelope = UserInputEnvelope(
            external_id="u-i3",
            content="ingest me",
            channel="websocket",
            explicit_pool="main",
            pre_resolved_session=_session_info(session_id),
            attachments=[AttachmentRef(local_path="", filename="photo.png")],
        )
        # Write the staging file the AttachmentRef points at.
        staging = _media_dir(root, "main") / "_tmp"
        staging.mkdir(parents=True, exist_ok=True)
        staged = staging / "i3.png"
        staged.write_bytes(png_bytes)
        envelope.attachments[0].local_path = str(staged)

        with bind_workspace_root(root):
            result = await server._input_pipeline.handle(envelope, server._input_ctx)
        assert result.should_continue()
        assert len(envelope.resolved_attachments) == 1
        rec = envelope.resolved_attachments[0]
        attachment_id = rec.id
        full_sid = envelope.metadata["full_session_id"]

        # The record is persisted to the transcript by PersistUserMessageStage,
        # so the download handler (which scans the transcript) can find it.
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/sessions/{full_sid}/attachments/{attachment_id}?ws={root}"
            )
            assert resp.status == 200, (
                f"download should serve the ingested attachment; got {resp.status}"
            )
            assert (await resp.read()) == png_bytes, "served bytes match the original"
            # nosniff defense-in-depth header is present (M1).
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        finally:
            await client.close()


def _session_info(session_id: str):
    """Build a SessionInfo for a ``<prefix>.<agent>`` id (used as
    pre_resolved_session so the pipeline does not re-encode the id)."""
    from bot.webui.server import SessionInfo

    return SessionInfo.from_str(session_id)


# ---------------------------------------------------------------------------
# R1: refresh-recovery — history replay returns outbound attachment records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_replay_returns_outbound_attachment_records() -> None:
    """R1: a SendFileToUserTool-persisted AssistantTurnEvent (no turn_id, empty
    blocks, one outbound Attachment) is returned by GET /api/sessions/{id}/messages
    as an assistant_turn event carrying the serialized Attachment record, so the
    frontend can re-render a download card after a page refresh / backend restart
    (ADR-0013 §11).

    This is the regression guard for the fix that replaced the hardcoded
    ``"attachments": []`` in ``_handle_get_messages`` with
    ``MaterializedTurn.attachments`` (collected by ``_materialize_events``).
    """
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        server = _build_server(ws_root)
        store = server._store  # type: ignore[attr-defined]

        session_id = "abc123.main"
        att = Attachment(
            id="att-refresh",
            kind=Kind.OTHER,
            name="report.txt",
            mime="text/plain",
            size=len(b"report-body"),
            path=str(ws_root / "report.txt"),
            locator=AttachmentLocator.WORKSPACE,
        )
        # The file is on disk so a subsequent download still works.
        (ws_root / "report.txt").write_bytes(b"report-body")
        # Persist exactly what SendFileToUserTool._persist_attachment writes:
        # an AssistantTurnEvent with blocks=[], no turn_id, and one record.
        await store.append(
            session_id,
            AssistantTurnEvent(
                session_id=session_id,
                agent_name="main",
                blocks=[],
                attachments=[att.to_dict()],
            ),
            sessions_dir=_sessions_dir(ws_root),
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/messages")
            assert resp.status == 200
            events = await resp.json()
            assistant_turns = [e for e in events if e["event"] == "assistant_turn"]
            assert len(assistant_turns) == 1, (
                f"expected one assistant_turn; got {len(assistant_turns)}"
            )
            turn = assistant_turns[0]
            assert turn["blocks"] == [], (
                "attachment-only carrier has no conversational blocks"
            )
            assert turn["attachments"] == [att.to_dict()], (
                "the outbound Attachment record must round-trip so the frontend "
                "can build the download URL and re-render the card after refresh"
            )
            # The download endpoint still resolves the same record — proving the
            # record the replay returns is the one find_attachment scans.
            dl = await client.get(
                f"/api/sessions/{session_id}/attachments/{att.id}"
            )
            assert dl.status == 200
            assert (await dl.read()) == b"report-body"
        finally:
            await client.close()
