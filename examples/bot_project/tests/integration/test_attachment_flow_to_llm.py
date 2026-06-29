"""End-to-end attachment data-flow + injection regression guard (ADR-0013).

Mocks uploads of several file kinds (PNG image, JPEG image, plain-text doc) +
user text, then drives the FULL inbound chain up to (not including) the LLM
call, asserting the three properties that were repeatedly broken:

1. **Data flow completeness** — each uploaded file becomes a gate-accepted
   Attachment record (correct kind/mime/path) carried on the InputMessage.
2. **LLM injection completeness** — the mechanism-B path reference
   ``[Attachment: name (mime, size) @ abs_path]`` reaches the LLM-bound
   history (``context_state.history``) for every attachment, surviving the
   broker dispatch boundary AND the turn_runner user_content override.
3. **Asymmetry** — the injection is NOT in session management (the persisted
   transcript ``UserMessageEvent.content`` stays the original text) but IS
   saved to session memory (``messages.jsonl`` carries the injection).

These three drops (media_store wiring / broker serialization / turn_runner
override) each silently passed the prior test suite because every existing
test bypassed at least one layer. This test exercises the real layers.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.input_pipeline.assembly import build_webui_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.input_pipeline.stages.skill_parse import ParsedSkill, SkillRegistry
from bot.service.media_store import WorkspaceScopedMediaStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent

from modex_agent.core.types import InputMessage
from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.media.models import AttachmentLocator, Kind
from modex_agent.memory.system import MemorySystemContextManager, create_memory_system
from modex_agent.messaging.broker import Address
from modex_agent.messaging.broker_bridge import build_input_broker_message
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.pool import input_message_from_dispatch_envelope
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.workspace.runtime import bind_workspace_root

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
# JPEG SOI + APP0/JFIF header
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 40
_TXT = b"hello world, this is a plain text attachment\n" * 3


class _NoSkill(SkillRegistry):
    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        return None


def _make_builder() -> TurnContextBuilder:
    """TurnContextBuilder wired for build_turn_request + preprocess + assemble
    (the unit under test). command_processor is None so plain input takes the
    no-command-processor branch — the one whose user_content override used to
    discard the injection."""
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


def _write(root: Path, name: str, data: bytes) -> Path:
    uploads = root / "uploads"
    uploads.mkdir(exist_ok=True)
    p = uploads / name
    p.write_bytes(data)
    return p


@pytest.mark.asyncio
async def test_attachment_flow_to_llm_injection_and_asymmetry() -> None:
    """Full chain: mocked uploads -> input pipeline -> broker -> turn build ->
    preprocess -> assemble, asserting data flow, LLM injection, and the
    transcript-clean / memory-injected asymmetry."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        # --- mock uploads: PNG, JPEG, plain-text, staged as the upload
        # endpoint would (temp files the AttachmentRefs point at). ---
        png = _write(root, "photo.png", _PNG)
        jpg = _write(root, "scan.jpg", _JPEG)
        txt = _write(root, "notes.txt", _TXT)

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
            enqueue_message=enqueued.append,
            command_adapter=cmd,
            current_ws_provider=(lambda r=root: r),
            media_store=media_store,
        )
        pipeline = build_webui_pipeline(skill_registry=_NoSkill())

        user_text = "please look at these files"
        env = UserInputEnvelope(
            external_id="u1",
            content=user_text,
            channel="websocket",
            explicit_pool="main",
            attachments=[
                AttachmentRef(local_path=str(png), filename="photo.png"),
                AttachmentRef(local_path=str(jpg), filename="scan.jpg"),
                AttachmentRef(local_path=str(txt), filename="notes.txt"),
            ],
        )

        with bind_workspace_root(root):
            result = await pipeline.handle(env, ctx)
        assert result.should_continue()

        # ── 1. Data flow completeness: 3 gate-accepted records, correct kinds ──
        assert len(env.resolved_attachments) == 3
        assert len(enqueued) == 1
        msg = enqueued[0]
        assert msg.attachments_resolved == env.resolved_attachments
        by_name = {r.name: r for r in msg.attachments_resolved}
        assert by_name["photo.png"].kind is Kind.IMAGE
        assert by_name["photo.png"].mime == "image/png"
        assert by_name["scan.jpg"].kind is Kind.IMAGE
        assert by_name["scan.jpg"].mime == "image/jpeg"
        assert by_name["notes.txt"].kind is Kind.EXTRACTABLE_DOCUMENT
        assert by_name["notes.txt"].mime == "text/plain"
        for rec in msg.attachments_resolved:
            assert rec.locator is AttachmentLocator.MEDIA
            assert rec.path.startswith(".modex/media/main/uploads/")  # ws-relative

        # ── 2. Asymmetry: transcript user-message content is the ORIGINAL text,
        #    with the records attached separately. NO injection. ──
        full_sid = env.metadata["full_session_id"]
        with bind_workspace_root(root):
            events = list(transcript_store.load(full_sid))
        user_events = [e for e in events if isinstance(e, UserMessageEvent)]
        assert len(user_events) == 1
        assert user_events[0].content == user_text
        assert "[Attachment:" not in user_events[0].content
        assert len(user_events[0].attachments) == 3

        # ── 3. Broker dispatch boundary: records survive the broker round-trip
        #    (PoolRouter._route_to_pool -> input_message_from_dispatch_envelope). ──
        broker_msg = build_input_broker_message(msg, Address(kind="agent", name="main"))
        envelope = AgentMessageEnvelope.from_broker_message(broker_msg)
        assert envelope is not None
        redispatched = input_message_from_dispatch_envelope(
            envelope, session=msg.session, metadata={}
        )
        assert len(redispatched.attachments_resolved) == 3
        assert {r.name for r in redispatched.attachments_resolved} == {
            "photo.png", "scan.jpg", "notes.txt",
        }

        # ── 4. turn_runner override does NOT discard the injection: plain input
        #    returns user_content=None so preprocess's sanitized content is kept. ──
        builder = _make_builder()
        turn_req = await builder.build_turn_request(redispatched, full_sid, {}, None)
        assert turn_req is not None
        assert turn_req.user_content is None, (
            "plain input must not override preprocess's injected content"
        )

        # ── 5. preprocess injects a path-reference line for EVERY attachment,
        #    each carrying the resolved absolute path. ──
        with bind_workspace_root(root):
            sanitized, media_blocks, _ = await builder.preprocess(
                redispatched, full_sid, {}, None
            )
        assert sanitized is not None
        assert sanitized.startswith(user_text), "original user text preserved at head"
        assert "[Attachment: photo.png (image/png, " in sanitized
        assert "[Attachment: scan.jpg (image/jpeg, " in sanitized
        assert "[Attachment: notes.txt (text/plain, " in sanitized
        # Absolute path (resolved against the bound workspace root).
        assert str((root / by_name["photo.png"].path).resolve()) in sanitized
        assert media_blocks == [], "mechanism A is dormant in v1"

        # ── 6. assemble: the injected content is what the LLM receives
        #    (context_state.history user message), AND it is persisted to
        #    session memory. Uses a real file-backed memory system. ──
        mem_root = root / "memory"
        mem_system = create_memory_system(workspace=mem_root, session_only=True)
        ctx_mgr = MemorySystemContextManager(
            memory_system=mem_system, base_system_prompt="test"
        )
        with bind_workspace_root(root):
            context_state = await builder.assemble(
                full_sid,
                redispatched,
                {"input_metadata": {}},
                sanitized,
                [],
                None,
                ctx_mgr,
                None,
                False,
            )

        # LLM-bound history carries the injected user message.
        history_msgs = await context_state.history.to_list()
        user_msgs = [m for m in history_msgs if m.get("role") == "user"]
        assert user_msgs, "user message must reach the LLM-bound history"
        assert "[Attachment: photo.png (image/png, " in user_msgs[-1]["content"]
        assert "[Attachment: notes.txt (text/plain, " in user_msgs[-1]["content"]

        # ── 7. Saved to session memory: a fresh load (re-read from disk) still
        #    has the injection — proves it persisted to messages.jsonl, not just
        #    in-memory. ──
        with bind_workspace_root(root):
            reloaded = await ctx_mgr.load(full_sid, tool_manager=MagicMock())
        reloaded_user = [
            m for m in (await reloaded.history.to_list()) if m.get("role") == "user"
        ]
        assert reloaded_user, "user message must persist to session memory"
        assert "[Attachment: photo.png (image/png, " in reloaded_user[-1]["content"]
        assert "[Attachment: scan.jpg (image/jpeg, " in reloaded_user[-1]["content"]
