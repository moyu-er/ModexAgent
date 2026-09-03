"""Cross-turn ``media://`` reference persistence e2e (multimodal lifecycle, todo 15).

Turn 1 drives the REAL history chain: ReadFileTool via ToolManager with a
ToolExecutionContext carrying the tmp media store → ToolResult parts →
build_tool_message → ScopedMessageHistory.append → SqliteMessageStore (tmp
SQLite). Turn 2 reloads from a NEW store over the same database
(``load_messages`` → ``ChatMessage.from_dicts``, no cache reuse), resolves
the persisted ``media://`` reference via ``inject_multimodal``, and feeds the
injected messages to the anthropic engine (real ``create_llm_provider``
factory on a ``httpx.MockTransport``) — the ``tool_result`` block carries the
image source. The ``sqlite_master`` snapshot is byte-identical before/after:
zero SQL/table changes across the whole lifecycle.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from modex_agent.agents.react.media_injection import inject_multimodal
from modex_agent.agents.react.message_builder import build_assistant_message, build_tool_message
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.constants import InterfaceFormat
from modex_agent.core.history import ListMessageHistory
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.media import StoredMediaKind
from modex_agent.core.message import (
    ChatMessage,
    ImageUrl,
    ImageUrlPart,
    MessageRole,
    TextPart,
    build_media_ref,
    parse_media_ref,
)
from modex_agent.core.scope import MemoryContext, RecordScope
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolExecutionContext
from modex_agent.core.types import ToolCall
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.media.store import LocalFileMediaStore
from modex_agent.memory.core.split_stores import MemoryStoreBundle
from modex_agent.memory.default_system import ScopedMessageHistory
from modex_agent.memory.layers.session import ScopedSessionMemoryManager
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.cursor_store import SqliteCursorStore
from modex_agent.persistence.adapters.kv_store import SqliteKVStore
from modex_agent.persistence.adapters.message_store import SqliteMessageStore
from modex_agent.providers.http.provider import HTTPStreamProvider
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.standard.file_tool import ReadFileTool

pytestmark = pytest.mark.integration

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()

_SESSION = "s.main"
_CALL_ID = "call-x"

_CAPABLE = ModelInfo(
    model_name="test-vision",
    capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
)

_MEMORY_CTX = MemoryContext(session_id=_SESSION, agent_id="main")


class _PoolScopedRecordScope(RecordScope):
    """Framework-test-local ``RecordScope`` with the pool dimension.

    Mirrors the bot's ``BotRecordScope`` canonical JSON so the SQLite
    adapters get pool-scoped scope_keys. ``BotRecordScope`` lives in the
    examples layer and cannot be imported by framework tests (ADR-0028
    layering) — same convention as
    ``tests/unit/persistence/adapters/conftest.py``.
    """

    pool: str | None = None


def _scope_for(context: MemoryContext) -> RecordScope:
    return _PoolScopedRecordScope(
        pool="default", session_id=context.session_id, agent_id=context.agent_id
    )


async def _schema_snapshot(connection: ConnectionManager) -> list[tuple[str, str]]:
    rows = await connection.query_all(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    return [(str(row[0]), str(row[1])) for row in rows]


def _anthropic_stream() -> bytes:
    """Minimal well-formed anthropic SSE stream (one text block, end_turn)."""
    frames: list[str] = []
    for event, payload in [
        (
            "message_start",
            {"message": {"role": "assistant", "usage": {"input_tokens": 1, "output_tokens": 1}}},
        ),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
        ("content_block_stop", {"index": 0}),
        (
            "message_delta",
            {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}},
        ),
        ("message_stop", {}),
    ]:
        frames.append(f"event: {event}\ndata: {json.dumps({**payload, 'type': event})}\n\n")
    return "".join(frames).encode()


def _inject_ctx(store: LocalFileMediaStore) -> AgentContext:
    """AgentContext carrying the tmp media store + IMAGE caps (turn-2 turn)."""
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str(_SESSION), turn_id="t2"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    services.model_info = _CAPABLE
    services.media_store = store
    runtime = AgentRuntime(services=services, state=state)
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(_SESSION),
        identity=state.identity,
        runtime=runtime,
    )


async def test_media_ref_survives_turn_boundary_into_anthropic_body(tmp_path: Path) -> None:
    connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await connection.open()
    try:
        schema_before = await _schema_snapshot(connection)

        # ── Turn 1: real read tool → ToolResult parts → history.append ──────────
        img = tmp_path / "chain.png"
        img.write_bytes(_PNG_BYTES)
        store = LocalFileMediaStore(tmp_path / "media")

        tool_manager = InMemoryToolManager()
        tool_manager.register(ReadFileTool())
        result = await tool_manager.execute(
            "read",
            {"path": str(img)},
            ctx=ToolExecutionContext(
                model_info=_CAPABLE, session_id=_SESSION, media_store=store
            ),
        )
        assert result.error is None
        image_parts = [p for p in result.content if isinstance(p, ImageUrlPart)]
        assert len(image_parts) == 1
        aid = parse_media_ref(image_parts[0].image_url.url)
        assert aid is not None
        # Persist-before-return: the READS snapshot in the tmp store backs the ref.
        assert store.read_bytes(_SESSION, aid, kind=StoredMediaKind.READS) == _PNG_BYTES
        expected_parts = [
            TextPart(text=f"[Image read: {img} (image/png)]"),
            ImageUrlPart(image_url=ImageUrl(url=build_media_ref(aid))),
        ]
        assert result.content == expected_parts

        async def storage_factory(context: MemoryContext) -> MemoryStoreBundle:
            scope = _scope_for(context)
            return MemoryStoreBundle(
                messages=SqliteMessageStore(connection, scope, ttl_seconds=0.0),
                kv=SqliteKVStore(connection, scope),
                cursors=SqliteCursorStore(connection, scope),
            )

        history = ScopedMessageHistory(
            manager=ScopedSessionMemoryManager(storage_factory),
            context=_MEMORY_CTX,
        )
        assistant = build_assistant_message(
            None,
            [ToolCall(call_id=_CALL_ID, tool_name="read", arguments={"path": str(img)})],
        )
        await history.append(assistant)
        await history.append(build_tool_message(result, _CALL_ID))

        # ── Turn 2: NEW load from the same store — no cache reuse ────────────────
        turn2_store = SqliteMessageStore(
            connection, _scope_for(_MEMORY_CTX), ttl_seconds=0.0
        )
        loaded = ChatMessage.from_dicts(await turn2_store.load_messages())
        assert [m.role for m in loaded] == [MessageRole.ASSISTANT, MessageRole.TOOL]
        tool_msg = loaded[1]
        assert tool_msg.tool_call_id == _CALL_ID
        assert tool_msg.name == "read"
        # The parts round-trip complete with the media:// reference intact.
        assert tool_msg.content == expected_parts

        injected = inject_multimodal(loaded, _inject_ctx(store))
        parts = injected[1].content
        assert isinstance(parts, list)
        image = parts[1]
        assert isinstance(image, ImageUrlPart)
        assert image.image_url.url == f"data:image/png;base64,{_PNG_B64}"
        # Copy-on-write: the persisted history keeps its reference, never base64.
        persisted = loaded[1].content
        assert isinstance(persisted, list)
        assert persisted[1] == ImageUrlPart(image_url=ImageUrl(url=build_media_ref(aid)))

        # ── Anthropic engine body: the tool_result block carries the image ──────
        requests: list[httpx.Request] = []

        async def recording(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200, content=_anthropic_stream(), headers={"content-type": "text/event-stream"}
            )

        provider = create_llm_provider(
            LLMConfig(
                model="test-model",
                api_key="test-key",
                base_url="https://api.example.com/v1",
                interface_format=InterfaceFormat.ANTHROPIC,
            )
        )
        assert isinstance(provider, HTTPStreamProvider)
        retired = provider._client
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
        try:
            async for _ in provider.stream(LLMRequest(model="test-model", messages=injected)):
                pass
        finally:
            await provider.aclose()
            await retired.aclose()

        assert len(requests) == 1
        body = json.loads(requests[0].content)
        tool_result = body["messages"][-1]["content"][0]
        assert tool_result["type"] == "tool_result"
        assert tool_result["tool_use_id"] == _CALL_ID
        assert tool_result["content"][0] == {
            "type": "text",
            "text": f"[Image read: {img} (image/png)]",
        }
        assert tool_result["content"][1] == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
        }

        # ── Zero-SQL: the whole lifecycle added no tables or columns ────────────
        assert await _schema_snapshot(connection) == schema_before
    finally:
        await connection.close()


async def test_hand_built_string_image_url_rejected_by_from_dict() -> None:
    """Pitfall negative: ``image_url`` must be a nested ``{"url": ...}`` object.

    A hand-built dict that puts the URL string directly under ``image_url``
    fails ``ChatMessage.from_dict`` validation — the nested-model trap when
    constructing parts dicts by hand instead of going through the tool chain.
    """
    bad = {
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": "media://aid-1"},
        ],
    }
    with pytest.raises(ValidationError):
        ChatMessage.from_dict(bad)
