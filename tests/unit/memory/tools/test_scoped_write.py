"""Tests for ScopedWriteFileTool."""

import pytest
from pathlib import Path

from modex_agent.memory.tools.scoped_write import ScopedWriteFileTool


@pytest.fixture
def tool(tmp_path: Path) -> ScopedWriteFileTool:
    return ScopedWriteFileTool(allowed_dirs=[tmp_path])


@pytest.mark.asyncio
async def test_write_file_success(tmp_path: Path, tool: ScopedWriteFileTool) -> None:
    target = tmp_path / "out.txt"

    result = await tool.execute(path=str(target), content="written")
    assert result.success
    assert target.read_text(encoding="utf-8") == "written"


@pytest.mark.asyncio
async def test_write_nested_file(tmp_path: Path, tool: ScopedWriteFileTool) -> None:
    target = tmp_path / "sub" / "dir" / "deep.txt"

    result = await tool.execute(path=str(target), content="nested write")
    assert result.success
    assert target.read_text(encoding="utf-8") == "nested write"


@pytest.mark.asyncio
async def test_write_overwrites_existing(tmp_path: Path, tool: ScopedWriteFileTool) -> None:
    target = tmp_path / "exists.txt"
    target.write_text("old", encoding="utf-8")

    result = await tool.execute(path=str(target), content="new")
    assert result.success
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_write_rejects_outside_dir(tmp_path: Path) -> None:
    tool = ScopedWriteFileTool(allowed_dirs=[tmp_path])
    result = await tool.execute(path="/tmp/outside.txt", content="nope")
    assert not result.success
    assert "ACCESS DENIED" in result.error


@pytest.mark.asyncio
async def test_description_contains_allowed_paths(tmp_path: Path) -> None:
    tool = ScopedWriteFileTool(allowed_dirs=[tmp_path])
    assert str(tmp_path) in tool.description
