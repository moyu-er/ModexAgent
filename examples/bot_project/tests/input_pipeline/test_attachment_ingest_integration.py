"""Pipeline integration test — an AttachmentRef becomes a persisted Attachment.

Runs the full WebUI pipeline (real stage composition from
:func:`build_webui_pipeline`) end to end with a real
:class:`WorkspaceScopedMediaStore` and asserts the channel-produced
``AttachmentRef`` flows through ingest → resolved Attachment on the envelope,
persisted under the media layout and reachable through the resolver.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.input_pipeline.assembly import build_webui_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.skill_parse import ParsedSkill, SkillRegistry
from bot.service.media_store import WorkspaceScopedMediaStore
from bot.service.model_config import BotModelConfig, ModelCfg, ProviderCfg
from bot.service.workspace_store import WorkspaceScopedTranscriptStore

from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.media.models import AttachmentLocator, Kind
from modex_agent.workspace.runtime import bind_workspace_root

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class _NoSkill(SkillRegistry):
    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        return None


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


@pytest.mark.asyncio
async def test_pipeline_turns_attachment_ref_into_persisted_attachment() -> None:
    """A WebUI envelope carrying one PNG AttachmentRef flows through the full
    pipeline and produces exactly one resolved Attachment, persisted under the
    media layout and readable via the resolver."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        uploads = root / "uploads"
        uploads.mkdir()
        png = uploads / "photo.png"
        png.write_bytes(_PNG_MAGIC + b"\x00" * 40)

        media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        transcript_store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        transcript_store.set_agent_pool_map({"main": "main"})

        pool_store = MagicMock()
        pool_store.get.return_value = "main"
        cmd = MagicMock()
        cmd._try_intercept_control = AsyncMock(return_value=False)

        ctx = BotInputContext(
            default_pool="main",
            pool_session_store=pool_store,
            agent_pool_map={"main": "main"},
            agent_resolver=lambda p: p,
            transcript_store=transcript_store,
            enqueue_message=MagicMock(),
            command_adapter=cmd,
            current_ws_provider=(lambda r=root: r),
            media_store=media_store,
        )
        pipeline = build_webui_pipeline(
            skill_registry=_NoSkill(), bot_model_config=_bot_model_config()
        )

        env = UserInputEnvelope(
            external_id="u1",
            content="look at this",
            channel="websocket",
            explicit_pool="main",
            attachments=[AttachmentRef(local_path=str(png), filename="photo.png")],
        )

        with bind_workspace_root(root):
            result = await pipeline.handle(env, ctx)

        assert result.should_continue()
        assert len(env.resolved_attachments) == 1
        rec = env.resolved_attachments[0]
        assert rec.kind is Kind.IMAGE
        assert rec.locator is AttachmentLocator.MEDIA
        assert rec.path.startswith(".modex/media/main/uploads/")
        # Reachable through the resolver (locator=media read path).
        with bind_workspace_root(root):
            store = media_store.store_for("main")
            on_disk = store.read(env.metadata["full_session_id"], rec.id)
        assert on_disk is not None
        assert on_disk.read_bytes() == png.read_bytes()


@pytest.mark.asyncio
async def test_pipeline_noop_when_no_attachments() -> None:
    """Regression: an envelope with no attachments still completes the pipeline
    unchanged — the ingest stage is a pure pass-through then."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        transcript_store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        transcript_store.set_agent_pool_map({"main": "main"})

        pool_store = MagicMock()
        pool_store.get.return_value = "main"
        cmd = MagicMock()
        cmd._try_intercept_control = AsyncMock(return_value=False)

        ctx = BotInputContext(
            default_pool="main",
            pool_session_store=pool_store,
            agent_pool_map={"main": "main"},
            agent_resolver=lambda p: p,
            transcript_store=transcript_store,
            enqueue_message=MagicMock(),
            command_adapter=cmd,
            current_ws_provider=(lambda r=root: r),
            media_store=media_store,
        )
        pipeline = build_webui_pipeline(
            skill_registry=_NoSkill(), bot_model_config=_bot_model_config()
        )

        env = UserInputEnvelope(external_id="u1", content="hi", channel="websocket")
        with bind_workspace_root(root):
            result = await pipeline.handle(env, ctx)

        assert result.should_continue()
        assert env.resolved_attachments == []
