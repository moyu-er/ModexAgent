"""TDD: argument_matcher should only apply to dangerous-named tools, not all tools."""
import pytest
from framework.agents.react.approval import TieredToolApprovalClassifier
from framework.approval.constants import ApprovalTier
from framework.interceptor.builtin.tool_approval import ToolNameMatcher, ArgumentMatcher
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


class TestReadOnlyToolsNeverNeedApproval:
    """Read-only tools like list_dir/read_file/cat must always be NORMAL,
    regardless of path arguments. Only tools in the dangerous name set
    should have their path arguments checked."""

    def test_list_dir_with_any_path_is_normal(self):
        """list_dir is a read-only tool — never needs approval."""
        c = TieredToolApprovalClassifier(
            dangerous=ToolNameMatcher({"shell", "write_file", "edit_file"}),
            argument_matcher=ArgumentMatcher({"."}),
        )
        tc = ToolCall(tool_name="list_dir", call_id="1",
                      arguments={"path": "/home"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_read_file_with_any_path_is_normal(self):
        """read_file is a read-only tool — never needs approval."""
        c = TieredToolApprovalClassifier(
            dangerous=ToolNameMatcher({"write_file", "edit_file"}),
            argument_matcher=ArgumentMatcher({"."}),
        )
        tc = ToolCall(tool_name="read_file", call_id="1",
                      arguments={"path": "/etc/passwd"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_cat_with_any_path_is_normal(self):
        """cat is a read-only tool — never needs approval."""
        c = TieredToolApprovalClassifier(
            dangerous=ToolNameMatcher({"write_file", "edit_file"}),
            argument_matcher=ArgumentMatcher({"."}),
        )
        tc = ToolCall(tool_name="cat", call_id="1",
                      arguments={"path": "/etc/hosts"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_search_content_with_any_path_is_normal(self):
        """search_content is read-only — never needs approval."""
        c = TieredToolApprovalClassifier(
            dangerous=ToolNameMatcher({"write_file", "edit_file"}),
            argument_matcher=ArgumentMatcher({"."}),
        )
        tc = ToolCall(tool_name="search_content", call_id="1",
                      arguments={"directory": "/home"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL


class TestDangerousToolsStillCheckArguments:
    """Dangerous-named tools should still have their path arguments checked."""

    def test_dangerous_tool_outside_allowed_dirs_is_dangerous(self):
        c = TieredToolApprovalClassifier(
            dangerous=ToolNameMatcher({"edit_file"}),
            argument_matcher=ArgumentMatcher({"."}),
        )
        tc = ToolCall(tool_name="edit_file", call_id="1",
                      arguments={"path": "/etc/shadow"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_dangerous_tool_inside_allowed_dir_still_dangerous(self):
        """A tool in the dangerous set is always DANGEROUS, even with safe path."""
        c = TieredToolApprovalClassifier(
            dangerous=ToolNameMatcher({"edit_file"}),
            argument_matcher=ArgumentMatcher({"."}),
        )
        tc = ToolCall(tool_name="edit_file", call_id="1",
                      arguments={"path": "./project/file.txt"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS
