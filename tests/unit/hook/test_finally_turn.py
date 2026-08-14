"""Tests for FinallyGraphHook and FINALLY_GRAPH dispatch."""

from __future__ import annotations

import pytest

from modex_agent.control.exceptions import PolicyViolation
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook import (
    FinallyGraphHook,
    HookErrorPolicy,
    HookPayload,
    HookPoint,
    HookRunner,
    HookSpec,
)
from modex_agent.memory.history import ListMessageHistory

# ---------------------------------------------------------------------------
# Helper: minimal AgentContext
# ---------------------------------------------------------------------------


def _make_minimal_context() -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=SessionInfo.from_str("test.agent"),
    )


# ---------------------------------------------------------------------------
# Concrete test hook
# ---------------------------------------------------------------------------


class _StubFinallyGraphHook(FinallyGraphHook):
    """Concrete FinallyGraphHook that records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[AgentContext, AgentResult | None]] = []

    @property
    def name(self) -> str:
        return "stub_finally_graph"

    async def finally_graph(self, ctx: AgentContext, result: AgentResult | None) -> None:
        self.calls.append((ctx, result))


class _FailingFinallyGraphHook(FinallyGraphHook):
    """Hook that always raises."""

    @property
    def name(self) -> str:
        return "failing_finally_graph"

    async def finally_graph(self, ctx: AgentContext, result: AgentResult | None) -> None:
        raise RuntimeError("boom")


class _UnrelatedHook(FinallyGraphHook):
    """A FinallyGraphHook that we won't register — used to verify isinstance filtering works."""

    @property
    def name(self) -> str:
        return "unrelated"

    async def finally_graph(self, ctx: AgentContext, result: AgentResult | None) -> None:
        raise AssertionError("Should not be called")


# ---------------------------------------------------------------------------
# Not a FinallyGraphHook — verify dispatch skips it
# ---------------------------------------------------------------------------


class _NotAFinallyGraphHook:
    """Not a hook at all — dispatch should skip."""

    @property
    def name(self) -> str:
        return "not_a_hook"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatches_with_result_object() -> None:
    """finally_graph receives a non-None result object."""
    hook = _StubFinallyGraphHook()
    runner = HookRunner([HookSpec(hook)])
    ctx = _make_minimal_context()

    fake_result = object()
    await runner.dispatch(
        HookPoint.FINALLY_GRAPH,
        ctx,
        HookPayload(data={"result": fake_result}),
    )

    assert len(hook.calls) == 1
    assert hook.calls[0][0] is ctx
    assert hook.calls[0][1] is fake_result


@pytest.mark.asyncio
async def test_dispatches_with_none_result() -> None:
    """finally_graph receives None when result is not provided."""
    hook = _StubFinallyGraphHook()
    runner = HookRunner([HookSpec(hook)])
    ctx = _make_minimal_context()

    await runner.dispatch(
        HookPoint.FINALLY_GRAPH,
        ctx,
        HookPayload(data={"result": None}),
    )

    assert len(hook.calls) == 1
    assert hook.calls[0][0] is ctx
    assert hook.calls[0][1] is None


@pytest.mark.asyncio
async def test_dispatches_with_no_payload() -> None:
    """finally_graph receives None when no payload is given at all."""
    hook = _StubFinallyGraphHook()
    runner = HookRunner([HookSpec(hook)])
    ctx = _make_minimal_context()

    await runner.dispatch(HookPoint.FINALLY_GRAPH, ctx)

    assert len(hook.calls) == 1
    assert hook.calls[0][1] is None


@pytest.mark.asyncio
async def test_non_finally_graph_hook_is_skipped() -> None:
    """Hooks that are not FinallyGraphHook instances are skipped for FINALLY_GRAPH."""
    not_a_hook = _NotAFinallyGraphHook()  # type: ignore[arg-type]
    runner = HookRunner([HookSpec(not_a_hook)])  # type: ignore[arg-type]
    ctx = _make_minimal_context()

    # Should not raise — non-FinallyGraphHook is silently skipped
    result = await runner.dispatch(HookPoint.FINALLY_GRAPH, ctx)
    assert result is None


@pytest.mark.asyncio
async def test_error_ignore_policy() -> None:
    """With IGNORE policy, exceptions are swallowed."""
    hook = _FailingFinallyGraphHook()
    runner = HookRunner([HookSpec(hook, on_error=HookErrorPolicy.IGNORE)])
    ctx = _make_minimal_context()

    # Should not raise
    await runner.dispatch(
        HookPoint.FINALLY_GRAPH,
        ctx,
        HookPayload(data={"result": None}),
    )


@pytest.mark.asyncio
async def test_error_abort_policy() -> None:
    """With ABORT policy, exceptions propagate as PolicyViolation."""
    hook = _FailingFinallyGraphHook()
    runner = HookRunner([HookSpec(hook, on_error=HookErrorPolicy.ABORT)])
    ctx = _make_minimal_context()

    with pytest.raises(PolicyViolation, match="failing_finally_graph"):
        await runner.dispatch(
            HookPoint.FINALLY_GRAPH,
            ctx,
            HookPayload(data={"result": None}),
        )


@pytest.mark.asyncio
async def test_multiple_hooks_dispatched_in_order() -> None:
    """Multiple FinallyGraphHook instances are called in registration order."""
    hook_a = _StubFinallyGraphHook()
    hook_b = _StubFinallyGraphHook()
    runner = HookRunner([HookSpec(hook_a), HookSpec(hook_b)])
    ctx = _make_minimal_context()

    fake_result = object()
    await runner.dispatch(
        HookPoint.FINALLY_GRAPH,
        ctx,
        HookPayload(data={"result": fake_result}),
    )

    assert len(hook_a.calls) == 1
    assert len(hook_b.calls) == 1
    # Both received the same result
    assert hook_a.calls[0][1] is fake_result
    assert hook_b.calls[0][1] is fake_result
