from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.persist_user_message import PersistUserMessageStage
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent
from framework.input_pipeline.envelope import UserInputEnvelope


def _ctx(store: WorkspaceScopedTranscriptStore) -> BotInputContext:
    return BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"coding": "coding", "main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=store,
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )


@pytest.mark.asyncio
async def test_persist_writes_user_message_with_full_session_id() -> None:
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"coding": "coding"})
        env = UserInputEnvelope(
            conversation_id="u1", content="hello", channel="qq"
        )
        env.metadata["resolved_pool"] = "coding"
        env.metadata["resolved_agent"] = "coding"
        env.metadata["full_session_id"] = "u1.coding"
        await PersistUserMessageStage().process(env, _ctx(store))
        events = list(store.load("u1.coding"))
        assert len(events) == 1
        assert isinstance(events[0], UserMessageEvent)
        assert events[0].content == "hello"


@pytest.mark.asyncio
async def test_persist_skips_known_control_commands() -> None:
    with TemporaryDirectory() as tmp:
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        store.set_agent_pool_map({"main": "main"})
        for cmd in ("/cd /tmp", "/pool coding", "/exit", "/stop"):
            env = UserInputEnvelope(conversation_id="u", content=cmd, channel="qq")
            env.metadata["resolved_agent"] = "main"
            env.metadata["full_session_id"] = "u.main"
            await PersistUserMessageStage().process(env, _ctx(store))
        events = list(store.load("u.main"))
        assert events == [], "control commands must not be persisted"
