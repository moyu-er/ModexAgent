"""Tests for ApprovalRuntime, ApprovalClassifier, TieredToolApprovalClassifier."""
from pathlib import Path

import pytest
from framework.agents.react.approval import (
    ApprovalClassifier,
    TieredToolApprovalClassifier,
    ApprovalRuntime,
)
from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig
from framework.approval.constants import ApprovalTier
from framework.interceptor.builtin.tool_approval import ArgumentMatcher
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
    def test_disabled_returns_normal(self):
        config = AgentApprovalConfig(enabled=False)
        c = TieredToolApprovalClassifier(config=config)
        tc = ToolCall(tool_name="shell", call_id="1", arguments={})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_tool_not_in_config_returns_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write_file": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        c = TieredToolApprovalClassifier(config=config)
        tc = ToolCall(tool_name="shell", call_id="1", arguments={})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_path_in_allowed_returns_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write_file": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="write_file", call_id="1", arguments={"path": "./file.txt"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_path_not_in_allowed_returns_dangerous(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write_file": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="write_file", call_id="1", arguments={"path": "/etc/passwd"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_empty_allowed_paths_all_dangerous(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"shell": ToolApprovalConfig(allowed_paths=[])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="shell", call_id="1", arguments={"command": "ls"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_star_allowed_paths_all_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"shell": ToolApprovalConfig(allowed_paths=["*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="shell", call_id="1", arguments={"command": "ls"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL


class TestApprovalRuntime:
    def test_construction(self):
        config = AgentApprovalConfig()
        classifier = TieredToolApprovalClassifier(config=config)
        ar = ApprovalRuntime(classifier=classifier)
        assert ar.classifier is classifier
        from framework.runtime.enums import ApprovalDenyPolicy
        assert ar.default_deny_policy is ApprovalDenyPolicy.TOOL_RESULT_ONLY
