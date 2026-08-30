"""Tests for multimodal image reading in ReadFileTool.

Covers: image detection via magic bytes, capability gate (IMAGE vs text-only),
READS-subtree snapshot persistence (persist-before-return), media:// reference
parts, no-store degradation, and text-file passthrough.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest

from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.message import ChatMessage, ImageUrlPart, TextPart
from modex_agent.core.tool_manager import ToolExecutionContext, ToolResult
from modex_agent.media.store import LocalFileMediaStore, StoredMediaKind
from modex_agent.memory.tools.scoped_read import ScopedReadFileTool
from modex_agent.tools.standard.file_tool import ReadFileTool

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_SESSION = "s.main"

_CAPABLE = ModelInfo(
    model_name="test-vision",
    capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
)
_TEXT_ONLY = ModelInfo(
    model_name="test-text",
    capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT})),
)


def _set_ctx(
    info: ModelInfo | None,
    store: LocalFileMediaStore | None = None,
) -> None:
    from modex_agent.core.tool_manager import _tool_execution_ctx

    ctx = (
        ToolExecutionContext(
            model_info=info,
            session_id=_SESSION,
            media_store=store,
        )
        if info is not None or store is not None
        else None
    )
    _tool_execution_ctx.set(ctx)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    from modex_agent.core.tool_manager import _tool_execution_ctx

    _tool_execution_ctx.set(None)
    yield
    _tool_execution_ctx.set(None)


def _image_parts(result: ToolResult) -> list[ImageUrlPart]:
    return [p for p in result.content if isinstance(p, ImageUrlPart)]


def _stored_aid(result: ToolResult) -> str:
    parts = _image_parts(result)
    assert len(parts) == 1
    url = parts[0].image_url.url
    assert url.startswith("media://")
    return url[len("media://") :]


async def test_read_image_persists_snapshot_and_returns_ref(tmp_path: Path) -> None:
    img = tmp_path / "test.png"
    img.write_bytes(_PNG_BYTES)
    store = LocalFileMediaStore(tmp_path / "media")
    _set_ctx(_CAPABLE, store)

    tool = ReadFileTool()
    result = await tool.execute(path=str(img))

    assert isinstance(result, ToolResult)
    assert result.error is None
    aid = _stored_aid(result)
    # Persist-before-return: the compressed snapshot exists in the READS
    # subtree and round-trips the source bytes (pass-through: within budget).
    stored = store.read_bytes(_SESSION, aid, kind=StoredMediaKind.READS)
    assert stored == _PNG_BYTES
    # The text hint rides as a TextPart alongside the reference.
    text_parts = [p for p in result.content if isinstance(p, TextPart)]
    assert text_parts[0].text == f"[Image read: {img} (image/png)]"
    # Never a data URL part — reference-only persistence.
    assert "data:" not in str(result.content)


async def test_read_image_text_only_model_returns_degradation_text(tmp_path: Path) -> None:
    img = tmp_path / "test.png"
    img.write_bytes(_PNG_BYTES)
    _set_ctx(_TEXT_ONLY, LocalFileMediaStore(tmp_path / "media"))

    tool = ReadFileTool()
    result = await tool.execute(path=str(img))

    assert isinstance(result, ToolResult)
    assert _image_parts(result) == []
    assert "Visual content not available" in result.message_content()


async def test_read_image_no_ctx_returns_degradation_text(tmp_path: Path) -> None:
    img = tmp_path / "test.png"
    img.write_bytes(_PNG_BYTES)

    tool = ReadFileTool()
    result = await tool.execute(path=str(img))

    assert isinstance(result, ToolResult)
    assert _image_parts(result) == []
    assert "Visual content not available" in result.message_content()


async def test_read_image_no_store_returns_degradation_text(tmp_path: Path) -> None:
    img = tmp_path / "test.png"
    img.write_bytes(_PNG_BYTES)
    _set_ctx(_CAPABLE, None)

    tool = ReadFileTool()
    result = await tool.execute(path=str(img))

    assert isinstance(result, ToolResult)
    assert _image_parts(result) == []
    assert "Visual content not available" in result.message_content()
    assert "no media store wired" in result.message_content()


async def test_read_image_corrupt_bytes_return_degradation_text(tmp_path: Path) -> None:
    img = tmp_path / "corrupt.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00garbage-not-an-image")
    _set_ctx(_CAPABLE, LocalFileMediaStore(tmp_path / "media"))

    tool = ReadFileTool()
    result = await tool.execute(path=str(img))

    assert isinstance(result, ToolResult)
    assert result.error is None
    assert _image_parts(result) == []
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

    assert isinstance(result, ToolResult)
    assert result.error is not None
    assert "not found" in result.error.lower() or "not found" in result.error


async def test_scoped_read_image_persists_snapshot_and_returns_ref(tmp_path: Path) -> None:
    img = tmp_path / "scoped.png"
    img.write_bytes(_PNG_BYTES)
    store = LocalFileMediaStore(tmp_path / "media")
    _set_ctx(_CAPABLE, store)

    tool = ScopedReadFileTool(allowed_dirs=[tmp_path])
    result = await tool.execute(path=str(img))

    assert isinstance(result, ToolResult)
    assert result.error is None
    aid = _stored_aid(result)
    assert store.read_bytes(_SESSION, aid, kind=StoredMediaKind.READS) == _PNG_BYTES


async def test_scoped_read_image_text_only_returns_degradation(tmp_path: Path) -> None:
    img = tmp_path / "scoped.png"
    img.write_bytes(_PNG_BYTES)
    _set_ctx(_TEXT_ONLY, LocalFileMediaStore(tmp_path / "media"))

    tool = ScopedReadFileTool(allowed_dirs=[tmp_path])
    result = await tool.execute(path=str(img))

    assert isinstance(result, ToolResult)
    assert _image_parts(result) == []
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
    scoped_store = LocalFileMediaStore(tmp_path / "media-scoped")
    plain_store = LocalFileMediaStore(tmp_path / "media-plain")
    _set_ctx(_CAPABLE, scoped_store)

    scoped = ScopedReadFileTool(allowed_dirs=[tmp_path])
    scoped_result = await scoped.execute(path=str(img))
    _set_ctx(_CAPABLE, plain_store)
    plain = ReadFileTool()
    plain_result = await plain.execute(path=str(img))

    assert isinstance(scoped_result, ToolResult)
    assert isinstance(plain_result, ToolResult)
    # Both persist their own snapshot and reference it — the image parts are
    # structurally identical (media:// refs), each backed by its own READS file.
    scoped_aid = _stored_aid(scoped_result)
    plain_aid = _stored_aid(plain_result)
    assert scoped_result.content[0] == plain_result.content[0]
    assert scoped_store.read_bytes(_SESSION, scoped_aid, kind=StoredMediaKind.READS) == _PNG_BYTES
    assert plain_store.read_bytes(_SESSION, plain_aid, kind=StoredMediaKind.READS) == _PNG_BYTES


async def test_image_chain_tool_to_message_to_injection(tmp_path: Path) -> None:
    """End-to-end: ReadFileTool → ToolManager.execute → build_tool_message →
    inject_multimodal → data URL on the LLM-bound message."""
    from modex_agent.agents.react.media_injection import inject_multimodal
    from modex_agent.agents.react.message_builder import build_tool_message
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.history import ListMessageHistory
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.runtime.enums import AgentKind, TurnPhase
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

    img = tmp_path / "chain.png"
    img.write_bytes(_PNG_BYTES)

    store = LocalFileMediaStore(tmp_path / "media")
    mgr = InMemoryToolManager()
    mgr.register(ReadFileTool())
    exec_ctx = ToolExecutionContext(
        model_info=_CAPABLE, session_id=_SESSION, media_store=store
    )
    result = await mgr.execute("read", {"path": str(img)}, ctx=exec_ctx)

    assert result.error is None
    _stored_aid(result)  # asserts exactly one media:// reference part

    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str(_SESSION), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    services.model_info = _CAPABLE
    services.media_store = store
    runtime = AgentRuntime(services=services, state=state)

    history = ListMessageHistory()
    await history.append(build_tool_message(result, "test_call_id"))

    agent_ctx = AgentContext(
        system_prompt="test",
        history=history,
        tool_manager=mgr,
        session=SessionInfo.from_str(_SESSION),
        identity=runtime.state.identity,
        runtime=runtime,
    )

    messages = [ChatMessage.coerce(m) for m in await agent_ctx.to_messages()]
    injected = inject_multimodal(messages, agent_ctx)

    tool_msg = injected[-1]
    content = tool_msg.content
    assert isinstance(content, list)
    types = [type(p) for p in content]
    assert TextPart in types
    assert ImageUrlPart in types
    image = next(p for p in content if isinstance(p, ImageUrlPart))
    assert image.image_url.url.startswith("data:image/png;base64,")
    payload = image.image_url.url.split(",", 1)[1]
    assert base64.b64decode(payload) == _PNG_BYTES
