"""G9 — QQ inbound attachment alignment with the shared ingest stage.

Asserts the QQ adapter is a thin AttachmentRef producer (ADR-0013 §12):

1. ``_on_message`` downloads IM-received files to a **temporary** dir (not the
   legacy ``data/media/qq/`` persistent path) and produces
   ``AttachmentRef(local_path=<temp>, filename=<original name>, ...)``.
2. A QQ-produced AttachmentRef flows through the real IM pipeline's ingest stage
   to a persisted ``Attachment`` record under the standard media layout,
   reachable through the resolver.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bot.adapters import qq as qq_mod
from bot.adapters.qq import QQInputAdapter
from bot.input_pipeline.assembly import build_im_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.skill_parse import PoolSkillResolverRegistry
from bot.service.media_store import WorkspaceScopedMediaStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore

from modex_agent.core.media import AttachmentLocator, Kind
from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.workspace.runtime import bind_workspace_root
from tests.input_pipeline.assembly_support import (
    TEST_ASSEMBLY_CTX,
    TEST_COMPONENT_REGISTRY,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _make_qq_data(*, content: str, attachments: list[SimpleNamespace]) -> SimpleNamespace:
    """Build the message shape ``_on_message`` reads off the QQ SDK event."""
    return SimpleNamespace(
        id="msg-1",
        content=content,
        author=SimpleNamespace(id="u1", user_openid="u1"),
        attachments=attachments,
        group_openid=None,
        channel_id="c1",
    )


@pytest.mark.asyncio
async def test_inbound_attachment_downloads_to_temp_and_carries_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_on_message`` downloads IM files to a temp dir and emits an
    ``AttachmentRef`` carrying the original filename — not the legacy
    persistent ``data/media/qq/`` path, and not an opaque id fallback."""
    adapter = QQInputAdapter(app_id="x", secret="y", allow_from=["*"])

    # Where the (patched) downloader writes bytes. Asserted to be the adapter's
    # inbound temp dir, not the legacy media_dir.
    captured_dest_dir: dict[str, Path] = {}

    png_bytes = _PNG_MAGIC + b"\x00" * 40

    async def fake_download(*, url: str, dest_dir: Path, filename_hint: str = "") -> str:
        captured_dest_dir["dest"] = dest_dir
        name = filename_hint or "qq_file.bin"
        target = dest_dir / name
        target.write_bytes(png_bytes)
        return str(target)

    monkeypatch.setattr(qq_mod, "download_file", fake_download)

    # Capture the seed envelope the adapter hands to the pipeline.
    captured: dict[str, UserInputEnvelope] = {}

    async def fake_handle(env: UserInputEnvelope, _ctx) -> MagicMock:
        captured["env"] = env
        result = MagicMock()
        result.should_continue.return_value = True
        return result

    adapter._input_pipeline = MagicMock()
    adapter._input_pipeline.handle = fake_handle
    adapter._output_adapter = None  # no Terminate-response surfacing needed

    data = _make_qq_data(
        content="see this",
        attachments=[SimpleNamespace(url="https://qq.example/f.png", filename="photo.png")],
    )
    await adapter._on_message(data, is_group=False)

    # 1. Downloaded into the adapter's inbound temp dir.
    assert captured_dest_dir["dest"] == Path(adapter._inbound_tmp.name)

    # 2. AttachmentRef carries temp local_path + the ORIGINAL filename.
    env = captured["env"]
    assert len(env.attachments) == 1
    ref = env.attachments[0]
    assert isinstance(ref, AttachmentRef)
    assert ref.filename == "photo.png"
    assert ref.local_path is not None
    # local_path lives under the temp dir, and the bytes are there.
    assert Path(ref.local_path).is_file()
    assert Path(ref.local_path).read_bytes() == png_bytes

    # 3. Convergence with webui (ADR-0013 §12): the adapter must NOT splice a
    #    "Received files:…" block into content — attachment perception is the
    #    shared ingest stage + transient path-reference injection's job, never
    #    the transcript/memory-bound content. Content stays the original text.
    assert env.content == "see this", (
        "QQ adapter must not concatenate attachment info into content; "
        "attachments travel as AttachmentRef, like webui"
    )

    await adapter.stop()  # exercises temp-dir cleanup path


@pytest.mark.asyncio
async def test_qq_attachment_ref_flows_through_im_pipeline_to_persisted_record() -> None:
    """A QQ-produced AttachmentRef (temp local_path + filename) flows through
    the real IM pipeline ingest stage and becomes a persisted ``Attachment``
    under the standard media layout, readable via the resolver."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        staging = root / "staging"
        staging.mkdir()
        png = staging / "photo.png"
        png.write_bytes(_PNG_MAGIC + b"\x00" * 40)

        media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        transcript_store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

        pool_store = MagicMock()
        pool_store.get.return_value = "main"
        cmd = MagicMock()
        cmd._try_intercept_control = AsyncMock(return_value=False)

        ctx = BotInputContext(
            default_pool="main",
            available_pools=lambda: {"main"},
            pool_session_store=pool_store,
            agent_resolver=lambda p: p,
            transcript_store=transcript_store,
            enqueue_message=MagicMock(),
            command_adapter=cmd,
            current_ws_provider=(lambda r=root: r),
            media_store=media_store,
        )
        pipeline = await build_im_pipeline(
            registry=TEST_COMPONENT_REGISTRY,
            ctx=TEST_ASSEMBLY_CTX,
            skill_registry=PoolSkillResolverRegistry({}),
            known_pools={"main"},
        )

        # Exactly the AttachmentRef shape QQ's _on_message now produces.
        env = UserInputEnvelope(
            external_id="u1",
            content="look at this",
            channel="qq",
            explicit_pool=None,
            attachments=[AttachmentRef(local_path=str(png), filename="photo.png")],
        )

        with bind_workspace_root(root):
            result = await pipeline.handle(env, ctx)

        assert result.should_continue()
        assert len(env.resolved_attachments) == 1
        rec = env.resolved_attachments[0]
        assert rec.kind is Kind.IMAGE
        assert rec.locator is AttachmentLocator.MEDIA
        # Original filename is preserved on the record (no opaque-id fallback).
        assert rec.name == "photo.png"
        assert rec.path.startswith(".modex/media/main/uploads/")

        # Reachable through the resolver (locator=media read path).
        with bind_workspace_root(root):
            store = media_store.store_for("main")
            on_disk = store.read(env.metadata["full_session_id"], rec.id)
        assert on_disk is not None
        assert on_disk.read_bytes() == png.read_bytes()

        # Asymmetry matches webui (ADR-0013 §1/§10): the persisted transcript
        # user_message content is the ORIGINAL text only — no "Received files:"
        # splicing, no temp path — and the Attachment travels as a separate
        # structured record. The agent-perception injection is transient
        # (memory-only), added by preprocess, never the transcript content.
        from bot.webui.events import UserMessageEvent

        full_sid = env.metadata["full_session_id"]
        with bind_workspace_root(root):
            events = await transcript_store.load(full_sid)
        user_events = [e for e in events if isinstance(e, UserMessageEvent)]
        assert user_events, "user message must be persisted to the transcript"
        assert user_events[0].content == "look at this"
        assert "Received files:" not in user_events[0].content
        assert "[Attachment:" not in user_events[0].content
        assert len(user_events[0].attachments) == 1
