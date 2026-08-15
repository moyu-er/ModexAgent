"""Tests for multimodal image reading in ReadFileTool.

Covers: image detection via magic bytes, capability gate (IMAGE vs text-only),
content_blocks production, Pillow compression, and text-file passthrough.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.tool_manager import ToolExecutionContext, ToolResult
from modex_agent.memory.tools.scoped_read import ScopedReadFileTool
from modex_agent.tools.standard.file_tool import ReadFileTool

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_CAPABLE = ModelInfo(
    model_name="test-vision",
    capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
)
_TEXT_ONLY = ModelInfo(
    model_name="test-text",
    capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT})),
)


def _set_ctx(info: ModelInfo | None) -> None:
    from modex_agent.core.tool_manager import _tool_execution_ctx

    ctx = ToolExecutionContext(model_info=info) if info is not None else None
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
    assert "Visual content not available" in result.message_content()


async def test_read_image_no_ctx_returns_degradation_text(tmp_path: Path) -> None:
    img = tmp_path / "test.png"
    img.write_bytes(_PNG_BYTES)

    tool = ReadFileTool()
    result = await tool.execute(path=str(img))

    from modex_agent.core.tool_manager import ToolResult

    assert isinstance(result, ToolResult)
    assert result.content_blocks is None
    assert "Visual content not available" in result.message_content()


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


async def test_scoped_read_image_capable_produces_content_blocks(tmp_path: Path) -> None:
    img = tmp_path / "scoped.png"
    img.write_bytes(_PNG_BYTES)
    _set_ctx(_CAPABLE)

    tool = ScopedReadFileTool(allowed_dirs=[tmp_path])
    result = await tool.execute(path=str(img))

    assert isinstance(result, ToolResult)
    assert result.error is None
    assert result.content_blocks is not None
    assert len(result.content_blocks) == 1
    block = result.content_blocks[0]
    assert block["type"] == "image_url"
    assert "image/png" in block["image_url"]["url"]


async def test_scoped_read_image_text_only_returns_degradation(tmp_path: Path) -> None:
    img = tmp_path / "scoped.png"
    img.write_bytes(_PNG_BYTES)
    _set_ctx(_TEXT_ONLY)

    tool = ScopedReadFileTool(allowed_dirs=[tmp_path])
    result = await tool.execute(path=str(img))

    assert isinstance(result, ToolResult)
    assert result.content_blocks is None
    assert "Visual content not available" in result.message_content()


async def test_scoped_read_text_file_unchanged(tmp_path: Path) -> None:
    txt = tmp_path / "scoped.txt"
    txt.write_text("scoped content\nline2\n", encoding="utf-8")
    _set_ctx(_CAPABLE)

    tool = ScopedReadFileTool(allowed_dirs=[tmp_path])
    result = await tool.execute(path=str(txt))

    assert isinstance(result, ToolResult)
    assert result.error is None
    assert "scoped content" in result.message_content()


async def test_scoped_read_converges_with_readfiletool_on_image(
    tmp_path: Path,
) -> None:
    img = tmp_path / "same.png"
    img.write_bytes(_PNG_BYTES)
    _set_ctx(_CAPABLE)

    scoped = ScopedReadFileTool(allowed_dirs=[tmp_path])
    plain = ReadFileTool()

    scoped_result = await scoped.execute(path=str(img))
    plain_result = await plain.execute(path=str(img))

    assert isinstance(scoped_result, ToolResult)
    assert isinstance(plain_result, ToolResult)
    assert scoped_result.content_blocks is not None
    assert plain_result.content_blocks is not None
    assert scoped_result.content_blocks == plain_result.content_blocks


async def test_image_chain_tool_to_cache_to_enrichment(tmp_path: Path) -> None:
    """End-to-end: ReadFileTool → ToolManager.execute → content_blocks →
    TOOL_MEDIA_CACHE → enrich_inline_media → image in user message."""
    from modex_agent.agents.react.nodes.llm import enrich_inline_media
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.history import ListMessageHistory
    from modex_agent.core.message import ChatMessage
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.core.types import MessageRole
    from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

    img = tmp_path / "chain.png"
    img.write_bytes(_PNG_BYTES)

    capable = ModelInfo(
        model_name="vision",
        capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
    )

    mgr = InMemoryToolManager()
    mgr.register(ReadFileTool())
    exec_ctx = ToolExecutionContext(model_info=capable)
    result = await mgr.execute("read", {"path": str(img)}, ctx=exec_ctx)

    assert result.error is None
    assert len(result.content) == 2
    assert result.content_blocks is not None
    assert len(result.content_blocks) == 1
    assert result.content_blocks[0]["type"] == "image_url"

    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    from modex_agent.media.tool_media import ToolMediaEntry

    call_id = "test_call_id"
    state.custom[TurnCustomKey.TOOL_MEDIA_CACHE] = {
        call_id: ToolMediaEntry(
            call_id=call_id,
            tool_name="read",
            image_blocks=result.content_blocks or [],
        )
    }

    services = AgentRuntimeServices()
    services.model_info = capable
    runtime = AgentRuntime(services=services, state=state)

    history = ListMessageHistory()
    await history.append(ChatMessage(role=MessageRole.USER, content="What is in this image?"))

    agent_ctx = AgentContext(
        system_prompt="test",
        history=history,
        tool_manager=mgr,
        session=SessionInfo.from_str("test.session"),
        identity=runtime.state.identity,
        runtime=runtime,
    )

    messages = await agent_ctx.to_messages()
    enriched = enrich_inline_media(messages, agent_ctx)

    user_msg = enriched[-1]
    assert user_msg["role"] == str(MessageRole.USER)
    content = user_msg["content"]
    assert isinstance(content, list)
    types = [p.get("type") for p in content]
    assert "text" in types
    assert "image_url" in types
