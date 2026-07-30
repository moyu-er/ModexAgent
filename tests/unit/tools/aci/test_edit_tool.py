"""Tests for tools.aci.edit_tool — AciEditTool inherits EditFileTool + lint suffix."""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.tools.aci.edit_tool import AciEditTool
from modex_agent.tools.lint import (
    FileLinter,
    LintIssue,
    LintRegistry,
    LintResult,
)
from modex_agent.tools.standard.file_tool import EditFileTool

# ── Inheritance ─────────────────────────────────────────────────────────────


class TestAciEditToolInheritance:
    """AciEditTool subclasses EditFileTool — same interface, enhanced output."""

    def test_is_edit_file_tool_subclass(self) -> None:
        """AciEditTool inherits from EditFileTool (isinstance check)."""
        tool = AciEditTool(LintRegistry())
        assert isinstance(tool, EditFileTool)

    def test_name_is_edit(self) -> None:
        """Tool name is 'edit' — drop-in replacement."""
        tool = AciEditTool(LintRegistry())
        assert tool.name == "edit"

    def test_description_inherited_unchanged(self) -> None:
        """Description is inherited from EditFileTool — not overridden."""
        tool = AciEditTool(LintRegistry())
        base = EditFileTool()
        assert tool.description == base.description

    def test_parameters_inherited_unchanged(self) -> None:
        """Parameters schema is inherited from EditFileTool."""
        tool = AciEditTool(LintRegistry())
        base = EditFileTool()
        assert tool.parameters == base.parameters


# ── Edit + lint suffix ──────────────────────────────────────────────────────


class _AlwaysCleanLinter(FileLinter):
    """Linter that always returns 0 issues — for testing the 'clean' path."""

    @property
    def name(self) -> str:
        return "test-clean"

    def supports(self, path: Path) -> bool:
        return path.suffix == ".py"

    async def lint(self, path: Path) -> LintResult:
        return LintResult(status="ok", issues=[])


class _AlwaysDirtyLinter(FileLinter):
    """Linter that always returns 2 fake issues — for testing the 'dirty' path."""

    @property
    def name(self) -> str:
        return "test-dirty"

    def supports(self, path: Path) -> bool:
        return path.suffix == ".py"

    async def lint(self, path: Path) -> LintResult:
        return LintResult(status="ok", issues=[
            LintIssue(
                message="undefined name 'bar'",
                source="test-dirty",
                line=10,
                column=5,
                severity="error",
                code="F821",
            ),
            LintIssue(
                message="expected 2 blank lines",
                source="test-dirty",
                line=12,
                column=1,
                severity="warning",
                code="E302",
            ),
        ])


class _UnavailableLinter(FileLinter):
    """Linter that reports unavailable — for testing fail-open reporting."""

    @property
    def name(self) -> str:
        return "test-unavail"

    def supports(self, path: Path) -> bool:
        return path.suffix == ".py"

    async def lint(self, path: Path) -> LintResult:
        return LintResult(status="unavailable", message="binary not found")


class TestAciEditToolLintSuffix:
    """AciEditTool appends lint diagnostics after the edit diff."""

    @pytest.mark.asyncio
    async def test_edit_clean_file_shows_zero_issues(self, tmp_path: Path) -> None:
        """Edit a .py file → lint runs → 0 issues → 'Lint: 0 issues' suffix."""
        py = tmp_path / "clean.py"
        py.write_text("x = 1\nprint(x)\n", encoding="utf-8")

        registry = LintRegistry()
        registry.register(_AlwaysCleanLinter())
        tool = AciEditTool(registry)

        result = await tool.execute(
            path=str(py),
            old_string="x = 1",
            new_string="x = 2",
        )
        assert isinstance(result, str)
        assert "Edit applied successfully" in result
        assert "Lint" in result
        assert "0 issues" in result

    @pytest.mark.asyncio
    async def test_edit_dirty_file_shows_issues(self, tmp_path: Path) -> None:
        """Edit a .py file → lint runs → issues appended with line/code/source."""
        py = tmp_path / "dirty.py"
        py.write_text("x = 1\nprint(x)\n", encoding="utf-8")

        registry = LintRegistry()
        registry.register(_AlwaysDirtyLinter())
        tool = AciEditTool(registry)

        result = await tool.execute(
            path=str(py),
            old_string="x = 1",
            new_string="x = 2",
        )
        assert isinstance(result, str)
        assert "Edit applied successfully" in result
        assert "Lint" in result
        assert "2 issues" in result
        assert "F821" in result
        assert "undefined name" in result
        assert "test-dirty" in result

    @pytest.mark.asyncio
    async def test_edit_non_py_file_shows_skipped(self, tmp_path: Path) -> None:
        """Edit a .md file → no linter matches → 'Lint: skipped' suffix."""
        md = tmp_path / "readme.md"
        md.write_text("# Hello\n", encoding="utf-8")

        registry = LintRegistry()
        registry.register(_AlwaysCleanLinter())  # only supports .py
        tool = AciEditTool(registry)

        result = await tool.execute(
            path=str(md),
            old_string="# Hello",
            new_string="# World",
        )
        assert isinstance(result, str)
        assert "Edit applied successfully" in result
        assert "skipped" in result

    @pytest.mark.asyncio
    async def test_edit_with_unavailable_linter(self, tmp_path: Path) -> None:
        """Edit a .py file → linter unavailable → 'unavailable' in suffix."""
        py = tmp_path / "x.py"
        py.write_text("x = 1\n", encoding="utf-8")

        registry = LintRegistry()
        registry.register(_UnavailableLinter())
        tool = AciEditTool(registry)

        result = await tool.execute(
            path=str(py),
            old_string="x = 1",
            new_string="x = 2",
        )
        assert isinstance(result, str)
        assert "Edit applied successfully" in result
        assert "unavailable" in result

    @pytest.mark.asyncio
    async def test_edit_error_path_no_lint(self, tmp_path: Path) -> None:
        """When edit fails (old_string not found), lint does NOT run."""
        py = tmp_path / "x.py"
        py.write_text("x = 1\n", encoding="utf-8")

        registry = LintRegistry()
        registry.register(_AlwaysDirtyLinter())
        tool = AciEditTool(registry)

        from modex_agent.core.tool_manager import ToolResult

        result = await tool.execute(
            path=str(py),
            old_string="NONEXISTENT",
            new_string="whatever",
        )
        # Edit fails → ToolResult with error, no lint suffix
        assert isinstance(result, ToolResult)
        assert result.error is not None
        assert "not found" in result.error or "old_string" in result.error

    @pytest.mark.asyncio
    async def test_edit_empty_old_string_create_file_no_lint(self, tmp_path: Path) -> None:
        """Creating a new file (empty old_string) → no lint (file may be incomplete)."""
        py = tmp_path / "new.py"

        registry = LintRegistry()
        registry.register(_AlwaysDirtyLinter())
        tool = AciEditTool(registry)

        result = await tool.execute(
            path=str(py),
            old_string="",
            new_string="x = 1\n",
        )
        assert isinstance(result, str)
        assert "Created" in result
        # No lint suffix for file creation
        assert "Lint" not in result

    @pytest.mark.asyncio
    async def test_edit_replace_all_still_lints(self, tmp_path: Path) -> None:
        """replace_all=True edit → lint still runs after successful replacement."""
        py = tmp_path / "multi.py"
        py.write_text("foo = 1\nfoo = 2\n", encoding="utf-8")

        registry = LintRegistry()
        registry.register(_AlwaysCleanLinter())
        tool = AciEditTool(registry)

        result = await tool.execute(
            path=str(py),
            old_string="foo",
            new_string="bar",
            replace_all=True,
        )
        assert isinstance(result, str)
        assert "All" in result or "replaced" in result
        assert "Lint" in result
        assert "0 issues" in result
