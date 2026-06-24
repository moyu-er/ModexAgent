"""Tests for ScopedListTool."""

import pytest
from pathlib import Path

from modex_agent.memory.tools.scoped_list import ScopedListTool


@pytest.fixture
def tool(tmp_path: Path) -> ScopedListTool:
    return ScopedListTool(allowed_dirs=[tmp_path])


@pytest.mark.asyncio
async def test_list_dir_success(tmp_path: Path, tool: ScopedListTool) -> None:
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "beta").mkdir()

    result = await tool.execute(path=str(tmp_path))
    assert result.success
    lines = result.result.split("\n")
    assert len(lines) == 2
    # sorted by name: alpha.txt, beta
    assert "file  alpha.txt" in lines[0]
    assert "dir  beta" in lines[1]


@pytest.mark.asyncio
async def test_list_nested_dir(tmp_path: Path, tool: ScopedListTool) -> None:
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "inner.txt").write_text("x", encoding="utf-8")

    result = await tool.execute(path=str(nested))
    assert result.success
    assert "file  inner.txt" in result.result


@pytest.mark.asyncio
async def test_list_rejects_outside_dir(tmp_path: Path) -> None:
    tool = ScopedListTool(allowed_dirs=[tmp_path])
    result = await tool.execute(path="/etc")
    assert not result.success
    assert "ACCESS DENIED" in result.error


@pytest.mark.asyncio
async def test_list_nonexistent_dir(tmp_path: Path, tool: ScopedListTool) -> None:
    result = await tool.execute(path=str(tmp_path / "nope"))
    assert not result.success
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_list_file_instead_of_dir(tmp_path: Path, tool: ScopedListTool) -> None:
    f = tmp_path / "afile.txt"
    f.write_text("x", encoding="utf-8")

    result = await tool.execute(path=str(f))
    assert not result.success
    assert "not a directory" in result.error.lower()


@pytest.mark.asyncio
async def test_list_empty_dir(tmp_path: Path, tool: ScopedListTool) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    result = await tool.execute(path=str(empty))
    assert result.success
    assert "empty" in result.result.lower()


@pytest.mark.asyncio
async def test_description_contains_allowed_paths(tmp_path: Path) -> None:
    tool = ScopedListTool(allowed_dirs=[tmp_path])
    assert str(tmp_path) in tool.description
