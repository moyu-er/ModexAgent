"""Tests: FanInInputAdapter records UserMessageEvent for IM (non-WebSocket) sources.

Covers:
1. QQ-style InputMessage → UserMessageEvent written to transcript store
2. Generic (Discord/Slack/Telegram) InputMessage → same behavior
3. WebSocket source is SKIPPED (WebUIServer already writes UserMessageEvent)
4. Multiple non-WebSocket sources → all recorded
5. No transcript_store → graceful degradation (no crash, no write)
6. Empty/whitespace-only content → skipped (no empty UserMessageEvent)
7. conversation_id extracted from metadata with fallback to session_id
8. Transcript error does NOT block the message pump
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from bot.adapters.fan_in import FanInInputAdapter
from bot.webui.transcript_store import JSONLTranscriptStore

from framework.core.types import InputMessage
from framework.pipeline.adapters import InputAdapter

# ── Stub adapters for testing ────────────────────────────────────────────────


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


class _StubWebSocketAdapter(_StubIMAdapter):
    """WebSocket adapter — should be SKIPPED by FanIn transcript recording."""

    def __init__(self, messages: list[InputMessage] | None = None) -> None:
        super().__init__(name="websocket", messages=messages)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_store(tmp: Path) -> JSONLTranscriptStore:
    return JSONLTranscriptStore(tmp / "transcripts")


def _qq_message(content: str, user_id: str = "qq_user_123") -> InputMessage:
    """Simulates a QQ-style InputMessage with conversation_id in metadata."""
    return InputMessage(
        content=content,
        session_id=user_id,
        source="qq",
        channel="qq",
        metadata={
            "conversation_id": user_id,
            "user_id": user_id,
            "is_group": False,
        },
    )


def _generic_message(content: str, channel: str = "discord") -> InputMessage:
    """Simulates a generic IM InputMessage without conversation_id in metadata."""
    return InputMessage(
        content=content,
        session_id=f"{channel}_session_1",
        source=channel,
        channel=channel,
    )


# ── Test 1: QQ message → UserMessageEvent recorded ──────────────────────────


@pytest.mark.asyncio
async def test_qq_message_recorded_to_transcript() -> None:
    """QQ InputMessage must produce a UserMessageEvent in the transcript store."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    qq = _StubIMAdapter(name="qq")
    fan_in = FanInInputAdapter(transcript_store=store, default_agent_name="main")
    fan_in.add_source(qq)

    await fan_in.start()
    try:
        qq.enqueue(_qq_message("帮我写一个 Python 脚本"))

        # Consume from fan_in to trigger pump
        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            break  # one message is enough
    finally:
        await fan_in.stop()

    # Message was forwarded
    assert len(received) == 1
    assert received[0].content == "帮我写一个 Python 脚本"

    # Transcript store has the user message
    events = list(store.load("qq_user_123.main"))
    assert len(events) == 1
    assert events[0].event == "user_message"
    assert events[0].content == "帮我写一个 Python 脚本"
    assert events[0].session_id == "qq_user_123.main"
    assert events[0].agent_name == "main"


# ── Test 2: Generic IM (Discord/Slack/Telegram) → also recorded ─────────────


@pytest.mark.asyncio
async def test_discord_message_recorded_to_transcript() -> None:
    """Discord InputMessage must also produce a UserMessageEvent."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    discord = _StubIMAdapter(name="discord")
    fan_in = FanInInputAdapter(transcript_store=store)
    fan_in.add_source(discord)

    await fan_in.start()
    try:
        discord.enqueue(_generic_message("deploy to production", "discord"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            break
    finally:
        await fan_in.stop()

    assert len(received) == 1

    # conversation_id falls back to session_id (no metadata.conversation_id)
    conv_id = "discord_session_1"
    events = list(store.load(f"{conv_id}.main"))
    assert len(events) == 1
    assert events[0].content == "deploy to production"


@pytest.mark.asyncio
async def test_slack_message_recorded_to_transcript() -> None:
    """Slack InputMessage must also produce a UserMessageEvent."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    slack = _StubIMAdapter(name="slack")
    fan_in = FanInInputAdapter(transcript_store=store)
    fan_in.add_source(slack)

    await fan_in.start()
    try:
        slack.enqueue(_generic_message("check CI status", "slack"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            break
    finally:
        await fan_in.stop()

    assert len(received) == 1
    events = list(store.load("slack_session_1.main"))
    assert len(events) == 1
    assert events[0].content == "check CI status"


# ── Test 3: WebSocket source is SKIPPED ──────────────────────────────────────


@pytest.mark.asyncio
async def test_websocket_source_not_recorded() -> None:
    """WebSocket InputMessage must NOT be recorded — WebUIServer already
    writes UserMessageEvent in _ws_send_message."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    ws = _StubWebSocketAdapter()
    fan_in = FanInInputAdapter(transcript_store=store)
    fan_in.add_source(ws)

    await fan_in.start()
    try:
        ws.enqueue(InputMessage(
            content="hello from webui",
            session_id="web_conv_1",
            channel="websocket",
        ))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            break
    finally:
        await fan_in.stop()

    # Message was forwarded
    assert len(received) == 1
    assert received[0].content == "hello from webui"

    # But transcript store is EMPTY — no UserMessageEvent for WebSocket
    events = list(store.load_conversation("web_conv_1"))
    assert len(events) == 0, (
        f"WebSocket messages must NOT be recorded by FanIn. "
        f"Found {len(events)} events."
    )


# ── Test 4: Multiple IM sources → all recorded ──────────────────────────────


@pytest.mark.asyncio
async def test_multiple_im_sources_all_recorded() -> None:
    """FanIn with QQ + Discord sources → both produce UserMessageEvents."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    qq = _StubIMAdapter(name="qq")
    discord = _StubIMAdapter(name="discord")

    fan_in = FanInInputAdapter(transcript_store=store)
    fan_in.add_source(qq)
    fan_in.add_source(discord)

    await fan_in.start()
    try:
        qq.enqueue(_qq_message("QQ 用户的问题"))
        discord.enqueue(_generic_message("Discord user question", "discord"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            if len(received) >= 2:
                break
    finally:
        await fan_in.stop()

    assert len(received) == 2

    # QQ message in transcript
    qq_events = list(store.load("qq_user_123.main"))
    assert len(qq_events) == 1
    assert qq_events[0].content == "QQ 用户的问题"

    # Discord message in transcript
    discord_events = list(store.load("discord_session_1.main"))
    assert len(discord_events) == 1
    assert discord_events[0].content == "Discord user question"


# ── Test 5: No transcript_store → graceful degradation ──────────────────────


@pytest.mark.asyncio
async def test_no_transcript_store_no_crash() -> None:
    """FanIn without transcript_store must work normally — no crash."""
    qq = _StubIMAdapter(name="qq")
    fan_in = FanInInputAdapter()  # no transcript_store
    fan_in.add_source(qq)

    await fan_in.start()
    try:
        qq.enqueue(_qq_message("this should not crash"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            break
    finally:
        await fan_in.stop()

    assert len(received) == 1
    assert received[0].content == "this should not crash"


# ── Test 6: Empty/whitespace content → skipped ──────────────────────────────


@pytest.mark.asyncio
async def test_empty_content_skipped() -> None:
    """InputMessage with empty or whitespace-only content must NOT be
    recorded to transcript (no useless empty UserMessageEvent)."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    qq = _StubIMAdapter(name="qq")
    fan_in = FanInInputAdapter(transcript_store=store)
    fan_in.add_source(qq)

    await fan_in.start()
    try:
        qq.enqueue(InputMessage(content="", session_id="empty_user", source="qq"))
        qq.enqueue(InputMessage(content="   ", session_id="ws_user", source="qq"))
        qq.enqueue(InputMessage(content="real message", session_id="real_user", source="qq"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            if len(received) >= 3:
                break
    finally:
        await fan_in.stop()

    assert len(received) == 3  # all 3 forwarded

    # Only the real message is in transcript
    all_events: list[Any] = []
    for session_id in store.list_sessions():
        all_events.extend(store.load(session_id))

    assert len(all_events) == 1, (
        f"Only non-empty messages should be recorded. Got {len(all_events)} events."
    )
    assert all_events[0].content == "real message"


# ── Test 7: conversation_id from metadata with fallback ─────────────────────


@pytest.mark.asyncio
async def test_conversation_id_from_metadata_with_fallback() -> None:
    """conversation_id comes from metadata.conversation_id if present,
    otherwise falls back to session_id."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    qq = _StubIMAdapter(name="qq")

    # Message WITH conversation_id in metadata
    msg_with_meta = InputMessage(
        content="with metadata",
        session_id="session_A",
        source="qq",
        metadata={"conversation_id": "conv_from_meta"},
    )
    # Message WITHOUT metadata
    msg_no_meta = InputMessage(
        content="no metadata",
        session_id="session_B",
        source="qq",
    )

    fan_in = FanInInputAdapter(transcript_store=store)
    fan_in.add_source(qq)

    await fan_in.start()
    try:
        qq.enqueue(msg_with_meta)
        qq.enqueue(msg_no_meta)

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            if len(received) >= 2:
                break
    finally:
        await fan_in.stop()

    # msg_with_meta → conv_id = "conv_from_meta"
    events_meta = list(store.load("conv_from_meta.main"))
    assert len(events_meta) == 1
    assert events_meta[0].content == "with metadata"

    # msg_no_meta → conv_id = session_id = "session_B"
    events_fallback = list(store.load("session_B.main"))
    assert len(events_fallback) == 1
    assert events_fallback[0].content == "no metadata"


# ── Test 8: Transcript error does not block pump ────────────────────────────


@pytest.mark.asyncio
async def test_transcript_error_does_not_block_pump() -> None:
    """If transcript_store.append() throws, the message must still be
    forwarded to the merged queue — the pump must not die."""
    failing_store = MagicMock()
    failing_store.append.side_effect = OSError("disk full")

    qq = _StubIMAdapter(name="qq")
    fan_in = FanInInputAdapter(transcript_store=failing_store)
    fan_in.add_source(qq)

    await fan_in.start()
    try:
        qq.enqueue(_qq_message("must still arrive"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            break
    finally:
        await fan_in.stop()

    # Message was forwarded despite transcript error
    assert len(received) == 1
    assert received[0].content == "must still arrive"

    # append was attempted
    failing_store.append.assert_called_once()


# ── Test 9: Multiple messages in sequence from same source ──────────────────


@pytest.mark.asyncio
async def test_multiple_messages_sequential() -> None:
    """Multiple messages from the same QQ user — all recorded in order."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    qq = _StubIMAdapter(name="qq")
    fan_in = FanInInputAdapter(transcript_store=store)
    fan_in.add_source(qq)

    await fan_in.start()
    try:
        qq.enqueue(_qq_message("第一个问题", "user_1"))
        qq.enqueue(_qq_message("第二个问题", "user_1"))
        qq.enqueue(_qq_message("第三个问题", "user_1"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            if len(received) >= 3:
                break
    finally:
        await fan_in.stop()

    assert len(received) == 3

    events = list(store.load("user_1.main"))
    assert len(events) == 3
    assert events[0].content == "第一个问题"
    assert events[1].content == "第二个问题"
    assert events[2].content == "第三个问题"


# ── Test 10: Telegram-style message with attachments metadata ──────────────


@pytest.mark.asyncio
async def test_telegram_message_with_attachments() -> None:
    """Telegram InputMessage with attachment metadata — still recorded."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    telegram = _StubIMAdapter(name="telegram")
    fan_in = FanInInputAdapter(transcript_store=store)
    fan_in.add_source(telegram)

    await fan_in.start()
    try:
        msg = InputMessage(
            content="请帮我分析这个图片",
            session_id="tg_chat_456",
            source="telegram",
            channel="telegram",
            metadata={
                "conversation_id": "tg_chat_456",
                "attachments": [{"url": "https://example.com/img.jpg"}],
            },
            attachments=["/tmp/img.jpg"],
        )
        telegram.enqueue(msg)

        received: list[InputMessage] = []
        async for m in fan_in.receive():
            received.append(m)
            break
    finally:
        await fan_in.stop()

    assert len(received) == 1

    events = list(store.load("tg_chat_456.main"))
    assert len(events) == 1
    assert events[0].content == "请帮我分析这个图片"


# ── Test 11: Mixed WebSocket + IM → only IM recorded ────────────────────────


@pytest.mark.asyncio
async def test_mixed_sources_only_im_recorded() -> None:
    """FanIn with WebSocket + QQ + Discord → only QQ and Discord recorded."""
    tmp = Path(tempfile.mkdtemp())
    store = _make_store(tmp)

    ws = _StubWebSocketAdapter()
    qq = _StubIMAdapter(name="qq")
    discord = _StubIMAdapter(name="discord")

    fan_in = FanInInputAdapter(transcript_store=store)
    fan_in.add_source(ws)
    fan_in.add_source(qq)
    fan_in.add_source(discord)

    await fan_in.start()
    try:
        ws.enqueue(InputMessage(content="web message", session_id="web_1", channel="websocket"))
        qq.enqueue(_qq_message("QQ 问题"))
        discord.enqueue(_generic_message("Discord question", "discord"))

        received: list[InputMessage] = []
        async for msg in fan_in.receive():
            received.append(msg)
            if len(received) >= 3:
                break
    finally:
        await fan_in.stop()

    assert len(received) == 3

    # Only QQ and Discord in transcript
    all_events: list[Any] = []
    for session_id in store.list_sessions():
        all_events.extend(store.load(session_id))

    assert len(all_events) == 2, (
        f"Expected 2 events (QQ + Discord), got {len(all_events)}. "
        f"WebSocket must be excluded."
    )

    contents = {e.content for e in all_events}
    assert "QQ 问题" in contents
    assert "Discord question" in contents
    assert "web message" not in contents
