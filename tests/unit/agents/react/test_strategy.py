"""Tests for SuspendStrategy implementations."""
import pytest

from framework.agents.react.strategy import InlineWaitStrategy
from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest


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
