"""Tests for modex_agent.providers.http.formats.openai_compat.

Canned chat-completions SSE data frames (SseFrame sequences, zero network)
drive the events() translator; build_body() is asserted independently on
canonical LLMRequest inputs. Scenarios mirror the task list: text stream
with usage tail frame, native reasoning_content, think-tag extraction on
and off, think-flush residual, interleaved tool-call fragments, LENGTH
discarding pending tools, governance-field zero leak, conditional
reasoning replay, extra_body override, non-standard role merge, and
malformed-payload failures.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from modex_agent.core.llm_request import LLMRequest, ReasoningEffort
from modex_agent.core.llm_struct import FinishReason, LLMErrorKind, TokenUsage
from modex_agent.core.message import (
    ChatMessage,
    ImageUrl,
    ImageUrlPart,
    MessageRole,
    TextPart,
    ToolCall,
)
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)
from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
from modex_agent.providers.http.protocol import ProtocolConfig
from modex_agent.providers.http.sse import DONE_SENTINEL, SseFrame

_ENGINE = OpenAICompatProtocol()
_RAW_ENGINE = OpenAICompatProtocol(parse_think_tags=False)
_LOGGER_NAME = "modex_agent.providers.http.formats.openai_compat"


def _frame(payload: dict[str, Any] | str) -> SseFrame:
    """One data-only SSE frame (the chat protocol carries no event line)."""
    if isinstance(payload, str):
        return SseFrame(data=payload)
    return SseFrame(data=json.dumps(payload))


def _frames(*frames: SseFrame) -> AsyncIterator[SseFrame]:
    """In-memory frame stream — zero network."""

    async def _gen() -> AsyncIterator[SseFrame]:
        for frame in frames:
            yield frame

    return _gen()


async def _run(*frames: SseFrame) -> list[LLMStreamEvent]:
    """Drive the engine's events() translator over canned frames."""
    return [event async for event in _ENGINE.events(_frames(*frames))]


async def _run_with(engine: OpenAICompatProtocol, *frames: SseFrame) -> list[LLMStreamEvent]:
    return [event async for event in engine.events(_frames(*frames))]


def _chunk(delta: dict[str, Any] | None = None, finish: str | None = None) -> SseFrame:
    """One chat.completion.chunk frame with a single choice."""
    choice: dict[str, Any] = {"delta": delta or {}}
    if finish is not None:
        choice["finish_reason"] = finish
    return _frame({"choices": [choice]})


def _tool_delta(
    index: int,
    arguments: str,
    call_id: str | None = None,
    name: str | None = None,
) -> SseFrame:
    """One delta.tool_calls entry; id/name ride only the first fragment."""
    entry: dict[str, Any] = {"index": index, "function": {"arguments": arguments}}
    if call_id is not None:
        entry["id"] = call_id
        entry["type"] = "function"
    if name is not None:
        entry["function"]["name"] = name
    return _chunk({"tool_calls": [entry]})


def _usage_frame(usage: dict[str, Any]) -> SseFrame:
    """The usage tail frame — empty choices array (PRD §4.1)."""
    return _frame({"choices": [], "usage": usage})


_DONE = _frame(DONE_SENTINEL)


def _cfg(
    effort: ReasoningEffort = ReasoningEffort.NONE,
    extra_body: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
) -> ProtocolConfig:
    return ProtocolConfig(
        reasoning_effort=effort,
        extra_body=extra_body,
        max_output_tokens=max_output_tokens,
    )


def _request(**overrides: Any) -> LLMRequest:
    defaults: dict[str, Any] = {
        "model": "deepseek-chat",
        "messages": [ChatMessage(role=MessageRole.USER, content="hi")],
    }
    return LLMRequest(**{**defaults, **overrides})


class TestEventStreamTranslation:
    async def test_text_stream_yields_deltas_usage_then_stop_finish(self) -> None:
        events = await _run(
            _chunk({"content": "Hel"}),
            _chunk({"content": "lo "}),
            _chunk({"content": "world"}),
            _chunk(finish="stop"),
            _usage_frame({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            _DONE,
        )
        assert events == [
            TextDelta(text="Hel"),
            TextDelta(text="lo "),
            TextDelta(text="world"),
            UsageSnapshot(usage=TokenUsage(input_tokens=10, output_tokens=5)),
            Finish(finish_reason=FinishReason.STOP, replay=None),
        ]
        usage = next(e for e in events if isinstance(e, UsageSnapshot)).usage
        # total_tokens is computed, never taken from the wire value 15-by-luck.
        assert usage.total_tokens == 15

    async def test_usage_tail_frame_normalizes_wire_cache_details(self) -> None:
        events = await _run(
            _usage_frame(
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 20},
                }
            ),
            _DONE,
        )
        assert events == [
            UsageSnapshot(
                usage=TokenUsage(
                    input_tokens=80,
                    cache_read_input_tokens=20,
                    output_tokens=50,
                )
            ),
            Finish(finish_reason=FinishReason.STOP, replay=None),
        ]

    async def test_native_reasoning_content_stream(self) -> None:
        events = await _run(
            _chunk({"reasoning_content": "let me think"}),
            _chunk({"reasoning_content": " more"}),
            # Content arriving after native reasoning passes through raw —
            # think extraction is off for the rest of the stream.
            _chunk({"content": "<think>ignored</think>answer"}),
            _chunk(finish="stop"),
            _DONE,
        )
        assert events == [
            ReasoningDelta(text="let me think"),
            ReasoningDelta(text=" more"),
            TextDelta(text="<think>ignored</think>answer"),
            Finish(finish_reason=FinishReason.STOP, replay=None),
        ]

    async def test_think_tags_parsed_when_enabled(self) -> None:
        events = await _run(
            _chunk({"content": "<think>"}),
            _chunk({"content": "step one"}),
            _chunk({"content": "</think>"}),
            _chunk({"content": "answer"}),
            _chunk(finish="stop"),
            _DONE,
        )
        assert events == [
            ReasoningDelta(text="step one"),
            TextDelta(text="answer"),
            Finish(finish_reason=FinishReason.STOP, replay=None),
        ]

    async def test_think_tags_passthrough_when_disabled(self) -> None:
        events = await _run_with(
            _RAW_ENGINE,
            _chunk({"content": "<think>"}),
            _chunk({"content": "step one"}),
            _chunk({"content": "</think>"}),
            _chunk({"content": "answer"}),
            _chunk(finish="stop"),
            _DONE,
        )
        assert events == [
            TextDelta(text="<think>"),
            TextDelta(text="step one"),
            TextDelta(text="</think>"),
            TextDelta(text="answer"),
            Finish(finish_reason=FinishReason.STOP, replay=None),
        ]

    async def test_think_extractor_flush_emits_residual_text_delta(self) -> None:
        # A trailing partial tag stays buffered while the stream is IDLE;
        # the [DONE] flush must release it as real content.
        events = await _run(
            _chunk({"content": "<th"}),
            _DONE,
        )
        assert events == [
            TextDelta(text="<th"),
            Finish(finish_reason=FinishReason.STOP, replay=None),
        ]

    async def test_interleaved_tool_fragments_finish_in_insertion_order(self) -> None:
        events = await _run(
            _tool_delta(0, "", call_id="call_a", name="get_weather"),
            _tool_delta(1, '{"tz":', call_id="call_b", name="get_time"),
            _tool_delta(0, '{"city":"SF"}'),
            _tool_delta(1, '"UTC"}'),
            _chunk(finish="tool_calls"),
            _usage_frame({"prompt_tokens": 10, "completion_tokens": 5}),
            _DONE,
        )
        assert events == [
            ToolCallComplete(
                call_id="call_a",
                tool_name="get_weather",
                arguments={"city": "SF"},
            ),
            ToolCallComplete(call_id="call_b", tool_name="get_time", arguments={"tz": "UTC"}),
            UsageSnapshot(usage=TokenUsage(input_tokens=10, output_tokens=5)),
            Finish(finish_reason=FinishReason.TOOL_CALLS, replay=None),
        ]

    async def test_length_finish_reason_discards_pending_tools(self) -> None:
        events = await _run(
            _tool_delta(0, '{"city":', call_id="call_a", name="get_weather"),
            _chunk(finish="length"),
            _DONE,
        )
        assert events == [Finish(finish_reason=FinishReason.LENGTH, replay=None)]

    async def test_content_filter_finish_reason_mapped(self) -> None:
        events = await _run(
            _chunk({"content": "partial"}),
            _chunk(finish="content_filter"),
            _DONE,
        )
        assert events == [
            TextDelta(text="partial"),
            Finish(finish_reason=FinishReason.CONTENT_FILTER, replay=None),
        ]

    async def test_bad_json_frame_yields_stream_failure(self) -> None:
        events = await _run(
            _chunk({"content": "partial "}),
            _frame("{not json"),
        )
        assert len(events) == 2
        failure = events[1]
        assert isinstance(failure, StreamFailure)
        assert failure.error_info.kind is LLMErrorKind.INVALID_REQUEST
        assert failure.error_info.should_retry is False
        assert "malformed SSE payload" in failure.error_info.message
        # The already-emitted deltas are the partial content (assembler
        # splice semantics) — the engine-side partial stays empty.
        assert failure.partial_content == ""

    async def test_non_dict_payload_yields_stream_failure(self) -> None:
        events = await _run(_frame("[1, 2, 3]"), _DONE)
        assert len(events) == 1
        failure = events[0]
        assert isinstance(failure, StreamFailure)
        assert failure.error_info.kind is LLMErrorKind.INVALID_REQUEST
        assert "malformed SSE payload" in failure.error_info.message

    async def test_tool_delta_without_identity_yields_stream_failure(self) -> None:
        # First fragment for an index carries no id/name and no pending
        # tool can supply them — the tool grammar is broken.
        events = await _run(
            _tool_delta(0, "{}"),
            _DONE,
        )
        assert len(events) == 1
        failure = events[0]
        assert isinstance(failure, StreamFailure)
        assert failure.error_info.kind is LLMErrorKind.INVALID_REQUEST
        assert "no id/name" in failure.error_info.message

    async def test_eof_without_done_yields_no_terminal_event(self) -> None:
        events = await _run(
            _chunk({"content": "partial"}),
        )
        assert events == [TextDelta(text="partial")]


class TestBuildBody:
    def test_stream_flags_and_sampling_mapping(self) -> None:
        body = _ENGINE.build_body(
            _request(
                temperature=0.3,
                top_p=0.9,
                max_output_tokens=256,
                stop=("END", "STOP"),
                prompt_cache_key="sess-1",
                reasoning_effort=ReasoningEffort.HIGH,
            ),
            _cfg(),
        )
        assert body["model"] == "deepseek-chat"
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert body["temperature"] == 0.3
        assert body["top_p"] == 0.9
        assert body["max_tokens"] == 256
        assert body["stop"] == ["END", "STOP"]
        assert body["prompt_cache_key"] == "sess-1"
        assert body["reasoning_effort"] == "high"

    def test_none_sampling_values_omit_keys_entirely(self) -> None:
        body = _ENGINE.build_body(_request(), _cfg())
        for absent in (
            "temperature",
            "top_p",
            "max_tokens",
            "stop",
            "prompt_cache_key",
            "reasoning_effort",
            "tools",
            "tool_choice",
        ):
            assert absent not in body
        assert body["messages"] == [{"role": "user", "content": "hi"}]

    def test_effort_and_max_tokens_fall_back_to_config(self) -> None:
        body = _ENGINE.build_body(
            _request(), _cfg(effort=ReasoningEffort.MEDIUM, max_output_tokens=512)
        )
        assert body["reasoning_effort"] == "medium"
        assert body["max_tokens"] == 512
        # The request envelope outranks the config when both carry a value.
        override = _ENGINE.build_body(
            _request(max_output_tokens=1024, reasoning_effort=ReasoningEffort.LOW),
            _cfg(effort=ReasoningEffort.MEDIUM, max_output_tokens=512),
        )
        assert override["max_tokens"] == 1024
        assert override["reasoning_effort"] == "low"

    def test_governance_fields_never_leak(self) -> None:
        request = _request(
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content="be brief"),
                ChatMessage(
                    role=MessageRole.USER,
                    content="hi",
                    name="bob",
                    token_count=5,
                    created_at=None,
                    truncatable_paths=["/a"],
                ),
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=None,
                    reasoning_content="thinking",
                    reasoning_signature="sig",
                    reasoning_item_id="rs_1",
                    reasoning_encrypted_content="enc",
                ),
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=None,
                    reasoning_content="kept",
                    tool_calls=[
                        ToolCall(
                            tool_name="get_weather", arguments={"city": "SF"}, call_id="call_1"
                        )
                    ],
                ),
                ChatMessage(role=MessageRole.TOOL, tool_call_id="call_1", content=""),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())
        dumped = json.dumps(body, ensure_ascii=False)
        for leaked in (
            "token_count",
            "content_format",
            "created_at",
            "truncatable_paths",
            "reasoning_signature",
            "reasoning_item_id",
            "reasoning_encrypted_content",
        ):
            assert leaked not in dumped, leaked
        messages = body["messages"]
        assert messages[0] == {"role": "system", "content": "be brief"}
        # ChatMessage.name is not lowered (PRD §3).
        assert messages[1] == {"role": "user", "content": "hi"}
        # Assistant turn WITHOUT tool_calls: reasoning_content is dropped.
        assert messages[2] == {"role": "assistant"}
        # Assistant turn WITH tool_calls: reasoning_content rides along.
        assert messages[3] == {
            "role": "assistant",
            "reasoning_content": "kept",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                }
            ],
        }
        # Empty tool output degrades to the placeholder.
        assert messages[4] == {"role": "tool", "tool_call_id": "call_1", "content": "(no output)"}

    def test_reasoning_replay_only_on_tool_call_turns(self) -> None:
        request = _request(
            messages=[
                # Text turn WITH reasoning: not replayed (no tool_calls).
                ChatMessage(role=MessageRole.ASSISTANT, content="hello", reasoning_content="why"),
                # Tool turn WITH reasoning: replayed.
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(tool_name="t", arguments={}, call_id="c1")],
                    reasoning_content="because",
                ),
                # Tool turn WITHOUT reasoning: key absent.
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(tool_name="t", arguments={}, call_id="c2")],
                ),
                ChatMessage(role=MessageRole.TOOL, tool_call_id="c1", content="r1"),
                ChatMessage(role=MessageRole.TOOL, tool_call_id="c2", content="r2"),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())
        messages = body["messages"]
        assert "reasoning_content" not in messages[0]
        assert messages[1]["reasoning_content"] == "because"
        assert "reasoning_content" not in messages[2]

    def test_tool_call_lowering_details(self) -> None:
        request = _request(
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        # call_id present → wire id; CJK survives ensure_ascii=False.
                        ToolCall(tool_name="search", arguments={"query": "城市"}, call_id="call_x"),
                        # call_id None → positional fallback.
                        ToolCall(tool_name="other", arguments={"a": 1}),
                    ],
                ),
                ChatMessage(role=MessageRole.TOOL, tool_call_id="call_x", content="done"),
                ChatMessage(role=MessageRole.TOOL, tool_call_id="call_1", content="done2"),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())
        wire_calls = body["messages"][0]["tool_calls"]
        assert wire_calls[0]["id"] == "call_x"
        assert wire_calls[0]["function"]["name"] == "search"
        assert wire_calls[0]["function"]["arguments"] == '{"query": "城市"}'
        assert wire_calls[1]["id"] == "call_1"

    def test_user_multimodal_parts_pass_through(self) -> None:
        request = _request(
            messages=[
                ChatMessage(
                    role=MessageRole.USER,
                    content=[
                        TextPart(text="look"),
                        ImageUrlPart(image_url=ImageUrl(url="https://x/i.png")),
                    ],
                ),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())
        assert body["messages"][0]["content"] == [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "https://x/i.png"}},
        ]

    def test_unresolved_media_refs_are_skipped_with_error_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = _request(
            messages=[
                ChatMessage(
                    role=MessageRole.SYSTEM,
                    content=[
                        TextPart(text="system"),
                        ImageUrlPart(image_url=ImageUrl(url="media://system-image")),
                    ],
                ),
                ChatMessage(
                    role=MessageRole.USER,
                    content=[
                        TextPart(text="user"),
                        ImageUrlPart(image_url=ImageUrl(url="media://user-image")),
                    ],
                ),
            ]
        )

        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            body = _ENGINE.build_body(request, _cfg())

        assert "media://" not in json.dumps(body)
        assert body["messages"] == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": [{"type": "text", "text": "user"}]},
        ]
        assert [record.getMessage() for record in caplog.records] == [
            "openai_compat engine: unresolved media:// reference reached the wire layer, "
            "part skipped: system-image",
            "openai_compat engine: unresolved media:// reference reached the wire layer, "
            "part skipped: user-image",
        ]

    def test_data_url_image_part_passes_through(self) -> None:
        data_url = "data:image/png;base64,aGVsbG8="
        body = _ENGINE.build_body(
            _request(
                messages=[
                    ChatMessage(
                        role=MessageRole.USER,
                        content=[ImageUrlPart(image_url=ImageUrl(url=data_url))],
                    )
                ]
            ),
            _cfg(),
        )

        assert body["messages"][0]["content"] == [
            {"type": "image_url", "image_url": {"url": data_url}}
        ]

    def test_unknown_user_content_part_skipped_with_error_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _ForeignPart:
            """Simulates a future ContentPart variant the engine does not know."""

        # model_construct bypasses validation — the only seam that lets a
        # non-ContentPart reach the engine (the closed union rejects it at
        # ChatMessage construction; this is the V2-FilePart future).
        msg = ChatMessage.model_construct(
            role=MessageRole.USER,
            content=[TextPart(text="keep"), _ForeignPart()],  # type: ignore[list-item]
        )
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            body = _ENGINE.build_body(_request(messages=[msg]), _cfg())
        assert body["messages"][0]["content"] == [{"type": "text", "text": "keep"}]
        assert "unrecognized user content part" in caplog.text
        assert "_ForeignPart" in caplog.text

    def test_all_parts_skipped_folds_to_empty_string(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _ForeignPart:
            pass

        msg = ChatMessage.model_construct(
            role=MessageRole.USER,
            content=[_ForeignPart()],  # type: ignore[list-item]
        )
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            body = _ENGINE.build_body(_request(messages=[msg]), _cfg())
        assert body["messages"][0]["content"] == ""

    def test_non_standard_roles_merged_to_nearest(self, caplog: pytest.LogCaptureFixture) -> None:
        request = _request(
            messages=[
                ChatMessage(role=MessageRole.COMPACT, content="compact summary"),
                ChatMessage(role=MessageRole.AGENT, content="agent note"),
                ChatMessage(role=MessageRole.SYSTEM_REMINDER, content="reminder"),
            ],
        )
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            body = _ENGINE.build_body(request, _cfg())
        assert [m["role"] for m in body["messages"]] == ["assistant", "user", "user"]
        assert caplog.text.count("merged to") == 3

    def test_tools_passthrough_and_tool_choice_auto(self) -> None:
        tool = {
            "type": "function",
            "function": {"name": "f", "description": "d", "parameters": {"type": "object"}},
        }
        body = _ENGINE.build_body(_request(tools=(tool,)), _cfg())
        assert body["tools"] == [tool]
        assert body["tool_choice"] == "auto"
        # No tools → no tool_choice key at all.
        empty = _ENGINE.build_body(_request(), _cfg())
        assert "tool_choice" not in empty

    def test_extra_body_merges_user_wins(self) -> None:
        body = _ENGINE.build_body(
            _request(extra_body={"custom": "request", "tool_choice": "required"}),
            _cfg(extra_body={"custom": "config", "temperature": 0.1}),
        )
        assert body["custom"] == "request"  # request-level wins over config-level
        assert body["temperature"] == 0.1  # config fills what the request omitted
        # tool_choice "auto" yields to the explicit extra_body override.
        request = _request(tools=({"type": "function", "function": {"name": "f"}},))
        overridden = _ENGINE.build_body(request, _cfg(extra_body={"tool_choice": "required"}))
        assert overridden["tool_choice"] == "required"


class TestToolMediaFlush:
    """Tool-produced media: fold text in place, flush ONE user message per
    contiguous TOOL run (attribution lines first, then the media parts)."""

    _DATA_URL = "data:image/png;base64,aGVsbG8="
    _SECOND_DATA_URL = "data:image/png;base64,d29ybGQ="

    def _tool_media_msg(self, call_id: str = "c1", name: str = "read") -> ChatMessage:
        return ChatMessage(
            role=MessageRole.TOOL,
            tool_call_id=call_id,
            name=name,
            content=[
                TextPart(text="[Image read: cat.png (image/png)]"),
                ImageUrlPart(image_url=ImageUrl(url=self._DATA_URL)),
            ],
        )

    def test_tool_media_flushes_user_message_after_tool_run(self) -> None:
        request = _request(
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        ToolCall(call_id="c1", tool_name="read", arguments={"path": "cat.png"})
                    ],
                ),
                self._tool_media_msg(),
                ChatMessage(role=MessageRole.USER, content="thanks"),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())

        assert body["messages"] == [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "cat.png"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "[Image read: cat.png (image/png)]",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Media from tool 'read' (call c1):"},
                    {"type": "image_url", "image_url": {"url": self._DATA_URL}},
                ],
            },
            {"role": "user", "content": "thanks"},
        ]

    def test_tool_media_flush_lands_at_end_when_tool_batch_is_list_tail(self) -> None:
        request = _request(
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="read", arguments={})],
                ),
                self._tool_media_msg(),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())

        assert [message["role"] for message in body["messages"]] == ["assistant", "tool", "user"]
        assert body["messages"][-1] == {
            "role": "user",
            "content": [
                {"type": "text", "text": "Media from tool 'read' (call c1):"},
                {"type": "image_url", "image_url": {"url": self._DATA_URL}},
            ],
        }

    def test_interleaved_tool_batches_flush_distinct_media_immediately(self) -> None:
        """Two TOOL runs separated by an assistant message → two flush
        messages, each carrying only its own run's attribution + media."""
        request = _request(
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="read", arguments={})],
                ),
                self._tool_media_msg("c1"),
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c2", tool_name="screenshot", arguments={})],
                ),
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="c2",
                    name="screenshot",
                    content=[
                        TextPart(text="[Image read: dog.png (image/png)]"),
                        ImageUrlPart(image_url=ImageUrl(url=self._SECOND_DATA_URL)),
                    ],
                ),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())

        user_flushes = [m for m in body["messages"] if m["role"] == "user"]
        assert len(user_flushes) == 2
        assert user_flushes[0]["content"] == [
            {"type": "text", "text": "Media from tool 'read' (call c1):"},
            {"type": "image_url", "image_url": {"url": self._DATA_URL}},
        ]
        assert user_flushes[1]["content"] == [
            {"type": "text", "text": "Media from tool 'screenshot' (call c2):"},
            {"type": "image_url", "image_url": {"url": self._SECOND_DATA_URL}},
        ]
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["assistant", "tool", "user", "assistant", "tool", "user"]

    def test_multiple_calls_in_one_run_group_attribution_lines_first(self) -> None:
        request = _request(
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        ToolCall(call_id="c1", tool_name="read", arguments={}),
                        ToolCall(call_id="c2", tool_name="screenshot", arguments={}),
                    ],
                ),
                self._tool_media_msg("c1"),
                self._tool_media_msg("c2", name="screenshot"),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())

        flush = body["messages"][-1]
        assert flush["role"] == "user"
        assert flush["content"] == [
            {"type": "text", "text": "Media from tool 'read' (call c1):"},
            {"type": "text", "text": "Media from tool 'screenshot' (call c2):"},
            {"type": "image_url", "image_url": {"url": self._DATA_URL}},
            {"type": "image_url", "image_url": {"url": self._DATA_URL}},
        ]

    def test_text_only_tool_run_body_is_byte_identical_to_no_flush_expectation(self) -> None:
        request = _request(
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="read", arguments={})],
                ),
                ChatMessage(role=MessageRole.TOOL, tool_call_id="c1", content="text output"),
                ChatMessage(role=MessageRole.USER, content="thanks"),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())

        expected_without_flush = (
            b'{"messages":[{"role":"assistant","tool_calls":[{"function":{"arguments":"{}",'
            b'"name":"read"},"id":"c1","type":"function"}]},{"content":"text output",'
            b'"role":"tool","tool_call_id":"c1"},{"content":"thanks","role":"user"}],'
            b'"model":"deepseek-chat","stream":true,"stream_options":{"include_usage":true}}'
        )
        actual = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        assert actual == expected_without_flush

    def test_unresolved_media_ref_in_tool_part_skipped_with_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = _request(
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="read", arguments={})],
                ),
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="c1",
                    content=[
                        TextPart(text="hint"),
                        ImageUrlPart(image_url=ImageUrl(url="media://stale-aid")),
                    ],
                ),
            ],
        )
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            body = _ENGINE.build_body(request, _cfg())

        # The unresolved reference never reaches the wire — neither in the
        # folded tool text nor in a flushed user message.
        assert "media://" not in json.dumps(body)
        assert body["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "hint",
        }
        assert any("stale-aid" in record.getMessage() for record in caplog.records)


class TestEndpointFacts:
    def test_url_joins_and_strips_trailing_slash(self) -> None:
        assert _ENGINE.url("https://api.deepseek.com/v1") == (
            "https://api.deepseek.com/v1/chat/completions"
        )
        assert _ENGINE.url("https://api.deepseek.com/v1/") == (
            "https://api.deepseek.com/v1/chat/completions"
        )

    def test_auth_headers(self) -> None:
        assert _ENGINE.auth_headers("sk-secret") == {"Authorization": "Bearer sk-secret"}
        assert _ENGINE.auth_headers(None) == {}

    def test_api_key_env(self) -> None:
        assert _ENGINE.api_key_env == "OPENAI_API_KEY"

    def test_default_error_classifier_inherited(self) -> None:
        info = _ENGINE.classify_http_error(401, b"", "openai_compat")
        assert info.kind is LLMErrorKind.AUTH
        assert info.should_retry is False
