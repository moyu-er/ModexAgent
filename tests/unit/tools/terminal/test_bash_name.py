"""Verify both shell tools register as 'bash' with distinct descriptions."""

from __future__ import annotations

from framework.tools.terminal.subprocess_tool import SubprocessTool


class TestBashToolName:
    """Both tools must register as 'bash' for LLM consistency."""

    def test_subprocess_tool_name_is_bash(self) -> None:
        tool = SubprocessTool(timeout=60)
        assert tool.name == "bash"

    def test_subprocess_description_mentions_independent(self) -> None:
        tool = SubprocessTool(timeout=60)
        desc = tool.description
        assert "fresh" in desc.lower() or "independently" in desc.lower()
        assert "do not" in desc.lower() or "does not" in desc.lower()

    def test_subprocess_description_no_impl_words(self) -> None:
        tool = SubprocessTool(timeout=60)
        desc = tool.description
        # Must not expose implementation details
        assert "subprocess" not in desc.lower()
        assert "SubprocessExecutor" not in desc
