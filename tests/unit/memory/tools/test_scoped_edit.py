"""Tests for ScopedEditFileTool."""

from pathlib import Path

import pytest

from modex_agent.memory.tools.scoped_edit import ScopedEditFileTool


@pytest.fixture
def tool(tmp_path: Path) -> ScopedEditFileTool:
    return ScopedEditFileTool(allowed_dirs=[tmp_path])


@pytest.mark.asyncio
async def test_edit_file_success(tmp_path: Path, tool: ScopedEditFileTool) -> None:
    f = tmp_path / "doc.txt"
    f.write_text("hello world", encoding="utf-8")

    result = await tool.execute(
        path=str(f), old_text="hello", new_text="goodbye"
    )
    assert result.success
    assert f.read_text(encoding="utf-8") == "goodbye world"


@pytest.mark.asyncio
async def test_edit_nested_file(tmp_path: Path, tool: ScopedEditFileTool) -> None:
    nested = tmp_path / "sub"
    nested.mkdir()
    f = nested / "deep.txt"
    f.write_text("aaa bbb", encoding="utf-8")

    result = await tool.execute(
        path=str(f), old_text="bbb", new_text="ccc"
    )
    assert result.success
    assert f.read_text(encoding="utf-8") == "aaa ccc"


@pytest.mark.asyncio
async def test_edit_replaces_only_first_occurrence(
    tmp_path: Path, tool: ScopedEditFileTool
) -> None:
    f = tmp_path / "multi.txt"
    f.write_text("aaa aaa aaa", encoding="utf-8")

    result = await tool.execute(
        path=str(f), old_text="aaa", new_text="bbb"
    )
    assert result.success
    assert f.read_text(encoding="utf-8") == "bbb aaa aaa"


@pytest.mark.asyncio
async def test_edit_rejects_outside_dir(tmp_path: Path) -> None:
    tool = ScopedEditFileTool(allowed_dirs=[tmp_path])
    result = await tool.execute(
        path="/etc/hosts", old_text="a", new_text="b"
    )
    assert not result.success
    assert "ACCESS DENIED" in result.error


@pytest.mark.asyncio
async def test_edit_nonexistent_file(tmp_path: Path, tool: ScopedEditFileTool) -> None:
    result = await tool.execute(
        path=str(tmp_path / "nope.txt"), old_text="a", new_text="b"
    )
    assert not result.success
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_edit_old_text_not_found(tmp_path: Path, tool: ScopedEditFileTool) -> None:
    f = tmp_path / "doc.txt"
    f.write_text("hello", encoding="utf-8")

    result = await tool.execute(
        path=str(f), old_text="missing", new_text="replacement"
    )
    assert not result.success
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_description_contains_allowed_paths(tmp_path: Path) -> None:
    tool = ScopedEditFileTool(allowed_dirs=[tmp_path])
    assert str(tmp_path) in tool.description
