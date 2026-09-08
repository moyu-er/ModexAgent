from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.persist_user_message import PersistUserMessageStage
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent

from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.messaging.models import ApprovalAction, ApprovalDecisionInput
from modex_agent.workspace.runtime import bind_workspace_root


def _ctx(store: WorkspaceScopedTranscriptStore) -> BotInputContext:
    return BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main", "coding"},
        pool_session_store=MagicMock(),
        agent_resolver=lambda p: p,
        transcript_store=store,
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )


@pytest.mark.asyncio
async def test_persist_writes_user_message_with_full_session_id() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        env = UserInputEnvelope(
            external_id="u1", content="hello", channel="qq"
        )
        env.metadata["resolved_pool"] = "coding"
        env.metadata["resolved_agent"] = "coding"
        env.metadata["full_session_id"] = "u1.coding"
        env.metadata[RoutingMeta.WORKSPACE] = str(root)
        with bind_workspace_root(root):
            await PersistUserMessageStage().process(env, _ctx(store))
            events = await store.load("u1.coding")
        assert len(events) == 1
        assert isinstance(events[0], UserMessageEvent)
        assert events[0].content == "hello"


@pytest.mark.asyncio
async def test_persist_skips_known_control_commands() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        with bind_workspace_root(root):
            for cmd in ("/cd /tmp", "/pool coding", "/exit", "/stop"):
                env = UserInputEnvelope(external_id="u", content=cmd, channel="qq")
                env.metadata["resolved_agent"] = "main"
                env.metadata["full_session_id"] = "u.main"
                env.metadata[RoutingMeta.WORKSPACE] = str(root)
                await PersistUserMessageStage().process(env, _ctx(store))
            events = await store.load("u.main")
        assert events == [], "control commands must not be persisted"


@pytest.mark.asyncio
async def test_persist_skips_approval_decision() -> None:
    """A decision envelope is NOT persisted as a user message.

    Mirrors the existing real-store assertion pattern (load the store and
    assert empty).  The envelope intentionally omits WORKSPACE so the test
    also pins that the skip runs before the hard WORKSPACE subscript — a
    decision envelope short-circuits workspace resolution.
    """
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        envelope = UserInputEnvelope(
            external_id="ext",
            content="",
            channel="websocket",
            metadata={
                RoutingMeta.FULL_SESSION_ID: "ext.main",
                RoutingMeta.RESOLVED_AGENT: "main",
                RoutingMeta.APPROVAL_DECISION: ApprovalDecisionInput(
                    tool_call_id="c1", action=ApprovalAction.ALLOW
                ),
                # NOTE: no WORKSPACE — decision must skip before that subscript
            },
        )
        with bind_workspace_root(root):
            await PersistUserMessageStage().process(envelope, _ctx(store))
            events = await store.load("ext.main")
        assert events == [], "approval decisions must not be persisted"
