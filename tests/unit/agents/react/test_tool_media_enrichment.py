"""Tests for tool-produced image injection via enrich_inline_media.

Verifies that images produced by tools (cached in TOOL_MEDIA_CACHE) are
injected into the LLM call's user message, alongside user attachments,
gated on IMAGE capability, and transient (not persisted).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modex_agent.agents.react.nodes.llm import enrich_inline_media
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.capabilities import Modality, ModelCapabilities
from modex_agent.core.session_id import SessionInfo
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
    services.model_capabilities = capabilities
    runtime = AgentRuntime(services=services, state=state)
    runtime.graph_runtime = ReactGraphRuntime()
    return runtime


def _make_ctx(runtime: AgentRuntime) -> AgentContext:
    return AgentContext(
        system_prompt="sys",
        history=None,  # type: ignore[arg-type]
        tool_manager=None,  # type: ignore[arg-type]
        session=SessionInfo.from_str("test.agent"),
        identity=runtime.state.identity,
        runtime=runtime,
    )


def test_tool_media_injected_into_user_message() -> None:
    runtime = _make_runtime(_CAPABLE)
    runtime.state.custom[TurnCustomKey.TOOL_MEDIA_CACHE] = {"call-1": [_TOOL_BLOCK]}
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": "describe the image"},
    ]
    out = enrich_inline_media(messages, ctx)

    assert len(out) == 1
    content = out[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe the image"}
    assert any(p.get("type") == "image_url" for p in content)


def test_tool_media_skipped_for_text_only_model() -> None:
    runtime = _make_runtime(_TEXT_ONLY)
    runtime.state.custom[TurnCustomKey.TOOL_MEDIA_CACHE] = {"call-1": [_TOOL_BLOCK]}
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": "describe the image"},
    ]
    out = enrich_inline_media(messages, ctx)

    assert out == messages


def test_tool_media_and_user_attachments_both_injected() -> None:
    from modex_agent.media.models import Attachment, AttachmentLocator, Kind

    runtime = _make_runtime(_CAPABLE)
    runtime.state.custom[TurnCustomKey.TOOL_MEDIA_CACHE] = {"call-1": [_TOOL_BLOCK]}

    img_path = Path("/fake/cat.png")
    att = Attachment(
        id="att-1",
        kind=Kind.IMAGE,
        name="cat.png",
        mime="image/png",
        size=100,
        path=str(img_path),
        locator=AttachmentLocator.WORKSPACE,
    )
    runtime.state.custom[TurnCustomKey.INLINE_ATTACHMENTS] = [att]
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": "look"},
    ]
    out = enrich_inline_media(messages, ctx)

    content = out[0]["content"]
    assert isinstance(content, list)
    image_parts = [p for p in content if p.get("type") == "image_url"]
    assert len(image_parts) >= 1


def test_tool_media_no_user_message_synthesizes_one() -> None:
    runtime = _make_runtime(_CAPABLE)
    runtime.state.custom[TurnCustomKey.TOOL_MEDIA_CACHE] = {"call-1": [_TOOL_BLOCK]}
    ctx = _make_ctx(runtime)

    messages: list[dict[str, object]] = [
        {"role": "assistant", "content": "I read an image"},
        {"role": "tool", "tool_call_id": "call-1", "content": "[Image read: cat.png]"},
    ]
    out = enrich_inline_media(messages, ctx)

    assert len(out) == 3
    assert out[-1]["role"] == "user"
    assert isinstance(out[-1]["content"], list)
