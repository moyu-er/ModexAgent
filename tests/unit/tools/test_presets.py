"""Tests for framework.tools.presets."""

from __future__ import annotations

from modex_agent.tools.presets import (
    EXPERIENCE_REVIEW_HOOK_NAME,
    ToolPreset,
    get_preset_tools,
    make_aci_edit_tool,
    make_ast_grep_tools,
)


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


class TestAciEditToolFactory:
    """``make_aci_edit_tool`` — the aci capability's TOOL-slot product
    (registry name ``aci_edit``, LLM-facing name ``edit``). The old
    supplement face is dead; the capability channel
    (``capabilities: {aci: {}}``) owns roster contribution."""

    def test_factory_produces_aci_edit_tool(self) -> None:
        """The factory produces a single AciEditTool."""
        from modex_agent.tools.aci.edit_tool import AciEditTool

        tool = make_aci_edit_tool()
        assert isinstance(tool, AciEditTool)

    def test_tool_name_is_edit(self) -> None:
        """The tool's LLM-facing name is 'edit' — drop-in upgrade contract."""
        assert make_aci_edit_tool().name == "edit"

    def test_inherits_edit_file_tool(self) -> None:
        """AciEditTool is a subclass of EditFileTool (drop-in replacement)."""
        from modex_agent.tools.aci.edit_tool import AciEditTool
        from modex_agent.tools.standard.file_tool import EditFileTool

        tool = make_aci_edit_tool()
        assert isinstance(tool, EditFileTool)
        assert isinstance(tool, AciEditTool)


class TestAstGrepToolsFactory:
    """``make_ast_grep_tools`` — the ast_grep capability's TOOL-slot
    product (registry names ``ast_grep_search`` / ``ast_grep_replace``).
    The old supplement face is dead; the capability channel
    (``capabilities: {ast_grep: {}}``) owns roster contribution."""

    def test_factory_produces_both_ast_tools(self) -> None:
        from modex_agent.tools.ast import AstGrepReplaceTool, AstGrepSearchTool

        tools = make_ast_grep_tools()
        assert [type(t) for t in tools] == [AstGrepSearchTool, AstGrepReplaceTool]

    def test_tool_names_are_the_registry_names(self) -> None:
        assert [t.name for t in make_ast_grep_tools()] == ["ast_grep_search", "ast_grep_replace"]


class TestExperienceReviewHookName:
    """The review hook's registration-name authority survives the
    supplement face's death (the hook factory + capability still use it)."""

    def test_experience_review_hook_name_is_the_registration_name(self) -> None:
        """EXPERIENCE_REVIEW_HOOK_NAME is importable and equals the name
        register_default_hooks registers the review hook under (the
        ``experience`` capability contributes it into hook rosters)."""
        from modex_agent.plugins.abc import ComponentSlot
        from modex_agent.plugins.defaults.hooks import (
            ExperienceReviewHookFactory,
            register_default_hooks,
        )
        from modex_agent.plugins.loader import PluginRegistrationContext
        from modex_agent.plugins.registry import ComponentRegistry

        assert EXPERIENCE_REVIEW_HOOK_NAME == "experience_review"
        registry = ComponentRegistry()
        with PluginRegistrationContext(registry) as ctx:
            register_default_hooks(ctx)
        factory = registry.resolve(ComponentSlot.HOOK, EXPERIENCE_REVIEW_HOOK_NAME)
        assert isinstance(factory, ExperienceReviewHookFactory)
