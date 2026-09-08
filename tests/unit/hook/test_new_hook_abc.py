"""Tests for BeforeLLMHook + AfterApprovalHook — the two new hook ABCs (T9)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from modex_agent.approval.constants import ApprovalTier
from modex_agent.control.exceptions import PolicyViolationError
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook import (
    AfterApprovalHook,
    BeforeGraphHook,
    BeforeLLMHook,
    HookErrorPolicy,
    HookPayload,
    HookPoint,
    HookRunner,
    HookSpec,
)
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import ApprovalSubjectType
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ToolArguments,
)
from modex_agent.tools.manager import InMemoryToolManager

# ---------------------------------------------------------------------------
# Helper: minimal AgentContext
# ---------------------------------------------------------------------------


def _make_minimal_context() -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
    )


def _make_chat_messages() -> list[ChatMessage]:
    return [
        ChatMessage(role=MessageRole.SYSTEM, content="You are a test agent."),
        ChatMessage(role=MessageRole.USER, content="Hello"),
    ]


def _make_approval_transaction() -> ApprovalTransaction:
    return ApprovalTransaction(
        approval_id="ap-1",
        turn_id="t1",
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch-1"],
        requests=[
            ApprovalRequestState(
                request_id="r1",
                approval_id="ap-1",
                tool_call_id="call-1",
                tool_name="write",
                arguments=ToolArguments(values={"path": "a.txt"}),
                tier=ApprovalTier.DANGEROUS,
                iteration=1,
            ),
        ],
    )


def test_hook_name_defaults_to_concrete_class_name() -> None:
    class MinimalBeforeGraphHook(BeforeGraphHook):
        async def before_graph(self, ctx: AgentContext) -> None:
            pass

    hook = MinimalBeforeGraphHook()

    assert hook.name == "MinimalBeforeGraphHook"


# BeforeLLMHook
# ===========================================================================


class _StubBeforeLLMHook(BeforeLLMHook):
    def __init__(self) -> None:
        self.calls: list[tuple[AgentContext, Sequence[ChatMessage] | None]] = []

    @property
    def name(self) -> str:
        return "stub_before_llm"

    async def before_llm(self, ctx: AgentContext, request: Sequence[ChatMessage]) -> None:
        self.calls.append((ctx, request))


class _FailingBeforeLLMHook(BeforeLLMHook):
    @property
    def name(self) -> str:
        return "failing_before_llm"

    async def before_llm(self, ctx: AgentContext, request: Sequence[ChatMessage]) -> None:
        raise RuntimeError("boom")


class _NotABeforeLLMHook:
    @property
    def name(self) -> str:
        return "not_a_hook"


@pytest.mark.asyncio
async def test_before_llm_dispatches_with_request() -> None:
    hook = _StubBeforeLLMHook()
    runner = HookRunner([HookSpec(hook)])
    ctx = _make_minimal_context()
    messages = _make_chat_messages()

    await runner.dispatch(
        HookPoint.BEFORE_LLM,
        ctx,
        HookPayload(data={"request": messages}),
    )

    assert len(hook.calls) == 1
    assert hook.calls[0][0] is ctx
    assert hook.calls[0][1] == messages


@pytest.mark.asyncio
async def test_before_llm_dispatches_with_no_payload() -> None:
    hook = _StubBeforeLLMHook()
    runner = HookRunner([HookSpec(hook)])
    ctx = _make_minimal_context()

    await runner.dispatch(HookPoint.BEFORE_LLM, ctx)

    assert len(hook.calls) == 1
    assert hook.calls[0][1] is None


@pytest.mark.asyncio
async def test_before_llm_skips_non_matching_hook() -> None:
    not_a_hook = _NotABeforeLLMHook()  # type: ignore[arg-type]
    runner = HookRunner([HookSpec(not_a_hook)])  # type: ignore[arg-type]
    ctx = _make_minimal_context()

    result = await runner.dispatch(HookPoint.BEFORE_LLM, ctx)
    assert result is None


@pytest.mark.asyncio
async def test_before_llm_error_ignore_policy() -> None:
    hook = _FailingBeforeLLMHook()
    runner = HookRunner([HookSpec(hook, on_error=HookErrorPolicy.IGNORE)])
    ctx = _make_minimal_context()

    await runner.dispatch(
        HookPoint.BEFORE_LLM,
        ctx,
        HookPayload(data={"request": _make_chat_messages()}),
    )


@pytest.mark.asyncio
async def test_before_llm_error_abort_policy() -> None:
    hook = _FailingBeforeLLMHook()
    runner = HookRunner([HookSpec(hook, on_error=HookErrorPolicy.ABORT)])
    ctx = _make_minimal_context()

    with pytest.raises(PolicyViolationError, match="failing_before_llm"):
        await runner.dispatch(
            HookPoint.BEFORE_LLM,
            ctx,
            HookPayload(data={"request": _make_chat_messages()}),
        )


@pytest.mark.asyncio
async def test_before_llm_multiple_hooks_in_order() -> None:
    hook_a = _StubBeforeLLMHook()
    hook_b = _StubBeforeLLMHook()
    runner = HookRunner([HookSpec(hook_a), HookSpec(hook_b)])
    ctx = _make_minimal_context()
    messages = _make_chat_messages()

    await runner.dispatch(
        HookPoint.BEFORE_LLM,
        ctx,
        HookPayload(data={"request": messages}),
    )

    assert len(hook_a.calls) == 1
    assert len(hook_b.calls) == 1
    assert hook_a.calls[0][1] == messages
    assert hook_b.calls[0][1] == messages


# ===========================================================================
# AfterApprovalHook
# ===========================================================================


class _StubAfterApprovalHook(AfterApprovalHook):
    def __init__(self) -> None:
        self.calls: list[tuple[AgentContext, ApprovalTransaction]] = []

    @property
    def name(self) -> str:
        return "stub_after_approval"

    async def after_approval(self, ctx: AgentContext, transaction: ApprovalTransaction) -> None:
        self.calls.append((ctx, transaction))


class _FailingAfterApprovalHook(AfterApprovalHook):
    @property
    def name(self) -> str:
        return "failing_after_approval"

    async def after_approval(self, ctx: AgentContext, transaction: ApprovalTransaction) -> None:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_after_approval_dispatches_with_transaction() -> None:
    hook = _StubAfterApprovalHook()
    runner = HookRunner([HookSpec(hook)])
    ctx = _make_minimal_context()
    tx = _make_approval_transaction()

    await runner.dispatch(
        HookPoint.AFTER_APPROVAL,
        ctx,
        HookPayload(data={"transaction": tx}),
    )

    assert len(hook.calls) == 1
    assert hook.calls[0][0] is ctx
    assert hook.calls[0][1] is tx


@pytest.mark.asyncio
async def test_after_approval_dispatches_with_no_payload() -> None:
    hook = _StubAfterApprovalHook()
    runner = HookRunner([HookSpec(hook)])
    ctx = _make_minimal_context()

    await runner.dispatch(HookPoint.AFTER_APPROVAL, ctx)

    assert len(hook.calls) == 1
    assert hook.calls[0][1] is None


@pytest.mark.asyncio
async def test_after_approval_skips_non_matching_hook() -> None:
    not_a_hook = _NotABeforeLLMHook()  # type: ignore[arg-type]
    runner = HookRunner([HookSpec(not_a_hook)])  # type: ignore[arg-type]
    ctx = _make_minimal_context()

    result = await runner.dispatch(HookPoint.AFTER_APPROVAL, ctx)
    assert result is None


@pytest.mark.asyncio
async def test_after_approval_error_ignore_policy() -> None:
    hook = _FailingAfterApprovalHook()
    runner = HookRunner([HookSpec(hook, on_error=HookErrorPolicy.IGNORE)])
    ctx = _make_minimal_context()

    await runner.dispatch(
        HookPoint.AFTER_APPROVAL,
        ctx,
        HookPayload(data={"transaction": _make_approval_transaction()}),
    )


@pytest.mark.asyncio
async def test_after_approval_error_abort_policy() -> None:
    hook = _FailingAfterApprovalHook()
    runner = HookRunner([HookSpec(hook, on_error=HookErrorPolicy.ABORT)])
    ctx = _make_minimal_context()

    with pytest.raises(PolicyViolationError, match="failing_after_approval"):
        await runner.dispatch(
            HookPoint.AFTER_APPROVAL,
            ctx,
            HookPayload(data={"transaction": _make_approval_transaction()}),
        )


@pytest.mark.asyncio
async def test_after_approval_multiple_hooks_in_order() -> None:
    hook_a = _StubAfterApprovalHook()
    hook_b = _StubAfterApprovalHook()
    runner = HookRunner([HookSpec(hook_a), HookSpec(hook_b)])
    ctx = _make_minimal_context()
    tx = _make_approval_transaction()

    await runner.dispatch(
        HookPoint.AFTER_APPROVAL,
        ctx,
        HookPayload(data={"transaction": tx}),
    )

    assert len(hook_a.calls) == 1
    assert len(hook_b.calls) == 1
    assert hook_a.calls[0][1] is tx
    assert hook_b.calls[0][1] is tx


# ===========================================================================
# Cross-hook isolation: a BeforeLLMHook should NOT be called for AFTER_APPROVAL
# and vice versa.
# ===========================================================================


@pytest.mark.asyncio
async def test_before_llm_hook_not_called_for_after_approval() -> None:
    hook = _StubBeforeLLMHook()
    runner = HookRunner([HookSpec(hook)])
    ctx = _make_minimal_context()

    await runner.dispatch(
        HookPoint.AFTER_APPROVAL,
        ctx,
        HookPayload(data={"transaction": _make_approval_transaction()}),
    )

    assert len(hook.calls) == 0


@pytest.mark.asyncio
async def test_after_approval_hook_not_called_for_before_llm() -> None:
    hook = _StubAfterApprovalHook()
    runner = HookRunner([HookSpec(hook)])
    ctx = _make_minimal_context()

    await runner.dispatch(
        HookPoint.BEFORE_LLM,
        ctx,
        HookPayload(data={"request": _make_chat_messages()}),
    )

    assert len(hook.calls) == 0
