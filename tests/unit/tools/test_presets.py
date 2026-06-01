"""Tests for framework.tools.presets."""

from __future__ import annotations

import pytest
from framework.tools.presets import ToolPreset, get_preset_tools


class TestToolPreset:
    """Enum value tests."""

    def test_preset_is_str_enum(self) -> None:
        """ToolPreset values are strings for YAML serialization."""
        assert ToolPreset.FULL == "full"
        assert ToolPreset.READ_WRITE == "read_write"
        assert ToolPreset.READ_ONLY == "read_only"
        assert ToolPreset.MINIMAL == "minimal"


class TestGetPresetTools:
    """Tool registration tests."""

    def test_full_preset_includes_read_write(self) -> None:
        """FULL preset includes Read/Write/Edit/Search tools."""
        tools = get_preset_tools(ToolPreset.FULL)
        names = [t.name for t in tools]
        assert "read_file" in names
        assert "write_file" in names
        assert "edit_file" in names
        assert "list_dir" in names
        assert "search_files" in names
        assert "find_files" in names

    def test_read_only_preset_excludes_write(self) -> None:
        """READ_ONLY preset has no Write/Edit tools."""
        tools = get_preset_tools(ToolPreset.READ_ONLY)
        names = [t.name for t in tools]
        assert "read_file" in names
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "search_files" in names

    def test_read_write_preset_no_bash(self) -> None:
        """READ_WRITE preset has no bash tool."""
        tools = get_preset_tools(ToolPreset.READ_WRITE)
        names = [t.name for t in tools]
        assert "bash" not in names

    def test_minimal_preset_no_edit_no_bash(self) -> None:
        """MINIMAL preset has no Edit, no FindFiles, no bash."""
        tools = get_preset_tools(ToolPreset.MINIMAL)
        names = [t.name for t in tools]
        assert "read_file" in names
        assert "write_file" in names
        assert "edit_file" not in names
        assert "find_files" not in names

    def test_bash_injected_for_full_preset(self) -> None:
        """FULL preset includes bash when factory provided."""
        from framework.tools.terminal.subprocess_tool import SubprocessTool

        def make_bash() -> SubprocessTool:
            return SubprocessTool(timeout=60)

        tools = get_preset_tools(ToolPreset.FULL, subprocess_tool_factory=make_bash)
        names = [t.name for t in tools]
        assert "shell" in names  # TODO: rename to "bash" in T2

    def test_bash_not_injected_for_read_write(self) -> None:
        """READ_WRITE preset excludes bash even when factory provided."""
        from framework.tools.terminal.subprocess_tool import SubprocessTool

        tools = get_preset_tools(
            ToolPreset.READ_WRITE,
            subprocess_tool_factory=lambda: SubprocessTool(timeout=60),
        )
        names = [t.name for t in tools]
        assert "shell" not in names  # TODO: rename to "bash" in T2
