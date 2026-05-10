"""End-to-end test: approval flow triggers for dangerous tools with path outside allowed dirs."""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from framework.agents.react.approval import (
    TieredToolApprovalClassifier, ApprovalRuntime, ApprovalClassifier,
)
from framework.agents.react.strategy import TurnStateSuspendStrategy
from framework.runtime.store import InMemoryTurnStateStore
from framework.runtime.policy import SnapshotPolicy
from framework.runtime.models import TurnSnapshot
from framework.runtime.enums import SnapshotReason


class _FakeSnapshotPolicy(SnapshotPolicy):
    def should_capture(self, state, reason):
        return True
    def capture(self, state, reason):
        return TurnSnapshot(
            identity=state.identity, agent_kind=state.agent_kind,
            phase=state.phase, reason=reason, resume_point=None,
            state_payload={},
        )
from framework.approval.constants import ApprovalDecision, ApprovalTier
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig
from framework.interceptor.builtin.tool_approval import ArgumentMatcher
from framework.core.types import ToolCall
from framework.core.agent import AgentContext
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory


def make_ctx(session_id="s1"):
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session_id=session_id,
    )


class TestApprovalFlowE2E:
    """Verify the full approval flow works for dangerous tools."""

    def _make_classifier(self) -> TieredToolApprovalClassifier:
        config = AgentApprovalConfig(
            enabled=True,
            tools={
                "edit_file": ToolApprovalConfig(allowed_paths=[]),
                "write_file": ToolApprovalConfig(allowed_paths=[]),
                "shell": ToolApprovalConfig(allowed_paths=[]),
            },
        )
        return TieredToolApprovalClassifier(
            config=config,
            argument_matcher=ArgumentMatcher(),
        )

    def test_classifier_dangerous_tool_outside_allowed_fails(self):
        """Classifier returns NORMAL for list_dir /home — read-only, no approval needed."""
        c = self._make_classifier()
        tc = ToolCall(tool_name="list_dir", call_id="1", arguments={"path": "/home"})
        # list_dir is NOT in config → always NORMAL
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_classifier_dangerous_tool_outside_allowed_triggers(self):
        """Classifier returns DANGEROUS for edit_file /etc — configured tool with empty allowed_paths."""
        c = self._make_classifier()
        tc = ToolCall(tool_name="edit_file", call_id="2",
                      arguments={"path": "/etc/shadow"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_classifier_dangerous_tool_inside_allowed_still_dangerous(self):
        """Classifier returns DANGEROUS for shell — configured tool with empty allowed_paths."""
        c = self._make_classifier()
        tc = ToolCall(tool_name="shell", call_id="3",
                      arguments={"command": "ls", "working_dir": "."})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_approval_runtime_bundles_classifier_and_strategy(self):
        """ApprovalRuntime correctly bundles classifier + strategy."""
        c = self._make_classifier()
        strategy = TurnStateSuspendStrategy(
            InMemoryTurnStateStore(),
            _FakeSnapshotPolicy(),
        )
        runtime = ApprovalRuntime(classifier=c, suspend_strategy=strategy)
        assert runtime.classifier is c
        assert runtime.suspend_strategy is strategy
        from framework.runtime.enums import ApprovalDenyPolicy
        assert runtime.default_deny_policy is ApprovalDenyPolicy.CANCEL_TURN

    def test_no_classifier_no_strategy_everything_normal(self):
        """Without classifier in approval runtime, all tools are NORMAL."""
        tc = ToolCall(tool_name="shell", call_id="1",
                      arguments={"command": "rm -rf /"})
        ctx = make_ctx()
        # Simulate runtime with no approval
        ctx.runtime = MagicMock()
        ctx.runtime.approval = None
        ctx.runtime.suspend_strategy = None

        # _get_tier falls back to NORMAL when no approval runtime
        from framework.agents.react.nodes.tool import ToolNode
        from framework.agents.react.agent import ReActAgent

        class _FakeProvider: pass
        agent = ReActAgent(_FakeProvider(), mode="full")
        node = ToolNode(agent)
        tier = node._get_tier(tc, ctx)
        assert tier == ApprovalTier.NORMAL
