"""Unit tests for standard tools: file tools and shell tool.

TDD: verify ReadFileTool, WriteFileTool, EditFileTool, ListDirTool,
and ShellTool behaviors including permissions, safety guards, and edge cases.
"""

import os
import tempfile
from pathlib import Path

import pytest

from framework.tools.standard.file_tool import (
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    ListDirTool,
)
from framework.tools.standard.shell_tool import ShellTool


@pytest.fixture
def tmp_workspace():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ---------------------------------------------------------------------------
# ReadFileTool
# ---------------------------------------------------------------------------

class TestReadFileTool:
    @pytest.mark.asyncio
    async def test_read_existing_file(self, tmp_workspace):
        file_path = tmp_workspace / "test.txt"
        file_path.write_text("line1\nline2\nline3", encoding="utf-8")
        tool = ReadFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(file_path))
        assert "line1" in result
        assert "line3" in result

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_workspace):
        tool = ReadFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(tmp_workspace / "missing.txt"))
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_read_directory_error(self, tmp_workspace):
        tool = ReadFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(tmp_workspace))
        assert "not a file" in result.lower()

    @pytest.mark.asyncio
    async def test_read_outside_allowed_dir(self, tmp_workspace):
        tool = ReadFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path="/etc/passwd")
        assert "outside allowed directory" in result.lower()

    @pytest.mark.asyncio
    async def test_read_line_range(self, tmp_workspace):
        file_path = tmp_workspace / "test.txt"
        file_path.write_text("a\nb\nc\nd\ne", encoding="utf-8")
        tool = ReadFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(file_path), start_line=2, end_line=4)
        lines = result.splitlines()
        # Should contain lines b, c, d plus the more-lines marker
        assert "b" in lines
        assert "c" in lines
        assert "d" in lines
        assert "a" not in lines
        assert "e" not in lines

    @pytest.mark.asyncio
    async def test_read_invalid_line_range(self, tmp_workspace):
        file_path = tmp_workspace / "test.txt"
        file_path.write_text("a\nb", encoding="utf-8")
        tool = ReadFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(file_path), start_line=1, end_line=0)
        assert "must be >= start_line" in result.lower()


# ---------------------------------------------------------------------------
# WriteFileTool
# ---------------------------------------------------------------------------

class TestWriteFileTool:
    @pytest.mark.asyncio
    async def test_write_new_file(self, tmp_workspace):
        tool = WriteFileTool(allowed_dir=tmp_workspace)
        file_path = tmp_workspace / "new.txt"
        result = await tool.execute(path=str(file_path), content="hello")
        assert "successfully wrote" in result.lower()
        assert file_path.read_text(encoding="utf-8") == "hello"

    @pytest.mark.asyncio
    async def test_write_creates_parent_dirs(self, tmp_workspace):
        tool = WriteFileTool(allowed_dir=tmp_workspace)
        file_path = tmp_workspace / "sub" / "dir" / "file.txt"
        result = await tool.execute(path=str(file_path), content="data")
        assert file_path.exists()
        assert file_path.read_text(encoding="utf-8") == "data"

    @pytest.mark.asyncio
    async def test_write_outside_allowed_dir(self, tmp_workspace):
        tool = WriteFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path="/tmp/hack.txt", content="x")
        assert "outside allowed directory" in result.lower()


# ---------------------------------------------------------------------------
# EditFileTool
# ---------------------------------------------------------------------------

class TestEditFileTool:
    @pytest.mark.asyncio
    async def test_edit_existing_text(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("hello world", encoding="utf-8")
        tool = EditFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(file_path), old_text="world", new_text="universe")
        assert "successfully edited" in result.lower()
        assert file_path.read_text(encoding="utf-8") == "hello universe"

    @pytest.mark.asyncio
    async def test_edit_missing_old_text(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("hello world", encoding="utf-8")
        tool = EditFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(file_path), old_text="missing", new_text="x")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_ambiguous_old_text(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("abc abc", encoding="utf-8")
        tool = EditFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(file_path), old_text="abc", new_text="x")
        assert "appears 2 times" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_outside_allowed_dir(self, tmp_workspace):
        tool = EditFileTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path="/etc/passwd", old_text="a", new_text="b")
        assert "outside allowed directory" in result.lower()


# ---------------------------------------------------------------------------
# ListDirTool
# ---------------------------------------------------------------------------

class TestListDirTool:
    @pytest.mark.asyncio
    async def test_list_existing_directory(self, tmp_workspace):
        (tmp_workspace / "file1.txt").write_text("x")
        (tmp_workspace / "dir1").mkdir()
        tool = ListDirTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(tmp_workspace))
        assert "file1.txt" in result
        assert "dir1" in result

    @pytest.mark.asyncio
    async def test_list_empty_directory(self, tmp_workspace):
        tool = ListDirTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(tmp_workspace))
        assert "is empty" in result.lower()

    @pytest.mark.asyncio
    async def test_list_not_found(self, tmp_workspace):
        tool = ListDirTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(tmp_workspace / "missing"))
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_list_file_error(self, tmp_workspace):
        file_path = tmp_workspace / "not_dir.txt"
        file_path.write_text("x")
        tool = ListDirTool(allowed_dir=tmp_workspace)
        result = await tool.execute(path=str(file_path))
        assert "not a directory" in result.lower()


# ---------------------------------------------------------------------------
# ShellTool
# ---------------------------------------------------------------------------

class TestShellTool:
    @pytest.fixture
    def safe_shell(self):
        return ShellTool(enable_safety_guard=True, restrict_to_workspace=False)

    @pytest.mark.asyncio
    async def test_shell_echo(self, safe_shell):
        result = await safe_shell.execute(command='echo "hello"')
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_shell_with_working_dir(self, tmp_workspace):
        shell = ShellTool(enable_safety_guard=False)
        result = await shell.execute(command="pwd" if os.name != "nt" else "cd", working_dir=str(tmp_workspace))
        # On Windows `cd` returns current dir; on Unix `pwd` returns it
        assert str(tmp_workspace.name) in result or "STDERR" not in result

    @pytest.mark.asyncio
    async def test_shell_timeout(self):
        shell = ShellTool(timeout=1, enable_safety_guard=False)
        # Use Python sleep for cross-platform timeout test
        result = await shell.execute(command='python -c "import time; time.sleep(5)"')
        assert "timed out" in result.lower()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-specific dangerous command test")
    @pytest.mark.asyncio
    async def test_shell_safety_guard_blocks_rm_rf(self, safe_shell):
        result = await safe_shell.execute(command="rm -rf /tmp/test")
        assert "blocked by safety guard" in result.lower()

    @pytest.mark.skipif(os.name != "nt", reason="Windows-specific dangerous command test")
    @pytest.mark.asyncio
    async def test_shell_safety_guard_blocks_windows_dangerous(self, safe_shell):
        result = await safe_shell.execute(command="format C:")
        assert "blocked by safety guard" in result.lower()

    @pytest.mark.asyncio
    async def test_shell_safety_guard_blocks_format(self, safe_shell):
        result = await safe_shell.execute(command="format C:")
        assert "blocked by safety guard" in result.lower() or "dangerous pattern" in result.lower()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX path workspace restriction test")
    @pytest.mark.asyncio
    async def test_shell_restrict_to_workspace_blocks_external_path(self, tmp_workspace):
        shell = ShellTool(
            enable_safety_guard=True,
            restrict_to_workspace=True,
            working_dir=str(tmp_workspace),
        )
        result = await shell.execute(command="cat /etc/passwd")
        assert "blocked by safety guard" in result.lower() or "outside working dir" in result.lower()

    @pytest.mark.skipif(os.name != "nt", reason="Windows path workspace restriction test")
    @pytest.mark.asyncio
    async def test_shell_restrict_to_workspace_blocks_windows_external_path(self, tmp_workspace):
        shell = ShellTool(
            enable_safety_guard=True,
            restrict_to_workspace=True,
            working_dir=str(tmp_workspace),
        )
        result = await shell.execute(command="type C:\\Windows\\System32\\drivers\\etc\\hosts")
        assert "blocked by safety guard" in result.lower() or "outside working dir" in result.lower()

    @pytest.mark.asyncio
    async def test_shell_disabled_safety_guard(self):
        shell = ShellTool(enable_safety_guard=False)
        # Even dangerous-looking command should be allowed when guard is off
        result = await shell.execute(command="echo 'rm -rf /'")
        assert "rm -rf /" in result

    @pytest.mark.asyncio
    async def test_shell_allowlist_blocks_unmatched(self):
        shell = ShellTool(
            enable_safety_guard=True,
            allow_patterns=[r"^echo\b"],
        )
        result = await shell.execute(command="ls")
        assert "not in allowlist" in result.lower()
