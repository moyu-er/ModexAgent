from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.input_pipeline.assembly import build_im_pipeline, build_webui_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.skill_parse import ParsedSkill, SkillRegistry
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent
from framework.core.types import InputMessage
from framework.input_pipeline.envelope import UserInputEnvelope


class _NoSkill(SkillRegistry):
    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        return None


class _FakeSkill(SkillRegistry):
    def __init__(self, skills: set[str]) -> None:
        self._skills = skills

    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        if name not in self._skills:
            return None
        return ParsedSkill(name=name, raw=content, xml_form=f"<skill name='{name}'>{content}</skill>")


def _make_ctx(
    store: WorkspaceScopedTranscriptStore,
    enqueued: list[InputMessage] | None = None,
    command_adapter: MagicMock | None = None,
) -> BotInputContext:
    pool_store = MagicMock()
    pool_store.get.return_value = "main"
    sink = enqueued if enqueued is not None else MagicMock()
    # S2 awaits command_adapter._try_intercept_control for every message, so
    # the default mock must be an AsyncMock returning False (not handled).
    cmd = command_adapter or MagicMock()
    if command_adapter is None:
        cmd._try_intercept_control = AsyncMock(return_value=False)
    return BotInputContext(
        default_pool="main",
        pool_session_store=pool_store,
        agent_pool_map={"main": "main", "coding": "coding"},
        agent_resolver=lambda p: p,
        transcript_store=store,
        enqueue_message=(sink.append if enqueued is not None else sink),
        command_adapter=cmd,
    )


@pytest.mark.asyncio
async def test_im_normal_message_persisted_and_enqueued() -> None:
    """IM adapter already produced the seed envelope (S0); pipeline = S2..S8."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued)
        pipe = build_im_pipeline(
            skill_registry=_NoSkill(), known_pools={"main", "coding"}
        )
        env = UserInputEnvelope(conversation_id="u1", content="hello", channel="qq")
        await pipe.handle(env, ctx)
        events = list(store.load("u1.main"))
        assert len(events) == 1 and events[0].content == "hello"
        assert len(enqueued) == 1 and enqueued[0].content == "hello"


@pytest.mark.asyncio
async def test_webui_explicit_coding_pool_persisted_to_coding() -> None:
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"coding": "coding"})
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued)
        pipe = build_webui_pipeline(
            skill_registry=_NoSkill(), known_pools={"coding"}
        )
        env = UserInputEnvelope(
            conversation_id="uuid1",
            content="hi",
            channel="websocket",
            explicit_pool="coding",
        )
        await pipe.handle(env, ctx)
        events = list(store.load("uuid1.coding"))
        assert len(events) == 1 and events[0].content == "hi"


@pytest.mark.asyncio
async def test_im_stop_command_not_persisted() -> None:
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        cmd_adapter = MagicMock()
        # _try_intercept_control is async in the framework -> AsyncMock
        cmd_adapter._try_intercept_control = AsyncMock(return_value=True)
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued, command_adapter=cmd_adapter)
        pipe = build_im_pipeline(skill_registry=_NoSkill(), known_pools={"main"})
        env = UserInputEnvelope(conversation_id="u1", content="/stop", channel="qq")
        await pipe.handle(env, ctx)
        assert list(store.load("u1.main")) == []
        assert enqueued == [], "/stop must not be enqueued"


# ── Missing E2E tests (added per spec §9 review) ───────────────────────


@pytest.mark.asyncio
async def test_im_cd_command_not_persisted() -> None:
    """S2 intercepts /cd via _try_intercept_control, terminates, never persists."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        cmd_adapter = MagicMock()
        cmd_adapter._try_intercept_control = AsyncMock(return_value=True)
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued, command_adapter=cmd_adapter)
        pipe = build_im_pipeline(skill_registry=_NoSkill(), known_pools={"main"})
        env = UserInputEnvelope(conversation_id="u1", content="/cd /tmp", channel="qq")
        result = await pipe.handle(env, ctx)
        assert not result.should_continue(), "/cd must terminate"
        assert list(store.load("u1.main")) == []
        assert enqueued == [], "/cd must not be enqueued"


@pytest.mark.asyncio
async def test_im_pool_command_switches_pool_and_terminates() -> None:
    """S2 intercepts /pool shortcut (e.g. /coding), sets pool, terminates."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued)
        # pool_session_store.get returns "main" by default; /coding switches
        pipe = build_im_pipeline(
            skill_registry=_NoSkill(), known_pools={"main", "coding"}
        )
        env = UserInputEnvelope(conversation_id="u1", content="/coding", channel="qq")
        result = await pipe.handle(env, ctx)
        assert not result.should_continue(), "/coding must terminate"
        ctx.pool_session_store.set.assert_called_once_with("u1", "coding")
        assert list(store.load("u1.main")) == []
        assert enqueued == [], "/coding must not be enqueued"


@pytest.mark.asyncio
async def test_im_exit_command_not_persisted() -> None:
    """S2 intercepts /exit via _try_intercept_control, terminates, never persists."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        cmd_adapter = MagicMock()
        cmd_adapter._try_intercept_control = AsyncMock(return_value=True)
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued, command_adapter=cmd_adapter)
        pipe = build_im_pipeline(skill_registry=_NoSkill(), known_pools={"main"})
        env = UserInputEnvelope(conversation_id="u1", content="/exit", channel="qq")
        result = await pipe.handle(env, ctx)
        assert not result.should_continue(), "/exit must terminate"
        assert list(store.load("u1.main")) == []
        assert enqueued == [], "/exit must not be enqueued"


@pytest.mark.asyncio
async def test_im_valid_skill_persisted_raw_llm_gets_xml() -> None:
    """S6 converts /skillName to XML; S7 persists raw content; S8 enqueues XML."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued)
        pipe = build_im_pipeline(
            skill_registry=_FakeSkill({"office-expert"}),
            known_pools={"main"},
        )
        env = UserInputEnvelope(
            conversation_id="u1",
            content="/office-expert make ppt",
            channel="qq",
        )
        await pipe.handle(env, ctx)
        # Transcript must have raw content (for replay/display)
        events = list(store.load("u1.main"))
        assert len(events) == 1
        assert events[0].content == "/office-expert make ppt"
        # LLM must receive XML form, not raw text
        assert len(enqueued) == 1
        assert enqueued[0].content.startswith("<skill")


@pytest.mark.asyncio
async def test_im_invalid_skill_terminates_not_persisted() -> None:
    """S6 terminates unknown /skill; nothing persisted or enqueued."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued)
        pipe = build_im_pipeline(
            skill_registry=_NoSkill(), known_pools={"main"}
        )
        env = UserInputEnvelope(
            conversation_id="u1", content="/nosuch thing", channel="qq"
        )
        result = await pipe.handle(env, ctx)
        assert not result.should_continue(), "invalid skill must terminate"
        response = getattr(result, "response", {})
        assert isinstance(response, dict) and "message" in response
        assert list(store.load("u1.main")) == []
        assert enqueued == [], "invalid skill must not be enqueued"


@pytest.mark.asyncio
async def test_webui_invalid_skill_terminates_not_persisted() -> None:
    """WebUI pipeline (S4..S8): unknown /skill terminates in S6, not persisted."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued)
        pipe = build_webui_pipeline(
            skill_registry=_NoSkill(), known_pools={"main"}
        )
        env = UserInputEnvelope(
            conversation_id="uuid1",
            content="/nosuch thing",
            channel="websocket",
            explicit_pool="main",
        )
        result = await pipe.handle(env, ctx)
        assert not result.should_continue(), "invalid skill must terminate"
        response = getattr(result, "response", {})
        assert isinstance(response, dict) and "message" in response
        assert list(store.load("uuid1.main")) == []
        assert enqueued == [], "invalid skill must not be enqueued"


@pytest.mark.asyncio
async def test_multi_channel_pool_isolation() -> None:
    """IM pool switch (conv_id="u1") does not affect WebUI conversation (conv_id="uuid1")."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main", "coding": "coding"})

        # Shared pool_session_store with per-conversation keys
        pool_store = MagicMock()
        pool_store.get.return_value = "main"
        enqueued_im: list[InputMessage] = []
        enqueued_ws: list[InputMessage] = []

        # IM context: conversation_id = "u1"
        cmd_adapter_im = MagicMock()
        cmd_adapter_im._try_intercept_control = AsyncMock(return_value=False)
        ctx_im = BotInputContext(
            default_pool="main",
            pool_session_store=pool_store,
            agent_pool_map={"main": "main", "coding": "coding"},
            agent_resolver=lambda p: p,
            transcript_store=store,
            enqueue_message=enqueued_im.append,
            command_adapter=cmd_adapter_im,
        )
        pipe_im = build_im_pipeline(
            skill_registry=_NoSkill(), known_pools={"main", "coding"}
        )

        # WebUI context: conversation_id = "uuid1"
        cmd_adapter_ws = MagicMock()
        cmd_adapter_ws._try_intercept_control = AsyncMock(return_value=False)
        ctx_ws = BotInputContext(
            default_pool="main",
            pool_session_store=pool_store,
            agent_pool_map={"main": "main", "coding": "coding"},
            agent_resolver=lambda p: p,
            transcript_store=store,
            enqueue_message=enqueued_ws.append,
            command_adapter=cmd_adapter_ws,
        )
        pipe_ws = build_webui_pipeline(
            skill_registry=_NoSkill(), known_pools={"main", "coding"}
        )

        # IM switches pool to "coding" via /coding command
        env_im = UserInputEnvelope(conversation_id="u1", content="/coding", channel="qq")
        result_im = await pipe_im.handle(env_im, ctx_im)
        assert not result_im.should_continue()

        # PoolSessionStore.set called with IM conversation_id
        pool_store.set.assert_called_with("u1", "coding")

        # WebUI sends normal message with explicit_pool="main"
        env_ws = UserInputEnvelope(
            conversation_id="uuid1",
            content="hello from webui",
            channel="websocket",
            explicit_pool="main",
        )
        result_ws = await pipe_ws.handle(env_ws, ctx_ws)
        assert result_ws.should_continue()

        # WebUI message is persisted to "uuid1.main", not affected by IM pool switch
        events_ws = list(store.load("uuid1.main"))
        assert len(events_ws) == 1
        assert events_ws[0].content == "hello from webui"

        # IM pool switch did not enqueue or persist any message
        assert enqueued_im == []
        assert list(store.load("u1.main")) == []


@pytest.mark.asyncio
async def test_webui_slash_cd_produces_error_not_enqueued() -> None:
    """WebUI pipeline (no S2/S3): /cd reaches S6 as unknown skill, terminates."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued)
        pipe = build_webui_pipeline(
            skill_registry=_NoSkill(), known_pools={"main"}
        )
        env = UserInputEnvelope(
            conversation_id="uuid1",
            content="/cd /tmp",
            channel="websocket",
            explicit_pool="main",
        )
        result = await pipe.handle(env, ctx)
        assert not result.should_continue(), "/cd must terminate in WebUI"
        # Terminate carries response so _ws_send_message can build ERROR envelope
        response = getattr(result, "response", None)
        assert response is not None, "Terminate must carry a response for ERROR envelope"
        assert list(store.load("uuid1.main")) == []
        assert enqueued == [], "/cd must not be enqueued in WebUI"


@pytest.mark.asyncio
async def test_im_pwd_command_handled_and_not_persisted() -> None:
    """S2 delegates /pwd to _try_intercept_control, which returns True.
    /pwd is a read-only workspace command (like /cd and /exit) —
    it must be handled at the adapter layer, never persist, never enqueue.
    """
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        cmd_adapter = MagicMock()
        cmd_adapter._try_intercept_control = AsyncMock(return_value=True)
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued, command_adapter=cmd_adapter)
        pipe = build_im_pipeline(skill_registry=_NoSkill(), known_pools={"main"})
        env = UserInputEnvelope(conversation_id="u1", content="/pwd", channel="qq")
        result = await pipe.handle(env, ctx)
        assert not result.should_continue(), "/pwd must terminate"
        cmd_adapter._try_intercept_control.assert_awaited_once_with("/pwd", "u1")
        assert list(store.load("u1.main")) == []
        assert enqueued == [], "/pwd must not be enqueued"


# ═══════════════════════════════════════════════════════════════════
# Per-pool skill resolution (S6 uses resolved_pool from S5 envelope)
# ═══════════════════════════════════════════════════════════════════


class _PoolSkillRegistry(SkillRegistry):
    """Registry that maps pool→skills, simulating per-pool SkillManagers."""

    def __init__(self, pool_skills: dict[str, set[str]]) -> None:
        self._pool_skills = pool_skills

    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        skills = self._pool_skills.get(pool, set())
        if name not in skills:
            return None
        return ParsedSkill(
            name=name, raw=content,
            xml_form=f"<skill name='{name}' pool='{pool}'>{content}</skill>",
        )


@pytest.mark.asyncio
async def test_skill_resolved_from_correct_pool() -> None:
    """S6 must use envelope.metadata["resolved_pool"] (set by S5)
    to look up skills in the correct pool's registry."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main", "coding": "coding"})
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued)

        # "coding" pool has office-expert, "main" pool has brainstorming
        registry = _PoolSkillRegistry({
            "main": {"brainstorming"},
            "coding": {"office-expert"},
        })
        pipe = build_im_pipeline(
            skill_registry=registry, known_pools={"main", "coding"},
        )

        # ── /brainstorming in "main" pool ─────────────────────────
        env = UserInputEnvelope(
            conversation_id="u1",
            content="/brainstorming new idea",
            channel="qq",
        )
        result = await pipe.handle(env, ctx)
        assert result.should_continue(), (
            "/brainstorming must succeed in main pool"
        )
        assert enqueued[0].content.startswith("<skill")
        assert "brainstorming" in enqueued[0].content

        # ── /office-expert in "main" pool (NOT available) ────────
        enqueued.clear()
        env2 = UserInputEnvelope(
            conversation_id="u2",
            content="/office-expert make ppt",
            channel="qq",
        )
        result2 = await pipe.handle(env2, ctx)
        assert not result2.should_continue(), (
            "/office-expert must NOT be found in main pool"
        )
        resp = getattr(result2, "response", {})
        assert "not recognized" in str(resp.get("message", ""))
        assert enqueued == []
        # u2.main must be empty — the unrecognized skill was terminated before S7
        assert list(store.load("u2.main")) == []


@pytest.mark.asyncio
async def test_skill_pool_isolation_webui() -> None:
    """WebUI pipeline: explicit_pool="coding" → S5 resolves it →
    S6 checks coding pool, not main pool."""
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main", "coding": "coding"})
        enqueued: list[InputMessage] = []
        ctx = _make_ctx(store, enqueued)

        registry = _PoolSkillRegistry({
            "main": {"brainstorming"},
            "coding": {"office-expert"},
        })
        pipe = build_webui_pipeline(
            skill_registry=registry, known_pools={"main", "coding"},
        )

        # WebUI with explicit_pool="coding" — /office-expert should work
        env = UserInputEnvelope(
            conversation_id="uuid1",
            content="/office-expert make ppt",
            channel="websocket",
            explicit_pool="coding",
        )
        result = await pipe.handle(env, ctx)
        assert result.should_continue(), (
            "/office-expert must succeed in coding pool (WebUI)"
        )

        # Same pool — /brainstorming should NOT work
        enqueued.clear()
        env2 = UserInputEnvelope(
            conversation_id="uuid2",
            content="/brainstorming",
            channel="websocket",
            explicit_pool="coding",
        )
        result2 = await pipe.handle(env2, ctx)
        assert not result2.should_continue(), (
            "/brainstorming must NOT be found in coding pool"
        )
        assert list(store.load("uuid2.coding")) == []
