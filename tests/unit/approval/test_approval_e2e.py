"""End-to-end test: approval flow triggers for dangerous tools with path outside allowed dirs."""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from framework.agents.react.approval import (
    TieredToolApprovalClassifier, ApprovalRuntime, ApprovalClassifier,
)
from framework.agents.react.strategy import SuspendResumeStrategy
from framework.agents.react.strategy import InMemoryTurnResumeStateStore
from framework.approval.store import LocalFileApprovalStateStore
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

    def test_strategy_saves_approval_state_and_interrupts(self, tmp_path):
        """SuspendResumeStrategy saves state and raises GraphInterrupt."""
        workspace = tmp_path / "approval"
        strategy = SuspendResumeStrategy(
            LocalFileApprovalStateStore(workspace),
            InMemoryTurnResumeStateStore(),
        )

        ctx = make_ctx()
        ctx.metadata["_react_iteration"] = 1
        ctx.metadata["ITERATION_MSGS"] = []

        requests = [
            ApprovalRequest(
                tool_name="edit_file", tool_call_id="2",
                arguments={"path": "/etc/shadow"},
                tier="dangerous", iteration=1,
            )
        ]

        # solicit_approval should raise GraphInterrupt (suspends execution)
        from framework.core.graph.interrupt import GraphInterrupt

        async def _test():
            with pytest.raises(GraphInterrupt):
                await strategy.solicit_approval(
                    requests, ctx,
                    all_tool_calls=[
                        {"id": "2", "type": "function",
                         "function": {"name": "edit_file", "arguments": {"path": "/etc/shadow"}}}
                    ],
                    llm_content="ok",
                )

            # After interrupt, approval state should be saved to disk
            loaded = await strategy.load_approval_state(ctx.session_id)
            assert loaded is not None
            assert loaded.session_id == ctx.session_id
            assert len(loaded.requests) == 1
            assert loaded.requests[0].tool_name == "edit_file"

            # Resume state should also be saved
            resume = await strategy.load_resume_state(ctx.session_id)
            assert resume is not None
            assert resume.resume_node == "tool"

            # Cleanup
            await strategy.delete_approval_state(ctx.session_id)
            await strategy.delete_resume_state(ctx.session_id)

        asyncio.run(_test())

    def test_approval_runtime_bundles_classifier_and_strategy(self):
        """ApprovalRuntime correctly bundles classifier + strategy."""
        c = self._make_classifier()
        strategy = SuspendResumeStrategy(
            MagicMock(), MagicMock(),
        )
        runtime = ApprovalRuntime(classifier=c, suspend_strategy=strategy)
        assert runtime.classifier is c
        assert runtime.suspend_strategy is strategy
        assert runtime.deny_as_cancel is True

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
