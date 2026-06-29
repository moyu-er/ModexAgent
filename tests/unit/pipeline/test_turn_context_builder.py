"""TurnContextBuilder — per-turn context/request/AgentContext assembly.

Unit tests for the four responsibilities extracted from AgentPipeline:
``build_turn_request``, ``preprocess``, ``assemble``, ``build_runtime_and_context``.

These construct the builder directly (no AgentPipeline) so the tests target
the builder's own contract, not the pipeline's wiring.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.approval.types import ApprovalAction
from modex_agent.commands.constants import CommandAction, CommandDispatchPolicy, CommandParseStatus
from modex_agent.commands.models import (
    CommandHandlingResult,
    CommandParseResult,
    SlashCommandInvocation,
)
from modex_agent.core.context import ContextState, InMemoryContextManager
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import InputMessage
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder, TurnRequest
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.store import InMemoryTurnStateStore


def _make_builder(**overrides: Any) -> TurnContextBuilder:
    """Construct a builder with sane defaults; tests override what they exercise."""
    defaults: dict[str, Any] = dict(
        agent=MagicMock(name="agent"),
        tool_manager=InMemoryToolManager(),
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
        output_adapter=MagicMock(spec=OutputAdapter),
        turn_store=None,
        registry=TurnSessionRegistry(),
    )
    defaults.update(overrides)
    return TurnContextBuilder(**defaults)


# ---------------------------------------------------------------------------
# TurnRequest dataclass
# ---------------------------------------------------------------------------


def test_turn_request_is_frozen() -> None:
    """TurnRequest is a frozen value object — immutability is part of the contract."""
    req = TurnRequest(
        session_id="s1",
        input_msg=MagicMock(),
        user_content="hi",
        append_user_message=True,
        trigger_agent=True,
    )
    with pytest.raises(Exception):
        req.session_id = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# build_turn_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_turn_request_plain_input_without_command_processor() -> None:
    """No command_processor → content is treated as plain user input.

    Plain input carries no command transform, so ``user_content`` is None —
    turn_runner then keeps preprocess's sanitized_content, which is where the
    attachment path-reference injection (ADR-0013 §10) lives. Returning the raw
    content here made turn_runner override and discard the injection, so the
    agent never perceived uploaded files.
    """
    builder = _make_builder()
    input_msg = InputMessage(content="hello world", session=SessionInfo.from_str("s:main"))

    req = await builder.build_turn_request(input_msg, "s:main", {}, None)

    assert req is not None
    assert req.user_content is None
    assert req.append_user_message is True
    assert req.trigger_agent is True
    assert req.approval_action is None
    assert req.command_result is None


@pytest.mark.asyncio
async def test_build_turn_request_plain_input_preserves_injection_surface() -> None:
    """Regression: PLAIN_INPUT (with a command_processor) must not set user_content.

    If user_content were the raw content, turn_runner would override
    sanitized_content and discard the attachment injection. None leaves
    preprocess's injected content in place so the agent perceives attachments.
    """
    from unittest.mock import MagicMock

    from modex_agent.commands.constants import CommandParseStatus
    from modex_agent.commands.models import CommandParseResult

    builder = _make_builder()
    builder._command_processor = MagicMock()
    builder._command_processor.parse.return_value = CommandParseResult(
        status=CommandParseStatus.PLAIN_INPUT
    )
    input_msg = InputMessage(content="see image", session=SessionInfo.from_str("s:main"))

    req = await builder.build_turn_request(input_msg, "s:main", {}, None)

    assert req is not None
    assert req.user_content is None, (
        "plain input must not override preprocess's sanitized (injected) content"
    )


@pytest.mark.asyncio
async def test_build_turn_request_returns_none_for_notice_action() -> None:
    """A command whose action is NOTICE produces no turn request (notice already sent)."""
    processor = MagicMock()
    processor.parse.return_value = CommandParseResult(
        status=CommandParseStatus.VALID_COMMAND,
        invocation=SlashCommandInvocation(command="help", args="", raw="/help"),
    )
    processor.handle = AsyncMock(return_value=CommandHandlingResult(
        action=CommandAction.NOTICE,
        dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
        notice="help text",
    ))
    output_adapter = MagicMock(spec=OutputAdapter)
    builder = _make_builder(command_processor=processor, output_adapter=output_adapter)
    input_msg = InputMessage(content="/help", session=SessionInfo.from_str("s:main"))

    req = await builder.build_turn_request(input_msg, "s:main", {}, None)

    assert req is None
    # The notice was forwarded to the output adapter.
    assert output_adapter.send.await_count == 1


@pytest.mark.asyncio
async def test_build_turn_request_approval_decision_carries_action() -> None:
    """APPROVAL_DECISION actions thread the approval_action into the TurnRequest."""
    processor = MagicMock()
    processor.parse.return_value = CommandParseResult(
        status=CommandParseStatus.VALID_COMMAND,
        invocation=SlashCommandInvocation(command="approve", args="", raw="/approve"),
    )
    processor.handle = AsyncMock(return_value=CommandHandlingResult(
        action=CommandAction.APPROVAL_DECISION,
        dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
        approval_action=ApprovalAction.ALLOW,
    ))
    builder = _make_builder(command_processor=processor)
    input_msg = InputMessage(content="/approve", session=SessionInfo.from_str("s:main"))

    req = await builder.build_turn_request(input_msg, "s:main", {}, None)

    assert req is not None
    assert req.trigger_agent is False
    assert req.append_user_message is False
    assert req.user_content is None
    assert req.approval_action == ApprovalAction.ALLOW


@pytest.mark.asyncio
async def test_build_turn_request_short_circuits_on_approval_decision() -> None:
    """A webui approval_decision bypasses command processing -> resume branch."""
    from modex_agent.approval.views import ApprovalDecisionInput

    builder = _make_builder()  # command_processor=None; short-circuit fires first anyway
    msg = InputMessage(
        content="",
        session=SessionInfo.from_str("s.main"),
        approval_decision=ApprovalDecisionInput("call_1", ApprovalAction.DENY),
    )

    tr = await builder.build_turn_request(msg, "s.main", {}, None)

    assert tr is not None
    assert tr.trigger_agent is False
    assert tr.append_user_message is False
    assert tr.user_content is None
    assert tr.approval_action == ApprovalAction.DENY
    assert tr.approval_tool_call_id == "call_1"


@pytest.mark.asyncio
async def test_build_turn_request_uses_pool_turn_store_when_pool_data_wired() -> None:
    """When pool_data is wired, the command_context.turn_store comes from the snapshot."""
    snap_turn = MagicMock(name="snap_turn")
    snapshot = MagicMock(spec=PoolDataSnapshot)
    snapshot.turn_store = snap_turn

    captured: dict[str, Any] = {}

    processor = MagicMock()
    processor.parse.return_value = CommandParseResult(
        status=CommandParseStatus.VALID_COMMAND,
        invocation=SlashCommandInvocation(command="x", args="", raw="/x"),
    )

    async def _handle(text: str, ctx):  # noqa: ANN001
        captured["turn_store"] = ctx.turn_store
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            notice="n",
        )

    processor.handle = _handle
    builder = _make_builder(command_processor=processor, turn_store=MagicMock(name="self_turn"))
    input_msg = InputMessage(content="/x", session=SessionInfo.from_str("s:main"))

    await builder.build_turn_request(input_msg, "s:main", {}, None, pool_data=snapshot)

    assert captured["turn_store"] is snap_turn


# ---------------------------------------------------------------------------
# preprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preprocess_applies_sanitizer() -> None:
    """The sanitizer is invoked and its return value is the sanitized content."""
    calls: list[str] = []

    def sanitizer(text: str) -> str:
        calls.append(text)
        return text.upper()

    builder = _make_builder(sanitizer=sanitizer)
    input_msg = InputMessage(content="hi", session=SessionInfo.from_str("s:main"))

    sanitized, media_blocks, media_processor = await builder.preprocess(
        input_msg, "s:main", {}, None
    )

    assert sanitized == "HI"
    assert calls == ["hi"]
    assert media_blocks == []
    assert media_processor is None


@pytest.mark.asyncio
async def test_preprocess_applies_route_prompt_modifier_when_no_source_agent() -> None:
    """A route_result.prompt_modifier is prepended unless the message is from another agent."""
    route = MagicMock()
    route.prompt_modifier = "[prefix] "
    builder = _make_builder()
    input_msg = InputMessage(content="body", session=SessionInfo.from_str("s:main"))

    sanitized, _, _ = await builder.preprocess(input_msg, "s:main", {}, route)

    assert sanitized == "[prefix] body"


@pytest.mark.asyncio
async def test_preprocess_skips_route_modifier_for_agent_messages() -> None:
    """source_agent messages keep their content untouched by the route modifier."""
    route = MagicMock()
    route.prompt_modifier = "[prefix] "
    builder = _make_builder()
    input_msg = InputMessage(content="body", session=SessionInfo.from_str("s:main"))

    sanitized, _, _ = await builder.preprocess(
        input_msg, "s:main", {"source_agent": "sub1"}, route
    )

    assert sanitized == "body"


# ---------------------------------------------------------------------------
# build_runtime_and_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_runtime_and_context_snapshot_turn_store_wins() -> None:
    """Pool snapshot turn_store overrides self.turn_store in the built runtime."""
    snap_turn = MagicMock(name="snap_turn")
    snapshot = MagicMock(spec=PoolDataSnapshot)
    snapshot.turn_store = snap_turn
    snapshot.trace_store = None

    context_state = ContextState()
    ctx_mgr = MagicMock()
    ctx_mgr.wrap_governance.return_value = None

    agent = MagicMock()
    agent.name = "main"
    builder = _make_builder(
        agent=agent,
        turn_store=MagicMock(name="self_turn"),  # must NOT win
    )

    ctx, _emitter = builder.build_runtime_and_context(
        SessionInfo.from_str("s:main"),
        context_state,
        ctx_mgr,
        pool_data=snapshot,
    )

    assert ctx.workspace_snapshot is snapshot
    assert ctx.runtime is not None
    assert ctx.runtime.services.turn_store is snap_turn


@pytest.mark.asyncio
async def test_build_runtime_and_context_falls_back_to_self_turn_store() -> None:
    """Without a snapshot, self.turn_store backs the runtime."""
    self_turn = InMemoryTurnStateStore()
    context_state = ContextState()
    ctx_mgr = MagicMock()
    ctx_mgr.wrap_governance.return_value = None

    agent = MagicMock()
    agent.name = "main"
    builder = _make_builder(agent=agent, turn_store=self_turn)

    ctx, _emitter = builder.build_runtime_and_context(
        SessionInfo.from_str("s:main"),
        context_state,
        ctx_mgr,
        pool_data=None,
    )

    assert ctx.workspace_snapshot is None
    assert ctx.runtime is not None
    assert ctx.runtime.services.turn_store is self_turn


@pytest.mark.asyncio
async def test_build_runtime_and_context_runtime_services_override_wins() -> None:
    """Process-scope runtime_services.turn_store takes top precedence."""
    from modex_agent.runtime.services import AgentRuntimeServices

    override_turn = MagicMock(name="override_turn")
    snap_turn = MagicMock(name="snap_turn")
    self_turn = MagicMock(name="self_turn")
    snapshot = MagicMock(spec=PoolDataSnapshot)
    snapshot.turn_store = snap_turn
    snapshot.trace_store = None

    context_state = ContextState()
    ctx_mgr = MagicMock()
    ctx_mgr.wrap_governance.return_value = None

    agent = MagicMock()
    agent.name = "main"
    builder = _make_builder(
        agent=agent,
        turn_store=self_turn,
        runtime_services=AgentRuntimeServices(turn_store=override_turn),
    )

    ctx, _emitter = builder.build_runtime_and_context(
        SessionInfo.from_str("s:main"),
        context_state,
        ctx_mgr,
        pool_data=snapshot,
    )

    assert ctx.runtime.services.turn_store is override_turn


@pytest.mark.asyncio
async def test_build_runtime_and_context_emitter_factory_used_when_wired() -> None:
    """When emitter_factory is set its return value is the turn emitter."""
    sentinel = MagicMock(name="emitter")
    emitter_factory = MagicMock(return_value=sentinel)
    builder = _make_builder(
        agent=MagicMock(name="agent"),
        turn_store=InMemoryTurnStateStore(),
        emitter_factory=emitter_factory,
    )

    _ctx, emitter = builder.build_runtime_and_context(
        SessionInfo.from_str("s:main"),
        ContextState(),
        MagicMock(wrap_governance=MagicMock(return_value=None)),
    )

    assert emitter is sentinel
    emitter_factory.assert_called_once_with("s:main")


@pytest.mark.asyncio
async def test_build_runtime_and_context_propagates_model_capabilities() -> None:
    """Pool-level base_services.model_capabilities reaches the per-turn runtime.

    ADR-0013 §2: capabilities are threaded pool → base AgentRuntimeServices →
    per-turn AgentRuntimeServices so the inline renderer can bind to
    ``ctx.runtime.model_capabilities``. This mirrors how governance/approval
    propagate through build_runtime_and_context.
    """
    from modex_agent.ioc.configs.llm import Modality, ModelCapabilities
    from modex_agent.runtime.services import AgentRuntimeServices

    caps = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))
    builder = _make_builder(
        agent=MagicMock(name="agent"),
        turn_store=InMemoryTurnStateStore(),
        runtime_services=AgentRuntimeServices(model_capabilities=caps),
    )

    ctx, _emitter = builder.build_runtime_and_context(
        SessionInfo.from_str("s:main"),
        ContextState(),
        MagicMock(wrap_governance=MagicMock(return_value=None)),
    )

    assert ctx.runtime is not None
    assert ctx.runtime.model_capabilities is caps
    assert ctx.runtime.model_capabilities.supports(Modality.IMAGE)


@pytest.mark.asyncio
async def test_build_runtime_and_context_carries_inline_image_attachments() -> None:
    """Image-kind resolved attachments land in turn state for the inline renderer.

    ADR-0014 §3 / OpenSpec native-multimodal-inline unit 3: the current turn's
    image-kind resolved Attachments are carried in
    ``ctx.runtime.state.custom[INLINE_ATTACHMENTS]`` so the renderer (unit 4)
    can find them. Non-image attachments are dropped (only image-kind is
    eligible for native inline rendering). The records carry path/mime/kind/name
    but NO base64 — bytes are materialized lazily by unit 5.
    """
    from modex_agent.media.models import Attachment, AttachmentLocator, Kind
    from modex_agent.runtime.enums import TurnCustomKey

    img = Attachment(
        id="a1",
        kind=Kind.IMAGE,
        name="cat.png",
        mime="image/png",
        size=12345,
        path="media/cat.png",
        locator=AttachmentLocator.MEDIA,
    )
    doc = Attachment(
        id="a2",
        kind=Kind.EXTRACTABLE_DOCUMENT,
        name="report.pdf",
        mime="application/pdf",
        size=999,
        path="media/report.pdf",
        locator=AttachmentLocator.MEDIA,
    )

    builder = _make_builder(
        agent=MagicMock(name="agent"),
        turn_store=InMemoryTurnStateStore(),
    )

    ctx, _emitter = builder.build_runtime_and_context(
        SessionInfo.from_str("s:main"),
        ContextState(),
        MagicMock(wrap_governance=MagicMock(return_value=None)),
        inline_attachments=[img, doc],
    )

    assert ctx.runtime is not None
    carried = ctx.runtime.state.custom[TurnCustomKey.INLINE_ATTACHMENTS]
    assert [a.id for a in carried] == ["a1"]
    assert carried[0].kind is Kind.IMAGE
    assert carried[0].path == "media/cat.png"
    assert carried[0].mime == "image/png"
    assert carried[0].name == "cat.png"
    # Attachment is the path-only VO — it has no bytes/base64 field at all.
    assert not hasattr(carried[0], "data")
    assert not hasattr(carried[0], "base64")


@pytest.mark.asyncio
async def test_build_runtime_and_context_inline_attachments_defaults_empty() -> None:
    """Omitting the param yields an empty list carrier (no KeyError for readers)."""
    from modex_agent.runtime.enums import TurnCustomKey

    builder = _make_builder(
        agent=MagicMock(name="agent"),
        turn_store=InMemoryTurnStateStore(),
    )

    ctx, _emitter = builder.build_runtime_and_context(
        SessionInfo.from_str("s:main"),
        ContextState(),
        MagicMock(wrap_governance=MagicMock(return_value=None)),
    )

    assert ctx.runtime is not None
    assert ctx.runtime.state.custom[TurnCustomKey.INLINE_ATTACHMENTS] == []


# ---------------------------------------------------------------------------
# assemble (smoke — full coverage lives in test_context_assembler / pipeline e2e)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_delegates_to_context_assembler() -> None:
    """assemble() loads context via the ctx_mgr passed in (delegation contract)."""
    ctx_mgr = InMemoryContextManager()
    builder = _make_builder()
    input_msg = InputMessage(content="hi", session=SessionInfo.from_str("s:main"))

    state = await builder.assemble(
        "s:main",
        input_msg,
        {},
        "hi",
        [],
        None,
        ctx_mgr,
        None,
        False,
    )

    assert state is not None
    history = await state.history.to_list()
    # The user message was appended (append_user_message defaults True, not approval cmd).
    assert any(m.get("role") == "user" for m in history)
