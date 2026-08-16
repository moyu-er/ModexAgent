"""Tests for framework.tools.presets."""

from __future__ import annotations

from modex_agent.tools.presets import ToolPreset, get_preset_tools, get_supplement_tools, ToolSupplement


class TestToolPreset:
    """Enum value tests."""

    def test_preset_is_str_enum(self) -> None:
        """ToolPreset values are strings for YAML serialization."""
        assert ToolPreset.FULL == "full"
        assert ToolPreset.READ_WRITE == "read_write"
        assert ToolPreset.READ_ONLY == "read_only"


class TestGetPresetTools:
    """Tool registration tests."""

    def test_full_preset_includes_read_write(self) -> None:
        """FULL preset includes Read/Write/Edit/Search tools."""
        tools = get_preset_tools(ToolPreset.FULL)
        names = [t.name for t in tools]
        assert "read" in names
        assert "write" in names
        assert "edit" in names
        assert "ls" in names
        assert "grep" in names
        assert "glob" in names

    def test_read_only_preset_excludes_write(self) -> None:
        """READ_ONLY preset has no Write/Edit tools."""
        tools = get_preset_tools(ToolPreset.READ_ONLY)
        names = [t.name for t in tools]
        assert "read" in names
        assert "write" not in names
        assert "edit" not in names
        assert "grep" in names

    def test_read_write_preset_has_bash(self) -> None:
        """READ_WRITE preset includes bash for code review (git diff, git log)."""
        from modex_agent.tools.terminal.subprocess_tool import SubprocessTool

        tools = get_preset_tools(
            ToolPreset.READ_WRITE,
            subprocess_tool_factory=lambda: SubprocessTool(timeout=60),
        )
        names = [t.name for t in tools]
        assert "bash" in names

    def test_bash_injected_for_full_preset(self) -> None:
        """FULL preset includes bash when factory provided."""
        from modex_agent.tools.terminal.subprocess_tool import SubprocessTool

        def make_bash() -> SubprocessTool:
            return SubprocessTool(timeout=60)

        tools = get_preset_tools(ToolPreset.FULL, subprocess_tool_factory=make_bash)
        names = [t.name for t in tools]
        assert "bash" in names

    def test_none_preset_returns_empty(self) -> None:
        """NONE preset returns zero standard tools."""
        tools = get_preset_tools(ToolPreset.NONE)
        assert len(tools) == 0

    def test_none_preset_no_bash(self) -> None:
        """NONE preset does not get bash even with factory."""
        from modex_agent.tools.terminal.subprocess_tool import SubprocessTool

        tools = get_preset_tools(
            ToolPreset.NONE,
            subprocess_tool_factory=lambda: SubprocessTool(timeout=60),
        )
        names = [t.name for t in tools]
        assert "bash" not in names


class TestAciSupplement:
    """ACI supplement — AciEditTool replaces EditFileTool via name overwrite."""

    def test_aci_is_supplement_value(self) -> None:
        """ToolSupplement.ACI serializes as 'aci'."""
        assert ToolSupplement.ACI == "aci"

    def test_aci_supplement_produces_aci_edit_tool(self) -> None:
        """ACI supplement produces a single AciEditTool (not a list of tools)."""
        from modex_agent.tools.aci.edit_tool import AciEditTool

        tools = get_supplement_tools([ToolSupplement.ACI])
        assert len(tools) == 1
        assert isinstance(tools[0], AciEditTool)

    def test_aci_supplement_tool_name_is_edit(self) -> None:
        """ACI supplement's tool name is 'edit' — overwrites preset's EditFileTool."""
        tools = get_supplement_tools([ToolSupplement.ACI])
        assert tools[0].name == "edit"

    def test_aci_supplement_inherits_edit_file_tool(self) -> None:
        """AciEditTool is a subclass of EditFileTool (drop-in replacement)."""
        from modex_agent.tools.aci.edit_tool import AciEditTool
        from modex_agent.tools.standard.file_tool import EditFileTool

        tools = get_supplement_tools([ToolSupplement.ACI])
        assert isinstance(tools[0], EditFileTool)
        assert isinstance(tools[0], AciEditTool)

    def test_aci_supplement_wraps_with_root_provider(self) -> None:
        """ACI supplement tools get wrapped with WorkspaceRootProvider when given."""
        from pathlib import Path

        from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

        class _FakeProvider(WorkspaceRootProvider):
            def current(self) -> Path:
                return Path("/fake/workspace")

        tools = get_supplement_tools([ToolSupplement.ACI], root_provider=_FakeProvider())
        assert len(tools) == 1
        assert tools[0].name == "edit"

    def test_aci_supplement_combines_with_ast_grep(self) -> None:
        """ACI + AST_GREP supplements combine: AciEditTool + ast_grep tools."""
        from modex_agent.tools.aci.edit_tool import AciEditTool

        tools = get_supplement_tools([ToolSupplement.AST_GREP, ToolSupplement.ACI])
        names = [t.name for t in tools]
        assert "edit" in names
        assert "ast_grep_search" in names
        assert "ast_grep_replace" in names
        edit_tool = next(t for t in tools if t.name == "edit")
        assert isinstance(edit_tool, AciEditTool)

    def test_aci_supplement_combines_with_todo(self) -> None:
        """ACI + TODO supplements combine: AciEditTool + todo tools."""
        from pathlib import Path

        from modex_agent.runtime.store import JsonFileTodoStore

        from modex_agent.tools.aci.edit_tool import AciEditTool

        store = JsonFileTodoStore(Path("/tmp/test_todos_aci"))
        tools = get_supplement_tools([ToolSupplement.TODO, ToolSupplement.ACI], todo_store=store)
        names = [t.name for t in tools]
        assert "edit" in names
        assert "todo_read" in names
        assert "todo_write" in names

    def test_aci_supplement_does_not_duplicate_edit(self) -> None:
        """ACI supplement only produces one 'edit' tool (no duplicate)."""
        tools = get_supplement_tools([ToolSupplement.ACI, ToolSupplement.ACI])
        # seen dedup prevents duplicate
        edit_tools = [t for t in tools if t.name == "edit"]
        assert len(edit_tools) == 1
