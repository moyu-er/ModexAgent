"""Tests for the main-agent tool-name resolver (Task 1.6)."""

from __future__ import annotations

from bot.service.pool.tool_projection import build_main_agent_tool_names


class TestBuildMainAgentToolNames:
    def test_full_preset_with_ast_grep_supplement(self) -> None:
        """FULL preset + ast_grep + task covers the core tool set."""
        names = build_main_agent_tool_names("full", ["ast_grep"], use_terminal=False)
        # Core file/search tools from FULL preset.
        assert {"read", "write", "edit", "ls", "grep", "glob"} <= names
        # ast_grep supplement.
        assert {"ast_grep_search", "ast_grep_replace"} <= names
        # communication tools present (task for subagents, send_to_peer for peers).
        assert "task" in names
        assert "send_to_peer" in names
        # send_to_agent is NOT registered on the main agent.
        assert "send_to_agent" not in names

    def test_terminal_added_when_use_terminal(self) -> None:
        names = build_main_agent_tool_names("full", [], use_terminal=True)
        # Real terminal tool names: CommandTool.name="bash",
        # ProcessTool.name="process", TerminalTool.name="terminal".
        assert {"bash", "process", "terminal"} <= names

    def test_terminal_absent_when_not_use_terminal(self) -> None:
        names = build_main_agent_tool_names("full", [], use_terminal=False)
        # bash still comes from the preset's SubprocessTool; process/terminal
        # are the terminal-manager-only tools that drop out.
        assert "process" not in names
        assert "terminal" not in names

    def test_bash_present_for_full_preset(self) -> None:
        """The main agent gets bash (SubprocessTool) for FULL/READ_WRITE/READ_ONLY."""
        for preset in ("full", "read_write", "read_only"):
            names = build_main_agent_tool_names(preset, [], use_terminal=False)
            assert "bash" in names, f"preset={preset}"
        # NONE gets no bash.
        names = build_main_agent_tool_names("none", [], use_terminal=False)
        assert "bash" not in names

    def test_read_only_preset_excludes_write_edit(self) -> None:
        names = build_main_agent_tool_names("read_only", [], use_terminal=False)
        assert "read" in names
        assert "write" not in names
        assert "edit" not in names
