"""Tests: unconfigured tools are always NORMAL; configured tools check paths."""
from pathlib import Path

import pytest
from framework.agents.react.approval import TieredToolApprovalClassifier
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


class TestUnconfiguredToolsNeverNeedApproval:
    """Tools not in AgentApprovalConfig.tools are always NORMAL."""

    def test_list_dir_with_any_path_is_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"bash": ToolApprovalConfig(allowed_paths=[])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="ls", call_id="1", arguments={"path": "/home"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_read_file_with_any_path_is_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="read_file", call_id="1", arguments={"path": "/etc/passwd"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_cat_with_any_path_is_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"edit": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="cat", call_id="1", arguments={"path": "/etc/hosts"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_search_content_with_any_path_is_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="search_content", call_id="1", arguments={"directory": "/home"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL


class TestConfiguredToolsCheckPaths:
    """Tools in AgentApprovalConfig.tools have their path arguments checked."""

    def test_configured_tool_outside_allowed_paths_is_dangerous(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"edit": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="edit", call_id="1", arguments={"path": "/etc/shadow"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_configured_tool_inside_allowed_paths_is_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"edit": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="edit", call_id="1", arguments={"path": "./project/file.txt"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL
