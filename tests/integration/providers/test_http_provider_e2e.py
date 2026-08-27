"""End-to-end integration: HTTPStreamProvider across all three protocol formats.

Zero real network — every request lands on an ``httpx.MockTransport``. The
chain under test is the full framework path::

    LLMConfig → create_llm_provider → HTTPStreamProvider → protocol engine
    → wire body → SSE stream → LLMStreamEvents → EventAssembler → LLMResponse

The transport is injected by swapping the provider's ``httpx.AsyncClient``
after the factory build: ``create_llm_provider`` has no transport channel,
and the swap replaces exactly the socket layer MockTransport exists to fake —
everything above it (engine routing, body build, URL resolution, header merge,
idle-free SSE parsing) runs the real path.

Scenarios (T25):
1. full stream (text + reasoning + tool call + usage + finish) × 3 formats;
2. anthropic thinking+signature two-round replay (round-2 request body
   carries the thinking block rebuilt from the round-1 response);
3. endpoint_url verbatim override (no per-format join suffix);
4. top_p passthrough into the request body × 3 formats;
5. ReactLlmClient + fake ctx + emitter call sequence over the full transport;
6. tool-media placement × 3 formats (multimodal lifecycle): a READS snapshot
   in a tmp store → paired [assistant(tool_calls), TOOL(media://)] history →
   inject_multimodal resolves the reference → anthropic embeds the image
   inside the tool_result block, compat flushes one attributed follow-up
   user message (image_url — protocol limit), responses embeds it natively
   in the function_call_output (input_image).

SIZE_OK: pure LOC exceeds the 250 ceiling because the canned SSE streams are
faithful wire-format data tables (three formats × full event sequences) —
the same exception class as the per-engine format tests (T14-T16).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, assert_never

import httpx
import pytest

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.media_injection import inject_multimodal
from modex_agent.agents.react.message_builder import build_assistant_message
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.constants import FinishReason, InterfaceFormat
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.message import (
    ChatMessage,
    ImageUrl,
    ImageUrlPart,
    MessageRole,
    TextPart,
    build_media_ref,
)
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import TokenUsage, ToolCall
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.media.store import LocalFileMediaStore, StoredMediaKind
from modex_agent.memory.history import ListMessageHistory
from modex_agent.providers.http.formats.anthropic import AnthropicProtocol
from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
from modex_agent.providers.http.formats.openai_responses import OpenAIResponsesProtocol
from modex_agent.providers.http.provider import HTTPStreamProvider
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

pytestmark = pytest.mark.integration

_SSE_HEADERS = {"content-type": "text/event-stream"}

type ProviderFactory = Callable[
    [LLMConfig, bytes], Awaitable[tuple[HTTPStreamProvider, list[httpx.Request]]]
]


def _chat_sse(*payloads: dict[str, Any] | str) -> bytes:
    """Encode data-only chat-completions SSE frames."""
    parts = []
    for payload in payloads:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        parts.append(f"data: {data}\n\n")
    return "".join(parts).encode()


def _event_sse(*pairs: tuple[str, dict[str, Any]]) -> bytes:
    """Encode event+data SSE frames (anthropic / responses), type mirrored into data."""
    parts = []
    for event, payload in pairs:
        data = {**payload, "type": event}
        parts.append(f"event: {event}\ndata: {json.dumps(data)}\n\n")
    return "".join(parts).encode()


@dataclass(frozen=True)
class _FormatCase:
    """Per-format bundle: factory routing, canned full stream, replay expectations."""

    fmt: InterfaceFormat
    engine: type
    stream: bytes
    call_id: str
    signature: str | None
    item_id: str | None


def _compat_full_stream() -> bytes:
    return _chat_sse(
        {"choices": [{"delta": {"reasoning_content": "Let me think"}}]},
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"delta": {"content": " world"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"city": "Beijing"}'}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        "[DONE]",
    )


def _responses_full_stream() -> bytes:
    return _event_sse(
        ("response.created", {"response": {"id": "resp_1"}}),
        (
            "response.output_item.added",
            {"output_index": 0, "item": {"type": "reasoning", "id": "rs_1", "summary": []}},
        ),
        (
            "response.reasoning_summary_text.delta",
            {"item_id": "rs_1", "summary_index": 0, "delta": "Let me think"},
        ),
        ("response.output_item.done", {"item": {"type": "reasoning", "id": "rs_1", "summary": []}}),
        (
            "response.output_item.added",
            {
                "output_index": 1,
                "item": {"type": "message", "role": "assistant", "id": "msg_1", "content": []},
            },
        ),
        ("response.content_part.added", {"item_id": "msg_1"}),
        ("response.output_text.delta", {"item_id": "msg_1", "delta": "Hello world"}),
        (
            "response.output_item.done",
            {
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": "msg_1",
                    "content": [{"type": "output_text", "text": "Hello world"}],
                }
            },
        ),
        ("response.content_part.done", {"item_id": "msg_1"}),
        (
            "response.output_item.added",
            {
                "output_index": 2,
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_a",
                    "name": "get_weather",
                    "arguments": "",
                },
            },
        ),
        (
            "response.function_call_arguments.delta",
            {"item_id": "fc_1", "delta": '{"city": "Beijing"}'},
        ),
        (
            "response.output_item.done",
            {
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_a",
                    "name": "get_weather",
                    "arguments": '{"city": "Beijing"}',
                }
            },
        ),
        (
            "response.completed",
            {"response": {"id": "resp_1", "usage": {"input_tokens": 10, "output_tokens": 5}}},
        ),
    )


def _anthropic_full_stream() -> bytes:
    return _event_sse(
        (
            "message_start",
            {"message": {"role": "assistant", "usage": {"input_tokens": 10, "output_tokens": 1}}},
        ),
        (
            "content_block_start",
            {"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me think"}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "signature_delta", "signature": "sig-abc123"}},
        ),
        ("content_block_stop", {"index": 0}),
        ("content_block_start", {"index": 1, "content_block": {"type": "text", "text": ""}}),
        (
            "content_block_delta",
            {"index": 1, "delta": {"type": "text_delta", "text": "Hello world"}},
        ),
        ("content_block_stop", {"index": 1}),
        (
            "content_block_start",
            {
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_A",
                    "name": "get_weather",
                    "input": {},
                },
            },
        ),
        (
            "content_block_delta",
            {
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"city": "Beijing"}'},
            },
        ),
        ("content_block_stop", {"index": 2}),
        (
            "message_delta",
            {
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
        ),
        ("message_stop", {}),
    )


_ALL_CASES = [
    _FormatCase(
        InterfaceFormat.OPENAI_COMPATIBLE,
        OpenAICompatProtocol,
        _compat_full_stream(),
        "call_a",
        None,
        None,
    ),
    _FormatCase(
        InterfaceFormat.OPENAI_RESPONSE,
        OpenAIResponsesProtocol,
        _responses_full_stream(),
        "call_a",
        None,
        "rs_1",
    ),
    _FormatCase(
        InterfaceFormat.ANTHROPIC,
        AnthropicProtocol,
        _anthropic_full_stream(),
        "toolu_A",
        "sig-abc123",
        None,
    ),
]
_CASE_IDS = [case.fmt.value for case in _ALL_CASES]


def _config(fmt: InterfaceFormat, **overrides: Any) -> LLMConfig:
    defaults: dict[str, Any] = {
        "model": "test-model",
        "api_key": "test-key",
        "base_url": "https://api.example.com/v1",
        "interface_format": fmt,
    }
    return LLMConfig(**{**defaults, **overrides})


def _user_message() -> list[ChatMessage]:
    return [ChatMessage(role=MessageRole.USER, content="hi")]


@pytest.fixture
async def e2e_provider() -> AsyncIterator[ProviderFactory]:
    """Build factory-routed providers on a recording MockTransport; closes clients."""
    created: list[HTTPStreamProvider] = []
    retired: list[httpx.AsyncClient] = []

    async def _make(
        config: LLMConfig, stream: bytes
    ) -> tuple[HTTPStreamProvider, list[httpx.Request]]:
        requests: list[httpx.Request] = []

        async def recording(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=stream, headers=_SSE_HEADERS)

        provider = create_llm_provider(config)
        assert isinstance(provider, HTTPStreamProvider)
        # Swap only the network layer; the real client is retired (closed in
        # teardown) so no socket resource leaks past the test.
        retired.append(provider._client)
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
        created.append(provider)
        return provider, requests

    yield _make
    for provider in created:
        await provider.aclose()
    for client in retired:
        await client.aclose()


# ─── Scenario 1: full stream (text + reasoning + tool call + usage + finish) ──


@pytest.mark.parametrize("case", _ALL_CASES, ids=_CASE_IDS)
async def test_full_stream_assembles_complete_response_across_formats(
    e2e_provider: ProviderFactory, case: _FormatCase
) -> None:
    provider, _requests = await e2e_provider(_config(case.fmt), case.stream)
    assert type(provider._protocol) is case.engine

    response = await provider.chat_stream(messages=_user_message())

    assert response.error is None
    assert response.content == "Hello world"
    assert response.reasoning_content == "Let me think"
    assert response.reasoning_signature == case.signature
    assert response.reasoning_item_id == case.item_id
    assert response.reasoning_encrypted_content is None
    assert response.finish_reason == FinishReason.TOOL_CALLS
    assert response.usage == TokenUsage(input_tokens=10, output_tokens=5)
    assert response.usage.total_tokens == 15
    assert [(tc.call_id, tc.tool_name, tc.arguments) for tc in response.tool_calls] == [
        (case.call_id, "get_weather", {"city": "Beijing"})
    ]


# ─── Scenario 2: anthropic thinking+signature two-round replay ────────────────


async def test_anthropic_thinking_signature_replayed_in_second_round_request(
    e2e_provider: ProviderFactory,
) -> None:
    round1_stream = _event_sse(
        (
            "message_start",
            {"message": {"role": "assistant", "usage": {"input_tokens": 10, "output_tokens": 1}}},
        ),
        (
            "content_block_start",
            {"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "thinking_delta", "thinking": "Plan first"}},
        ),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "signature_delta", "signature": "sig-round1"}},
        ),
        ("content_block_stop", {"index": 0}),
        ("content_block_start", {"index": 1, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 1, "delta": {"type": "text_delta", "text": "Answer"}}),
        ("content_block_stop", {"index": 1}),
        (
            "message_delta",
            {
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 4},
            },
        ),
        ("message_stop", {}),
    )
    provider, requests = await e2e_provider(_config(InterfaceFormat.ANTHROPIC), round1_stream)

    first = await provider.chat_stream(messages=[ChatMessage(role=MessageRole.USER, content="q")])
    assert first.content == "Answer"
    assert first.reasoning_content == "Plan first"
    assert first.reasoning_signature == "sig-round1"

    # Round 1 carried no assistant history yet — the request was a plain
    # user turn (no thinking block on the wire).
    round1_body = json.loads(requests[0].content)
    assert round1_body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "q"}]}]

    # The agent's canonical assistant message, assembled exactly like
    # LLMNode does from the response's replay fields.
    assistant = build_assistant_message(
        first.content,
        list(first.tool_calls or []),
        reasoning_content=first.reasoning_content,
        reasoning_signature=first.reasoning_signature,
    )
    second = await provider.chat_stream(
        messages=[
            ChatMessage(role=MessageRole.USER, content="q"),
            assistant,
            ChatMessage(role=MessageRole.USER, content="q2"),
        ]
    )
    assert second.error is None

    # Round 2 closure: the assistant turn on the wire carries the thinking
    # block with the round-1 signature — the replay chain is closed.
    assert len(requests) == 2
    round2_body = json.loads(requests[1].content)
    assistant_turns = [m for m in round2_body["messages"] if m["role"] == "assistant"]
    assert len(assistant_turns) == 1
    assert assistant_turns[0]["content"] == [
        {"type": "thinking", "thinking": "Plan first", "signature": "sig-round1"},
        {"type": "text", "text": "Answer"},
    ]


# ─── Scenario 3: endpoint_url verbatim override ───────────────────────────────


@pytest.mark.parametrize("case", _ALL_CASES, ids=_CASE_IDS)
async def test_endpoint_url_used_verbatim_bypassing_format_join(
    e2e_provider: ProviderFactory, case: _FormatCase
) -> None:
    provider, requests = await e2e_provider(
        _config(case.fmt, endpoint_url="https://gateway.example.com/llm"), case.stream
    )
    await provider.chat_stream(messages=_user_message())

    # Exactly the configured value — no /chat/completions, /responses, or
    # /v1/messages suffix from the per-format join.
    assert str(requests[0].url) == "https://gateway.example.com/llm"


# ─── Scenario 4: top_p passthrough into the request body ─────────────────────


@pytest.mark.parametrize("case", _ALL_CASES, ids=_CASE_IDS)
async def test_top_p_from_llm_config_reaches_request_body(
    e2e_provider: ProviderFactory, case: _FormatCase
) -> None:
    provider, requests = await e2e_provider(_config(case.fmt, top_p=0.42), case.stream)
    await provider.chat_stream(messages=_user_message())

    body = json.loads(requests[0].content)
    assert body["top_p"] == 0.42


# ─── Scenario 5: ReactLlmClient + emitter over the full transport ────────────


def _make_ctx(services: AgentRuntimeServices | None = None) -> AgentContext:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(
        services=AgentRuntimeServices() if services is None else services,
        state=state,
    )
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
        max_iterations=5,
        identity=state.identity,
        runtime=runtime,
    )


class _RecordingEmitter:
    """Records every emitter call as (method, *args) tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def wants_streaming(self) -> bool:
        return True

    async def emit(self, event: object, data: object = None) -> None:
        self.calls.append(("emit", event, data))

    async def emit_delta(self, delta: str) -> None:
        self.calls.append(("emit_delta", delta))

    async def emit_content(self, full_content: str) -> None:
        self.calls.append(("emit_content", full_content))

    async def emit_stream_end(self, resuming: bool = False) -> None:
        self.calls.append(("emit_stream_end", resuming))

    async def emit_complete(self, result: object) -> None:
        self.calls.append(("emit_complete", result))

    async def emit_error(self, error: str) -> None:
        self.calls.append(("emit_error", error))


async def test_react_llm_client_drives_emitter_over_full_transport(
    e2e_provider: ProviderFactory,
) -> None:
    compat_case = _ALL_CASES[0]
    provider, _requests = await e2e_provider(_config(compat_case.fmt), compat_case.stream)
    ctx = _make_ctx()
    emitter = _RecordingEmitter()
    ctx.emitter = emitter

    response = await ReactLlmClient(provider).call([{"role": "user", "content": "hi"}], ctx)

    assert emitter.calls == [
        ("emit", ReActEvent.MODEL_REASONING, "Let me think"),
        ("emit_delta", "Hello"),
        ("emit", ReActEvent.MODEL_OUTPUT, "Hello"),
        ("emit_delta", " world"),
        ("emit", ReActEvent.MODEL_OUTPUT, " world"),
        ("emit_stream_end", True),
    ]
    assert response.error is None
    assert response.content == "Hello world"
    assert response.reasoning_content == "Let me think"
    assert [tc.tool_name for tc in response.tool_calls] == ["get_weather"]


# ─── Scenario 6: tool-media placement after inject_multimodal ────────────────

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()
_PNG_DATA_URL = f"data:image/png;base64,{_PNG_B64}"


@pytest.mark.parametrize("case", _ALL_CASES, ids=_CASE_IDS)
async def test_tool_media_placement_after_injection_across_formats(
    e2e_provider: ProviderFactory, case: _FormatCase, tmp_path: Path
) -> None:
    # The read tool's READS snapshot, persisted exactly like file_tool does.
    store = LocalFileMediaStore(tmp_path / "media")
    sid = str(SessionInfo.from_str("test.agent"))
    store.save(sid, "snap-1", _PNG_BYTES, kind=StoredMediaKind.READS)

    # The read-image turn: the TOOL message MUST be paired with the assistant
    # tool_call — an orphan TOOL message raises ProtocolStructureError.
    history: list[ChatMessage] = [
        ChatMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[
                ToolCall(call_id="call-x", tool_name="read", arguments={"path": "/ws/img.png"})
            ],
        ),
        ChatMessage(
            role=MessageRole.TOOL,
            tool_call_id="call-x",
            name="read",
            content=[
                TextPart(text="[Image read: /ws/img.png (image/png)]"),
                ImageUrlPart(image_url=ImageUrl(url=build_media_ref("snap-1"))),
            ],
        ),
    ]

    services = AgentRuntimeServices()
    services.model_info = ModelInfo(
        model_name="test",
        capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
    )
    services.media_store = store
    injected = inject_multimodal(history, _make_ctx(services))

    # The media:// reference resolved to a data-URL part on the LLM-bound
    # copy; the persisted history keeps its reference (copy-on-write).
    resolved_parts = injected[1].content
    assert isinstance(resolved_parts, list)
    assert resolved_parts[1] == ImageUrlPart(image_url=ImageUrl(url=_PNG_DATA_URL))
    persisted_parts = history[1].content
    assert isinstance(persisted_parts, list)
    assert persisted_parts[1] == ImageUrlPart(image_url=ImageUrl(url=build_media_ref("snap-1")))

    provider, requests = await e2e_provider(_config(case.fmt), case.stream)
    async for _ in provider.stream(LLMRequest(model="test-model", messages=injected)):
        pass

    assert len(requests) == 1
    body = json.loads(requests[0].content)
    match case.fmt:
        case InterfaceFormat.ANTHROPIC:
            # The image rides natively inside the tool_result block.
            tool_result = body["messages"][-1]["content"][0]
            assert tool_result["type"] == "tool_result"
            assert tool_result["tool_use_id"] == "call-x"
            assert tool_result["content"][0] == {
                "type": "text",
                "text": "[Image read: /ws/img.png (image/png)]",
            }
            assert tool_result["content"][1] == {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
            }
        case InterfaceFormat.OPENAI_COMPATIBLE:
            # Exactly one follow-up user message: attribution line + image_url.
            follow_ups = [
                m
                for m in body["messages"]
                if m["role"] == "user" and isinstance(m["content"], list)
            ]
            assert len(follow_ups) == 1
            texts = [p for p in follow_ups[0]["content"] if p["type"] == "text"]
            images = [p for p in follow_ups[0]["content"] if p["type"] == "image_url"]
            assert len(texts) == 1
            assert "'read'" in texts[0]["text"]
            assert "call-x" in texts[0]["text"]
            assert len(images) == 1
            assert images[0]["image_url"] == {"url": _PNG_DATA_URL}
        case InterfaceFormat.OPENAI_RESPONSE:
            # The image rides natively in the paired function_call_output.
            outputs = [item for item in body["input"] if item.get("type") == "function_call_output"]
            assert len(outputs) == 1
            assert outputs[0]["call_id"] == "call-x"
            output = outputs[0]["output"]
            assert isinstance(output, list)
            texts = [p for p in output if p["type"] == "input_text"]
            images = [p for p in output if p["type"] == "input_image"]
            assert len(texts) == 1
            assert "read" in texts[0]["text"]
            assert len(images) == 1
            assert images[0]["image_url"] == _PNG_DATA_URL
            # No synthetic follow-up user item — this scenario's history has
            # no user message at all, so any user input item is the old flush.
            assert not any(item.get("role") == "user" for item in body["input"])
        case unreachable:
            assert_never(unreachable)
