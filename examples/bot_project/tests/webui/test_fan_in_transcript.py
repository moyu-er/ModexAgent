"""Tests: FanInInputAdapter message forwarding + IM pipeline persistence.

FanIn no longer persists user messages — that responsibility is now owned
by ``PersistUserMessageStage`` (S7) in the input pipeline.  These tests
verify:

1. FanIn still forwards messages from all sources to the merged queue
2. IM pipeline (S2..S8) persists user messages via the pipeline
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.adapters.fan_in import FanInInputAdapter
from bot.input_pipeline.assembly import build_im_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.skill_parse import ParsedSkill, SkillRegistry
from bot.webui.transcript_store import JSONLTranscriptStore
from framework.core.session_id import SessionInfo, SessionIdFactory
from framework.core.types import InputMessage
from framework.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from framework.pipeline.adapters import InputAdapter


def _sid(agent: str, conv: str) -> str:
    """Factory-derived full session id for an agent + conversation_id."""
    return SessionIdFactory().create(agent_name=agent, external_id=conv).session_id

# ── Stub adapters / registries for testing ────────────────────────────────────


class _StubIMAdapter(InputAdapter):
    """Simulates an IM adapter (QQ, Discord, Slack, etc.) that yields
    ``InputMessage`` objects from its ``receive()`` method."""

    def __init__(self, name: str = "qq", messages: list[InputMessage] | None = None) -> None:
        self._name = name
        self._messages = list(messages or [])
        self._queue: asyncio.Queue[InputMessage] = asyncio.Queue()
        for m in self._messages:
            self._queue.put_nowait(m)

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        while True:
            msg = await self._queue.get()
            yield msg

    def enqueue(self, msg: InputMessage) -> None:
        """Inject a message for the pump to pick up."""
        self._queue.put_nowait(msg)


class _NoSkillRegistry(SkillRegistry):
    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_store(tmp: Path) -> JSONLTranscriptStore:
    return JSONLTranscriptStore(tmp / "transcripts")


def _make_pipeline_ctx(store: JSONLTranscriptStore, enqueued: list[InputMessage] | None = None) -> BotInputContext:
    """Build a BotInputContext wired to the IM pipeline."""
    pool_store = MagicMock()
    pool_store.get.return_value = "main"
    cmd_adapter = MagicMock()
    cmd_adapter._try_intercept_control = AsyncMock(return_value=False)
    sink = enqueued if enqueued is not None else MagicMock()
    return BotInputContext(
        default_pool="main",
        pool_session_store=pool_store,
        agent_pool_map={"main": "main", "coding": "coding"},
        agent_resolver=lambda p: p,
        transcript_store=store,
        enqueue_message=(sink.append if enqueued is not None else sink),
        command_adapter=cmd_adapter,
    )


# ══════════════════════════════════════════════════════════════════════════════
# FanIn forwarding tests (no persistence — pipeline owns that)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fan_in_forwards_messages_from_single_source() -> None:
    """FanIn forwards messages from a single IM adapter to the merged queue."""
    qq = _StubIMAdapter(name="qq")
    fan_in = FanInInputAdapter()
    fan_in.add_source(qq)

    await fan_in.start()
    try:
        qq.enqueue(InputMessage(content="hello", session=SessionInfo.from_str("u1", default_agent_name="main"), source="qq"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            break
    finally:
        await fan_in.stop()

    assert len(received) == 1
    assert received[0].content == "hello"


@pytest.mark.asyncio
async def test_fan_in_forwards_from_multiple_sources() -> None:
    """FanIn forwards messages from multiple IM sources in arrival order."""
    qq = _StubIMAdapter(name="qq")
    discord = _StubIMAdapter(name="discord")

    fan_in = FanInInputAdapter()
    fan_in.add_source(qq)
    fan_in.add_source(discord)

    await fan_in.start()
    try:
        qq.enqueue(InputMessage(content="QQ msg", session=SessionInfo.from_str("qq_1", default_agent_name="main"), source="qq"))
        discord.enqueue(InputMessage(content="Discord msg", session=SessionInfo.from_str("dc_1", default_agent_name="main"), source="discord"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            if len(received) >= 2:
                break
    finally:
        await fan_in.stop()

    assert len(received) == 2
    contents = {m.content for m in received}
    assert "QQ msg" in contents
    assert "Discord msg" in contents


@pytest.mark.asyncio
async def test_fan_in_forwards_sequential_messages() -> None:
    """FanIn forwards multiple sequential messages from the same source."""
    qq = _StubIMAdapter(name="qq")
    fan_in = FanInInputAdapter()
    fan_in.add_source(qq)

    await fan_in.start()
    try:
        qq.enqueue(InputMessage(content="first", session=SessionInfo.from_str("u1", default_agent_name="main"), source="qq"))
        qq.enqueue(InputMessage(content="second", session=SessionInfo.from_str("u1", default_agent_name="main"), source="qq"))
        qq.enqueue(InputMessage(content="third", session=SessionInfo.from_str("u1", default_agent_name="main"), source="qq"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            if len(received) >= 3:
                break
    finally:
        await fan_in.stop()

    assert len(received) == 3
    assert [m.content for m in received] == ["first", "second", "third"]


# ══════════════════════════════════════════════════════════════════════════════
# IM pipeline persistence tests (replaces old FanIn persistence assertions)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_im_pipeline_persists_qq_message() -> None:
    """QQ message run through the IM pipeline is persisted as UserMessageEvent."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    enqueued: list[InputMessage] = []
    ctx = _make_pipeline_ctx(store, enqueued)
    pipe = build_im_pipeline(skill_registry=_NoSkillRegistry(), known_pools={"main", "coding"})

    env = UserInputEnvelope(
        external_id="qq_user_123",
        content="help me write a Python script",
        channel="qq",
    )
    await pipe.handle(env, ctx)

    # Persisted
    events = list(store.load(_sid("main", "qq_user_123")))
    assert len(events) == 1
    assert events[0].event == "user_message"
    assert events[0].content == "help me write a Python script"
    assert events[0].session_id == _sid("main", "qq_user_123")
    assert events[0].agent_name == "main"

    # Enqueued
    assert len(enqueued) == 1
    assert enqueued[0].content == "help me write a Python script"


@pytest.mark.asyncio
async def test_im_pipeline_persists_discord_message() -> None:
    """Discord message run through the IM pipeline is persisted."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)


    enqueued: list[InputMessage] = []
    ctx = _make_pipeline_ctx(store, enqueued)
    pipe = build_im_pipeline(skill_registry=_NoSkillRegistry(), known_pools={"main"})

    env = UserInputEnvelope(
        external_id="discord_session_1",
        content="deploy to production",
        channel="discord",
    )
    await pipe.handle(env, ctx)

    events = list(store.load(_sid("main", "discord_session_1")))
    assert len(events) == 1
    assert events[0].content == "deploy to production"

    assert len(enqueued) == 1
    assert enqueued[0].content == "deploy to production"


@pytest.mark.asyncio
async def test_im_pipeline_persists_message_with_attachments() -> None:
    """IM message with attachment metadata is persisted."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)


    enqueued: list[InputMessage] = []
    ctx = _make_pipeline_ctx(store, enqueued)
    pipe = build_im_pipeline(skill_registry=_NoSkillRegistry(), known_pools={"main"})

    env = UserInputEnvelope(
        external_id="tg_chat_456",
        content="analyze this image",
        channel="telegram",
        attachments=[AttachmentRef(local_path="/tmp/img.jpg")],
    )
    await pipe.handle(env, ctx)

    events = list(store.load(_sid("main", "tg_chat_456")))
    assert len(events) == 1
    assert events[0].content == "analyze this image"


@pytest.mark.asyncio
async def test_im_pipeline_persists_multiple_sequential() -> None:
    """Multiple sequential messages from same user — all persisted in order."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)


    enqueued: list[InputMessage] = []
    ctx = _make_pipeline_ctx(store, enqueued)
    pipe = build_im_pipeline(skill_registry=_NoSkillRegistry(), known_pools={"main"})

    for text in ("first question", "second question", "third question"):
        env = UserInputEnvelope(external_id="user_1", content=text, channel="qq")
        await pipe.handle(env, ctx)

    events = list(store.load(_sid("main", "user_1")))
    assert len(events) == 3
    assert events[0].content == "first question"
    assert events[1].content == "second question"
    assert events[2].content == "third question"

    assert len(enqueued) == 3


@pytest.mark.asyncio
async def test_im_pipeline_skips_control_commands() -> None:
    """IM pipeline control stages terminate before persistence for /stop."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)


    cmd_adapter = MagicMock()
    cmd_adapter._try_intercept_control = AsyncMock(return_value=True)
    enqueued: list[InputMessage] = []
    pool_store = MagicMock()
    pool_store.get.return_value = "main"
    ctx = BotInputContext(
        default_pool="main",
        pool_session_store=pool_store,
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=store,
        enqueue_message=enqueued.append,
        command_adapter=cmd_adapter,
    )
    pipe = build_im_pipeline(skill_registry=_NoSkillRegistry(), known_pools={"main"})

    env = UserInputEnvelope(external_id="u1", content="/stop", channel="qq")
    await pipe.handle(env, ctx)

    # Not persisted and not enqueued
    assert list(store.load(_sid("main", "u1"))) == []
    assert enqueued == []
