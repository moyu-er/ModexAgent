"""Unit tests for search tools: SearchFilesTool and FindFilesTool.

Tests backend fallback, pagination, regex/literal matching, and error handling.
All tests use the pure Python backend (rg/fd are not required in test environment).
"""

import tempfile
from pathlib import Path

import pytest

from framework.tools.standard.search_tool import FindFilesTool, SearchFilesTool


@pytest.fixture
def tmp_workspace():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestFindFilesTool:
    @pytest.mark.asyncio
    async def test_find_by_extension(self, tmp_workspace):
        (tmp_workspace / "a.py").write_text("x")
        (tmp_workspace / "b.py").write_text("x")
        (tmp_workspace / "c.txt").write_text("x")
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace))
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result
        assert "Found 2 files" in result

    @pytest.mark.asyncio
    async def test_find_recursive(self, tmp_workspace):
        (tmp_workspace / "sub").mkdir()
        (tmp_workspace / "sub" / "deep.py").write_text("x")
        (tmp_workspace / "root.py").write_text("x")
        tool = FindFilesTool()
        result = await tool.execute(pattern="**/*.py", path=str(tmp_workspace))
        assert "root.py" in result
        assert "deep.py" in result

    @pytest.mark.asyncio
    async def test_find_no_matches(self, tmp_workspace):
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.md", path=str(tmp_workspace))
        assert "No files matching" in result

    @pytest.mark.asyncio
    async def test_find_pagination(self, tmp_workspace):
        for i in range(10):
            (tmp_workspace / f"file{i}.py").write_text("x")
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace), max_results=5)
        assert "Found 5 files" in result
        lines = result.splitlines()
        py_files = [ln for ln in lines if ln.endswith(".py")]
        assert len(py_files) == 5

    @pytest.mark.asyncio
    async def test_find_not_found_directory(self, tmp_workspace):
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace / "missing"))
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_find_not_a_directory(self, tmp_workspace):
        (tmp_workspace / "file.txt").write_text("x")
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace / "file.txt"))
        assert "not a directory" in result.lower()


class TestSearchFilesTool:
    @pytest.mark.asyncio
    async def test_search_literal_match(self, tmp_workspace):
        (tmp_workspace / "code.py").write_text("def hello():\n    pass\n")
        tool = SearchFilesTool()
        result = await tool.execute(query="hello", path=str(tmp_workspace), regex=False)
        assert "code.py" in result
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_search_regex_match(self, tmp_workspace):
        (tmp_workspace / "code.py").write_text("def hello_world():\n    pass\n")
        tool = SearchFilesTool()
        result = await tool.execute(query=r"hello_\w+", path=str(tmp_workspace), regex=True)
        assert "code.py" in result
        assert "hello_world" in result

    @pytest.mark.asyncio
    async def test_search_file_pattern_filter(self, tmp_workspace):
        (tmp_workspace / "a.py").write_text("target\n")
        (tmp_workspace / "b.txt").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(query="target", path=str(tmp_workspace), file_pattern="*.py")
        assert "a.py" in result
        assert "b.txt" not in result

    @pytest.mark.asyncio
    async def test_search_context_lines(self, tmp_workspace):
        (tmp_workspace / "code.py").write_text("line1\nline2\nline3\ntarget\nline5\nline6\nline7\n")
        tool = SearchFilesTool()
        result = await tool.execute(query="target", path=str(tmp_workspace), context_lines=2)
        # target on line 4; 2 lines context means lines 2-3 before, 5-6 after
        assert "line2" in result
        assert "line3" in result
        assert ">" in result
        assert "line5" in result
        assert "line6" in result
        # line1 and line7 are outside the 2-line context range
        assert "line1" not in result
        assert "line7" not in result

    @pytest.mark.asyncio
    async def test_search_no_matches(self, tmp_workspace):
        tool = SearchFilesTool()
        result = await tool.execute(query="nonexistent", path=str(tmp_workspace))
        assert "No matches found" in result

    @pytest.mark.asyncio
    async def test_search_invalid_regex(self, tmp_workspace):
        tool = SearchFilesTool()
        result = await tool.execute(query="[invalid", path=str(tmp_workspace), regex=True)
        assert "Invalid regex" in result

    @pytest.mark.asyncio
    async def test_search_pagination(self, tmp_workspace):
        for i in range(10):
            (tmp_workspace / f"file{i}.py").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(query="target", path=str(tmp_workspace), max_results=5)
        assert "Found 5 matches" in result
        match_count = result.count("target")
        assert match_count == 5

    @pytest.mark.asyncio
    async def test_search_skips_binary(self, tmp_workspace):
        (tmp_workspace / "binary.dat").write_bytes(b"\x00\x01\x02target\x03")
        (tmp_workspace / "text.py").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(query="target", path=str(tmp_workspace))
        assert "text.py" in result
        assert "binary.dat" not in result

    @pytest.mark.asyncio
    async def test_search_not_found_directory(self, tmp_workspace):
        tool = SearchFilesTool()
        result = await tool.execute(query="test", path=str(tmp_workspace / "missing"))
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_search_not_a_directory(self, tmp_workspace):
        (tmp_workspace / "file.txt").write_text("x")
        tool = SearchFilesTool()
        result = await tool.execute(query="test", path=str(tmp_workspace / "file.txt"))
        assert "not a directory" in result.lower()
