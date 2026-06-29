"""Tests for the attachment ingest stage — gate + persist + record.

Covers: accept path produces a persisted Attachment under the media layout
with a workspace-relative ``path``; reject path produces nothing and stores
no bytes; budget enforcement runs after save. The stage is a no-op when the
envelope carries no attachments or the context has no media wiring.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.attachment_ingest import AttachmentIngestStage
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.service.media_store import WorkspaceScopedMediaStore

from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.ioc.configs.pool import MediaConfig
from modex_agent.media.models import AttachmentLocator, Kind
from modex_agent.workspace.runtime import bind_workspace_root

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MZ = b"MZ"  # PE executable magic
_PDF_MAGIC = b"%PDF-1.4"


def _write_png(tmp: Path, name: str = "photo.png") -> Path:
    p = tmp / name
    p.write_bytes(_PNG_MAGIC + b"\x00" * 40)
    return p


def _ctx(
    *,
    media_store: WorkspaceScopedMediaStore,
    root: Path,
) -> BotInputContext:
    return BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
        media_store=media_store,
    )


def _ctx_with_pool_config(
    *,
    media_store: WorkspaceScopedMediaStore,
    root: Path,
    pool_config: MediaConfig,
) -> BotInputContext:
    """Like ``_ctx`` but wires a per-pool ``media_config_for_pool`` resolver.

    Exercises the ADR-0013 §7 per-pool override path: the ingest stage must
    consult ``ctx.media_config_for(pool)`` (the resolver) rather than the
    default ``ctx.media_config`` instance.
    """
    return BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
        media_store=media_store,
        media_config_for_pool=lambda _pool: pool_config,
    )


def _envelope(root: Path, attachments: list[AttachmentRef]) -> UserInputEnvelope:
    env = UserInputEnvelope(
        external_id="u1", content="see image", channel="websocket"
    )
    env.metadata[RoutingMeta.RESOLVED_POOL] = "main"
    env.metadata[RoutingMeta.FULL_SESSION_ID] = "u1.main"
    env.metadata[RoutingMeta.WORKSPACE] = str(root)
    env.attachments = attachments
    return env


@pytest.mark.asyncio
async def test_accept_path_persists_attachment_with_ws_relative_path() -> None:
    """An accepted PNG produces an Attachment persisted under the media layout,
    with ``path`` relative to the workspace root (ADR §4) and locator=MEDIA."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        uploads = root / "uploads"
        uploads.mkdir()
        png = _write_png(uploads)

        media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        ctx = _ctx(media_store=media_store, root=root)
        env = _envelope(root, [AttachmentRef(local_path=str(png), filename="photo.png")])

        await AttachmentIngestStage().process(env, ctx)

        assert len(env.resolved_attachments) == 1
        rec = env.resolved_attachments[0]
        assert rec.kind is Kind.IMAGE
        assert rec.mime == "image/png"
        assert rec.size == png.stat().st_size
        assert rec.locator is AttachmentLocator.MEDIA
        # path is relative to the workspace root and uses forward slashes.
        assert rec.path.startswith(".modex/media/main/uploads/")
        assert "\\" not in rec.path
        # The bytes are readable through the resolver (locator=media). The
        # resolver routes by the ctxvar workspace root, so bind it for the read
        # — the stage saved under this same bound root.
        with bind_workspace_root(root):
            store = media_store.store_for("main")
            on_disk = store.read("u1.main", rec.id)
        assert on_disk is not None and on_disk.read_bytes() == png.read_bytes()


@pytest.mark.asyncio
async def test_reject_path_records_nothing_and_stores_no_bytes() -> None:
    """A PE disguised as .png is rejected by the gate; nothing is stored, the
    envelope carries no resolved attachment."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        uploads = root / "uploads"
        uploads.mkdir()
        trojan = uploads / "trojan.png"
        trojan.write_bytes(_MZ + b"\x00" * 60)

        media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        ctx = _ctx(media_store=media_store, root=root)
        env = _envelope(
            root, [AttachmentRef(local_path=str(trojan), filename="trojan.png")]
        )

        await AttachmentIngestStage().process(env, ctx)

        assert env.resolved_attachments == []
        with bind_workspace_root(root):
            store = media_store.store_for("main")
            assert store.list_session("u1.main") == []


@pytest.mark.asyncio
async def test_mixed_accept_and_reject_keeps_only_accepted() -> None:
    """One accepted PNG + one rejected PE → exactly one resolved Attachment."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        uploads = root / "uploads"
        uploads.mkdir()
        png = _write_png(uploads, "ok.png")
        bad = uploads / "bad.png"
        bad.write_bytes(_MZ + b"\x00" * 30)

        media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        ctx = _ctx(media_store=media_store, root=root)
        env = _envelope(
            root,
            [
                AttachmentRef(local_path=str(png), filename="ok.png"),
                AttachmentRef(local_path=str(bad), filename="bad.png"),
            ],
        )

        await AttachmentIngestStage().process(env, ctx)

        assert len(env.resolved_attachments) == 1
        assert env.resolved_attachments[0].kind is Kind.IMAGE


@pytest.mark.asyncio
async def test_budget_enforcement_invoked_on_save(tmp_path: Path) -> None:
    """enforce_budget must run after save (oldest-by-mtime eviction to the
    configured session budget). Verified by spying on the resolved store.

    The resolver routes by the ctxvar workspace root and caches one store per
    resolved media dir, so the spy must attach to the SAME store the stage
    resolves — obtain it under the same bound root."""
    root = tmp_path
    uploads = root / "uploads"
    uploads.mkdir()
    png = _write_png(uploads)

    media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
    ctx = _ctx(media_store=media_store, root=root)
    env = _envelope(root, [AttachmentRef(local_path=str(png), filename="photo.png")])

    # Spy on the concrete store the resolver hands the stage for this pool/root.
    with bind_workspace_root(root):
        store = media_store.store_for("main")
    store.enforce_budget = MagicMock(wraps=store.enforce_budget)  # type: ignore[assignment]

    await AttachmentIngestStage().process(env, ctx)

    store.enforce_budget.assert_called_once()
    # Called with the session id + the configured session budget.
    call_args = store.enforce_budget.call_args
    assert call_args.args[0] == "u1.main"
    assert call_args.args[1] == ctx.media_config.session_budget_bytes


@pytest.mark.asyncio
async def test_noop_when_no_attachments() -> None:
    """An envelope with no attachments flows through unchanged."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        ctx = _ctx(media_store=media_store, root=root)
        env = _envelope(root, [])
        result = await AttachmentIngestStage().process(env, ctx)
        assert result.should_continue()
        assert env.resolved_attachments == []


@pytest.mark.asyncio
async def test_noop_when_media_store_not_wired() -> None:
    """Legacy contexts (no media_store) pass attachments through unchanged —
    existing callers without media wiring are not broken."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        uploads = root / "uploads"
        uploads.mkdir()
        png = _write_png(uploads)
        # No media_store on the context.
        ctx = BotInputContext(
            default_pool="main",
            pool_session_store=MagicMock(),
            agent_pool_map={"main": "main"},
            agent_resolver=lambda p: p,
            transcript_store=MagicMock(),
            enqueue_message=MagicMock(),
            command_adapter=MagicMock(),
        )
        env = _envelope(root, [AttachmentRef(local_path=str(png), filename="p.png")])
        result = await AttachmentIngestStage().process(env, ctx)
        assert result.should_continue()
        assert env.resolved_attachments == []


@pytest.mark.asyncio
async def test_pdf_classifies_as_extractable_document() -> None:
    """A PDF (extractable-document kind) passes the gate and is persisted."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        uploads = root / "uploads"
        uploads.mkdir()
        pdf = uploads / "doc.pdf"
        pdf.write_bytes(_PDF_MAGIC + b"\n" + b"\x00" * 40)

        media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        ctx = _ctx(media_store=media_store, root=root)
        env = _envelope(root, [AttachmentRef(local_path=str(pdf), filename="doc.pdf")])

        await AttachmentIngestStage().process(env, ctx)

        assert len(env.resolved_attachments) == 1
        rec = env.resolved_attachments[0]
        assert rec.kind is Kind.EXTRACTABLE_DOCUMENT
        assert rec.mime == "application/pdf"


@pytest.mark.asyncio
async def test_per_pool_media_config_override_is_honored() -> None:
    """A per-pool ``media_config_for_pool`` resolver overrides the default
    instance: a tight ``max_image_bytes`` rejects a PNG the default 20 MB cap
    would accept. Verifies the ingest stage reads
    ``ctx.media_config_for(pool)`` (ADR-0013 §7), not the default
    ``ctx.media_config``."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        uploads = root / "uploads"
        uploads.mkdir()
        png = _write_png(uploads)  # 48 bytes — well under the default 20 MB cap

        media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        # Tight per-pool cap: reject anything above 8 bytes for images.
        pool_config = MediaConfig(max_image_bytes=8)
        ctx = _ctx_with_pool_config(
            media_store=media_store, root=root, pool_config=pool_config
        )
        env = _envelope(root, [AttachmentRef(local_path=str(png), filename="photo.png")])

        await AttachmentIngestStage().process(env, ctx)

        # Rejected by the per-pool cap → nothing persisted.
        assert env.resolved_attachments == []
        with bind_workspace_root(root):
            store = media_store.store_for("main")
            assert store.list_session("u1.main") == []
