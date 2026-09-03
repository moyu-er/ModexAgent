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

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.input_pipeline.assembly import build_webui_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.skill_parse import PoolSkillResolverRegistry
from bot.service.media_store import WorkspaceScopedMediaStore
from bot.service.model_config import BotModelConfig, ModelCfg, ProviderCfg
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent

from examples.bot_project.tests.input_pipeline.assembly_support import (
    TEST_ASSEMBLY_CTX,
    TEST_COMPONENT_REGISTRY,
)

# Media-lifecycle layer (user-message parts carrier + LLM-boundary injection):
# the carrier (context_assembler) persists media:// reference parts on the user
# message UNCONDITIONALLY (model-agnostic); inject_multimodal applies the
# per-request modality gate and resolves references back to data URLs at the
# LLM call.
from modex_agent.agents.react.media_injection import inject_multimodal
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.media import Attachment, AttachmentLocator, Kind
from modex_agent.core.message import ChatMessage, ImageUrlPart, TextPart
from modex_agent.core.session_id import SessionInfo
from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.ioc.configs.llm import Modality, ModelCapabilities
from modex_agent.memory.context import ContextManager, InMemoryContextManager
from modex_agent.memory.history import ListMessageHistory
from modex_agent.memory.system import MemorySystemContextManager, create_memory_system
from modex_agent.messaging.broker import Address
from modex_agent.messaging.broker_bridge import build_input_broker_message
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.pool import input_message_from_dispatch_envelope
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.workspace.runtime import bind_workspace_root

# A REAL 1x1 PNG (decodable) so the media lifecycle can compress/resolve it.
_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
# JPEG SOI + APP0/JFIF header
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 40
_TXT = b"hello world, this is a plain text attachment\n" * 3


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


def _make_builder(runtime_services: AgentRuntimeServices | None = None) -> TurnContextBuilder:
    """TurnContextBuilder wired for build_turn_request + preprocess + assemble
    (the unit under test). command_processor is None so plain input takes the
    no-command-processor branch — the one whose user_content override used to
    discard the injection."""
    return TurnContextBuilder(
        agent=MagicMock(name="agent"),
        tool_manager=MagicMock(name="tool_manager"),
        sanitizer=None,
        command_processor=None,
        skill_resolver=None,
        context_builder=None,
        agent_descriptor=None,
        max_iterations=5,
        safety=MagicMock(name="safety"),
        runtime_services=runtime_services,
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

        pool_store = MagicMock()
        pool_store.get.return_value = "main"
        cmd = MagicMock()
        cmd._try_intercept_control = AsyncMock(return_value=False)

        enqueued: list[InputMessage] = []
        ctx = BotInputContext(
            default_pool="main",
            available_pools=lambda: {"main"},
            pool_session_store=pool_store,
            agent_resolver=lambda p: p,
            transcript_store=transcript_store,
            enqueue_message=enqueued.append,
            command_adapter=cmd,
            current_ws_provider=(lambda r=root: r),
            media_store=media_store,
        )
        pipeline = await build_webui_pipeline(
            registry=TEST_COMPONENT_REGISTRY,
            ctx=TEST_ASSEMBLY_CTX,
            skill_registry=PoolSkillResolverRegistry({}),
            bot_model_config=_bot_model_config(),
        )

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
            events = await transcript_store.load(full_sid)
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
            envelope, session=msg.session
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
            sanitized = await builder.preprocess(
                redispatched, full_sid, {}, None
            )
        assert sanitized is not None
        assert sanitized.startswith(user_text), "original user text preserved at head"
        assert "[Attachment: photo.png (image/png, " in sanitized
        assert "[Attachment: scan.jpg (image/jpeg, " in sanitized
        assert "[Attachment: notes.txt (text/plain, " in sanitized
        # Absolute path (resolved against the bound workspace root).
        assert str((root / by_name["photo.png"].path).resolve()) in sanitized

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
                ctx_mgr,
                None,
                False,
            )

        # LLM-bound history carries the injected user message. Two image
        # attachments ride as parts: [TextPart(含引用行), ImageUrlPart×2];
        # the reference lines live in the text part.
        history_msgs = await context_state.history.to_list()
        user_msgs = [m for m in history_msgs if m.get("role") == "user"]
        assert user_msgs, "user message must reach the LLM-bound history"
        carrier_text, carrier_images = _parts_summary(user_msgs[-1]["content"])
        assert "[Attachment: photo.png (image/png, " in carrier_text
        assert "[Attachment: notes.txt (text/plain, " in carrier_text
        assert carrier_images == 2, "both image attachments ride as parts"

        # ── 7. Saved to session memory: a fresh load (re-read from disk) still
        #    has the injection — proves it persisted to messages.jsonl, not just
        #    in-memory. ──
        with bind_workspace_root(root):
            reloaded = await ctx_mgr.load(full_sid, tool_manager=MagicMock())
        reloaded_user = [
            m for m in (await reloaded.history.to_list()) if m.get("role") == "user"
        ]
        assert reloaded_user, "user message must persist to session memory"
        reloaded_text, reloaded_images = _parts_summary(reloaded_user[-1]["content"])
        assert "[Attachment: photo.png (image/png, " in reloaded_text
        assert "[Attachment: scan.jpg (image/jpeg, " in reloaded_text
        assert reloaded_images == 2


# ---------------------------------------------------------------------------
# Media lifecycle — user-message parts carrier + LLM-boundary injection.
# Exercises the real chain: pipeline upload → preprocess → assemble_context
# carrier (media:// reference parts persisted UNCONDITIONALLY — the carrier is
# model-agnostic) → inject_multimodal applying the per-request modality gate
# and resolving references to data URLs at the LLM call. Asserts the three
# properties the persisted-carrier design requires:
#   (i)   vision-capable pool → the user message PERSISTS media:// reference
#         parts (never base64), and injection resolves them to data URLs;
#   (ii)  a plain turn without image attachments keeps the string content
#         form (no parts, no image_url);
#   (iii) a [text]-only-capable pool STILL persists the reference parts (a
#         later vision-model turn on the same session sees the images);
#         inject_multimodal is the gate that drops them per-request.
# ---------------------------------------------------------------------------

_VISION_CAPS = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))
_TEXT_CAPS = ModelCapabilities(modalities=frozenset({Modality.TEXT}))


def _parts_summary(content: object) -> tuple[str, int]:
    """(joined text, image-part count) of a user-message content value.

    Accepts both part shapes that cross this chain: pydantic ContentPart
    objects (in-memory history) and their dict form (persistence round-trip).
    """
    if not isinstance(content, list):
        return (content if isinstance(content, str) else ""), 0
    texts: list[str] = []
    images = 0
    for part in content:
        if isinstance(part, TextPart):
            texts.append(part.text)
        elif isinstance(part, ImageUrlPart):
            images += 1
        elif isinstance(part, dict):
            if part.get("type") == "text":
                texts.append(str(part.get("text", "")))
            elif part.get("type") == "image_url":
                images += 1
    return "".join(texts), images


def _make_ctx(
    *,
    capabilities: ModelCapabilities,
    store: object | None,
    session: str,
) -> AgentContext:
    """A minimal-but-real AgentContext exercising inject_multimodal."""
    session_info = SessionInfo.from_str(session)
    identity = TurnIdentity(agent_id="main", session=session_info, turn_id="t1")
    state = ReActTurnState(
        identity=identity, agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING
    )
    services = AgentRuntimeServices(
        model_info=ModelInfo(model_name="test", capabilities=capabilities),
    )
    services.media_store = store  # type: ignore[assignment]
    runtime = AgentRuntime(services=services, state=state)
    return AgentContext(
        system_prompt="sys",
        history=ListMessageHistory(),
        tool_manager=MagicMock(name="tool_manager"),
        session=session_info,
        runtime=runtime,
        identity=identity,
    )


async def _carrier_user_message(
    *,
    capabilities: ModelCapabilities,
    redispatched: InputMessage,
    full_sid: str,
    ctx_mgr: ContextManager,
) -> ChatMessage:
    """Run preprocess + assemble with the given model caps; return the persisted
    user message from the LLM-bound history."""
    services = AgentRuntimeServices()
    services.model_info = ModelInfo(model_name="test", capabilities=capabilities)
    builder = _make_builder(runtime_services=services)
    sanitized = await builder.preprocess(redispatched, full_sid, {}, None)
    assert sanitized is not None
    state = await builder.assemble(
        full_sid, redispatched, {}, sanitized, ctx_mgr, None, False
    )
    messages = await state.history.to_list()
    return next(m for m in messages if m.role == "user")


@pytest.mark.asyncio
async def test_media_carrier_persists_refs_and_injection_resolves() -> None:
    """(i) Vision pool: the user message persists media:// reference parts;
    inject_multimodal resolves them to data URLs backed by the uploaded bytes."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        media_store = WorkspaceScopedMediaStore(data_dir_name=".modex")
        png = _write(root, "photo.png", _PNG)
        env = UserInputEnvelope(
            external_id="u1",
            content="describe this",
            channel="websocket",
            explicit_pool="main",
            attachments=[AttachmentRef(local_path=str(png), filename="photo.png")],
        )
        pool_store = MagicMock()
        pool_store.get.return_value = "main"
        cmd = MagicMock()
        cmd._try_intercept_control = AsyncMock(return_value=False)
        enqueued: list[InputMessage] = []
        pipeline = await build_webui_pipeline(
            registry=TEST_COMPONENT_REGISTRY,
            ctx=TEST_ASSEMBLY_CTX,
            skill_registry=PoolSkillResolverRegistry({}),
            bot_model_config=_bot_model_config(),
        )
        pipeline_ctx = BotInputContext(
            default_pool="main",
            available_pools=lambda: {"main"},
            pool_session_store=pool_store,
            agent_resolver=lambda p: p,
            transcript_store=WorkspaceScopedTranscriptStore(data_dir_name=".modex"),
            enqueue_message=enqueued.append,
            command_adapter=cmd,
            current_ws_provider=(lambda r=root: r),
            media_store=media_store,
        )
        with bind_workspace_root(root):
            result = await pipeline.handle(env, pipeline_ctx)
        assert result.should_continue()
        assert len(enqueued) == 1
        full_sid = str(env.metadata["full_session_id"])
        img = env.resolved_attachments[0]
        assert img.kind is Kind.IMAGE

        redispatched = InputMessage(
            content="describe this",
            session=enqueued[0].session,
            attachments_resolved=[img],
        )
        with bind_workspace_root(root):
            user_msg = await _carrier_user_message(
                capabilities=_VISION_CAPS,
                redispatched=redispatched,
                full_sid=full_sid,
                ctx_mgr=InMemoryContextManager(),
            )

        # The persisted user message carries reference parts — never base64.
        content = user_msg.content
        assert isinstance(content, list)
        assert isinstance(content[0], TextPart)
        assert "describe this" in content[0].text
        image_part = content[1]
        assert isinstance(image_part, ImageUrlPart)
        assert image_part.image_url.url == f"media://{img.id}"
        assert "base64" not in str(content)

        # inject_multimodal resolves the reference against the pool store.
        with bind_workspace_root(root):
            store = media_store.store_for("main")
            ctx = _make_ctx(
                capabilities=_VISION_CAPS, store=store, session=full_sid
            )
            injected = inject_multimodal(
                [ChatMessage.coerce(user_msg.to_dict())], ctx
            )
        injected_content = injected[0].content
        assert isinstance(injected_content, list)
        injected_image = next(
            p for p in injected_content if isinstance(p, ImageUrlPart)
        )
        assert injected_image.image_url.url.startswith("data:image/png;base64,")
        payload = injected_image.image_url.url.split(",", 1)[1]
        assert base64.b64decode(payload) == _PNG


@pytest.mark.asyncio
async def test_media_carrier_plain_turn_stays_string() -> None:
    """(ii) A turn without image attachments keeps the string content form."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        redispatched = InputMessage(
            content="just a question",
            session=SessionInfo.from_str("conv.main"),
        )
        with bind_workspace_root(root):
            user_msg = await _carrier_user_message(
                capabilities=_VISION_CAPS,
                redispatched=redispatched,
                full_sid="conv.main",
                ctx_mgr=InMemoryContextManager(),
            )

        assert isinstance(user_msg.content, str)
        assert user_msg.content == "just a question"
        assert "image_url" not in user_msg.content


@pytest.mark.asyncio
async def test_media_carrier_text_only_pool_still_persists_parts() -> None:
    """(iii) A [text]-only-capable pool still persists the reference parts —
    the carrier is model-agnostic; inject_multimodal drops the image
    per-request (text + mechanism-B lines remain) so a later vision-model
    turn on the same session still sees the images."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        png = _write(root, "photo.png", _PNG)
        att = Attachment(
            id="id-photo",
            kind=Kind.IMAGE,
            name="photo.png",
            mime="image/png",
            size=len(_PNG),
            path=str(png),
            locator=AttachmentLocator.MEDIA,
        )
        redispatched = InputMessage(
            content="describe this",
            session=SessionInfo.from_str("conv.main"),
            attachments_resolved=[att],
        )
        with bind_workspace_root(root):
            user_msg = await _carrier_user_message(
                capabilities=_TEXT_CAPS,
                redispatched=redispatched,
                full_sid="conv.main",
                ctx_mgr=InMemoryContextManager(),
            )

        content = user_msg.content
        assert isinstance(content, list), "carrier persists parts regardless of caps"
        assert isinstance(content[0], TextPart)
        assert "[Attachment: photo.png" in content[0].text
        assert isinstance(content[1], ImageUrlPart)
        assert content[1].image_url.url == "media://id-photo"

        # The per-request gate: text-only caps → inject drops the image part,
        # keeps the text (mechanism-B path line still informs the agent).
        ctx = _make_ctx(capabilities=_TEXT_CAPS, store=None, session="conv.main")
        injected = inject_multimodal([ChatMessage.coerce(user_msg.to_dict())], ctx)
        injected_content = injected[0].content
        assert isinstance(injected_content, list)
        assert all(not isinstance(p, ImageUrlPart) for p in injected_content)
        injected_text = next(p for p in injected_content if isinstance(p, TextPart))
        assert "[Attachment: photo.png" in injected_text.text
