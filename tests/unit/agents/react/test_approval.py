"""Tests for ApprovalRuntime, ApprovalClassifier, TieredToolApprovalClassifier."""
from pathlib import Path

import pytest
from modex_agent.approval.runtime import (
    ApprovalClassifier,
    TieredToolApprovalClassifier,
    ApprovalRuntime,
)
from modex_agent.approval.config import AgentApprovalConfig, ToolApprovalConfig
from modex_agent.approval.constants import ApprovalTier
from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher
from modex_agent.core.types import ToolCall
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.memory.history import ListMessageHistory


def make_ctx():
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
    )


class TestTieredToolApprovalClassifier:
    def test_disabled_returns_normal(self):
        config = AgentApprovalConfig(enabled=False)
        c = TieredToolApprovalClassifier(config=config)
        tc = ToolCall(tool_name="bash", call_id="1", arguments={})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_tool_not_in_config_returns_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        c = TieredToolApprovalClassifier(config=config)
        tc = ToolCall(tool_name="bash", call_id="1", arguments={})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_path_in_allowed_returns_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="write", call_id="1", arguments={"path": "./file.txt"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_path_not_in_allowed_returns_dangerous(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="write", call_id="1", arguments={"path": "/etc/passwd"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_empty_allowed_paths_all_dangerous(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"bash": ToolApprovalConfig(allowed_paths=[])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="bash", call_id="1", arguments={"command": "ls"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_star_allowed_paths_all_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"bash": ToolApprovalConfig(allowed_paths=["*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="bash", call_id="1", arguments={"command": "ls"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL


class TestApprovalRuntime:
    def test_construction(self):
        config = AgentApprovalConfig()
        classifier = TieredToolApprovalClassifier(config=config)
        ar = ApprovalRuntime(classifier=classifier)
        assert ar.classifier is classifier
        from modex_agent.runtime.enums import ApprovalDenyPolicy
        assert ar.default_deny_policy is ApprovalDenyPolicy.TOOL_RESULT_ONLY
