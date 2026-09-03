"""Lifecycle tests for persisted ``media://`` parts at the LLM boundary.

Replaces the former inline-enrichment divergence tests: the user message and
tool results now PERSIST ``media://`` reference parts (never base64);
``LLMNode._build_messages`` resolves them via ``inject_multimodal`` AFTER
governance — the persisted history keeps references, only the LLM-bound copy
carries data URLs.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from modex_agent.agents.react.nodes.llm import LLMNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.governance import ContextGovernance
from modex_agent.core.media import StoredMediaKind
from modex_agent.core.message import (
    ChatMessage,
    ImageUrl,
    ImageUrlPart,
    TextPart,
    build_media_ref,
)
from modex_agent.core.scope import MemoryContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.media.store import LocalFileMediaStore
from modex_agent.memory.default_system import ScopedMessageHistory
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_graph import create_null_coordinator

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_CAPABLE = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))
_TEXT_ONLY = ModelCapabilities(modalities=frozenset({Modality.TEXT}))

_SESSION = SessionInfo.from_str("test.agent")
_SESSION_ID = str(_SESSION)


def _make_runtime(
    capabilities: ModelCapabilities | None,
    store: LocalFileMediaStore | None,
) -> AgentRuntime:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session=_SESSION, turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    services.model_info = (
        ModelInfo(model_name="test", capabilities=capabilities) if capabilities else None
    )
    services.media_store = store
    return AgentRuntime(services=services, state=state)


def _scoped_history(tmp_path: Path) -> ScopedMessageHistory:
    """A real ScopedMessageHistory backed by a file store.

    Using the real history (not a mock) is load-bearing: the test reads back
    what was *persisted*, proving resolution never mutated storage — only the
    LLM-bound copy changed.
    """
    registry = DefaultMemoryStoreRegistry(tmp_path)
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="s1", user_id="u1")
    return ScopedMessageHistory(manager=layer_set.session, context=ctx)


def _make_ctx(
    runtime: AgentRuntime,
    history: ScopedMessageHistory,
) -> AgentContext:
    return AgentContext(
        system_prompt="sys",
        history=history,
        tool_manager=None,  # type: ignore[arg-type]
        session=_SESSION,
        identity=runtime.state.identity,
        runtime=runtime,
    )


async def _build(
    ctx: AgentContext,
    graph_runtime: ReactGraphRuntime | None = None,
) -> list[ChatMessage]:
    node = LLMNode.__new__(LLMNode)
    return await node._build_messages(
        ctx,
        graph_runtime or ReactGraphRuntime(),
        create_null_coordinator(),
    )


class TestLLMBoundaryResolution:
    async def test_history_persists_reference_not_base64(self, tmp_path: Path) -> None:
        """The persisted user content keeps the media:// ref; the LLM-bound
        copy resolves it to a data URL."""
        store = LocalFileMediaStore(tmp_path / "media")
        store.save(_SESSION_ID, "aid-1", _PNG_BYTES, kind=StoredMediaKind.READS)

        history = _scoped_history(tmp_path)
        await history.append(
            {
                "role": "user",
                "content": [
                    TextPart(text="here is a cat"),
                    ImageUrlPart(image_url=ImageUrl(url=build_media_ref("aid-1"))),
                ],
            }
        )

        ctx = _make_ctx(_make_runtime(_CAPABLE, store), history)
        messages = await _build(ctx)

        user_msg = next(m for m in messages if m.role == "user")
        content = user_msg.content
        assert isinstance(content, list)
        image = next(p for p in content if isinstance(p, ImageUrlPart))
        assert image.image_url.url.startswith("data:image/png;base64,")
        payload = image.image_url.url.split(",", 1)[1]
        assert base64.b64decode(payload) == _PNG_BYTES

        persisted = await history.to_list()
        persisted_user = next(m for m in persisted if m.role == "user")
        persisted_content = persisted_user.content
        assert isinstance(persisted_content, list)
        persisted_image = next(p for p in persisted_content if isinstance(p, ImageUrlPart))
        assert persisted_image.image_url.url == build_media_ref("aid-1")
        assert "base64" not in str(persisted_user.to_dict())

    async def test_text_only_model_keeps_text_drops_image(self, tmp_path: Path) -> None:
        store = LocalFileMediaStore(tmp_path / "media")
        store.save(_SESSION_ID, "aid-1", _PNG_BYTES, kind=StoredMediaKind.READS)

        history = _scoped_history(tmp_path)
        await history.append(
            {
                "role": "user",
                "content": [
                    TextPart(text="here is a cat"),
                    ImageUrlPart(image_url=ImageUrl(url=build_media_ref("aid-1"))),
                ],
            }
        )

        ctx = _make_ctx(_make_runtime(_TEXT_ONLY, store), history)
        messages = await _build(ctx)

        user_msg = next(m for m in messages if m.role == "user")
        assert user_msg.content == [TextPart(text="here is a cat")]

    async def test_no_store_degrades_to_placeholder(self, tmp_path: Path) -> None:
        history = _scoped_history(tmp_path)
        await history.append(
            {
                "role": "user",
                "content": [
                    TextPart(text="here is a cat"),
                    ImageUrlPart(image_url=ImageUrl(url=build_media_ref("aid-1"))),
                ],
            }
        )

        ctx = _make_ctx(_make_runtime(_CAPABLE, None), history)
        messages = await _build(ctx)

        user_msg = next(m for m in messages if m.role == "user")
        assert user_msg.content == [
            TextPart(text="here is a cat"),
            TextPart(text="[media unavailable: aid-1]"),
        ]

    async def test_plain_history_passes_through(self, tmp_path: Path) -> None:
        history = _scoped_history(tmp_path)
        await history.append({"role": "user", "content": "plain question"})

        ctx = _make_ctx(_make_runtime(_CAPABLE, None), history)
        messages = await _build(ctx)

        user_msg = next(m for m in messages if m.role == "user")
        assert user_msg.content == "plain question"

    async def test_governance_sees_references_before_resolution(
        self, tmp_path: Path
    ) -> None:
        """Governance runs on the pre-injection copy: it observes the tiny
        media:// references (never base64); only the final output resolves."""
        store = LocalFileMediaStore(tmp_path / "media")
        store.save(_SESSION_ID, "aid-1", _PNG_BYTES, kind=StoredMediaKind.READS)

        history = _scoped_history(tmp_path)
        await history.append(
            {
                "role": "user",
                "content": [
                    TextPart(text="look"),
                    ImageUrlPart(image_url=ImageUrl(url=build_media_ref("aid-1"))),
                ],
            }
        )

        seen: list[Any] = []

        class _RecordingGovernance(ContextGovernance):
            async def apply(
                self, messages: list[dict[str, Any]], ctx: AgentContext
            ) -> list[dict[str, Any]]:
                seen.extend(messages)
                return messages

        graph_runtime = ReactGraphRuntime(governance=_RecordingGovernance())  # type: ignore[arg-type]
        ctx = _make_ctx(_make_runtime(_CAPABLE, store), history)
        out = await _build(ctx, graph_runtime)

        gov_user = [m for m in seen if m.get("role") == "user"][-1]
        assert build_media_ref("aid-1") in str(gov_user["content"])
        assert "base64" not in str(gov_user)

        out_user = next(m for m in out if m.role == "user")
        content = out_user.content
        assert isinstance(content, list)
        image = next(p for p in content if isinstance(p, ImageUrlPart))
        assert image.image_url.url.startswith("data:image/png;base64,")

    async def test_tool_result_reference_resolves_on_llm_copy(
        self, tmp_path: Path
    ) -> None:
        """A TOOL message carrying a media:// part (the read tool's output)
        resolves on the LLM-bound copy; the persisted history keeps the ref."""
        from modex_agent.core.types import ToolCall

        store = LocalFileMediaStore(tmp_path / "media")
        store.save(_SESSION_ID, "aid-9", _PNG_BYTES, kind=StoredMediaKind.READS)

        history = _scoped_history(tmp_path)
        await history.append(
            ChatMessage(
                role="user",
                content="what is in the image?",
            )
        )
        await history.append(
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(call_id="c1", tool_name="read", arguments={})],
            )
        )
        await history.append(
            ChatMessage(
                role="tool",
                tool_call_id="c1",
                name="read",
                content=[
                    TextPart(text="[Image read: cat.png (image/png)]"),
                    ImageUrlPart(image_url=ImageUrl(url=build_media_ref("aid-9"))),
                ],
            )
        )

        ctx = _make_ctx(_make_runtime(_CAPABLE, store), history)
        messages = await _build(ctx)

        tool_msg = next(m for m in messages if m.role == "tool")
        content = tool_msg.content
        assert isinstance(content, list)
        image = next(p for p in content if isinstance(p, ImageUrlPart))
        assert image.image_url.url.startswith("data:image/png;base64,")

        persisted = await history.to_list()
        persisted_tool = next(m for m in persisted if m.role == "tool")
        persisted_content = persisted_tool.content
        assert isinstance(persisted_content, list)
        persisted_image = next(p for p in persisted_content if isinstance(p, ImageUrlPart))
        assert persisted_image.image_url.url == build_media_ref("aid-9")
