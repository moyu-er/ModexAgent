"""Tests for ApprovalRuntime, ApprovalClassifier, TieredToolApprovalClassifier."""
import pytest
from framework.agents.react.approval import (
    ApprovalClassifier,
    TieredToolApprovalClassifier,
    ApprovalRuntime,
)
from framework.approval.constants import ApprovalTier
from framework.interceptor.builtin.tool_approval import ToolNameMatcher
from framework.core.types import ToolCall
from framework.core.agent import AgentContext
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory


def make_ctx():
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
    )


class TestTieredToolApprovalClassifier:
    def test_normal_by_default(self):
        c = TieredToolApprovalClassifier()
        tc = ToolCall(tool_name="read_file", call_id="1", arguments={"path": "x.txt"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_hardline_overrides(self):
        c = TieredToolApprovalClassifier(
            hardline=ToolNameMatcher({"rm"}),
        )
        tc = ToolCall(tool_name="rm", call_id="1", arguments={})
        assert c.classify(tc, make_ctx()) == ApprovalTier.HARDLINE

    def test_dangerous_matched(self):
        c = TieredToolApprovalClassifier(
            dangerous=ToolNameMatcher({"shell"}),
        )
        tc = ToolCall(tool_name="shell", call_id="1", arguments={})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_sensitive_matched(self):
        c = TieredToolApprovalClassifier(
            sensitive=ToolNameMatcher({"api_call"}),
        )
        tc = ToolCall(tool_name="api_call", call_id="1", arguments={})
        assert c.classify(tc, make_ctx()) == ApprovalTier.SENSITIVE

    def test_hardline_has_priority_over_dangerous(self):
        c = TieredToolApprovalClassifier(
            hardline=ToolNameMatcher({"rm"}),
            dangerous=ToolNameMatcher({"rm", "shell"}),
        )
        tc = ToolCall(tool_name="rm", call_id="1", arguments={})
        assert c.classify(tc, make_ctx()) == ApprovalTier.HARDLINE


class TestApprovalRuntime:
    def test_construction(self):
        from framework.agents.react.strategy import InlineWaitStrategy
        from framework.control.channel import InMemoryControlChannel
        classifier = TieredToolApprovalClassifier()
        strategy = InlineWaitStrategy(InMemoryControlChannel())
        ar = ApprovalRuntime(classifier=classifier, suspend_strategy=strategy)
        assert ar.classifier is classifier
        assert ar.suspend_strategy is strategy
        assert ar.deny_as_cancel is True
