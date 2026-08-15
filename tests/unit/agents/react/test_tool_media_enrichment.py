"""Tests for tool-produced image injection via the media strategy.

Path B (SyntheticUserMessageStrategy): tool-produced images are injected as a
synthetic ``role: "user"`` message appended AFTER tool-result messages, with
per-call attribution (tool name + call ID).  This is distinct from user
attachments, which are injected into the existing user message.

Both sources share the same IMAGE capability gate and the same image_url wire
format; they differ only in injection position and attribution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modex_agent.agents.react.nodes.llm import enrich_inline_media
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.session_id import SessionInfo
from modex_agent.media.tool_media import ToolMediaEntry
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

_CAPABLE = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))
_TEXT_ONLY = ModelCapabilities(modalities=frozenset({Modality.TEXT}))

_TOOL_BLOCK: dict[str, Any] = {
    "type": "image_url",
    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
}


def _make_runtime(capabilities: ModelCapabilities | None) -> AgentRuntime:
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    services.model_info = ModelInfo(model_name="test", capabilities=capabilities) if capabilities else None
    return AgentRuntime(services=services, state=state)


def _make_ctx(runtime: AgentRuntime) -> AgentContext:
    return AgentContext(
        system_prompt="sys",
        history=None,  # type: ignore[arg-type]
        tool_manager=None,  # type: ignore[arg-type]
        session=SessionInfo.from_str("test.agent"),
        identity=runtime.state.identity,
        runtime=runtime,
    )


def _make_entry(call_id: str = "call-1", tool_name: str = "read") -> ToolMediaEntry:
    return ToolMediaEntry(
        call_id=call_id,
        tool_name=tool_name,
        image_blocks=[_TOOL_BLOCK],
    )


# -- Path B: synthetic user message after tool results ----------------------


def test_tool_media_appended_as_synthetic_user_message() -> None:
    """Tool images go into a NEW user message after tool results, not into the existing user message."""
    runtime = _make_runtime(_CAPABLE)
    runtime.state.custom[TurnCustomKey.TOOL_MEDIA_CACHE] = {"call-1": _make_entry()}
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": "describe the image"},
        {"role": "assistant", "tool_calls": [{"id": "call-1", "name": "read"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "[Image read: cat.png (image/png)]"},
    ]
    out = enrich_inline_media(messages, ctx)

    # Original messages unchanged + 1 synthetic user message appended.
    assert len(out) == 4
    assert out[0]["content"] == "describe the image"
    assert out[3]["role"] == "user"
    content = out[3]["content"]
    assert isinstance(content, list)
    assert any(p.get("type") == "image_url" for p in content)


def test_synthetic_message_carries_attribution() -> None:
    """The synthetic user message text must name the tool and call ID."""
    runtime = _make_runtime(_CAPABLE)
    runtime.state.custom[TurnCustomKey.TOOL_MEDIA_CACHE] = {"call-1": _make_entry("call-1", "read")}
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "tool", "tool_call_id": "call-1", "content": "[Image read: cat.png]"},
    ]
    out = enrich_inline_media(messages, ctx)

    synthetic = out[-1]
    assert synthetic["role"] == "user"
    content = synthetic["content"]
    assert isinstance(content, list)
    text_parts = [p for p in content if p.get("type") == "text"]
    assert len(text_parts) == 1
    assert "read" in text_parts[0]["text"]
    assert "call-1" in text_parts[0]["text"]


def test_tool_media_skipped_for_text_only_model() -> None:
    """Text-only model → no injection, messages returned unchanged."""
    runtime = _make_runtime(_TEXT_ONLY)
    runtime.state.custom[TurnCustomKey.TOOL_MEDIA_CACHE] = {"call-1": _make_entry()}
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": "describe the image"},
    ]
    out = enrich_inline_media(messages, ctx)

    assert out == messages


def test_tool_media_skipped_when_cache_empty() -> None:
    """No tool media in cache → no synthetic message."""
    runtime = _make_runtime(_CAPABLE)
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": "hello"},
    ]
    out = enrich_inline_media(messages, ctx)

    assert out == messages


def test_multiple_tool_calls_produce_grouped_attribution() -> None:
    """Multiple tool calls → one synthetic message with per-call attribution blocks."""
    runtime = _make_runtime(_CAPABLE)
    runtime.state.custom[TurnCustomKey.TOOL_MEDIA_CACHE] = {
        "call-1": _make_entry("call-1", "read"),
        "call-2": _make_entry("call-2", "read"),
    }
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "tool", "tool_call_id": "call-1", "content": "[Image read: a.png]"},
        {"role": "tool", "tool_call_id": "call-2", "content": "[Image read: b.png]"},
    ]
    out = enrich_inline_media(messages, ctx)

    # One synthetic user message at the end.
    assert len(out) == 3
    synthetic = out[-1]
    assert synthetic["role"] == "user"
    content = synthetic["content"]
    assert isinstance(content, list)
    text_parts = [p for p in content if p.get("type") == "text"]
    image_parts = [p for p in content if p.get("type") == "image_url"]
    assert len(text_parts) == 2
    assert len(image_parts) == 2
    assert "call-1" in text_parts[0]["text"]
    assert "call-2" in text_parts[1]["text"]


# -- Convergence: user attachments still inject into existing user message --


_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_user_attachments_still_inject_into_user_message(tmp_path: Path) -> None:
    """User attachments inject into the last user message content (unchanged behavior)."""
    from modex_agent.media.models import Attachment, AttachmentLocator, Kind

    img_path = tmp_path / "cat.png"
    img_path.write_bytes(_PNG_BYTES)

    runtime = _make_runtime(_CAPABLE)
    att = Attachment(
        id="att-1",
        kind=Kind.IMAGE,
        name="cat.png",
        mime="image/png",
        size=len(_PNG_BYTES),
        path=str(img_path),
        locator=AttachmentLocator.WORKSPACE,
    )
    runtime.state.custom[TurnCustomKey.INLINE_ATTACHMENTS] = [att]
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": "look"},
    ]
    out = enrich_inline_media(messages, ctx)

    # User attachment injects INTO the existing user message (not appended).
    assert len(out) == 1
    content = out[0]["content"]
    assert isinstance(content, list)
    assert any(p.get("type") == "image_url" for p in content)


def test_user_attachment_and_tool_media_use_different_paths(tmp_path: Path) -> None:
    """User attachment → existing user message; tool media → synthetic appended message."""
    from modex_agent.media.models import Attachment, AttachmentLocator, Kind

    runtime = _make_runtime(_CAPABLE)
    runtime.state.custom[TurnCustomKey.TOOL_MEDIA_CACHE] = {"call-1": _make_entry()}

    img_path = tmp_path / "cat.png"
    img_path.write_bytes(_PNG_BYTES)
    att = Attachment(
        id="att-1",
        kind=Kind.IMAGE,
        name="cat.png",
        mime="image/png",
        size=len(_PNG_BYTES),
        path=str(img_path),
        locator=AttachmentLocator.WORKSPACE,
    )
    runtime.state.custom[TurnCustomKey.INLINE_ATTACHMENTS] = [att]
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": "look at both"},
        {"role": "assistant", "tool_calls": [{"id": "call-1", "name": "read"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "[Image read: dog.png]"},
    ]
    out = enrich_inline_media(messages, ctx)

    # User message (index 0) gets the attachment image injected into its content.
    user_msg = out[0]
    assert isinstance(user_msg["content"], list)
    user_image_parts = [p for p in user_msg["content"] if p.get("type") == "image_url"]
    assert len(user_image_parts) >= 1

    # Synthetic user message appended at the end for tool media.
    synthetic = out[-1]
    assert synthetic["role"] == "user"
    assert isinstance(synthetic["content"], list)
    tool_image_parts = [p for p in synthetic["content"] if p.get("type") == "image_url"]
    assert len(tool_image_parts) == 1
