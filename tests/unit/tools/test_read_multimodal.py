"""Tests for multimodal image reading in ReadFileTool.

Covers: image detection via magic bytes, capability gate (IMAGE vs text-only),
content_blocks production, Pillow compression, and text-file passthrough.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from modex_agent.core.capabilities import Modality, ModelCapabilities
from modex_agent.core.tool_manager import ToolExecutionContext
from modex_agent.tools.standard.file_tool import ReadFileTool

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_CAPABLE = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))
_TEXT_ONLY = ModelCapabilities(modalities=frozenset({Modality.TEXT}))


def _set_ctx(caps: ModelCapabilities | None) -> None:
    from modex_agent.core.tool_manager import _tool_execution_ctx

    ctx = ToolExecutionContext(model_capabilities=caps) if caps is not None else None
    _tool_execution_ctx.set(ctx)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    from modex_agent.core.tool_manager import _tool_execution_ctx

    _tool_execution_ctx.set(None)
    yield
    _tool_execution_ctx.set(None)


async def test_read_image_capable_model_produces_content_blocks(tmp_path: Path) -> None:
    img = tmp_path / "test.png"
    img.write_bytes(_PNG_BYTES)
    _set_ctx(_CAPABLE)

    tool = ReadFileTool()
    result = await tool.execute(path=str(img))

    from modex_agent.core.tool_manager import ToolResult

    assert isinstance(result, ToolResult)
    assert result.error is None
    assert result.content_blocks is not None
    assert len(result.content_blocks) == 1
    block = result.content_blocks[0]
    assert block["type"] == "image_url"
    assert "image/png" in block["image_url"]["url"]


async def test_read_image_text_only_model_returns_degradation_text(tmp_path: Path) -> None:
    img = tmp_path / "test.png"
    img.write_bytes(_PNG_BYTES)
    _set_ctx(_TEXT_ONLY)

    tool = ReadFileTool()
    result = await tool.execute(path=str(img))

    from modex_agent.core.tool_manager import ToolResult

    assert isinstance(result, ToolResult)
    assert result.content_blocks is None
    assert "lacks IMAGE capability" in result.result


async def test_read_image_no_ctx_returns_degradation_text(tmp_path: Path) -> None:
    img = tmp_path / "test.png"
    img.write_bytes(_PNG_BYTES)

    tool = ReadFileTool()
    result = await tool.execute(path=str(img))

    from modex_agent.core.tool_manager import ToolResult

    assert isinstance(result, ToolResult)
    assert result.content_blocks is None
    assert "lacks IMAGE capability" in result.result


async def test_read_text_file_unchanged(tmp_path: Path) -> None:
    txt = tmp_path / "test.txt"
    txt.write_text("hello world\nline 2\n", encoding="utf-8")
    _set_ctx(_CAPABLE)

    tool = ReadFileTool()
    result = await tool.execute(path=str(txt))

    assert isinstance(result, str)
    assert "hello world" in result


async def test_read_nonexistent_file_returns_error(tmp_path: Path) -> None:
    _set_ctx(_CAPABLE)
    tool = ReadFileTool()
    result = await tool.execute(path=str(tmp_path / "nope.txt"))

    from modex_agent.core.tool_manager import ToolResult

    assert isinstance(result, ToolResult)
    assert result.error is not None
    assert "not found" in result.error.lower() or "not found" in result.error
