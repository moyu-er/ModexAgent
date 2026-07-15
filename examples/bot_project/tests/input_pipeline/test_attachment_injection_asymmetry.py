"""Regression: the mechanism-B path-reference injection is TRANSIENT.

ADR-0013 §10: ``preprocess`` injects ``[Attachment: ...]`` into the agent LLM
history only. The persisted transcript user-message event keeps the ORIGINAL
content and carries the Attachment record separately (G4). So after a turn
with an accepted attachment:

* the persisted ``UserMessageEvent.content`` does NOT contain ``[Attachment:``;
* the agent LLM history (the ``sanitized_content`` preprocess returns, which
  ``assemble_context`` appends to ``context_state.history``) DOES contain it.

This is the "memory vs session-management differ by one injection" property.
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
from bot.webui.events import UserMessageEvent

from modex_agent.core.types import InputMessage
from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.media.models import AttachmentLocator, Kind
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
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


def _make_builder() -> TurnContextBuilder:
    """A minimal TurnContextBuilder wired only for preprocess (the unit under test)."""
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


@pytest.mark.asyncio
async def test_injection_is_transient_transcript_excludes_it() -> None:
    """End-to-end: a WebUI attachment turn persists raw content to the transcript
    but injects the path reference only into the agent LLM history bound content."""
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

        enqueued: list[InputMessage] = []
        ctx = BotInputContext(
            default_pool="main",
            pool_session_store=pool_store,
            agent_pool_map={"main": "main"},
            agent_resolver=lambda p: p,
            transcript_store=transcript_store,
            enqueue_message=enqueued.append,  # capture the InputMessage built by EnqueueStage
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
        # G3 produced exactly one resolved Attachment, carried onto the InputMessage.
        assert len(env.resolved_attachments) == 1
        rec = env.resolved_attachments[0]
        assert rec.kind is Kind.IMAGE
        assert rec.locator is AttachmentLocator.MEDIA
        assert len(enqueued) == 1
        msg = enqueued[0]
        # Typed carriage: the resolved Attachment records reach the turn.
        assert msg.attachments_resolved == env.resolved_attachments

        # --- Transcript side: persisted user-message content is the RAW content,
        #     with the Attachment record attached separately. NO injection. ---
        full_sid = env.metadata["full_session_id"]
        with bind_workspace_root(root):
            events = await transcript_store.load(full_sid)
        user_events = [e for e in events if isinstance(e, UserMessageEvent)]
        assert len(user_events) == 1
        persisted = user_events[0]
        assert persisted.content == "look at this", "transcript keeps the original content"
        assert "[Attachment:" not in persisted.content, (
            "the path-reference injection MUST NOT enter the persisted transcript"
        )
        # The Attachment record rides the event as the id→path index (G4).
        assert len(persisted.attachments) == 1
        assert persisted.attachments[0]["kind"] == "image"

        # --- Agent LLM history side: preprocess injects the path reference into
        #     the content that assemble_context appends to context_state.history. ---
        with bind_workspace_root(root):
            builder = _make_builder()
            sanitized, media_blocks, media_processor = await builder.preprocess(
                msg, full_sid, {}, None
            )

        assert sanitized is not None
        assert sanitized.startswith("look at this\n"), "original content preserved at head"
        assert "[Attachment: photo.png (image/png, " in sanitized, (
            "LLM-history-bound content carries the transient injection"
        )
        assert sanitized.rstrip().endswith("]"), (
            f"injection line should close the outer bracket; got: {sanitized!r}"
        )
        # The injection carries an absolute path (resolved against the ws root).
        assert str((root / rec.path).resolve()) in sanitized or rec.path in sanitized
        # Mechanism A dormant in v1.
        assert media_blocks == []
        assert media_processor is None
