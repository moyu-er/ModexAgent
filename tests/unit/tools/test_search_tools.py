"""Unit tests for SearchFilesTool (grep).

Tests backend fallback, pagination, regex/literal matching, and error handling.
All tests use the pure Python backend (rg/fd are not required in test environment).
"""

import tempfile
from pathlib import Path

import pytest

from modex_agent.tools.standard.search_tool import SearchFilesTool


@pytest.fixture
def tmp_workspace():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestSearchFilesTool:
    @pytest.mark.asyncio
    async def test_search_literal_match(self, tmp_workspace):
        (tmp_workspace / "code.py").write_text("def hello():\n    pass\n")
        tool = SearchFilesTool()
        result = await tool.execute(pattern="hello", path=str(tmp_workspace), regex=False)
        assert "code.py" in result
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_search_regex_match(self, tmp_workspace):
        (tmp_workspace / "code.py").write_text("def hello_world():\n    pass\n")
        tool = SearchFilesTool()
        result = await tool.execute(pattern=r"hello_\w+", path=str(tmp_workspace), regex=True)
        assert "code.py" in result
        assert "hello_world" in result

    @pytest.mark.asyncio
    async def test_search_file_pattern_filter(self, tmp_workspace):
        (tmp_workspace / "a.py").write_text("target\n")
        (tmp_workspace / "b.txt").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(pattern="target", path=str(tmp_workspace), include="*.py")
        assert "a.py" in result
        assert "b.txt" not in result

    @pytest.mark.asyncio
    async def test_search_file_pattern_subdirectory(self, tmp_workspace):
        """Glob with directory prefix (e.g. sub/*.py) must work on all backends."""
        (tmp_workspace / "root.py").write_text("target\n")
        (tmp_workspace / "sub").mkdir()
        (tmp_workspace / "sub" / "nested.py").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(
            pattern="target", path=str(tmp_workspace), include="sub/*.py"
        )
        assert "nested.py" in result
        assert "root.py" not in result

    @pytest.mark.asyncio
    async def test_search_context_lines(self, tmp_workspace):
        (tmp_workspace / "code.py").write_text("line1\nline2\nline3\ntarget\nline5\nline6\nline7\n")
        tool = SearchFilesTool()
        result = await tool.execute(pattern="target", path=str(tmp_workspace), context_lines=2)
        # Core check: the match itself is always present
        assert "target" in result
        assert "code.py" in result
        # Context lines depend on backend behaviour (ripgrep --vimgrep -C
        # omits context on some Windows builds).  Verify the Python fallback
        # directly instead for deterministic context assertions.
        py_result = await tool._search_with_python(
            "target", tmp_workspace, "*", True, 50, 2
        )
        assert "line2" in py_result
        assert "line3" in py_result
        assert "line5" in py_result
        assert "line6" in py_result
        assert "line1" not in py_result
        assert "line7" not in py_result

    @pytest.mark.asyncio
    async def test_search_no_matches(self, tmp_workspace):
        tool = SearchFilesTool()
        result = await tool.execute(pattern="nonexistent", path=str(tmp_workspace))
        assert "No matches found" in result

    @pytest.mark.asyncio
    async def test_search_invalid_regex(self, tmp_workspace):
        tool = SearchFilesTool()
        result = await tool.execute(pattern="[invalid", path=str(tmp_workspace), regex=True)
        assert "Invalid regex" in result

    @pytest.mark.asyncio
    async def test_search_pagination(self, tmp_workspace):
        for i in range(10):
            (tmp_workspace / f"file{i}.py").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(pattern="target", path=str(tmp_workspace), max_results=5)
        # ripgrep --max-count is per-file, so total matches may exceed max_results.
        # Verify that the result indicates limiting and shows exactly max_results entries.
        assert "Found" in result
        assert "target" in result
        assert "not shown (limit: 5)" in result
        displayed_lines = [ln for ln in result.splitlines() if "target" in ln]
        assert len(displayed_lines) == 5

    @pytest.mark.asyncio
    async def test_search_skips_binary(self, tmp_workspace):
        (tmp_workspace / "binary.dat").write_bytes(b"\x00\x01\x02target\x03")
        (tmp_workspace / "text.py").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(pattern="target", path=str(tmp_workspace))
        assert "text.py" in result
        assert "binary.dat" not in result

    @pytest.mark.asyncio
    async def test_search_not_found_directory(self, tmp_workspace):
        tool = SearchFilesTool()
        result = await tool.execute(pattern="test", path=str(tmp_workspace / "missing"))
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_search_single_file(self, tmp_workspace):
        (tmp_workspace / "file.txt").write_text("target value\n")
        (tmp_workspace / "other.py").write_text("target value\n")
        tool = SearchFilesTool()
        result = await tool.execute(pattern="target", path=str(tmp_workspace / "file.txt"))
        assert "file.txt" in result
        assert "target" in result
        assert "other.py" not in result

    @pytest.mark.asyncio
    async def test_search_not_a_directory(self, tmp_workspace):
        (tmp_workspace / "file.txt").write_text("x")
        tool = SearchFilesTool()
        result = await tool.execute(pattern="test", path=str(tmp_workspace / "file.txt"))
        assert "not a directory" not in result.lower()
        assert "No matches found" in result or "Found" in result

    # ------------------------------------------------------------------
    # Regression: LLM providers may send numeric params as strings
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_search_string_max_results(self, tmp_workspace):
        (tmp_workspace / "a.py").write_text("target\n")
        (tmp_workspace / "b.py").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(
            pattern="target", path=str(tmp_workspace), max_results="4"
        )
        assert "Found" in result
        assert "target" in result

    @pytest.mark.asyncio
    async def test_search_string_context_lines(self, tmp_workspace):
        (tmp_workspace / "a.py").write_text("line1\ntarget\nline3\n")
        tool = SearchFilesTool()
        result = await tool.execute(
            pattern="target", path=str(tmp_workspace), context_lines="1"
        )
        assert "Found" in result
        assert "target" in result

    @pytest.mark.asyncio
    async def test_search_string_regex_bool(self, tmp_workspace):
        (tmp_workspace / "a.py").write_text("target.value\n")
        tool = SearchFilesTool()
        result = await tool.execute(
            pattern="target", path=str(tmp_workspace), regex="false"
        )
        assert "Found" in result

    @pytest.mark.asyncio
    async def test_search_string_max_results_and_context_lines(self, tmp_workspace):
        (tmp_workspace / "a.py").write_text("line1\ntarget\nline3\n")
        (tmp_workspace / "b.py").write_text("line1\ntarget\nline3\n")
        tool = SearchFilesTool()
        result = await tool.execute(
            pattern="target", path=str(tmp_workspace),
            max_results="4", context_lines="1",
        )
        assert "Found" in result
        assert "target" in result

    # ------------------------------------------------------------------
    # Parser unit tests — cross-platform path handling
    # ------------------------------------------------------------------

    def test_parse_vimgrep_unix_relative_path(self):
        tool = SearchFilesTool()
        result = tool._parse_vimgrep("/home/user/project/file.py:42:1:def hello():")
        assert result == ("/home/user/project/file.py", 42, "def hello():")

    def test_parse_vimgrep_windows_absolute_path(self):
        tool = SearchFilesTool()
        result = tool._parse_vimgrep(
            r"F:\tool\pythonProject\ModexAgent\framework\tools\standard\search_tool.py:202:5:    def _parse_vimgrep"
        )
        assert result == (
            r"F:\tool\pythonProject\ModexAgent\framework\tools\standard\search_tool.py",
            202,
            "    def _parse_vimgrep",
        )

    def test_parse_vimgrep_text_with_colons(self):
        """Text portion may contain colons — rsplit must not eat them."""
        tool = SearchFilesTool()
        result = tool._parse_vimgrep("file.py:10:1:a:b:c")
        assert result == ("file.py", 10, "a:b:c")

    def test_parse_vimgrep_too_few_colons(self):
        tool = SearchFilesTool()
        assert tool._parse_vimgrep("file.py:10") is None
        assert tool._parse_vimgrep("file.py") is None

    def test_parse_git_grep_line_unix_relative_path(self):
        result = SearchFilesTool._parse_git_grep_line("file.py:42:def hello():")
        assert result == ("file.py", 42, "def hello():")

    def test_parse_git_grep_line_windows_absolute_path(self):
        result = SearchFilesTool._parse_git_grep_line(
            r"F:\tool\project\file.py:42:def hello():"
        )
        assert result == (
            r"F:\tool\project\file.py",
            42,
            "def hello():",
        )

    def test_parse_git_grep_line_text_with_colons(self):
        """Text portion may contain colons — rsplit must keep them."""
        result = SearchFilesTool._parse_git_grep_line("file.py:10:a:b:c")
        assert result == ("file.py", 10, "a:b:c")

    def test_parse_git_grep_line_context_format(self):
        """Context lines use dash separator, not colon."""
        result = SearchFilesTool._parse_git_grep_line("file.py-9-import os")
        assert result == ("file.py", 9, "import os")

    def test_parse_git_grep_line_context_format_negative_lnum(self):
        """git grep uses negative line numbers for context lines before the match."""
        result = SearchFilesTool._parse_git_grep_line("file.py--5-import os")
        assert result == ("file.py", 5, "import os")
