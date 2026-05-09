"""Tests for SuspendStrategy implementations."""
import pytest

from framework.agents.react.constants import ReActMetaKey
from framework.agents.react.strategy import InMemoryTurnResumeStateStore
from framework.agents.react.strategy import (
    InlineWaitStrategy,
    SuspendResumeStrategy,
)
from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest
from framework.approval.store import InMemoryApprovalStateStore
from framework.core.graph.interrupt import GraphInterrupt, _current_resume


class _MockChannel:
    def __init__(self, responses):
        self._responses = responses
        self._idx = 0

    async def wait_for_decision(self, tool_call_id):
        resp = self._responses[self._idx]
        self._idx += 1
        return resp


class _MockEmitter:
    async def emit(self, event, data=None):
        pass


class TestInlineWaitStrategy:
    @pytest.mark.asyncio
    async def test_all_allowed(self):
        strategy = InlineWaitStrategy(_MockChannel(["allowed", "allowed"]))
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        ctx = type("Ctx", (), {"session_id": "s1", "emitter": _MockEmitter()})()
        decisions = await strategy.solicit_approval(reqs, ctx)
        assert decisions == [ApprovalDecision.ALLOWED, ApprovalDecision.ALLOWED]

    @pytest.mark.asyncio
    async def test_denied_cascades(self):
        strategy = InlineWaitStrategy(_MockChannel(["denied"]))
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        ctx = type("Ctx", (), {"session_id": "s1", "emitter": _MockEmitter()})()
        decisions = await strategy.solicit_approval(reqs, ctx)
        assert decisions == [ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED]


class TestSuspendResumeStrategy:
    @pytest.mark.asyncio
    async def test_first_call_raises(self):
        strategy = SuspendResumeStrategy(
            InMemoryApprovalStateStore(), InMemoryTurnResumeStateStore(),
        )
        reqs = [ApprovalRequest("t1", "c1", {}, "dangerous", 1)]
        ctx = type("Ctx", (), {
            "session_id": "s1",
            "metadata": {ReActMetaKey.ITERATION: 1, ReActMetaKey.ITERATION_MSGS: []},
        })()
        with pytest.raises(GraphInterrupt):
            await strategy.solicit_approval(reqs, ctx)

    @pytest.mark.asyncio
    async def test_second_call_returns_resume(self):
        store = InMemoryApprovalStateStore()
        strategy = SuspendResumeStrategy(store, InMemoryTurnResumeStateStore())
        reqs = [ApprovalRequest("t1", "c1", {}, "dangerous", 1)]
        ctx = type("Ctx", (), {
            "session_id": "s1",
            "metadata": {ReActMetaKey.ITERATION: 1, ReActMetaKey.ITERATION_MSGS: []},
        })()
        with pytest.raises(GraphInterrupt):
            await strategy.solicit_approval(reqs, ctx)
        token = _current_resume.set([ApprovalDecision.ALLOWED])
        try:
            decisions = await strategy.solicit_approval(reqs, ctx)
            assert decisions == [ApprovalDecision.ALLOWED]
        finally:
            _current_resume.reset(token)
