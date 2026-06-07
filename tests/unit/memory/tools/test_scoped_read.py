"""Tests for ScopedReadFileTool."""

import pytest
from pathlib import Path

from framework.memory.tools.scoped_read import ScopedReadFileTool


@pytest.fixture
def tool(tmp_path: Path) -> ScopedReadFileTool:
    return ScopedReadFileTool(allowed_dirs=[tmp_path])


@pytest.mark.asyncio
async def test_read_file_success(tmp_path: Path, tool: ScopedReadFileTool) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("hello world", encoding="utf-8")

    result = await tool.execute(path=str(f))
    assert result.success
    assert result.result == "hello world"
    assert result.error is None


@pytest.mark.asyncio
async def test_read_nested_file(tmp_path: Path, tool: ScopedReadFileTool) -> None:
    nested = tmp_path / "sub" / "dir"
    nested.mkdir(parents=True)
    f = nested / "deep.txt"
    f.write_text("nested content", encoding="utf-8")

    result = await tool.execute(path=str(f))
    assert result.success
    assert result.result == "nested content"


@pytest.mark.asyncio
async def test_read_rejects_outside_dir(tmp_path: Path) -> None:
    tool = ScopedReadFileTool(allowed_dirs=[tmp_path])
    result = await tool.execute(path="/etc/passwd")
    assert not result.success
    assert "outside allowed directories" in result.error


@pytest.mark.asyncio
async def test_read_nonexistent_file(tmp_path: Path, tool: ScopedReadFileTool) -> None:
    result = await tool.execute(path=str(tmp_path / "nope.txt"))
    assert not result.success
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_read_directory_instead_of_file(tmp_path: Path, tool: ScopedReadFileTool) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    result = await tool.execute(path=str(d))
    assert not result.success
    assert "not a file" in result.error.lower()


@pytest.mark.asyncio
async def test_description_contains_allowed_paths(tmp_path: Path) -> None:
    tool = ScopedReadFileTool(allowed_dirs=[tmp_path])
    assert str(tmp_path) in tool.description
