"""Tests for ScopedReadFileTool."""

import pytest
from pathlib import Path

from modex_agent.memory.tools.scoped_read import ScopedReadFileTool


@pytest.fixture
def tool(tmp_path: Path) -> ScopedReadFileTool:
    return ScopedReadFileTool(allowed_dirs=[tmp_path])


@pytest.mark.asyncio
async def test_read_file_success(tmp_path: Path, tool: ScopedReadFileTool) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("hello world", encoding="utf-8")

    result = await tool.execute(path=str(f))
    assert result.success
    assert "hello world" in result.result
    assert "read_status: complete" in result.result
    assert result.error is None


@pytest.mark.asyncio
async def test_read_nested_file(tmp_path: Path, tool: ScopedReadFileTool) -> None:
    nested = tmp_path / "sub" / "dir"
    nested.mkdir(parents=True)
    f = nested / "deep.txt"
    f.write_text("nested content", encoding="utf-8")

    result = await tool.execute(path=str(f))
    assert result.success
    assert "nested content" in result.result


@pytest.mark.asyncio
async def test_read_with_offset_and_limit(tmp_path: Path, tool: ScopedReadFileTool) -> None:
    f = tmp_path / "test.txt"
    f.write_text("a\nb\nc\nd\ne", encoding="utf-8")

    result = await tool.execute(path=str(f), offset=1, limit=2)
    assert result.success
    assert "b" in result.result
    assert "c" in result.result
    # "a" 不在内容行中（metadata 的 total_lines 包含 'a' 但那是后缀，不影响）
    content_lines = result.result.split("\n\n")[0].splitlines()
    assert content_lines == ["b", "c"]


@pytest.mark.asyncio
async def test_read_offset_exceeds_file(tmp_path: Path, tool: ScopedReadFileTool) -> None:
    f = tmp_path / "test.txt"
    f.write_text("a\nb", encoding="utf-8")

    result = await tool.execute(path=str(f), offset=10)
    assert not result.success
    assert "offset (10) exceeds file length (2 lines)" in result.error


@pytest.mark.asyncio
async def test_read_empty_file(tmp_path: Path, tool: ScopedReadFileTool) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")

    result = await tool.execute(path=str(f))
    assert result.success
    assert "(empty file)" in result.result
    assert "read_status: empty" in result.result


@pytest.mark.asyncio
async def test_read_rejects_outside_dir(tmp_path: Path) -> None:
    tool = ScopedReadFileTool(allowed_dirs=[tmp_path])
    result = await tool.execute(path="/etc/passwd")
    assert not result.success
    assert "outside" in result.error.lower()


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
