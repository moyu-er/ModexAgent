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
from modex_agent.media.models import Attachment, AttachmentLocator, Kind
from modex_agent.memory.system import MemorySystemContextManager, create_memory_system
from modex_agent.messaging.broker import Address
from modex_agent.messaging.broker_bridge import build_input_broker_message
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.pool import input_message_from_dispatch_envelope
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.workspace.runtime import bind_workspace_root

# Mechanism-A enrichment layer (native-multimodal-inline unit 5/6, ADR-0014)
from modex_agent.agents.react.nodes.llm import enrich_inline_attachments
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.ioc.configs.llm import ModelCapabilities, Modality
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

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


# ---------------------------------------------------------------------------
# Mechanism A — native multimodal inline (ADR-0014 / OpenSpec
# native-multimodal-inline unit 6). Exercises the enrichment step that the
# real LLM-bound message assembly calls last (`LLMNode._build_messages`), with
# a realistic AgentContext: a real PNG on disk, real Attachment records, a real
# ReActTurnState carrying INLINE_ATTACHMENTS, and ModelCapabilities on the
# runtime. Asserts the three properties the transient-carrier design requires:
#   (i)   vision-capable pool -> image_url inlined on the arrival turn;
#   (ii)  a subsequent turn with no current-turn image attachments -> only the
#         text-reference form survives (no image_url); the cached/persisted
#         history is never mutated by enrichment;
#   (iii) a [text]-only-capable pool never inlines, even with images present.
# ---------------------------------------------------------------------------


def _image_attachment(path: Path, name: str, mime: str) -> Attachment:
    return Attachment(
        id=f"id-{name}",
        kind=Kind.IMAGE,
        name=name,
        mime=mime,
        size=path.stat().st_size,
        path=str(path),
        locator=AttachmentLocator.MEDIA,
    )


def _build_ctx(
    *,
    capabilities: ModelCapabilities,
    inline_attachments: list[Attachment],
) -> AgentContext:
    """A minimal-but-real AgentContext exercising enrich_inline_attachments."""
    session = SessionInfo(session_id="s1", agent_name="main")
    identity = TurnIdentity(agent_id="main", session=session, turn_id="t1")
    state = ReActTurnState(
        identity=identity, agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING
    )
    if inline_attachments:
        state.custom[TurnCustomKey.INLINE_ATTACHMENTS] = inline_attachments
    services = AgentRuntimeServices(model_capabilities=capabilities)
    runtime = AgentRuntime(services=services, state=state)
    return AgentContext(
        system_prompt="sys",
        history=ListMessageHistory(),
        tool_manager=MagicMock(name="tool_manager"),
        session=session,
        runtime=runtime,
        identity=identity,  # non-None so get_react_state proceeds
    )


def _arrival_messages(user_text: str) -> list[dict[str, object]]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": user_text},
    ]


@pytest.mark.asyncio
async def test_mechanism_a_inlines_image_on_arrival_turn_for_vision_pool() -> None:
    """(i) A vision-capable pool inlines image_url on the arrival turn."""
    with TemporaryDirectory() as tmp:
        png = _write(Path(tmp), "photo.png", _PNG)
        att = _image_attachment(png, "photo.png", "image/png")
        ctx = _build_ctx(
            capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
            inline_attachments=[att],
        )
        out = enrich_inline_attachments(_arrival_messages("describe this"), ctx)

    user_msg = out[-1]
    assert user_msg["role"] == "user"
    content = user_msg["content"]
    assert isinstance(content, list), "vision pool must convert content to a block list"
    types = [b.get("type") for b in content]
    assert "image_url" in types, "vision pool must inline the image_url block"
    img_block = next(b for b in content if b.get("type") == "image_url")
    url = img_block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), "data-URL carrier with real base64"
    # Original text is preserved as the leading text block.
    assert content[0]["type"] == "text"
    assert "describe this" in content[0]["text"]


@pytest.mark.asyncio
async def test_mechanism_a_only_text_reference_on_subsequent_turn() -> None:
    """(ii) A subsequent turn (no current-turn image attachments) keeps only the
    text-reference form — enrichment never mutates persisted history, and the
    INLINE_IMAGE_CACHE from a prior turn is NOT re-applied when
    INLINE_ATTACHMENTS is empty."""
    with TemporaryDirectory() as tmp:
        png = _write(Path(tmp), "photo.png", _PNG)
        att = _image_attachment(png, "photo.png", "image/png")
        vision_caps = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))

        # Arrival turn: inlines + populates the per-turn cache.
        arrival_ctx = _build_ctx(
            capabilities=vision_caps, inline_attachments=[att]
        )
        arrival_out = enrich_inline_attachments(_arrival_messages("look"), arrival_ctx)
        assert "image_url" in str(arrival_out)

        # Subsequent turn: same capability, but NO current-turn attachments
        # (history carried forward as a text reference, as mechanism B writes it).
        subsequent_ctx = _build_ctx(
            capabilities=vision_caps, inline_attachments=[]
        )
        subsequent_messages = [
            {"role": "system", "content": "sys"},
            {
                "role": "user",
                "content": "next question [Attachment: photo.png (image/png, 48) @ /p]",
            },
        ]
        out = enrich_inline_attachments(subsequent_messages, subsequent_ctx)

    final_user = out[-1]
    # No inline carrier — only the persisted text-reference form survives.
    if isinstance(final_user["content"], list):
        assert not any(b.get("type") == "image_url" for b in final_user["content"]), (
            "no image_url may survive on a turn without current-turn images"
        )
    else:
        assert "image_url" not in str(final_user["content"])
    assert "[Attachment: photo.png" in str(final_user["content"]), (
        "mechanism-B text reference is preserved untouched"
    )


@pytest.mark.asyncio
async def test_mechanism_a_text_only_pool_never_inlines() -> None:
    """(iii) A [text]-only-capable pool never inlines, even with image
    attachments present on the turn."""
    with TemporaryDirectory() as tmp:
        png = _write(Path(tmp), "photo.png", _PNG)
        att = _image_attachment(png, "photo.png", "image/png")
        ctx = _build_ctx(
            capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT})),
            inline_attachments=[att],
        )
        out = enrich_inline_attachments(_arrival_messages("describe this"), ctx)

    user_msg = out[-1]
    # Content stays a plain string (no block list, no image_url).
    assert not isinstance(user_msg["content"], list), (
        "text-only pool must not convert content to a block list"
    )
    assert "image_url" not in str(user_msg["content"])
    assert user_msg["content"] == "describe this"
