"""T07 input prepare/delivery split (DESIGN.md §7, P01).

One shared stage orchestration consumed by both entry forms:

- ``prepare`` yields the typed outcome ``Prepared(InputMessage) | Handled(reason,
  notice)`` and never touches the channel queue callback.
- ``handle`` (the original adapter contract) prepares, then delivers the
  prepared message through the original sync ``ctx.enqueue_message`` callback —
  call count/order per envelope unchanged; stage order unchanged; S7 stays the
  single user-transcript writer (approval decisions never persist).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.input_pipeline.assembly import (
    build_im_pipeline,
    build_webui_pipeline,
)
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.prepare import Handled, Prepared
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent
from plugins.im_input_stages import IMInputStagesPlugin
from pydantic import BaseModel, ConfigDict

from modex_agent.input_pipeline.context import InputContext
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult
from modex_agent.messaging.models import InputMessage
from modex_agent.plugins.abc import SimpleFactory
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.runtime import bind_workspace_root
from tests.input_pipeline.assembly_support import (
    TEST_ASSEMBLY_CTX,
    TEST_COMPONENT_REGISTRY,
)
from tests.input_pipeline.test_integration import _bot_model_config, _NoSkill

_REPLACED_CONTENT = "rewritten by replacement stage"


class _ReplacementStageConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _ReplacementEnvelopeStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: InputContext
    ) -> StageResult:
        return Continue(value=replace(envelope, content=_REPLACED_CONTENT))


def _replacement_stage_registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        IMInputStagesPlugin().register(registration)
    with PluginRegistrationContext(registry) as registration:
        registration.register_input_stage(
            "replacement_envelope",
            SimpleFactory(_ReplacementEnvelopeStage(), _ReplacementStageConfig),
        )
    return registry


def _make_ctx(
    store,
    enqueued: list[InputMessage],
    root: Path,
    *,
    available_pools=lambda: {"main"},
) -> BotInputContext:
    pool_store = MagicMock()
    pool_store.get.return_value = "main"
    cmd = MagicMock()
    cmd._try_intercept_control = AsyncMock(return_value=False)
    return BotInputContext(
        default_pool="main",
        available_pools=available_pools,
        pool_session_store=pool_store,
        agent_resolver=lambda p: p,
        transcript_store=store,
        enqueue_message=enqueued.append,
        command_adapter=cmd,
        current_ws_provider=lambda: root,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["prepare", "handle"])
async def test_replacement_envelope_drives_persistence_and_delivery(
    mode: Literal["prepare", "handle"],
) -> None:
    """A plugin may replace the envelope carried by Continue; downstream S7
    persistence and the final delivery must observe that same replacement."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
            enqueued: list[InputMessage] = []
            ctx = _make_ctx(store, enqueued, root)
            registry = _replacement_stage_registry()
            pipe = await build_im_pipeline(
                registry=registry,
                ctx=AssemblyContext(
                    registry=registry,
                    workspace_ctx=WorkspaceContext.from_target(
                        root,
                        data_dir_name=".modex",
                        home=root,
                    ),
                ),
                skill_registry=_NoSkill(),
                known_pools={"main"},
            )
            envelope = UserInputEnvelope(
                external_id="u1", content="original", channel="qq"
            )

            if mode == "prepare":
                outcome = await pipe.prepare(envelope, ctx)
                assert isinstance(outcome, Prepared)
                delivered = outcome.message
                assert enqueued == []
            else:
                result = await pipe.handle(envelope, ctx)
                assert result.should_continue()
                assert len(enqueued) == 1
                delivered = enqueued[0]

            events = await store.load(delivered.session.session_id)
            assert len(events) == 1
            assert isinstance(events[0], UserMessageEvent)
            assert events[0].content == _REPLACED_CONTENT
            assert delivered.content == _REPLACED_CONTENT


@pytest.mark.asyncio
async def test_im_handle_prepares_then_enqueues_once_via_sync_callback() -> None:
    """Original handle contract: same stage order, sync callback delivered
    exactly once, one user record, Continue result for the adapter."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
            enqueued: list[InputMessage] = []
            ctx = _make_ctx(store, enqueued, root)
            pipe = await build_im_pipeline(
                registry=TEST_COMPONENT_REGISTRY,
                ctx=TEST_ASSEMBLY_CTX,
                skill_registry=_NoSkill(),
                known_pools={"main"},
            )
            from modex_agent.input_pipeline.envelope import UserInputEnvelope

            envelope = UserInputEnvelope(external_id="u1", content="hello", channel="qq")
            result = await pipe.handle(envelope, ctx)

            assert result.should_continue()
            assert len(enqueued) == 1, "queue callback called exactly once"
            assert enqueued[0].content == "hello"
            assert enqueued[0].chat_id == ""
            sid = enqueued[0].session.session_id
            events = await store.load(sid)
            assert len(events) == 1 and events[0].content == "hello"


@pytest.mark.asyncio
async def test_im_handle_continue_enqueued_once_not_persisted() -> None:
    """Second delivery point cannot be missed: /continue flows through the
    prepared outcome and is enqueued exactly once by handle."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
            enqueued: list[InputMessage] = []
            ctx = _make_ctx(store, enqueued, root)
            pipe = await build_im_pipeline(
                registry=TEST_COMPONENT_REGISTRY,
                ctx=TEST_ASSEMBLY_CTX,
                skill_registry=_NoSkill(),
                known_pools={"main"},
            )
            from modex_agent.input_pipeline.envelope import UserInputEnvelope

            result = await pipe.handle(
                UserInputEnvelope(external_id="u1", content="/continue", channel="qq"),
                ctx,
            )

            assert result.should_continue()
            assert len(enqueued) == 1
            assert enqueued[0].content == "/continue"
            assert await store.load(enqueued[0].session.session_id) == []


@pytest.mark.asyncio
async def test_webui_handle_terminate_exact_adapter_shape() -> None:
    """handle returns the ORIGINAL Terminate (exact reason + response payload),
    not a rebuilt lossy copy — the adapter surface is byte-identical."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
            enqueued: list[InputMessage] = []
            ctx = _make_ctx(store, enqueued, root)
            pipe = await build_webui_pipeline(
                registry=TEST_COMPONENT_REGISTRY,
                ctx=TEST_ASSEMBLY_CTX,
                skill_registry=_NoSkill(),
                bot_model_config=_bot_model_config(),
            )
            from modex_agent.input_pipeline.envelope import UserInputEnvelope

            result = await pipe.handle(
                UserInputEnvelope(
                    external_id="uuid1",
                    content="/cd /tmp",
                    channel="websocket",
                    explicit_pool="main",
                ),
                ctx,
            )

            from modex_agent.commands.constants import NOTICE_UNKNOWN_COMMAND

            assert not result.should_continue()
            assert result.reason == "unsupported_command"
            assert result.response == {
                "message": NOTICE_UNKNOWN_COMMAND.format(command="cd")
            }
            assert enqueued == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_reason", "expected_response"),
    [
        pytest.param("/stop", "session_command", None, id="s3-stop"),
        pytest.param(
            "/coding",
            "pool_switch",
            {"message": 'switch to "coding" pool'},
            id="s2-pool-shortcut",
        ),
        pytest.param(
            "/nosuchcmd",
            "unsupported_command",
            {"message": "Unknown command: /nosuchcmd. No such command or skill is available."},
            id="unsupported-notice",
        ),
    ],
)
async def test_handle_preserves_per_stage_terminate_shapes(
    content: str, expected_reason: str, expected_response: dict | None
) -> None:
    """Per-stage regression: each control stage's original Terminate shape
    (reason + full response payload) survives the preparation split."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
            enqueued: list[InputMessage] = []
            ctx = _make_ctx(store, enqueued, root)
            pipe = await build_im_pipeline(
                registry=TEST_COMPONENT_REGISTRY,
                ctx=TEST_ASSEMBLY_CTX,
                skill_registry=_NoSkill(),
                known_pools={"main", "coding"},
            )
            ctx.command_adapter._try_intercept_control.return_value = content == "/stop"
            from modex_agent.input_pipeline.envelope import UserInputEnvelope

            result = await pipe.handle(
                UserInputEnvelope(external_id="u1", content=content, channel="qq"),
                ctx,
            )

            assert not result.should_continue()
            assert result.reason == expected_reason
            if expected_response is None:
                assert result.response is None
            else:
                assert result.response == expected_response
            assert enqueued == []


@pytest.mark.asyncio
async def test_handle_handled_without_delivery_keeps_continue_shape() -> None:
    """A HANDLED envelope with no prepared carriage keeps the ORIGINAL shape:
    handle -> Continue, no enqueue, no Terminate; prepare -> Handled with no
    reason/notice (quietly consumed), never a Terminate."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
            enqueued: list[InputMessage] = []
            ctx = _make_ctx(store, enqueued, root)
            pipe = await build_im_pipeline(
                registry=TEST_COMPONENT_REGISTRY,
                ctx=TEST_ASSEMBLY_CTX,
                skill_registry=_NoSkill(),
                known_pools={"main"},
            )
            from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope

            envelope = UserInputEnvelope(external_id="u1", content="hi", channel="qq")
            envelope.command_status = CommandStatus.HANDLED
            handle_result = await pipe.handle(envelope, ctx)

            assert handle_result.should_continue(), "original shape is Continue"
            assert enqueued == [], "HANDLED without carriage skips the queue callback"

            prepare_envelope = UserInputEnvelope(
                external_id="u1", content="hi", channel="qq"
            )
            prepare_envelope.command_status = CommandStatus.HANDLED
            outcome = await pipe.prepare(prepare_envelope, ctx)
            assert isinstance(outcome, Handled)
            assert outcome.kind == "handled"
            assert outcome.reason is None and outcome.notice is None
